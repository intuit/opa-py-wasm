# OPA Wasm ABI notes

This document records the ABI facts the SDK relies on.

References:
- OPA WebAssembly docs: https://www.openpolicyagent.org/docs/latest/wasm/
- StyraOSS `opa-java-wasm`
- `open-policy-agent/npm-opa-wasm` (secondary ABI reference)
- `wasmtime.py` docs

## ABI version

Read from module globals:

```
opa_wasm_abi_version         major
opa_wasm_abi_minor_version   minor
```

MVP requires ABI **1.2+** so the one-shot `opa_eval` fast path is available.
The eval-context fallback (`opa_eval_ctx_*` + `eval`) is documented but deferred
unless explicitly implemented. ABI 1.3 adds heap-stash and value-free helpers,
used when present.

## Imports the host must provide

```
env.memory
env.opa_abort
env.opa_println
env.opa_builtin0 .. env.opa_builtin4
```

## Exports the SDK uses

```
entrypoints()            builtins()
opa_malloc(size)         opa_free(addr)
opa_json_parse(a, n)     opa_json_dump(value_addr)
opa_value_parse(a, n)    opa_value_dump(value_addr)
opa_heap_ptr_get()       opa_heap_ptr_set(addr)
opa_eval(...)            (fast path)
```

Capability flags detected per module: `supports_opa_eval`,
`supports_eval_context`, `supports_heap_stash`, `supports_value_free`.

## Metadata

`entrypoints()` → `{ "authz/allow": 0, ... }` (name → id). Stored at policy
level. `builtins()` → `{ "http.send": 0, ... }` (name → id). Both directions
(`builtin_name_to_id`, `builtin_id_to_name`) are kept; `builtin_id_to_name`
drives `opa_builtinN` dispatch.

## Verified against real fixtures (C2)

Confirmed by inspecting the committed `.wasm` fixtures compiled with OPA CLI
1.18.2 (`scripts/build_fixtures.py`):

- **ABI version emitted: `1.3`** (`opa_wasm_abi_version=1`, `opa_wasm_abi_minor_version=3`).
- The module **exports** all required functions plus `opa_eval`, the full
  `opa_eval_ctx_*` / `eval` set, `opa_heap_blocks_stash/restore`,
  `opa_heap_stash_clear`, `opa_value_free`, and `opa_value_add_path/remove_path`
  (the last two used by C9). So all four capability flags are `True`.
- `opa_eval` signature confirmed: **7× i32 → i32**
  (`reserved, entrypoint_id, data, input, input_len, heap_ptr, format`).
- The module **imports** `env.memory`, `env.opa_abort`, and `env.opa_builtin0..4`
  — but **not** `env.opa_println` in the current toolchain. The runtime only
  defines imports the module declares, so this is handled.

`OpaAbiSpec` requires ABI major 1 and treats minor ≥ 2 as "has the `opa_eval`
fast path". `OpaAbi` reads the globals, validates, checks required exports, and
derives the capability flags — all exercised against these real fixtures in
`tests/integration/test_abi_real.py`.

## `opa_eval` evaluation recipe (C4)

Verified against real fixtures — the per-evaluation sequence is:

1. Reset the heap to the post-data checkpoint (`opa_heap_ptr_set(data_heap_ptr)`).
2. Write the input as raw JSON bytes into the heap.
3. **Capture `heap_ptr = opa_heap_ptr_get()` *after* the input write.** This is
   the base `opa_eval` uses for its scratch allocations; if it points at or
   before the input buffer, eval clobbers the input and silently returns the
   wrong/empty result. (This bit us during C4 — `enabled=true` returned `[]`
   until the heap ptr was captured after the write.)
4. `opa_eval(0, entrypoint_id, data_addr, input_addr, input_len, heap_ptr, 0)`
   (`reserved=0`, `format=0` for JSON). Returns the address of a NUL-terminated
   JSON result string.
5. Read + parse the result.
6. Reset the heap to the data checkpoint in a `finally`.

Data (long-lived) is parsed once and lives below `data_heap_ptr`; input +
result (short-lived) live above it and are discarded by the reset.

## Allocator lifecycle: why pointer-reset, not `opa_free` / `opa_value_free`

The SDK deliberately manages per-evaluation memory by **bump-pointer reset**
(`opa_heap_ptr_set` back to `data_heap_ptr`) rather than by calling `opa_free` /
`opa_value_free` on individual values, and does **not** use the ABI 1.3
heap-stash helpers (`opa_heap_blocks_stash` / `_restore` / `_clear`). Rationale:

- **The `opa_eval` fast path owns its own scratch.** All allocations a single
  `opa_eval` makes (parsed input, intermediate values, the result string) live
  above `data_heap_ptr`. Resetting the heap pointer to that checkpoint after
  every evaluation reclaims *all* of them in O(1), with no per-value bookkeeping
  and no chance of a double-free or missed-free.
- **It is leak-free and stable.** Because the data region below the checkpoint
  is never touched by an evaluation and everything above is reclaimed wholesale,
  the heap pointer and Wasm page count return to a fixed value after each call.
  This is asserted by the soak test (`tests/concurrency/test_soak.py`): thousands
  of evaluations produce zero net heap-pointer or memory-size growth.
- **`opa_free` / stash-restore would be strictly more work for the same result.**
  They matter for the *eval-context* lifecycle (many calls sharing one context,
  where you want to free individual values between steps). We do not implement
  that lifecycle, so those helpers add complexity and failure modes without
  changing the memory outcome.

Capability flags for `opa_value_free` and the stash helpers are still *detected*
(so the information is available and a future eval-context implementation can use
them), they are simply not invoked by the `opa_eval` path. `opa_free` is wrapped
in `OpaAbi` for completeness/tests but is likewise unused by the hot path.

If the eval-context fallback is ever implemented, it **should** use
stash/restore (or explicit `opa_value_free`) for its intermediate values; the
pointer-reset shortcut is valid only because `opa_eval` is one-shot.
