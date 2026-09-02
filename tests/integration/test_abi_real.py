"""C2 integration tests: OpaAbi against real compiled OPA policies.

These require the committed ``.wasm`` fixtures (built by scripts/build_fixtures.py).
They validate that the ABI adapter's assumptions match what the OPA compiler
actually emits — the "don't guess the ABI" guarantee.
"""

from __future__ import annotations

import pytest

from opapywasm.abi import OpaAbi
from opapywasm.runtime import WasmtimeRuntimeFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_wasm]


def _abi(load_wasm, name: str) -> OpaAbi:
    rt = WasmtimeRuntimeFactory(load_wasm(name)).create_runtime()
    return OpaAbi(rt)


def test_real_policy_supports_opa_eval(load_wasm) -> None:
    abi = _abi(load_wasm, "allow_true")
    assert abi.supports_opa_eval is True


def test_real_policy_capability_flags(load_wasm) -> None:
    # The current OPA toolchain (ABI 1.3) exposes eval-ctx, heap-stash and
    # value-free. Assert they are detected so the flags track reality.
    abi = _abi(load_wasm, "allow_true")
    assert abi.supports_eval_context is True
    assert abi.supports_heap_stash is True
    assert abi.supports_value_free is True


def test_entrypoints_and_builtins_addresses_are_nonzero(load_wasm) -> None:
    abi = _abi(load_wasm, "multi_entrypoint")
    # These return addresses of OPA values in this instance's memory. Decoding
    # them into Python maps is C3; here we only assert we can obtain a handle.
    assert abi.entrypoints() > 0
    assert abi.builtins() >= 0


def test_heap_ptr_is_readable(load_wasm) -> None:
    abi = _abi(load_wasm, "allow_true")
    assert abi.opa_heap_ptr_get() > 0


@pytest.mark.parametrize(
    "name",
    [
        "allow_true",
        "allow_false",
        "input_based",
        "data_based",
        "multi_entrypoint",
        "return_object",
        "return_array",
        "return_scalar",
        "undefined_result",
        "default_builtins",
        "unsupported_builtin",
        "custom_builtin",
    ],
)
def test_all_fixtures_pass_abi_validation(load_wasm, name: str) -> None:
    # Constructing OpaAbi runs full ABI + required-export validation. Every
    # committed fixture must pass it, and report ABI 1.x with minor >= 2 (the
    # opa_eval fast path the MVP requires).
    abi = _abi(load_wasm, name)
    major, minor = abi.abi_version
    assert major == 1
    assert minor >= 2
