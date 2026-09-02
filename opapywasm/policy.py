"""Public thread-safe policy facade (milestones C4-C8).

``OpaWasmPolicy`` is the main entry point. It compiles the module once, owns a
shared :class:`~opapywasm.runtime.WasmtimeRuntimeFactory` and a bounded
:class:`~opapywasm.pool.OpaInstancePool`, and exposes the Pythonic API:
``from_wasm_file`` / ``from_wasm_bytes`` / ``from_wasm_stream``, ``set_data``,
``evaluate`` / ``evaluate_raw``, entrypoint enumeration and debug metadata.

Thread-safety (C5-C6): application threads call ``evaluate`` concurrently; the
pool hands out at most ``pool_size`` instances, each used by only one thread at
a time. ``set_data`` is O(1) - it swaps the policy-level ``(data, version)``
snapshot under a short lock; each pooled instance lazily reloads its data on the
next borrow if its version is behind. No two threads ever share an instance,
Store, Memory, or data address.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import BinaryIO

from .abi import OpaAbiSpec
from .builtins import BuiltinRegistry, default_builtins
from .errors import OpaEntrypointError, OpaEvaluationError, OpaInvalidInputError, OpaPolicyClosedError
from .instance import OpaPolicyInstance
from .pool import OpaInstancePool
from .runtime import WasmtimeRuntimeFactory
from .types import UNDEFINED, BuiltinCallable, JSONInput, JSONValue, PolicyConfig, Undefined

__all__ = ["OpaWasmPolicy"]

# Compact JSON separators — match what the codec writes into Wasm memory.
_COMPACT_SEPARATORS = (",", ":")


class OpaWasmPolicy:
    """Thread-safe public facade for evaluating a compiled OPA policy.

    Construct with one of the ``from_wasm_*`` class methods rather than calling
    ``__init__`` directly.
    """

    def __init__(
        self,
        wasm_bytes: bytes,
        config: PolicyConfig | None = None,
    ) -> None:
        self._config = config or PolicyConfig()
        self._factory = WasmtimeRuntimeFactory(
            wasm_bytes,
            max_memory_pages=self._config.max_memory_pages,
        )
        self._abi_spec = OpaAbiSpec()
        # Shared builtin registry (seeded with the SDK defaults) — every pooled
        # instance dispatches against the same registry's snapshots.
        self._registry = BuiltinRegistry(default_builtins())

        # Policy-level data snapshot guarded by a short lock. set_data swaps this;
        # instances lazily catch up on borrow via the version check.
        #
        # The snapshot is stored as immutable serialised JSON *bytes*, not the
        # caller's live object: a caller may keep mutating the dict/list it
        # passed to set_data, and instances reload lazily on later borrows, so
        # holding the object by reference would race. Serialising once at
        # set_data time makes every reload deterministic and validates/size-checks
        # the document a single time.
        self._data_lock = threading.Lock()
        self._data: bytes | None = None
        self._data_version = 0

        # Lifecycle: `close()` marks the policy closed and closes the pool, but
        # the shared epoch ticker must keep running until every in-flight
        # evaluation finishes (a runaway eval's deadline depends on it). We track
        # active evaluations and stop the ticker when the last one drains after
        # close, or immediately in close() if none are active.
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._active_evals = 0

        self._pool = OpaInstancePool(
            max_size=self._config.pool_size,
            factory=self._build_instance,
            snapshot_fn=self._data_snapshot,
            refresh_fn=self._refresh_instance,
            borrow_timeout_seconds=self._config.borrow_timeout_seconds,
        )

        # Policy-level metadata is a property of the compiled module, so it is
        # read once at construction from a probe instance rather than per
        # evaluation. The probe also validates the policy end-to-end (ABI,
        # exports, data load) before the pool serves any request. If anything
        # after factory creation fails, tear down the factory (and its ticker
        # thread) and pool so a failed construction leaks nothing.
        try:
            probe = self._build_instance()
            try:
                self._entrypoints = probe.entrypoints()
                self._builtins = probe.builtins()
                self._abi_version = probe.abi.abi_version
            finally:
                probe.close()  # don't rely on GC to free the probe's Store
        except BaseException:
            self._pool.close()
            self._factory.close()
            raise

    # -- constructors --------------------------------------------------------

    @classmethod
    def from_wasm_bytes(cls, wasm_bytes: bytes, config: PolicyConfig | None = None) -> OpaWasmPolicy:
        """Build a policy from in-memory Wasm bytes."""
        return cls(wasm_bytes, config)

    @classmethod
    def from_wasm_file(cls, path: str | Path, config: PolicyConfig | None = None) -> OpaWasmPolicy:
        """Build a policy from a compiled ``.wasm`` file path."""
        return cls(Path(path).read_bytes(), config)

    @classmethod
    def from_wasm_stream(cls, stream: BinaryIO, config: PolicyConfig | None = None) -> OpaWasmPolicy:
        """Build a policy from a readable binary stream."""
        return cls(stream.read(), config)

    # -- metadata ------------------------------------------------------------

    @property
    def entrypoints(self) -> dict[str, int]:
        """Mapping of ``{entrypoint_name: id}`` exposed by this policy."""
        return dict(self._entrypoints)

    @property
    def builtins(self) -> dict[str, int]:
        """Mapping of ``{builtin_name: id}`` this policy requires from the host."""
        return dict(self._builtins)

    @property
    def abi_version(self) -> tuple[int, int]:
        """Detected ``(major, minor)`` OPA Wasm ABI version."""
        return self._abi_version

    @property
    def data_version(self) -> int:
        """Monotonic counter bumped on each :meth:`set_data`."""
        with self._data_lock:
            return self._data_version

    @property
    def pool_size(self) -> int:
        return self._pool.max_size

    @property
    def instances_created(self) -> int:
        return self._pool.instances_created

    @property
    def instances_recreated(self) -> int:
        return self._pool.instances_recreated

    # -- data ----------------------------------------------------------------

    def set_data(self, data: JSONInput | None) -> None:
        """Replace the external ``data`` document (thread-safe).

        The document is serialised to immutable JSON bytes *now* — validated and
        size-checked once — then the policy-level snapshot is swapped and the
        version bumped under a short lock. Because the snapshot is a byte string,
        a caller may safely keep mutating the object it passed in afterwards; the
        policy holds no reference to it.

        In-flight evaluations finish against their current data; every subsequent
        borrow lazily reloads the instance if its version is behind, so once
        ``set_data`` returns, no *new* evaluation can see stale data.

        Raises:
            OpaInvalidInputError: if ``data`` is not JSON-serialisable (or, for a
                ``str``/``bytes`` document, not valid JSON) or exceeds
                ``max_data_bytes``.
            OpaPolicyClosedError: if the policy has been closed.
        """
        self._raise_if_closed()
        serialised = self._serialise_data(data)
        with self._data_lock:
            self._data = serialised
            self._data_version += 1

    def _raise_if_closed(self) -> None:
        with self._lifecycle_lock:
            closed = self._closed
        if closed:
            raise OpaPolicyClosedError("policy is closed")

    def _serialise_data(self, data: JSONInput | None) -> bytes | None:
        """Normalise a data document to size-checked, immutable JSON bytes."""
        if data is None:
            return None
        if isinstance(data, (str, bytes)):
            raw = data.encode("utf-8") if isinstance(data, str) else data
            try:
                json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise OpaInvalidInputError(f"data str/bytes is not valid JSON text: {exc}") from exc
        else:
            try:
                raw = json.dumps(data, separators=_COMPACT_SEPARATORS, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise OpaInvalidInputError(f"data is not JSON-serialisable: {exc}") from exc
        if len(raw) > self._config.max_data_bytes:
            raise OpaInvalidInputError(
                f"data is {len(raw)} bytes, exceeding the {self._config.max_data_bytes}-byte limit"
            )
        return raw

    def _data_snapshot(self) -> tuple[bytes | None, int]:
        with self._data_lock:
            return self._data, self._data_version

    # -- builtins ------------------------------------------------------------

    def register_builtin(self, name: str, fn: BuiltinCallable, *, replace: bool = True) -> None:
        """Register a custom host builtin.

        The callable receives already-decoded Python arguments and must return a
        JSON-compatible Python value. Registration is thread-safe and takes
        effect for subsequent evaluations (the registry publishes an immutable
        snapshot; the registry lock is never held while a builtin runs).

        The callable may run concurrently on multiple threads, so it must be
        thread-safe (see ``docs/thread_safety.md``).

        Raises:
            OpaPolicyClosedError: if the policy has been closed.
        """
        self._raise_if_closed()
        self._registry.register(name, fn, replace=replace)

    @property
    def registered_builtins(self) -> list[str]:
        """Names of all currently registered host builtins (defaults + custom)."""
        return self._registry.names()

    def _refresh_instance(self, instance: OpaPolicyInstance, data: object, version: int) -> None:
        # `data` is the serialised-bytes snapshot (or None); the codec treats
        # bytes as an already-serialised JSON document.
        instance.reload_data(data, version)  # type: ignore[arg-type]

    def _build_instance(self) -> OpaPolicyInstance:
        data, version = self._data_snapshot()
        return OpaPolicyInstance(
            self._factory,
            self._config,
            self._abi_spec,
            initial_data=data,
            data_version=version,
            builtins=self._registry,
            epoch=self._factory.epoch,
        )

    # -- evaluation ----------------------------------------------------------

    def evaluate_raw(self, input_value: JSONInput, entrypoint: str | int | None = None) -> JSONValue:
        """Evaluate and return the **raw** OPA result set.

        The result is typically ``[{"result": ...}]`` or ``[]`` for an undefined
        decision.

        Instance disposal is automatic: any failure *after* guest execution
        begins marks the instance :attr:`~opapywasm.instance.OpaPolicyInstance.poisoned`,
        and the pool discards it on release (rebuilding a clean replacement). A
        pre-execution failure (e.g. an oversize-input guard) leaves the instance
        clean, so it is returned to the pool unharmed.
        """
        entrypoint_id = self._resolve_entrypoint(entrypoint)
        # Register this evaluation as active so close() keeps the epoch ticker
        # alive while it runs (its timeout deadline depends on the ticker).
        self._enter_eval()
        try:
            with self._pool.borrow() as loan:
                return loan.instance.evaluate(input_value, entrypoint_id)
        finally:
            self._exit_eval()

    def evaluate(self, input_value: JSONInput, entrypoint: str | int | None = None) -> JSONValue | Undefined:
        """Evaluate and return the simplified decision.

        Returns ``result_set[0]["result"]`` when the decision is defined. This
        value may itself be JSON ``null`` (Python ``None``).

        An **undefined** decision (empty result set) returns the
        :data:`~opapywasm.types.UNDEFINED` sentinel — *not* ``None`` — so a
        genuinely-``null`` result and an undefined result are distinguishable.
        The sentinel is falsy, so ``if policy.evaluate(...):`` still behaves as
        expected. Under ``strict_result`` an undefined decision raises
        :class:`~opapywasm.errors.OpaEvaluationError` instead.

        Use :meth:`evaluate_raw` for the full OPA result set.
        """
        raw = self.evaluate_raw(input_value, entrypoint)
        return self._simplify_result(raw)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the policy; subsequent evaluations raise ``OpaPolicyClosedError``.

        Drains the instance pool and wakes any blocked ``evaluate`` calls with
        :class:`~opapywasm.errors.OpaPolicyClosedError`. The shared epoch-ticker
        thread is stopped only once no evaluation is still in flight — an
        in-flight (possibly runaway) evaluation keeps the ticker alive so its
        timeout can still fire; the last such evaluation to finish stops it. If
        no evaluation is active, the ticker is stopped here. ``close()`` itself
        does not block waiting for active evaluations. Idempotent.
        """
        self._pool.close()
        with self._lifecycle_lock:
            self._closed = True
            stop_ticker = self._active_evals == 0
        if stop_ticker:
            self._factory.close()

    def _enter_eval(self) -> None:
        """Mark an evaluation as in flight (blocks ticker shutdown)."""
        with self._lifecycle_lock:
            self._active_evals += 1

    def _exit_eval(self) -> None:
        """Mark an evaluation as finished; stop the ticker if closed and idle."""
        with self._lifecycle_lock:
            self._active_evals -= 1
            stop_ticker = self._closed and self._active_evals == 0
        if stop_ticker:
            self._factory.close()

    def __enter__(self) -> OpaWasmPolicy:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals -----------------------------------------------------------

    def _resolve_entrypoint(self, entrypoint: str | int | None) -> int:
        """Resolve a name/id/None into a numeric entrypoint id.

        ``None`` uses the configured ``default_entrypoint``, falling back to id
        ``0``. The resolved id/name is always validated against the policy's
        actual entrypoints, so a missing id ``0`` raises a clear
        :class:`OpaEntrypointError` rather than trapping inside ``opa_eval``.
        """
        if entrypoint is None:
            entrypoint = self._config.default_entrypoint
        if entrypoint is None:
            entrypoint = 0
        if isinstance(entrypoint, bool):  # bool is an int subclass; reject explicitly
            raise OpaEntrypointError(f"invalid entrypoint {entrypoint!r}")
        if isinstance(entrypoint, int):
            if entrypoint not in self._entrypoints.values():
                raise OpaEntrypointError(
                    f"unknown entrypoint id {entrypoint}; known ids: {sorted(self._entrypoints.values())}"
                )
            return entrypoint
        try:
            return self._entrypoints[entrypoint]
        except KeyError:
            raise OpaEntrypointError(
                f"unknown entrypoint {entrypoint!r}; known entrypoints: {sorted(self._entrypoints)}"
            ) from None

    def _simplify_result(self, raw: JSONValue) -> JSONValue | Undefined:
        # Undefined decision: the OPA result set is an empty list.
        if isinstance(raw, list) and not raw:
            if self._config.strict_result:
                raise OpaEvaluationError("policy returned an undefined/empty decision")
            return UNDEFINED
        # Defined decision: expected shape is [{"result": <value>}, ...].
        if isinstance(raw, list) and isinstance(raw[0], dict) and "result" in raw[0]:
            return raw[0]["result"]
        # Anything else is a shape we do not recognise. Silently collapsing it to
        # "undefined" would hide ABI corruption or an unexpected result shape, so
        # by default we raise; strict_result_shape=False restores the lenient
        # (treat-as-undefined) behaviour.
        if self._config.strict_result_shape:
            raise OpaEvaluationError(f"policy returned an unexpected result shape (not [{{'result': ...}}]): {raw!r}")
        if self._config.strict_result:
            raise OpaEvaluationError(f"policy returned an undefined/empty decision: {raw!r}")
        return UNDEFINED
