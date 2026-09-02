"""C5 unit tests: OpaInstancePool mechanics (no real Wasm).

Uses a fake instance so borrow/return, lazy refresh, discard/replace, timeout,
and close can be tested deterministically. TestConcurrentLifecycleSafety is
the exception — it uses a real (synthetic WAT) WasmtimeRuntime, since the
race it guards against only exists at the native wasmtime layer.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from opapywasm.errors import OpaPolicyClosedError, OpaPoolTimeoutError
from opapywasm.instance import OpaPolicyInstance
from opapywasm.pool import OpaInstancePool, PolicyLoan
from opapywasm.runtime import WasmtimeRuntime, WasmtimeRuntimeFactory

from .wat_helpers import large_data_segment_wasm


class FakeInstance:
    _counter = 0

    def __init__(self, data: object = None, version: int = 0) -> None:
        FakeInstance._counter += 1
        self.id = FakeInstance._counter
        self.data = data
        self.data_version = version
        self.reloads: list[tuple[object, int]] = []
        # Mirrors OpaPolicyInstance.poisoned; the pool checks it on release.
        self.poisoned = False
        # Mirrors OpaPolicyInstance.close(); the pool calls it to free native
        # resources. Tracked so tests can assert an instance was closed exactly
        # once (no leak, no double-free).
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def reload_data(self, data: object, version: int) -> None:
        self.data = data
        self.data_version = version
        self.reloads.append((data, version))


def make_pool(*, max_size=2, version=0, timeout=1.0) -> tuple[OpaInstancePool, dict]:
    state: dict[str, Any] = {"data": {"seed": 1}, "version": version}

    def factory() -> OpaPolicyInstance:
        return cast(OpaPolicyInstance, FakeInstance(state["data"], state["version"]))

    def snapshot() -> tuple[object, int]:
        return state["data"], state["version"]

    def refresh(inst: OpaPolicyInstance, data: object, ver: int) -> None:
        cast(FakeInstance, inst).reload_data(data, ver)

    pool = OpaInstancePool(
        max_size=max_size,
        factory=factory,
        snapshot_fn=snapshot,
        refresh_fn=refresh,
        borrow_timeout_seconds=timeout,
    )
    return pool, state


class TestBorrow:
    def test_borrow_yields_instance(self) -> None:
        pool, _ = make_pool()
        with pool.borrow() as loan:
            assert isinstance(loan, PolicyLoan)
            assert loan.instance is not None

    def test_lazy_creation_counts(self) -> None:
        pool, _ = make_pool(max_size=2)
        assert pool.instances_created == 0
        with pool.borrow():
            pass
        assert pool.instances_created == 1

    def test_instance_reused_across_borrows(self) -> None:
        pool, _ = make_pool(max_size=1)
        with pool.borrow() as a:
            first = a.instance
        with pool.borrow() as b:
            assert b.instance is first

    def test_never_exceeds_max_size(self) -> None:
        pool, _ = make_pool(max_size=3)
        held = []
        cms = [pool.borrow() for _ in range(3)]
        for cm in cms:
            held.append(cm.__enter__())
        ids = {loan.instance.id for loan in held}  # type: ignore[attr-defined]
        assert len(ids) == 3
        for cm in cms:
            cm.__exit__(None, None, None)
        assert pool.instances_created == 3


class TestTimeout:
    def test_timeout_when_exhausted(self) -> None:
        pool, _ = make_pool(max_size=1, timeout=0.15)
        cm = pool.borrow()
        cm.__enter__()
        try:
            with pytest.raises(OpaPoolTimeoutError), pool.borrow():
                pass
        finally:
            cm.__exit__(None, None, None)

    def test_none_timeout_blocks_until_available(self) -> None:
        pool, _ = make_pool(max_size=1, timeout=None)
        cm = pool.borrow()
        cm.__enter__()
        released = threading.Event()

        def releaser() -> None:
            time.sleep(0.1)
            cm.__exit__(None, None, None)
            released.set()

        threading.Thread(target=releaser).start()
        with pool.borrow():  # blocks until releaser returns the instance
            pass
        assert released.is_set()


class TestLazyRefresh:
    def test_stale_instance_refreshed_on_borrow(self) -> None:
        pool, state = make_pool(max_size=1, version=0)
        with pool.borrow() as loan:
            assert loan.instance.data_version == 0
        # bump policy version; next borrow must refresh the reused instance
        state["data"] = {"seed": 2}
        state["version"] = 1
        with pool.borrow() as loan:
            assert loan.instance.data_version == 1
            assert cast(FakeInstance, loan.instance).data == {"seed": 2}

    def test_up_to_date_instance_not_refreshed(self) -> None:
        pool, _ = make_pool(max_size=1, version=5)
        with pool.borrow() as loan:
            reloads_after_create = list(loan.instance.reloads)  # type: ignore[attr-defined]
        with pool.borrow() as loan:
            # version unchanged -> no extra reload beyond creation
            assert loan.instance.reloads == reloads_after_create  # type: ignore[attr-defined]


class TestDiscardReplace:
    def test_discarded_instance_replaced(self) -> None:
        pool, _ = make_pool(max_size=1)
        with pool.borrow() as loan:
            first = loan.instance
            loan.discard()
        assert pool.instances_recreated == 1
        with pool.borrow() as loan:
            assert loan.instance is not first  # rebuilt

    def test_healthy_instance_not_recreated(self) -> None:
        pool, _ = make_pool(max_size=1)
        with pool.borrow():
            pass
        assert pool.instances_recreated == 0


class TestClose:
    def test_close_blocks_borrow(self) -> None:
        pool, _ = make_pool()
        pool.close()
        with pytest.raises(OpaPolicyClosedError), pool.borrow():
            pass

    def test_close_is_idempotent(self) -> None:
        pool, _ = make_pool()
        pool.close()
        pool.close()
        assert pool.closed is True

    def test_close_frees_idle_instances(self) -> None:
        # close() must close every real instance sitting idle in the pool so its
        # native Store is freed promptly rather than at GC time.
        pool, _ = make_pool(max_size=2)
        with pool.borrow() as a:
            first = cast(FakeInstance, a.instance)
        assert first.close_calls == 0
        pool.close()
        assert first.close_calls == 1

    def test_close_during_borrow_does_not_leak_drawn_instance(self) -> None:
        # Regression: a borrower can draw a real idle instance a hair before
        # close() flips _closed and drains the queue — so close()'s drain never
        # sees that instance. _take_slot must then close the instance it is
        # dropping; otherwise its native Store leaks until GC.
        #
        # We reproduce the exact interleaving deterministically at the unit under
        # test, _take_slot(): seed one live instance in the idle queue, mark the
        # pool closed (as close() would after the drain already missed this
        # instance), then draw it exactly as borrow() does. _take_slot must fail
        # closed AND close the instance it drops.
        pool, _ = make_pool(max_size=1)
        with pool.borrow() as loan:
            live = cast(FakeInstance, loan.instance)
        # `live` is now idle in the queue. Flip the flag directly to model close()
        # having set it after this instance was already about to be drawn.
        pool._closed_event.set()
        assert live.close_calls == 0
        with pytest.raises(OpaPolicyClosedError):
            pool._take_slot()
        assert live.close_calls == 1, "drawn-then-dropped instance was leaked (never closed)"


class TestObservability:
    def test_available_reports_idle_slots(self) -> None:
        pool, _ = make_pool(max_size=2)
        assert pool.available == 2
        with pool.borrow():
            assert pool.available == 1
        assert pool.available == 2


class TestFactoryFailure:
    def test_factory_error_restores_slot(self) -> None:
        # A factory that raises must not leak the slot: the placeholder is put
        # back, so a later (working) borrow can still proceed.
        calls = {"n": 0}

        def flaky_factory() -> OpaPolicyInstance:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("cannot build instance")
            return cast(OpaPolicyInstance, FakeInstance())

        pool = OpaInstancePool(
            max_size=1,
            factory=flaky_factory,
            snapshot_fn=lambda: (None, 0),
            refresh_fn=lambda i, d, v: None,
            borrow_timeout_seconds=1.0,
        )
        with pytest.raises(RuntimeError, match="cannot build"), pool.borrow():
            pass
        # Slot restored -> second borrow builds successfully.
        with pool.borrow() as loan:
            assert loan.instance is not None


class TestConstruction:
    def test_rejects_zero_size(self) -> None:
        with pytest.raises(ValueError, match="max_size"):
            OpaInstancePool(
                max_size=0,
                factory=lambda: cast(OpaPolicyInstance, FakeInstance()),
                snapshot_fn=lambda: (None, 0),
                refresh_fn=lambda i, d, v: None,
                borrow_timeout_seconds=1.0,
            )


class _RealRuntimeInstance:
    """Minimal OpaPolicyInstance-shaped wrapper around a real WasmtimeRuntime.

    Unlike FakeInstance, this holds a genuinely-instantiated wasmtime Store
    with real host-callback Funcs — needed to exercise the wasmtime-level
    race in TestConcurrentLifecycleSafety below, which a pure Python fake
    cannot reproduce (there is no native allocation for it to race on).
    """

    def __init__(self, runtime: WasmtimeRuntime) -> None:
        self._runtime = runtime
        self.poisoned = False
        self.data_version = 0

    def close(self) -> None:
        self._runtime.close()

    def reload_data(self, data: object, version: int) -> None:
        self.data_version = version


class TestConcurrentLifecycleSafety:
    # Regression: wasmtime.py keeps one process-wide free-list of native
    # host-callback registrations (env.opa_abort, opa_builtinN, ...).
    # Allocating a new Store's callbacks concurrently with *finalising*
    # another Store's callbacks (Store.close() runs the native finalizer
    # synchronously) corrupts that free-list — a bug in wasmtime.py itself,
    # surfacing as a bare TypeError deep inside it rather than any typed
    # opapywasm error. Because the free-list is process-wide, the race spans
    # every pool in the process, not just one — runtime._NATIVE_LIFECYCLE_LOCK
    # (module-level, not per-pool) must serialize all of it. A module that
    # needs the runtime's initial-memory retry (see runtime.py) widens the
    # instantiation window enough to make the race reliably observable
    # without the lock — this is not specific to that fixture, just a
    # convenient way to trigger it deterministically in a fast unit test.

    @staticmethod
    def _make_pool_over(runtime_factory: WasmtimeRuntimeFactory) -> OpaInstancePool:
        return OpaInstancePool(
            max_size=8,
            factory=lambda: cast(OpaPolicyInstance, _RealRuntimeInstance(runtime_factory.create_runtime())),
            snapshot_fn=lambda: (None, 0),
            refresh_fn=lambda i, d, v: cast(_RealRuntimeInstance, i).reload_data(d, v),
            borrow_timeout_seconds=10.0,
        )

    @staticmethod
    def _worker(pool: OpaInstancePool, errors: list[str]) -> None:
        try:
            with pool.borrow():
                pass
        except OpaPoolTimeoutError:
            # Benign contention (e.g. a loaded CI runner), not the corruption
            # this test guards against — must not be conflated with it.
            pass
        except Exception as exc:
            errors.append(repr(exc))

    def test_concurrent_create_and_close_do_not_corrupt_wasmtime_state(self) -> None:
        # Two pools, not one: the free-list is process-wide, so the bug this
        # guards against is specifically pool A racing pool B, not just
        # concurrent threads within a single pool.
        wasm = large_data_segment_wasm()
        errors: list[str] = []

        def worker(pool: OpaInstancePool) -> None:
            self._worker(pool, errors)

        for _ in range(15):
            factory_a = WasmtimeRuntimeFactory(wasm)
            factory_b = WasmtimeRuntimeFactory(wasm)
            pool_a = self._make_pool_over(factory_a)
            pool_b = self._make_pool_over(factory_b)
            threads = [threading.Thread(target=worker, args=(pool,)) for pool in [pool_a, pool_b] * 8]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            pool_a.close()
            pool_b.close()
            factory_a.close()
            factory_b.close()

        assert errors == []

    def test_negative_control_fails_without_the_lock(self) -> None:
        # Proves the test above is a real guard, not a race that happens to
        # never lose: with the lock replaced by a no-op, the same workload
        # must reliably surface the free-list corruption. Run in a subprocess
        # (not monkeypatch in-process) because the corruption this induces is
        # in wasmtime's real, process-wide native state — it would otherwise
        # outlive this test and poison every test that runs after it.
        script = f"""
import contextlib, sys, threading
sys.path.insert(0, {str(Path(__file__).parent.parent.parent)!r})
import opapywasm.runtime as runtime
runtime._NATIVE_LIFECYCLE_LOCK = contextlib.nullcontext()

from opapywasm.instance import OpaPolicyInstance
from opapywasm.pool import OpaInstancePool
from opapywasm.runtime import WasmtimeRuntimeFactory
sys.path.insert(0, {str(Path(__file__).parent)!r})
from wat_helpers import large_data_segment_wasm

class _RealRuntimeInstance:
    def __init__(self, rt):
        self._runtime = rt
        self.poisoned = False
        self.data_version = 0
    def close(self):
        self._runtime.close()
    def reload_data(self, data, version):
        self.data_version = version

def make_pool(factory):
    return OpaInstancePool(
        max_size=8,
        factory=lambda: _RealRuntimeInstance(factory.create_runtime()),
        snapshot_fn=lambda: (None, 0),
        refresh_fn=lambda i, d, v: i.reload_data(d, v),
        borrow_timeout_seconds=10.0,
    )

wasm = large_data_segment_wasm()
errors = []

def worker(pool):
    try:
        with pool.borrow():
            pass
    except Exception as exc:
        errors.append(repr(exc))

factory_a = WasmtimeRuntimeFactory(wasm)
factory_b = WasmtimeRuntimeFactory(wasm)
pool_a = make_pool(factory_a)
pool_b = make_pool(factory_b)
threads = [threading.Thread(target=worker, args=(p,)) for p in [pool_a, pool_b] * 8]
for t in threads:
    t.start()
for t in threads:
    t.join()

assert errors, "expected free-list corruption without the lock"
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        assert (
            result.returncode == 0
        ), f"negative control did not reproduce corruption:\n{result.stdout}\n{result.stderr}"
