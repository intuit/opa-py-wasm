"""Thread-safe bounded instance pool (milestones C5-C6).

``OpaInstancePool`` is a bounded pool of
:class:`~opapywasm.instance.OpaPolicyInstance` objects. ``borrow()`` is a
context manager that hands out exactly one instance with a timeout, guarantees
it is never handed to another thread concurrently, and returns it (or discards
and replaces a poisoned one) on exit. It also supports lazy per-instance data
refresh (C6) and a ``close()`` that prevents further borrowing.

Concurrency model:

* A bounded ``queue.Queue`` holds *idle* instances. Instances are created lazily
  up to ``max_size`` — the queue is seeded with ``None`` placeholders, and the
  first borrow to draw a placeholder builds a real instance.
* ``borrow()`` removes an instance from the queue for the duration of the loan,
  so no two threads ever hold the same instance. It is returned on exit.
* Data updates are *lazy*: the pool holds a factory callback that reports the
  current ``(data, data_version)``. On borrow, an instance whose ``data_version``
  is behind is reloaded before being yielded. This keeps ``set_data`` O(1) and
  never blocks on in-flight evaluations (see ``OpaWasmPolicy.set_data``).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from .errors import OpaPolicyClosedError, OpaPoolTimeoutError

if TYPE_CHECKING:
    from .instance import OpaPolicyInstance

__all__ = ["OpaInstancePool", "PolicyLoan"]


class _CloseSentinel:
    """Marker pushed onto the idle queue by close() to wake blocked borrowers.

    A dedicated type (rather than a bare ``object()``) keeps the idle-queue
    element type expressible for the type checker and makes ``is`` checks
    self-documenting. Distinct from a ``None`` lazy-placeholder so a woken waiter
    knows the pool is shutting down rather than that it drew an uninstantiated
    slot.
    """


_CLOSE_SENTINEL = _CloseSentinel()


# Factory that builds a fresh instance carrying the current data + version.
InstanceFactory = Callable[[], "OpaPolicyInstance"]
# Reports the policy's current (data, data_version) for lazy refresh.
DataSnapshotFn = Callable[[], tuple[object, int]]
# Reloads an instance to a (data, data_version). Returns nothing.
RefreshFn = Callable[["OpaPolicyInstance", object, int], None]


class PolicyLoan:
    """An outstanding borrow of one instance from the pool.

    Yielded by :meth:`OpaInstancePool.borrow`. Callers use ``loan.instance`` and
    may call ``loan.discard()`` to signal that the instance is poisoned and must
    be replaced rather than returned to the pool.
    """

    __slots__ = ("_discarded", "instance")

    def __init__(self, instance: OpaPolicyInstance) -> None:
        self.instance = instance
        self._discarded = False

    def discard(self) -> None:
        """Mark this instance as poisoned; the pool will replace it on release."""
        self._discarded = True

    @property
    def discarded(self) -> bool:
        return self._discarded


class OpaInstancePool:
    """Bounded, thread-safe pool of single-use policy instances.

    Args:
        max_size: maximum number of live instances (== max concurrent evals).
        factory: builds a fresh instance seeded with the current data/version.
        snapshot_fn: returns the policy's current ``(data, data_version)``.
        refresh_fn: reloads a stale instance to a given ``(data, data_version)``.
        borrow_timeout_seconds: max wait for a free instance; ``None`` blocks
            forever.
    """

    def __init__(
        self,
        *,
        max_size: int,
        factory: InstanceFactory,
        snapshot_fn: DataSnapshotFn,
        refresh_fn: RefreshFn,
        borrow_timeout_seconds: float | None,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        self._factory = factory
        self._snapshot_fn = snapshot_fn
        self._refresh_fn = refresh_fn
        self._timeout = borrow_timeout_seconds

        # Idle slots: real instances, None placeholders (lazily instantiated), or
        # the close sentinel.
        self._idle: queue.Queue[OpaPolicyInstance | _CloseSentinel | None] = queue.Queue(maxsize=max_size)
        for _ in range(max_size):
            self._idle.put(None)

        # Shutdown flag. A threading.Event (not a plain bool) so the reads on the
        # borrow/release hot paths are safely published across threads without
        # holding _close_lock — correct under CPython's GIL and on a
        # free-threaded (no-GIL, 3.13t+) interpreter alike. _close_lock still
        # serialises the close() transition + drain.
        self._closed_event = threading.Event()
        self._close_lock = threading.Lock()
        # Bookkeeping for observability.
        self._created = 0
        self._recreated = 0
        self._stats_lock = threading.Lock()

    # -- properties ----------------------------------------------------------

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def instances_created(self) -> int:
        with self._stats_lock:
            return self._created

    @property
    def instances_recreated(self) -> int:
        with self._stats_lock:
            return self._recreated

    @property
    def available(self) -> int:
        """Approximate number of idle slots (best-effort; for observability)."""
        return self._idle.qsize()

    # -- borrow --------------------------------------------------------------

    @contextmanager
    def borrow(self) -> Iterator[PolicyLoan]:
        """Borrow one instance exclusively for the duration of the ``with`` block.

        Raises:
            OpaPolicyClosedError: if the pool is closed.
            OpaPoolTimeoutError: if no instance becomes free within the timeout.
        """
        if self._closed_event.is_set():
            raise OpaPolicyClosedError("policy is closed")

        slot = self._take_slot()
        instance: OpaPolicyInstance | None = None
        loan: PolicyLoan | None = None
        try:
            instance = slot if slot is not None else self._create_instance()
            self._refresh_if_stale(instance)
            loan = PolicyLoan(instance)
            yield loan
        finally:
            self._release(loan, instance)

    def _take_slot(self) -> OpaPolicyInstance | None:
        try:
            if self._timeout is None:
                slot = self._idle.get(block=True)
            else:
                slot = self._idle.get(block=True, timeout=self._timeout)
        except queue.Empty:
            raise OpaPoolTimeoutError(
                f"timed out after {self._timeout}s waiting for a free policy instance " f"(pool_size={self._max_size})"
            ) from None
        # Fail closed on *any* slot drawn once the pool is closed — the explicit
        # sentinel, or a plain None/instance a concurrent release put back after
        # close() drained. This prevents building an instance on a dead pool.
        if isinstance(slot, _CloseSentinel) or self._closed_event.is_set():
            # If we drew a *real* instance, close() must have flipped _closed and
            # drained the queue after this instance was already removed — so its
            # drain never saw it. borrow() raises before it assigns `instance`,
            # so _release() will get None and cannot close it either. Free it here
            # or its native Store leaks until GC (defeating close()'s
            # free-resources-promptly guarantee).
            if slot is not None and not isinstance(slot, _CloseSentinel):
                self._close_instance(slot)
            # Re-post the sentinel to chain-wake any other blocked waiter.
            # Non-blocking: if the queue is already full, every waiter has a slot
            # to draw and will hit this same closed check, so the wake isn't lost.
            self._put_wake_sentinel()
            raise OpaPolicyClosedError("policy is closed")
        return slot

    def _put_wake_sentinel(self) -> None:
        """Post the close sentinel without ever blocking (drop if the queue is full)."""
        try:
            self._idle.put_nowait(_CLOSE_SENTINEL)
        except queue.Full:
            pass

    def _release(self, loan: PolicyLoan | None, instance: OpaPolicyInstance | None) -> None:
        """Return the instance to the pool, or replace a poisoned/failed one.

        Restores the slot so the pool preserves its size: a healthy instance is
        returned as-is; a discarded, self-poisoned, or (once closed) any instance
        is replaced with a ``None`` placeholder so the live object is dropped
        promptly and rebuilt lazily only if the pool is reused.

        An instance is replaced when:

        * the caller explicitly called :meth:`PolicyLoan.discard`, or
        * the instance marked *itself* poisoned (a failure after entering Wasm —
          see :attr:`OpaPolicyInstance.poisoned`), or
        * the pool has been closed (drop it rather than resurrect a dead pool).

        Puts are non-blocking: exactly one slot was removed for this loan, so
        there is normally room, but ``close()`` may have drained the queue
        concurrently — a resulting full queue just means the slot is already
        accounted for, so dropping the put is correct (never deadlock).
        """
        if instance is None:
            # Instance construction failed; put the placeholder back.
            self._put_slot(None)
            return
        closed = self._closed_event.is_set()
        discard = closed or (loan is not None and loan.discarded) or instance.poisoned
        if discard:
            if not closed:
                with self._stats_lock:
                    self._recreated += 1
            self._put_slot(None)  # rebuilt lazily next time (never, if closed)
            self._close_instance(instance)  # free native resources promptly
            return
        self._put_slot(instance)

    def _put_slot(self, slot: OpaPolicyInstance | None) -> None:
        """Return a slot to the idle queue without ever blocking."""
        try:
            self._idle.put_nowait(slot)
        except queue.Full:
            pass

    def _create_instance(self) -> OpaPolicyInstance:
        # self._factory() constructs a WasmtimeRuntime, which serializes
        # native allocation via runtime._NATIVE_LIFECYCLE_LOCK — not here,
        # since that lock must be process-wide, not per-pool.
        instance = self._factory()
        with self._stats_lock:
            self._created += 1
        return instance

    def _close_instance(self, instance: OpaPolicyInstance) -> None:
        """Best-effort close of a discarded instance; never raises.

        Native wasmtime teardown happens inside instance.close() (via
        WasmtimeRuntime.close()), which itself holds
        runtime._NATIVE_LIFECYCLE_LOCK.
        """
        try:
            instance.close()
        except Exception:  # pragma: no cover - defensive; close is already lenient
            pass

    def _refresh_if_stale(self, instance: OpaPolicyInstance) -> None:
        data, version = self._snapshot_fn()
        if instance.data_version != version:
            self._refresh_fn(instance, data, version)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the pool; subsequent borrows raise :class:`OpaPolicyClosedError`.

        Drains and drops idle instances, then posts a wake sentinel so any thread
        currently blocked in :meth:`borrow` waiting for a free slot is released
        with :class:`OpaPolicyClosedError` instead of hanging until timeout.

        Instances currently on loan are *not* waited for: their ``with`` blocks
        release normally and :meth:`_release` drops them because the pool is
        closed. ``close()`` does not block on active loans.
        """
        with self._close_lock:
            if self._closed_event.is_set():
                return
            self._closed_event.set()
            # Drain idle instances and close them so native Store/Memory/Instance
            # resources are freed promptly rather than at GC time.
            while True:
                try:
                    slot = self._idle.get_nowait()
                except queue.Empty:
                    break
                if slot is not None and not isinstance(slot, _CloseSentinel):
                    self._close_instance(slot)
            # Wake a blocked waiter; it re-posts the sentinel to chain-wake the
            # rest. Non-blocking: concurrently-releasing loans may have already
            # refilled the queue, in which case those waiters each draw a slot
            # and hit the `_closed` check in _take_slot, so no wake is needed.
            self._put_wake_sentinel()

    @property
    def closed(self) -> bool:
        return self._closed_event.is_set()
