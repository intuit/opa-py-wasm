"""C6 concurrency tests: thread-safe data updates.

set_data must be safe to call while other threads evaluate, evaluations after
set_data returns must see the new data, and no instance may serve stale data
once it is re-borrowed.
"""

from __future__ import annotations

import threading
import time

import pytest

from opapywasm import OpaWasmPolicy, PolicyConfig

pytestmark = [pytest.mark.concurrency, pytest.mark.requires_wasm]


def test_evaluate_after_set_data_sees_new_data(load_wasm) -> None:
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("data_based"), PolicyConfig(pool_size=4))
    policy.set_data({"roles": {"u": "viewer"}})
    # Warm the whole pool so every instance starts at the old version.
    _warm_pool(policy, {"user": "u"}, expected=False)

    policy.set_data({"roles": {"u": "admin"}})
    # Every subsequent evaluation (across all pooled instances) must see admin.
    for _ in range(50):
        assert policy.evaluate({"user": "u"}) is True


def test_concurrent_eval_during_updates_never_crashes(load_wasm) -> None:
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("data_based"), PolicyConfig(pool_size=4))
    policy.set_data({"roles": {"u": "viewer"}})
    errors: list[str] = []
    stop = threading.Event()

    def evaluator() -> None:
        while not stop.is_set():
            try:
                # Result may be True or False depending on interleaving; the
                # contract here is only "no crash, always a valid bool".
                result = policy.evaluate({"user": "u"})
                if result not in (True, False):
                    errors.append(f"non-bool result {result!r}")
            except Exception as exc:
                errors.append(repr(exc))

    def updater() -> None:
        for i in range(80):
            policy.set_data({"roles": {"u": "admin" if i % 2 else "viewer"}})
            time.sleep(0.001)

    evs = [threading.Thread(target=evaluator) for _ in range(8)]
    up = threading.Thread(target=updater)
    for e in evs:
        e.start()
    up.start()
    up.join()
    stop.set()
    for e in evs:
        e.join()

    assert errors == []


def test_no_stale_data_after_update_completes(load_wasm) -> None:
    # Deterministic version: no concurrent evals during the assertion window.
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("data_based"), PolicyConfig(pool_size=3))
    for role, expected in [("viewer", False), ("admin", True), ("viewer", False), ("admin", True)]:
        policy.set_data({"roles": {"u": role}})
        # Hit enough times to exercise every pooled instance.
        assert all(policy.evaluate({"user": "u"}) is expected for _ in range(30))


def test_data_version_increments_per_set(load_wasm) -> None:
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("allow_true"), PolicyConfig(pool_size=2))
    start = policy.data_version
    for i in range(5):
        policy.set_data({"n": i})
    assert policy.data_version == start + 5


def _warm_pool(policy: OpaWasmPolicy, input_value: dict, *, expected: bool) -> None:
    """Force all pooled instances to be created (and cached at current version)."""
    barrier = threading.Barrier(policy.pool_size)
    results: list[object] = []
    lock = threading.Lock()

    def hold(_tid: int) -> None:
        barrier.wait()  # ensure all threads borrow simultaneously -> distinct instances
        r = policy.evaluate(input_value)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=hold, args=(t,)) for t in range(policy.pool_size)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r is expected for r in results)
