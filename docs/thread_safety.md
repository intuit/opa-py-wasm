# Thread safety

## The contract

Wasmtime's `Engine` and a compiled `Module` may be shared across threads. A
`Store`, `Instance`, `Memory`, its bound exports, and all OPA heap state and
value addresses derived from them **must not** be used concurrently by more than
one thread.

`opapywasm` enforces this by:

1. Bundling everything instance-local into a single `OpaPolicyInstance`.
2. Handing instances out **exclusively** through `OpaInstancePool.borrow()`.
   No two threads ever hold the same instance at once.
3. Providing a **blocking, thread-safe** public API. The application (or a
   higher-level library) owns request-level concurrency.

There is **no hidden `ThreadPoolExecutor`** inside the wrapper. `evaluate()`
runs on the calling thread; the pool bounds how many run at once.

## Correct usage

```
Application threads ── policy.evaluate(...) ──► OpaWasmPolicy
                                                   borrow instance (exclusive)
                                                   evaluate
                                                   return instance to pool
```

## What is shared vs. per-instance

| Shared (immutable)                 | Per-instance (never shared)                     |
|------------------------------------|-------------------------------------------------|
| `Engine`, compiled `Module`        | `Store`, `Instance`, `Memory`, exports          |
| `OpaAbiSpec`                       | `OpaAbi`                                         |
| entrypoint map, builtin id↔name    | `data_addr`, `data_heap_ptr`, `initial_heap_ptr`|
| `BuiltinRegistry` snapshot         | `MemoryCodec`, `DataManager`, `Evaluator`       |

## Data updates

`set_data()` is O(1): under a short lock it swaps the policy-level
`(data, data_version)` snapshot and bumps the version. It does **not** touch
pooled instances directly, so it never blocks on in-flight evaluations.

Refresh is **lazy**: `OpaInstancePool.borrow()` compares the borrowed instance's
`data_version` to the policy snapshot and reloads that instance's data (via
`reload_data`, which resets the heap and re-parses data into that instance's own
memory) before yielding it. Because taking the instance out of the pool
happens-before the snapshot read, once `set_data()` returns no *new* evaluation
can observe stale data. In-flight evaluations complete against the data they
started with.

Each instance owns its own `data_addr` / `data_heap_ptr`; these are never shared
across instances, even after a reload.

An alternative eager drain-and-reload was considered; the lazy model was chosen
because it keeps `set_data` non-blocking and correctly handles instances that
have not been lazily created yet.

## Custom builtins run concurrently

A callable registered via `register_builtin` is **shared across all pooled
instances** and is invoked on whichever application thread is evaluating. It can
therefore run on several threads at once. Custom builtins **must be
thread-safe**: no unsynchronised shared mutable state (guard it yourself if you
have any). They should also be bounded — a builtin runs inside the caller's
`eval_timeout_seconds` deadline — and must not retain guest memory pointers
across calls. Prefer pure/functional builtins. The SDK's default builtins
(`sprintf`, `json.is_valid`, the `yaml.*` family) are all thread-safe.

## Lifecycle and `close()`

`close()` stops new borrows and wakes any thread blocked waiting for an instance
with `OpaPolicyClosedError`. It does **not** block on evaluations already in
flight. The shared epoch-ticker thread (present only when `eval_timeout_seconds`
is set) is kept alive until the last in-flight evaluation finishes, so a runaway
evaluation still times out even if `close()` is called concurrently; the last
evaluation to complete stops the ticker.
