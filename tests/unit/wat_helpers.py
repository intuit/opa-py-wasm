"""Synthetic WebAssembly fixtures for runtime/ABI unit tests.

These let the low-level layers (C1 runtime, C2 ABI validation) be tested without
the OPA CLI: we hand-write minimal modules in WebAssembly text format that import
the OPA host functions and export enough to exercise the code under test. Real
compiled OPA policies are used by the integration tests (C4+).
"""

from __future__ import annotations

import wasmtime

# A minimal module shaped like an OPA policy: imports env.memory + all the OPA
# host functions, exports a couple of ABI globals and helper functions that call
# back into the host so tests can drive opa_abort / opa_builtin0.
OPA_LIKE_WAT = r"""
(module
  (import "env" "memory" (memory 2))
  (import "env" "opa_abort" (func $abort (param i32)))
  (import "env" "opa_println" (func $println (param i32)))
  (import "env" "opa_builtin0" (func $b0 (param i32 i32) (result i32)))
  (import "env" "opa_builtin1" (func $b1 (param i32 i32 i32) (result i32)))
  (import "env" "opa_builtin2" (func $b2 (param i32 i32 i32 i32) (result i32)))
  (import "env" "opa_builtin3" (func $b3 (param i32 i32 i32 i32 i32) (result i32)))
  (import "env" "opa_builtin4" (func $b4 (param i32 i32 i32 i32 i32 i32) (result i32)))
  ;; message string "boom" at offset 16 for the abort test
  (data (i32.const 16) "boom\00")
  (func (export "opa_malloc") (param i32) (result i32) i32.const 0)
  (func (export "trigger_abort") (call $abort (i32.const 16)))
  (func (export "trigger_println") (call $println (i32.const 16)))
  (func (export "trigger_builtin0") (result i32) (call $b0 (i32.const 7) (i32.const 0)))
  (global (export "opa_wasm_abi_version") i32 (i32.const 1))
  (global (export "opa_wasm_abi_minor_version") i32 (i32.const 2))
)
"""

# A valid module that is NOT an OPA policy (no env.memory import).
NON_OPA_WAT = r"""
(module
  (func (export "add") (param i32 i32) (result i32)
    local.get 0
    local.get 1
    i32.add))
"""

# Valid wasm, imports env.memory, but also imports an unsatisfiable function
# from a foreign module — instantiation must fail.
UNSATISFIABLE_IMPORT_WAT = r"""
(module
  (import "env" "memory" (memory 1))
  (import "other" "missing" (func)))
"""


# Module that aborts with a wildly out-of-bounds address, so the host's lenient
# c-string read fails and must degrade to a placeholder rather than crash.
OOB_ABORT_WAT = r"""
(module
  (import "env" "memory" (memory 1))
  (import "env" "opa_abort" (func $abort (param i32)))
  (func (export "trigger_oob_abort") (call $abort (i32.const 999999999))))
"""


def opa_like_wasm() -> bytes:
    return bytes(wasmtime.wat2wasm(OPA_LIKE_WAT))


def oob_abort_wasm() -> bytes:
    return bytes(wasmtime.wat2wasm(OOB_ABORT_WAT))


# Imports "env.memory" but as a function rather than a memory — malformed OPA.
ENV_MEMORY_WRONG_KIND_WAT = r"""
(module
  (import "env" "memory" (func)))
"""


def env_memory_wrong_kind_wasm() -> bytes:
    return bytes(wasmtime.wat2wasm(ENV_MEMORY_WRONG_KIND_WAT))


# Declares only a 2-page minimum on env.memory (the runtime's default initial
# size) but has an active data segment written at offset 131072 (start of the
# 3rd page) — instantiation must copy that segment in immediately, before the
# guest ever gets a chance to call memory.grow, so a host that always
# instantiates with exactly 2 initial pages traps. Reproduces real compiled
# OPA modules whose embedded data/string literals overflow 128 KiB.
LARGE_DATA_SEGMENT_WAT = r"""
(module
  (import "env" "memory" (memory 2))
  (import "env" "opa_abort" (func $abort (param i32)))
  (import "env" "opa_builtin0" (func (param i32 i32) (result i32)))
  (import "env" "opa_builtin1" (func (param i32 i32 i32) (result i32)))
  (import "env" "opa_builtin2" (func (param i32 i32 i32 i32) (result i32)))
  (import "env" "opa_builtin3" (func (param i32 i32 i32 i32 i32) (result i32)))
  (import "env" "opa_builtin4" (func (param i32 i32 i32 i32 i32 i32) (result i32)))
  (data (i32.const 131072) "needs a 3rd page")
  (func (export "opa_malloc") (param i32) (result i32) (i32.const 0))
)
"""


def large_data_segment_wasm() -> bytes:
    return bytes(wasmtime.wat2wasm(LARGE_DATA_SEGMENT_WAT))


# A start function that unconditionally traps via `unreachable` — an
# instantiation-time trap unrelated to memory sizing (TrapCode.UNREACHABLE,
# not MEMORY_OUT_OF_BOUNDS). Must fail immediately, not drive the
# memory-doubling retry loop.
UNRELATED_TRAP_WAT = r"""
(module
  (import "env" "memory" (memory 2))
  (func $start (unreachable))
  (start $start)
  (func (export "opa_malloc") (param i32) (result i32) (i32.const 0))
)
"""


def unrelated_instantiation_trap_wasm() -> bytes:
    return bytes(wasmtime.wat2wasm(UNRELATED_TRAP_WAT))


# ---------------------------------------------------------------------------
# Configurable OPA-ABI-shaped module builder for C2 (ABI adapter) tests.
# ---------------------------------------------------------------------------

# Exports every supported OPA policy must provide (see OpaAbiSpec).
ABI_REQUIRED_EXPORTS: tuple[str, ...] = (
    "opa_malloc",
    "opa_free",
    "opa_json_parse",
    "opa_json_dump",
    "opa_heap_ptr_get",
    "opa_heap_ptr_set",
    "entrypoints",
    "builtins",
)

# WAT stub bodies keyed by export name — minimal functions with the right arity.
_ABI_EXPORT_STUBS: dict[str, str] = {
    "opa_malloc": '(func (export "opa_malloc") (param i32) (result i32) (local.get 0))',
    "opa_free": '(func (export "opa_free") (param i32))',
    "opa_json_parse": '(func (export "opa_json_parse") (param i32 i32) (result i32) (i32.const 0))',
    "opa_json_dump": '(func (export "opa_json_dump") (param i32) (result i32) (i32.const 0))',
    "opa_heap_ptr_get": '(func (export "opa_heap_ptr_get") (result i32) (i32.const 0))',
    "opa_heap_ptr_set": '(func (export "opa_heap_ptr_set") (param i32))',
    "entrypoints": '(func (export "entrypoints") (result i32) (i32.const 0))',
    "builtins": '(func (export "builtins") (result i32) (i32.const 0))',
}

_OPA_EVAL_STUB = '(func (export "opa_eval") (param i32 i32 i32 i32 i32 i32 i32) (result i32) (i32.const 0))'

EVAL_CTX_STUBS: tuple[str, ...] = (
    '(func (export "opa_eval_ctx_new") (result i32) (i32.const 0))',
    '(func (export "opa_eval_ctx_set_input") (param i32 i32))',
    '(func (export "opa_eval_ctx_set_data") (param i32 i32))',
    '(func (export "opa_eval_ctx_set_entrypoint") (param i32 i32))',
    '(func (export "eval") (param i32))',
    '(func (export "opa_eval_ctx_get_result") (param i32) (result i32) (i32.const 0))',
)

HEAP_STASH_STUBS: tuple[str, ...] = (
    '(func (export "opa_heap_blocks_stash") (result i32) (i32.const 0))',
    '(func (export "opa_heap_blocks_restore") (param i32))',
    '(func (export "opa_heap_stash_clear"))',
)

VALUE_FREE_STUB = '(func (export "opa_value_free") (param i32))'


def abi_module_wat(
    *,
    major: int = 1,
    minor: int = 2,
    include_minor_global: bool = True,
    exports: tuple[str, ...] | None = None,
    include_opa_eval: bool = True,
    extra_funcs: tuple[str, ...] = (),
) -> str:
    """Build WAT for an OPA-ABI-shaped module with configurable knobs.

    Lets C2 tests vary the ABI version, drop required exports, toggle the
    ``opa_eval`` fast path, and add capability exports.
    """
    exports = ABI_REQUIRED_EXPORTS if exports is None else exports
    lines = ["(module", '(import "env" "memory" (memory 1))']
    lines += [_ABI_EXPORT_STUBS[name] for name in exports]
    if include_opa_eval:
        lines.append(_OPA_EVAL_STUB)
    lines += list(extra_funcs)
    lines.append(f'(global (export "opa_wasm_abi_version") i32 (i32.const {major}))')
    if include_minor_global:
        lines.append(f'(global (export "opa_wasm_abi_minor_version") i32 (i32.const {minor}))')
    lines.append(")")
    return "\n".join(lines)


def abi_module_wasm(**kwargs: object) -> bytes:
    return bytes(wasmtime.wat2wasm(abi_module_wat(**kwargs)))  # type: ignore[arg-type]


def abi_module_wasm_bad_signature() -> bytes:
    """An OPA-ABI-shaped module whose ``opa_malloc`` has the wrong signature.

    ``opa_malloc`` should be ``(i32) -> i32``; here it takes no params, so the
    construction-time signature validation must reject it.
    """
    exports = tuple(e for e in ABI_REQUIRED_EXPORTS if e != "opa_malloc")
    lines = ["(module", '(import "env" "memory" (memory 1))']
    lines += [_ABI_EXPORT_STUBS[name] for name in exports]
    # Wrong: no param.
    lines.append('(func (export "opa_malloc") (result i32) (i32.const 0))')
    lines.append(_OPA_EVAL_STUB)
    lines.append('(global (export "opa_wasm_abi_version") i32 (i32.const 1))')
    lines.append('(global (export "opa_wasm_abi_minor_version") i32 (i32.const 2))')
    lines.append(")")
    return bytes(wasmtime.wat2wasm("\n".join(lines)))


def abi_module_wasm_float_version() -> bytes:
    """An OPA-ABI-shaped module whose version global is an f64, not an i32.

    Exercises the 'ABI version globals are not integers' guard.
    """
    lines = ["(module", '(import "env" "memory" (memory 1))']
    lines += [_ABI_EXPORT_STUBS[name] for name in ABI_REQUIRED_EXPORTS]
    lines.append(_OPA_EVAL_STUB)
    lines.append('(global (export "opa_wasm_abi_version") f64 (f64.const 1))')
    lines.append(")")
    return bytes(wasmtime.wat2wasm("\n".join(lines)))


def non_opa_wasm() -> bytes:
    return bytes(wasmtime.wat2wasm(NON_OPA_WAT))


def unsatisfiable_import_wasm() -> bytes:
    return bytes(wasmtime.wat2wasm(UNSATISFIABLE_IMPORT_WAT))
