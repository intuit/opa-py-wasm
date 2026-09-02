"""C1 unit tests: Wasmtime runtime foundation.

Uses synthetic WAT modules (see wat_helpers) so these run without the OPA CLI.
"""

from __future__ import annotations

import logging

import pytest
import wasmtime

from opapywasm.errors import OpaAbortError, OpaBuiltinError, OpaInvalidPolicyError
from opapywasm.runtime import WasmtimeRuntime, WasmtimeRuntimeFactory

from .wat_helpers import (
    env_memory_wrong_kind_wasm,
    large_data_segment_wasm,
    non_opa_wasm,
    oob_abort_wasm,
    opa_like_wasm,
    unrelated_instantiation_trap_wasm,
    unsatisfiable_import_wasm,
)


@pytest.fixture
def factory() -> WasmtimeRuntimeFactory:
    return WasmtimeRuntimeFactory(opa_like_wasm())


class TestFactory:
    def test_compiles_valid_module(self, factory: WasmtimeRuntimeFactory) -> None:
        assert factory.engine is not None
        assert factory.module is not None

    def test_engine_and_module_are_shared_across_runtimes(self, factory: WasmtimeRuntimeFactory) -> None:
        rt1 = factory.create_runtime()
        rt2 = factory.create_runtime()
        # Shared immutable engine/module...
        assert factory.engine is factory.engine
        # ...but independent per-instance stores/memories.
        assert rt1.store is not rt2.store
        assert rt1.memory is not rt2.memory
        assert rt1.instance is not rt2.instance

    @pytest.mark.parametrize("bad", [b"", b"not wasm", b"\x00asm\xff\xff\xff\xff"])
    def test_invalid_wasm_raises(self, bad: bytes) -> None:
        with pytest.raises(OpaInvalidPolicyError):
            WasmtimeRuntimeFactory(bad)

    def test_module_without_env_memory_rejected(self) -> None:
        with pytest.raises(OpaInvalidPolicyError, match=r"env\.memory"):
            WasmtimeRuntimeFactory(non_opa_wasm())

    def test_env_memory_wrong_kind_rejected(self) -> None:
        with pytest.raises(OpaInvalidPolicyError, match="not a memory"):
            WasmtimeRuntimeFactory(env_memory_wrong_kind_wasm())


class TestInstantiation:
    def test_creates_runtime_with_exports(self, factory: WasmtimeRuntimeFactory) -> None:
        rt = factory.create_runtime()
        assert isinstance(rt, WasmtimeRuntime)
        assert rt.has_export("opa_malloc")
        assert "opa_wasm_abi_version" in rt.exports()

    def test_host_memory_registered(self, factory: WasmtimeRuntimeFactory) -> None:
        rt = factory.create_runtime()
        # 2 initial pages as configured by the runtime.
        assert rt.memory.size(rt.store) == 2

    def test_unsatisfiable_import_raises(self) -> None:
        f = WasmtimeRuntimeFactory(unsatisfiable_import_wasm())
        with pytest.raises(OpaInvalidPolicyError, match="instantiate"):
            f.create_runtime()

    def test_module_needing_larger_initial_memory_still_instantiates(self) -> None:
        # Regression: a module whose active data segments overflow the default
        # 2-page initial memory must not trap during instantiation — the host
        # must retry with a larger initial size rather than assuming every OPA
        # module fits in 128 KiB before its first memory.grow.
        f = WasmtimeRuntimeFactory(large_data_segment_wasm())
        rt = f.create_runtime()
        assert rt.memory.size(rt.store) > 2

    def test_module_needing_larger_initial_memory_logs_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        f = WasmtimeRuntimeFactory(large_data_segment_wasm())
        with caplog.at_level(logging.WARNING, logger="opapywasm.runtime"):
            rt = f.create_runtime()
        assert any("larger initial memory" in r.message and r.levelname == "WARNING" for r in caplog.records)
        assert str(rt.memory.size(rt.store)) in caplog.records[-1].message

    def test_module_needing_larger_initial_memory_respects_max_pages_ceiling(self) -> None:
        # A max_memory_pages cap below what the module's data segments need must
        # still fail fast with a typed error, not an uncaught wasmtime.Trap.
        f = WasmtimeRuntimeFactory(large_data_segment_wasm(), max_memory_pages=2)
        with pytest.raises(OpaInvalidPolicyError, match="instantiate"):
            f.create_runtime()

    def test_default_ceiling_climb_gives_up_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Exercises the max_memory_pages=None path's climb toward the default
        # ceiling without actually allocating up to the real 1 GiB — the
        # module needs a 3rd page (see large_data_segment_wasm), so capping
        # the ceiling below that forces the same "give up" branch a genuinely
        # oversized module would hit at 16384 pages, just cheaply.
        monkeypatch.setattr("opapywasm.runtime._MAX_INSTANTIATION_MEMORY_PAGES", 2)
        f = WasmtimeRuntimeFactory(large_data_segment_wasm())
        with pytest.raises(OpaInvalidPolicyError, match="instantiate"):
            f.create_runtime()

    def test_unrelated_instantiation_trap_fails_fast_without_retrying(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Regression: only a memory-overflow trap should drive the retry loop.
        # A module that traps for an unrelated reason (here: `unreachable` in
        # its start function) must fail on the first attempt — retrying would
        # not help, and would let a malformed/adversarial module burn CPU and
        # up to 1 GiB of RSS across every doubling attempt for nothing. Counts
        # actual Store constructions rather than trusting the final error,
        # since a fixture that fails identically on every attempt would raise
        # the same typed error whether it retried 1 or 14 times.
        store_count = 0
        real_store = wasmtime.Store

        def counting_store(*args: object, **kwargs: object) -> wasmtime.Store:
            nonlocal store_count
            store_count += 1
            return real_store(*args, **kwargs)

        monkeypatch.setattr("opapywasm.runtime.wasmtime.Store", counting_store)

        f = WasmtimeRuntimeFactory(unrelated_instantiation_trap_wasm())
        with pytest.raises(OpaInvalidPolicyError, match="instantiate"):
            f.create_runtime()
        assert store_count == 1

    def test_missing_export_raises(self, factory: WasmtimeRuntimeFactory) -> None:
        rt = factory.create_runtime()
        with pytest.raises(OpaInvalidPolicyError, match="missing required export"):
            rt.get_export("nope")
        assert not rt.has_export("nope")

    def test_get_func_on_non_function_raises(self, factory: WasmtimeRuntimeFactory) -> None:
        rt = factory.create_runtime()
        # opa_wasm_abi_version is a global, not a function.
        with pytest.raises(OpaInvalidPolicyError, match="not a function"):
            rt.get_func("opa_wasm_abi_version")


class TestHostImports:
    def test_opa_abort_reads_message_and_raises(self, factory: WasmtimeRuntimeFactory) -> None:
        rt = factory.create_runtime()
        with pytest.raises(OpaAbortError, match="boom"):
            rt.get_func("trigger_abort")(rt.store)

    def test_opa_abort_with_bad_address_still_raises(self) -> None:
        # An out-of-bounds message address must still raise OpaAbortError rather
        # than crashing the host. wasmtime clamps reads past the memory end, so
        # the message degrades to empty rather than a placeholder — either way
        # the abort surfaces as a typed error.
        rt = WasmtimeRuntimeFactory(oob_abort_wasm()).create_runtime()
        with pytest.raises(OpaAbortError):
            rt.get_func("trigger_oob_abort")(rt.store)

    def test_opa_println_does_not_raise(self, factory: WasmtimeRuntimeFactory) -> None:
        rt = factory.create_runtime()
        # Should log and return without error.
        assert rt.get_func("trigger_println")(rt.store) is None

    def test_builtin_without_dispatch_raises_naming_import(self, factory: WasmtimeRuntimeFactory) -> None:
        rt = factory.create_runtime()
        with pytest.raises(OpaBuiltinError, match="opa_builtin0"):
            rt.get_func("trigger_builtin0")(rt.store)

    def test_builtin_dispatch_invoked_with_id_and_args(self, factory: WasmtimeRuntimeFactory) -> None:
        seen: dict[str, object] = {}

        def dispatch(builtin_id: int, ctx: int, args: list[int]) -> int:
            seen["id"] = builtin_id
            seen["ctx"] = ctx
            seen["args"] = args
            return builtin_id + 1

        rt = factory.create_runtime(builtin_dispatch=dispatch)
        result = rt.get_func("trigger_builtin0")(rt.store)
        assert result == 8  # builtin_id 7 + 1
        assert seen == {"id": 7, "ctx": 0, "args": []}

    def test_each_runtime_has_independent_dispatch(self, factory: WasmtimeRuntimeFactory) -> None:
        rt_no = factory.create_runtime()
        rt_yes = factory.create_runtime(builtin_dispatch=lambda bid, ctx, args: 42)
        assert rt_yes.get_func("trigger_builtin0")(rt_yes.store) == 42
        with pytest.raises(OpaBuiltinError):
            rt_no.get_func("trigger_builtin0")(rt_no.store)
