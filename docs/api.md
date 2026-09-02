# API reference

## `PolicyConfig`

Immutable dataclass configuring a policy. Fields:

| Field | Default | Meaning |
|-------|---------|---------|
| `pool_size` | `4` | Pooled instances; bounds max concurrent evaluations. |
| `default_entrypoint` | `None` | Name/id used when `evaluate` gets no `entrypoint`. |
| `borrow_timeout_seconds` | `5.0` | Wait for a free instance; `None` blocks forever. |
| `strict_result` | `False` | Raise (vs. return `UNDEFINED`) on an undefined decision. |
| `strict_result_shape` | `True` | Raise on a non-empty result whose shape is not `[{"result": ...}]`. |
| `max_input_bytes` | `1_000_000` | Reject larger serialised input. |
| `max_data_bytes` | `10_000_000` | Reject larger serialised data. |
| `max_result_bytes` | `10_000_000` | Reject larger result documents. |
| `max_cstring_scan_bytes` | `10_000_000` | Cap on Wasm C-string scans. |
| `parse_raw_json_input` | `True` | Validate raw `str`/`bytes` input as JSON. |
| `max_memory_pages` | `4096` | Per-instance linear-memory cap (64 KiB pages); `None` = unbounded. |
| `eval_timeout_seconds` | `30.0` | Per-eval wall-clock deadline (epoch interruption); `None` = no limit. |

## `OpaWasmPolicy`

A bounded, thread-safe pool of policy instances. Construct once, call `evaluate`
from many threads.

```python
OpaWasmPolicy.from_wasm_file(path, config=...)
OpaWasmPolicy.from_wasm_bytes(data, config=...)
OpaWasmPolicy.from_wasm_stream(stream, config=...)

policy.set_data(data)                        # JSON object / string / bytes; None clears
policy.evaluate(input, entrypoint=None)      # simplified decision, or UNDEFINED
policy.evaluate_raw(input, entrypoint=None)  # raw OPA result set
policy.register_builtin(name, callable)      # custom host builtin
policy.entrypoints                           # {name: id}
policy.builtins                              # {required builtin name: id}
policy.abi_version                           # (major, minor)
policy.close()                               # or use as a context manager
```

`evaluate` returns the decision value when defined (which may be JSON `null` →
Python `None`), or the `UNDEFINED` sentinel (`opapywasm.UNDEFINED`) for an
undefined decision. `UNDEFINED` is falsy, so truthiness checks behave naturally.

## Resource-budget guidance

`max_memory_pages` and `eval_timeout_seconds` are **caps**, not reservations —
they bound worst-case usage, they do not pre-commit it. Note the worst-case
guest-memory ceiling scales with the pool:

```
worst-case guest memory  ≈  pool_size × max_memory_pages × 64 KiB
default (pool_size=4, max_memory_pages=4096)  ≈  4 × 256 MiB  =  ~1 GiB
```

Size these against your container/pod memory limit. For memory-constrained
deployments lower `max_memory_pages` (a typical policy uses a few pages) and/or
`pool_size`. `eval_timeout_seconds` should be set below any upstream request
timeout so a slow policy fails fast rather than tying up a worker.

## Errors

All raise types derive from `OpaWasmError`. See `opapywasm.errors` for the full
hierarchy (`OpaInvalidPolicyError`, `OpaAbiError`, `OpaEvaluationError`,
`OpaBuiltinError`, `OpaMemoryError`, `OpaPoolTimeoutError`,
`OpaPolicyClosedError`, `OpaInvalidInputError`, `OpaEntrypointError`,
`OpaDataError`, `OpaAbortError`).
