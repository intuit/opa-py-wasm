# Parity with `opa-java-wasm`

`opapywasm` targets **functional** parity with StyraOSS `opa-java-wasm` — the
same capabilities, exposed through an idiomatic Python API (names and shapes
differ; behaviour does not). It adds stronger explicit thread-safety and
resource-limit guarantees on top.

## Feature parity table

Legend: ✅ supported · ➕ supported and richer than Java · ⛔ not supported.
Java-column entries reflect what `opa-java-wasm` actually documents.

| Capability | `opa-java-wasm` | `opapywasm` | Notes |
|------------|-----------------|-------------|-------|
| Load from bytes / file / stream | ✅ | ✅ | Java: `byte[]`/`InputStream`/`Path`/`File`; Python: `from_wasm_bytes`/`from_wasm_file`/`from_wasm_stream` |
| Set external `data` | ✅ | ✅ | Python accepts a JSON string/bytes *or* a Python object; snapshotted immutably |
| Per-request `input` | ✅ | ✅ | |
| Evaluate | ✅ | ✅ | Java `evaluate` → result set; `evaluate_raw` matches it, `evaluate` simplifies |
| Entrypoint selection by name or id | ⛔ (not documented) | ➕ | pass a name/id to `evaluate`, or set `default_entrypoint` |
| Enumerate entrypoints | ✅ | ✅ | `policy.entrypoints` |
| Enumerate required builtins | ✅ | ✅ | `policy.builtins` |
| `opa_eval` fast path | ✅ | ✅ | ABI 1.2+; eval-context path intentionally unimplemented (rejected at load) |
| Custom host builtins | ⛔ (not documented) | ➕ | `register_builtin(name, fn)` |
| Default builtins: `sprintf`, `json.is_valid`, `yaml.*` | ✅ | ✅ | YAML via `opa-py-wasm[yaml]` extra; `json.is_valid` is inlined by OPA, not a host builtin |
| Bounded, thread-safe instance pool | ✅ | ✅ | Built into `OpaWasmPolicy`; `pool_size` in config |
| Execution-time / memory limits | ⛔ (not documented) | ➕ | `eval_timeout_seconds` (epoch), `max_memory_pages`, byte caps |
| Undefined vs. defined-null distinction | ⛔ (caller's job) | ➕ | `UNDEFINED` sentinel vs `None` |
| Bundle `.tar.gz` loading | ⛔ (not documented) | ⛔ | Neither SDK loads bundles; both take a compiled wasm module |

## Behavioural notes

- **Pooling is built in.** Java splits `OpaPolicy` from `OpaPolicyPool.create(...)`.
  In Python a single `OpaWasmPolicy` *is* a bounded, thread-safe pool (size set
  via `PolicyConfig(pool_size=...)`), so there is no separate pool type and no
  manual `borrow()`/`loan` in application code — the facade borrows for you.
- **No hidden worker threads.** `evaluate()` runs on the calling thread; the pool
  only bounds how many evaluations run at once. The application owns request
  concurrency. (One internal daemon thread exists per policy *only* when
  `eval_timeout_seconds` is set — it drives the Wasmtime epoch clock.)
- **Builtin semantics are best-effort, not byte-for-byte.** `json.is_valid` and
  the YAML family delegate to standard libraries and match closely. `sprintf`
  maps Go `fmt` verbs onto Python formatting and covers the common verbs
  (`%d %s %f %g %x %o %b %q %c %t %v %T %%`, plus width/precision/flags); it is
  not a complete Go `fmt` reimplementation, so exotic verbs and some rounding
  edge cases may differ. Vectors verified against native `opa eval` live in
  `tests/integration/test_sprintf_parity.py`.
- **Undefined vs. null.** `evaluate()` returns the `UNDEFINED` sentinel for an
  undefined decision and `None` only for a genuine JSON `null`. Java returns the
  raw result set and leaves this to the caller; `evaluate_raw` is the equivalent
  raw view here.
