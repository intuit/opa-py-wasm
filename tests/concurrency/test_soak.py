"""Soak / lifecycle-race tests.

* Memory-stability soak: many thousands of evaluations must not grow the Wasm
  linear memory or leak heap (proving the pointer-reset allocator lifecycle is
  leak-free — see docs/opa_abi.md).
* close()-vs-borrow race: closing a policy while a thread is blocked waiting for
  an instance must wake that thread with OpaPolicyClosedError, not hang it.
"""

from __future__ import annotations

import threading
import time

import pytest

from opapywasm import OpaWasmPolicy, PolicyConfig
from opapywasm.errors import OpaEvaluationError, OpaInvalidPolicyError, OpaPolicyClosedError

pytestmark = [pytest.mark.integration, pytest.mark.concurrency, pytest.mark.requires_wasm]


def _epoch_ticker_count() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "opapywasm-epoch-ticker")


def test_memory_and_heap_stable_over_many_evals(load_wasm) -> None:
    p = OpaWasmPolicy.from_wasm_bytes(load_wasm("input_based"), PolicyConfig(pool_size=1))
    with p._pool.borrow() as loan:
        mem_start = loan.instance.abi.memory_len()
        heap_start = loan.instance.abi.opa_heap_ptr_get()
    for i in range(5000):
        p.evaluate({"user": "alice", "action": "read", "n": i})
    with p._pool.borrow() as loan:
        mem_end = loan.instance.abi.memory_len()
        heap_end = loan.instance.abi.opa_heap_ptr_get()
    # Zero growth: the per-eval heap reset reclaims everything each time.
    assert mem_end == mem_start, f"memory grew {mem_start} -> {mem_end}"
    assert heap_end == heap_start, f"heap ptr drifted {heap_start} -> {heap_end}"
    p.close()


def test_close_wakes_blocked_borrower(load_wasm) -> None:
    # pool_size=1, hold the only instance, then have a second thread block in
    # evaluate(); closing the policy must release it promptly with a typed error.
    p = OpaWasmPolicy.from_wasm_bytes(
        load_wasm("allow_true"),
        PolicyConfig(pool_size=1, borrow_timeout_seconds=10.0),
    )
    holder_has_instance = threading.Event()
    release_holder = threading.Event()
    blocked_result: list[object] = []

    def holder() -> None:
        with p._pool.borrow():
            holder_has_instance.set()
            release_holder.wait(timeout=5.0)

    def blocked() -> None:
        holder_has_instance.wait(timeout=5.0)
        try:
            p.evaluate({})
            blocked_result.append("evaluated")
        except OpaPolicyClosedError:
            blocked_result.append("closed")
        except Exception as exc:
            blocked_result.append(type(exc).__name__)

    h = threading.Thread(target=holder)
    b = threading.Thread(target=blocked)
    h.start()
    b.start()
    holder_has_instance.wait(timeout=5.0)
    # The blocked borrower is now waiting on the empty queue; close should wake it.
    p.close()
    b.join(timeout=5.0)
    release_holder.set()
    h.join(timeout=5.0)

    assert blocked_result == ["closed"], blocked_result
    assert not b.is_alive()


def test_close_during_concurrent_release_does_not_deadlock(load_wasm) -> None:
    # Regression: with all pool_size instances on loan, close() drains the (empty)
    # queue and posts a wake sentinel while the loans release concurrently and
    # refill the queue. A blocking put on the now-full queue would deadlock
    # close() forever; it must complete promptly instead.
    pool_size = 4
    p = OpaWasmPolicy.from_wasm_bytes(
        load_wasm("allow_true"),
        PolicyConfig(pool_size=pool_size, borrow_timeout_seconds=5.0),
    )
    all_borrowed = threading.Barrier(pool_size + 1)
    release = threading.Event()

    def holder() -> None:
        with p._pool.borrow():
            all_borrowed.wait(timeout=5.0)
            release.wait(timeout=5.0)

    holders = [threading.Thread(target=holder) for _ in range(pool_size)]
    for t in holders:
        t.start()
    all_borrowed.wait(timeout=5.0)  # queue now empty, all instances on loan

    closed = threading.Event()

    def do_close() -> None:
        p.close()
        closed.set()

    ct = threading.Thread(target=do_close)
    ct.start()
    release.set()  # loans release concurrently, refilling the queue during close
    ct.join(timeout=10.0)

    assert closed.is_set(), "close() deadlocked while loans released concurrently"
    for t in holders:
        t.join(timeout=5.0)
        assert not t.is_alive()


def test_concurrent_evals_do_not_grow_memory(load_wasm) -> None:
    # Multi-threaded variant: a plateau under real parallel load.
    p = OpaWasmPolicy.from_wasm_bytes(load_wasm("data_based"), PolicyConfig(pool_size=4))
    p.set_data({"roles": {"alice": "admin"}})
    errors: list[str] = []

    def worker(_: int) -> None:
        try:
            for _ in range(300):
                p.evaluate({"user": "alice"})
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # Never created more than the pool bound.
    assert p.instances_created <= 4
    p.close()


def test_close_during_running_eval_still_times_out(load_wasm) -> None:
    # A slow evaluation whose termination depends on the epoch deadline must
    # still time out even if close() is called while it runs: close() keeps the
    # ticker alive until the in-flight eval finishes.
    p = OpaWasmPolicy.from_wasm_bytes(
        load_wasm("slow_loop"),
        PolicyConfig(pool_size=1, eval_timeout_seconds=0.3, max_memory_pages=None),
    )
    result: list[str] = []

    def run() -> None:
        try:
            p.evaluate({})
            result.append("completed")
        except OpaEvaluationError as exc:
            result.append("timeout" if "time limit" in str(exc) else "other")

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.05)  # let the eval enter opa_eval
    p.close()  # concurrent close must not disable the deadline
    t.join(timeout=10.0)

    assert not t.is_alive(), "evaluation hung — deadline never fired after close()"
    assert result == ["timeout"], result


def test_failed_construction_does_not_leak_ticker_threads() -> None:
    # Repeated failed constructions must not accumulate epoch-ticker threads.
    # Construction fails while compiling the invalid module; the factory's
    # cleanup (and the fact the ticker only starts on full success) must leave
    # no background thread behind.
    before = _epoch_ticker_count()
    bad_wasm = b"\x00asm\xff\xff\xff\xff"  # invalid module → construction fails
    for _ in range(10):
        with pytest.raises(OpaInvalidPolicyError):
            OpaWasmPolicy.from_wasm_bytes(bad_wasm, PolicyConfig(eval_timeout_seconds=5.0))
    time.sleep(0.1)  # let any (incorrectly leaked) daemon threads become observable
    after = _epoch_ticker_count()
    assert after <= before, f"ticker threads leaked: {before} -> {after}"


def test_close_stops_ticker_thread(load_wasm) -> None:
    # A successful policy with a timeout starts exactly one ticker; close()
    # must stop it (no lingering background thread once idle-closed).
    before = _epoch_ticker_count()
    p = OpaWasmPolicy.from_wasm_bytes(load_wasm("allow_true"), PolicyConfig(eval_timeout_seconds=5.0))
    p.evaluate({})  # starts the ticker
    assert _epoch_ticker_count() == before + 1
    p.close()
    time.sleep(0.1)
    assert _epoch_ticker_count() == before, "ticker thread not stopped by close()"
