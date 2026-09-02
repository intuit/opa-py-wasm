"""C2 unit tests: OPA ABI adapter.

Version detection, ABI validation, required-export checks, and capability-flag
derivation are exercised with synthetic WAT modules (no OPA CLI). Decoding of
real ``entrypoints()`` / ``builtins()`` value addresses needs a real compiled
policy and is covered by the integration suite (C3/C4, gated on requires_wasm).
"""

from __future__ import annotations

import pytest

from opapywasm.abi import OpaAbi, OpaAbiSpec
from opapywasm.errors import OpaAbiError
from opapywasm.runtime import WasmtimeRuntimeFactory

from .wat_helpers import (
    ABI_REQUIRED_EXPORTS,
    EVAL_CTX_STUBS,
    HEAP_STASH_STUBS,
    VALUE_FREE_STUB,
    abi_module_wasm,
    abi_module_wasm_bad_signature,
    abi_module_wasm_float_version,
)


def make_abi(**kwargs: object) -> OpaAbi:
    rt = WasmtimeRuntimeFactory(abi_module_wasm(**kwargs)).create_runtime()
    return OpaAbi(rt)


class TestOpaAbiSpec:
    def test_check_version_accepts_1_x(self) -> None:
        spec = OpaAbiSpec()
        spec.check_version(1, 0)
        spec.check_version(1, 3)

    @pytest.mark.parametrize("major", [0, 2, 99])
    def test_check_version_rejects_other_majors(self, major: int) -> None:
        with pytest.raises(OpaAbiError, match="major version"):
            OpaAbiSpec().check_version(major, 0)

    def test_requires_eval_fast_path(self) -> None:
        spec = OpaAbiSpec()
        assert spec.requires_eval_fast_path(2) is True
        assert spec.requires_eval_fast_path(1) is False

    def test_spec_is_frozen(self) -> None:
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            OpaAbiSpec().min_major = 3  # type: ignore[misc]


class TestVersionDetection:
    def test_reads_major_and_minor(self) -> None:
        abi = make_abi(major=1, minor=2)
        assert abi.abi_version == (1, 2)

    def test_absent_minor_global_means_below_fast_path(self) -> None:
        # With the minor global absent, minor detects as 0, which is below the
        # opa_eval fast-path threshold (minor >= 2). Since this SDK requires the
        # fast path, such a module fails fast at construction rather than
        # constructing and erroring later.
        with pytest.raises(OpaAbiError, match="opa_eval fast path"):
            make_abi(include_minor_global=False, include_opa_eval=True)

    def test_unsupported_major_raises(self) -> None:
        with pytest.raises(OpaAbiError, match="major version"):
            make_abi(major=2)

    def test_missing_version_global_raises(self) -> None:
        # A module with the required funcs but no version global is not an OPA policy.
        wat = (
            "(module "
            '(import "env" "memory" (memory 1)) '
            + " ".join(
                f'(func (export "{n}") (result i32) (i32.const 0))'
                for n in ("entrypoints", "builtins", "opa_heap_ptr_get")
            )
            + ")"
        )
        import wasmtime

        rt = WasmtimeRuntimeFactory(bytes(wasmtime.wat2wasm(wat))).create_runtime()
        with pytest.raises(OpaAbiError, match="opa_wasm_abi_version"):
            OpaAbi(rt)

    def test_non_integer_version_global_raises(self) -> None:
        rt = WasmtimeRuntimeFactory(abi_module_wasm_float_version()).create_runtime()
        with pytest.raises(OpaAbiError, match="not integers"):
            OpaAbi(rt)


class TestRequiredExports:
    def test_all_present_ok(self) -> None:
        assert make_abi().abi_version == (1, 2)

    @pytest.mark.parametrize("drop", ABI_REQUIRED_EXPORTS)
    def test_missing_required_export_raises(self, drop: str) -> None:
        remaining = tuple(e for e in ABI_REQUIRED_EXPORTS if e != drop)
        with pytest.raises(OpaAbiError, match="missing required OPA ABI export"):
            make_abi(exports=remaining)


class TestCapabilities:
    def test_opa_eval_detected(self) -> None:
        abi = make_abi(minor=2, include_opa_eval=True)
        assert abi.supports_opa_eval is True

    def test_opa_eval_requires_minor_2(self) -> None:
        # opa_eval export present but ABI minor 1 -> fast path not eligible.
        # This SDK requires the fast path, so even with eval-ctx present it now
        # fails fast at construction (rather than silently falling back).
        with pytest.raises(OpaAbiError, match="opa_eval fast path"):
            make_abi(minor=1, include_opa_eval=True, extra_funcs=EVAL_CTX_STUBS)

    def test_eval_context_only_module_rejected(self) -> None:
        # A context-only module (no opa_eval) is rejected at construction because
        # the eval-context fallback is deliberately not implemented.
        with pytest.raises(OpaAbiError, match="opa_eval fast path"):
            make_abi(include_opa_eval=False, extra_funcs=EVAL_CTX_STUBS)

    def test_heap_stash_and_value_free_detected(self) -> None:
        abi = make_abi(extra_funcs=(*HEAP_STASH_STUBS, VALUE_FREE_STUB))
        assert abi.supports_heap_stash is True
        assert abi.supports_value_free is True

    def test_capabilities_absent_by_default(self) -> None:
        abi = make_abi()
        assert abi.supports_eval_context is False
        assert abi.supports_heap_stash is False
        assert abi.supports_value_free is False

    def test_no_eval_path_raises(self) -> None:
        with pytest.raises(OpaAbiError, match="opa_eval fast path"):
            make_abi(include_opa_eval=False)

    def test_mistyped_export_signature_rejected(self) -> None:
        rt = WasmtimeRuntimeFactory(abi_module_wasm_bad_signature()).create_runtime()
        with pytest.raises(OpaAbiError, match="unexpected signature"):
            OpaAbi(rt)


class TestExportWrappers:
    def test_malloc_and_heap_wrappers(self) -> None:
        abi = make_abi()
        # stub opa_malloc echoes its arg; opa_heap_ptr_get returns 0.
        assert abi.opa_malloc(42) == 42
        assert abi.opa_heap_ptr_get() == 0
        # void wrappers must not raise.
        abi.opa_free(0)
        abi.opa_heap_ptr_set(0)

    def test_json_and_metadata_wrappers(self) -> None:
        abi = make_abi()
        assert abi.opa_json_parse(0, 0) == 0
        assert abi.opa_json_dump(0) == 0
        assert abi.entrypoints() == 0
        assert abi.builtins() == 0

    def test_opa_eval_wrapper_callable_when_supported(self) -> None:
        abi = make_abi()
        # stub returns 0; just confirm the arity/threading is correct.
        assert abi.opa_eval(0, 0, 0, 0, 0) == 0

    def test_abi_spec_is_shared_not_bound(self) -> None:
        # One spec instance can be passed to many OpaAbi bindings.
        spec = OpaAbiSpec()
        rt1 = WasmtimeRuntimeFactory(abi_module_wasm()).create_runtime()
        rt2 = WasmtimeRuntimeFactory(abi_module_wasm()).create_runtime()
        a1 = OpaAbi(rt1, spec)
        a2 = OpaAbi(rt2, spec)
        assert a1._spec is a2._spec  # asserting the shared-spec contract
