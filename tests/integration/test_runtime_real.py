"""C1 integration tests: WasmtimeRuntime against real compiled OPA policies."""

from __future__ import annotations

import pytest

from opapywasm.runtime import WasmtimeRuntimeFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_wasm]


def test_real_policy_instantiates(load_wasm) -> None:
    factory = WasmtimeRuntimeFactory(load_wasm("allow_true"))
    rt = factory.create_runtime()
    # Real OPA modules export these ABI functions and the memory.
    assert rt.has_export("opa_malloc")
    assert rt.has_export("opa_eval")
    assert rt.memory.size(rt.store) >= 1


def test_real_policy_does_not_import_opa_println(load_wasm) -> None:
    # The current OPA toolchain does not import env.opa_println; the runtime
    # must still instantiate (it only defines imports the module declares).
    factory = WasmtimeRuntimeFactory(load_wasm("allow_true"))
    declared = {(imp.module, imp.name) for imp in factory.module.imports}
    assert ("env", "memory") in declared
    assert ("env", "opa_println") not in declared
    factory.create_runtime()  # must not raise


def test_many_independent_runtimes_from_one_factory(load_wasm) -> None:
    factory = WasmtimeRuntimeFactory(load_wasm("allow_true"))
    runtimes = [factory.create_runtime() for _ in range(5)]
    stores = {id(rt.store) for rt in runtimes}
    memories = {id(rt.memory) for rt in runtimes}
    assert len(stores) == 5
    assert len(memories) == 5
