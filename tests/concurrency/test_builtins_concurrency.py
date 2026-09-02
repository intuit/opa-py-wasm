"""C7 concurrency: builtin registry + dispatch under load."""

from __future__ import annotations

import threading

import pytest

from opapywasm import OpaWasmPolicy, PolicyConfig
from opapywasm.builtins import BuiltinRegistry

pytestmark = [pytest.mark.concurrency, pytest.mark.requires_wasm]


def test_custom_builtin_invoked_concurrently(load_wasm) -> None:
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("custom_builtin"), PolicyConfig(pool_size=4))
    policy.register_builtin("my.custom_builtin", lambda x: {"doubled": x * 2})
    errors: list[object] = []

    def worker(tid: int) -> None:
        for i in range(150):
            n = tid * 1000 + i
            try:
                if policy.evaluate({"value": n}) != {"doubled": n * 2}:
                    errors.append((tid, i))
            except Exception as exc:
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_registry_register_is_thread_safe() -> None:
    # Hammer register() from many threads; snapshots must always be consistent
    # dicts and the final registry must contain every name.
    registry = BuiltinRegistry()
    errors: list[object] = []

    def register_many(tid: int) -> None:
        for i in range(200):
            name = f"fn.{tid}.{i}"
            registry.register(name, lambda: tid)
            snap = registry.snapshot()  # must never raise / be a torn dict
            if name not in snap:
                errors.append(name)

    threads = [threading.Thread(target=register_many, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(registry.names()) == 8 * 200
