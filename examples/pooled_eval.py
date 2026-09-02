"""Concurrent evaluation across many threads sharing one pooled policy.

A single OpaWasmPolicy owns a bounded pool of Wasm instances and is safe to call
from many threads at once — construct it once and share it.

    uv run python examples/pooled_eval.py
"""

from __future__ import annotations

import threading
from pathlib import Path

from opapywasm import OpaWasmPolicy, PolicyConfig

WASM = Path(__file__).parent.parent / "tests" / "fixtures" / "wasm" / "data_based.wasm"


def main() -> None:
    policy = OpaWasmPolicy.from_wasm_file(
        WASM,
        config=PolicyConfig(pool_size=4, default_entrypoint="authz/allow"),
    )
    policy.set_data({"roles": {"alice": "admin"}})

    results: list[bool] = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        user = "alice" if tid % 2 == 0 else "bob"
        decision = bool(policy.evaluate({"user": user}))
        with lock:
            results.append(decision)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(results)
    print(f"16 threads, pool_size=4: {allowed} allowed, {len(results) - allowed} denied")
    print(f"instances actually created: {policy.instances_created} (<= pool_size)")


if __name__ == "__main__":
    main()
