"""C5 concurrency stress tests against real compiled policies.

The core guarantee: many threads sharing one OpaWasmPolicy with a pool smaller
than the thread count always get correct results, with no data bleed and no
traps, across many iterations.
"""

from __future__ import annotations

import threading

import pytest

from opapywasm import OpaWasmPolicy, PolicyConfig

pytestmark = [pytest.mark.concurrency, pytest.mark.requires_wasm]


def _run_threads(target, n_threads: int) -> None:
    threads = [threading.Thread(target=target, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


@pytest.mark.parametrize("pool_size", [1, 2, 4])
def test_data_policy_no_bleed_under_load(load_wasm, pool_size: int) -> None:
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("data_based"), PolicyConfig(pool_size=pool_size))
    policy.set_data({"roles": {"alice": "admin", "bob": "viewer"}})
    errors: list[tuple] = []

    n_threads, per_thread = 12, 150

    def worker(tid: int) -> None:
        for i in range(per_thread):
            user = "alice" if (tid + i) % 2 == 0 else "bob"
            expected = user == "alice"
            try:
                if policy.evaluate({"user": user}) != expected:
                    errors.append((tid, i, user))
            except Exception as exc:
                errors.append((tid, i, repr(exc)))

    _run_threads(worker, n_threads)
    assert errors == []
    # Pool must never create more than pool_size instances.
    assert policy.instances_created <= pool_size


def test_distinct_inputs_per_thread(load_wasm) -> None:
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("input_based"), PolicyConfig(pool_size=3))
    errors: list[tuple] = []

    def worker(tid: int) -> None:
        # Half the threads always send an allowed action, half a denied one.
        allowed = tid % 2 == 0
        action = "read" if allowed else "write"
        for _ in range(200):
            if policy.evaluate({"user": "alice", "action": action}) is not allowed:
                errors.append((tid, action))

    _run_threads(worker, 10)
    assert errors == []


def test_pool_never_exceeds_size_high_contention(load_wasm) -> None:
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("allow_true"), PolicyConfig(pool_size=2))

    def worker(_tid: int) -> None:
        for _ in range(300):
            assert policy.evaluate({}) is True

    _run_threads(worker, 20)
    assert policy.instances_created <= 2


@pytest.mark.parametrize("iteration", range(3))
def test_repeatable_across_iterations(load_wasm, iteration: int) -> None:
    # Re-run a smaller stress a few times to catch nondeterministic races.
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("data_based"), PolicyConfig(pool_size=2))
    policy.set_data({"roles": {"admin_user": "admin"}})
    errors: list[tuple] = []

    def worker(tid: int) -> None:
        for _ in range(100):
            if policy.evaluate({"user": "admin_user"}) is not True:
                errors.append((tid,))
            if policy.evaluate({"user": "ghost"}) is not False:
                errors.append((tid,))

    _run_threads(worker, 8)
    assert errors == []
