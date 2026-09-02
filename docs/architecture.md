# Architecture

`opapywasm` is layered to keep three concerns separate:

```
wasmtime.py        Runs WebAssembly (engine, module, store, instance, memory).
OPA Wasm ABI       The low-level protocol for talking to a compiled policy.wasm.
opapywasm wrapper  A safe, Pythonic SDK over the above.
```

## Object model

```
OpaWasmPolicy            Thread-safe public facade. Owns the pool.
  └─ OpaInstancePool     Bounded, thread-safe queue of instances.
       └─ OpaPolicyInstance   One Wasm instance. NOT thread-safe alone.
            ├─ Store / Instance / Memory / exports   (wasmtime, per instance)
            ├─ OpaAbi          Per-instance ABI adapter.
            ├─ MemoryCodec     Python ↔ JSON ↔ Wasm-memory marshalling.
            ├─ DataManager     Per-instance data + heap checkpointing.
            └─ Evaluator       One evaluation on one exclusive instance.

Shared, immutable, safe to share across instances:
  Engine, compiled Module, OpaAbiSpec, entrypoint map, builtin id↔name maps,
  BuiltinRegistry snapshot.
```

## Compile once, instantiate many

The `Engine` and compiled `Module` are created once and shared. Each pooled
`OpaPolicyInstance` gets its own `Store`, `Memory`, `Instance`, bound exports,
`OpaAbi`, `MemoryCodec`, `DataManager` and `Evaluator`. OPA value addresses and
heap state are **instance-local** — an address from instance A is meaningless in
instance B.

See [thread_safety.md](thread_safety.md) for the concurrency contract and
[opa_abi.md](opa_abi.md) for the ABI details.
