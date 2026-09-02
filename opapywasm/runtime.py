"""Wasmtime runtime foundation (milestone C1).

Owns the shared :class:`wasmtime.Engine` and compiled :class:`wasmtime.Module`,
and the per-instance factory that produces ``Store`` / ``Memory`` / ``Instance``
objects. Nothing here understands the OPA ABI — it only wires up the host
imports (``env.memory``, ``env.opa_abort``, ``env.opa_println``,
``env.opa_builtin0..4``) and instantiates modules.

Thread-safety: ``WasmtimeRuntimeFactory`` (and the ``Engine`` / ``Module`` it
holds) is safe to share across threads. A ``WasmtimeRuntime`` it produces —
which bundles a ``Store``, ``Memory``, ``Instance`` and bound exports — is
**not** and must be used by only one thread at a time.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import wasmtime

from .errors import OpaAbortError, OpaBuiltinError, OpaInvalidPolicyError

if TYPE_CHECKING:
    from wasmtime import Caller

__all__ = ["WasmtimeRuntime", "WasmtimeRuntimeFactory", "EpochTicker"]

logger = logging.getLogger(__name__)

# Wasm page size (bytes). Linear memory grows in units of this.
_WASM_PAGE_BYTES = 64 * 1024

# How often the shared epoch ticker bumps the engine epoch. Each tick is one
# "epoch unit"; a per-eval deadline in seconds is converted to a tick count.
_EPOCH_TICK_SECONDS = 0.01

# A deadline far enough in the future that it never fires in practice, used for
# operations that must not be time-limited (instantiation, metadata probes, and
# evaluations when eval_timeout_seconds is None). ~2^40 ticks ≈ 348 years at the
# tick cadence above.
_NO_DEADLINE_TICKS = 1 << 40

# OPA Wasm ABI host imports the module expects under the "env" namespace.
# These names and signatures are part of the OPA Wasm ABI contract; see
# docs/opa_abi.md. `env.memory` is an imported memory, the rest are functions.
_ENV_MODULE = "env"
_MEMORY_IMPORT = "memory"
# opa_builtinN takes (builtin_id, ctx, arg0..arg{N-1}) and returns an addr.
_BUILTIN_IMPORTS = {
    "opa_builtin0": 0,
    "opa_builtin1": 1,
    "opa_builtin2": 2,
    "opa_builtin3": 3,
    "opa_builtin4": 4,
}

# A host builtin dispatch callback. Receives (builtin_id, ctx, [arg_addrs]) and
# returns the result value address. Supplied per-instance by OpaPolicyInstance;
# when absent, the runtime installs a stub that raises OpaBuiltinError.
BuiltinDispatch = Callable[[int, int, list[int]], int]

# Initial page count for the host-provided memory. OPA modules import
# `env.memory` and the host owns it. Two pages (128 KiB) is a small starting
# size; OPA grows it via `memory.grow` as needed *at run time*. It is not
# enough for every module, though: a module's *active data segments* (e.g.
# embedded Rego data/string literals baked in at compile time) are copied into
# linear memory during instantiation itself, before the guest ever gets a
# chance to call `memory.grow` — an instantiation whose data segments overflow
# the initial size traps immediately. `_MAX_INSTANTIATION_MEMORY_PAGES` bounds
# how far instantiation retries grow the initial size before giving up.
_DEFAULT_MEMORY_PAGES = 2
_MAX_INSTANTIATION_MEMORY_PAGES = 16384  # 1 GiB ceiling for the instantiation-retry loop

# wasmtime.py's host-callback registrations (env.opa_abort, opa_builtinN, ...)
# live in one process-wide free-list with no internal locking. Allocating a
# new Store's callbacks concurrently with another Store's Store.close() (which
# finalizes them synchronously) corrupts it. Module-level, not per-pool, since
# every WasmtimeRuntime construction/close — from any pool, or a policy's
# metadata probe — must serialize against every other one, process-wide.
_NATIVE_LIFECYCLE_LOCK = threading.Lock()


def _is_memory_overflow_trap(exc: Exception) -> bool:
    """True only for the specific trap the memory-doubling retry exists for.

    Any other instantiation failure (bad ABI, missing import, a malformed or
    adversarial module) must fail immediately instead of retrying — more
    memory would not fix it, and retrying lets a bad module burn CPU/RSS
    across every doubling attempt for nothing.
    """
    return isinstance(exc, wasmtime.Trap) and exc.trap_code == wasmtime.TrapCode.MEMORY_OUT_OF_BOUNDS


class EpochTicker:
    """Shared background thread that advances a Wasmtime engine's epoch.

    Wasmtime epoch interruption works by (a) enabling ``epoch_interruption`` on
    the engine's :class:`wasmtime.Config`, (b) periodically calling
    :meth:`wasmtime.Engine.increment_epoch` from some thread, and (c) each store
    calling :meth:`wasmtime.Store.set_epoch_deadline` with a tick budget. When
    the engine's epoch passes a store's deadline, an in-progress guest call in
    that store traps.

    One ticker per policy drives *all* of that policy's instances, so N pooled
    instances cost a single daemon thread, not one per evaluation. The thread is
    lazily started on first use and stopped by :meth:`close`.

    :attr:`tick_seconds` is the real-time granularity of a single epoch unit;
    higher layers convert a per-eval second budget into a tick count.
    """

    tick_seconds = _EPOCH_TICK_SECONDS

    def __init__(self, engine: wasmtime.Engine) -> None:
        self._engine = engine
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def ensure_running(self) -> None:
        """Start the ticker thread if it is not already running (idempotent)."""
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="opapywasm-epoch-ticker",
                daemon=True,
            )
            self._thread.start()

    def deadline_ticks(self, seconds: float) -> int:
        """Convert a wall-clock ``seconds`` budget into an epoch tick count (>=1).

        Rounds *up* so the effective deadline is never shorter than requested
        (e.g. a 19 ms budget at a 10 ms tick becomes 2 ticks, not 1).
        """
        return max(1, math.ceil(seconds / self.tick_seconds))

    def _run(self) -> None:
        # `wait` returns True once stop is set; until then it times out every
        # tick and we advance the epoch. Using the event as the sleep avoids a
        # busy loop and lets close() wake us immediately.
        while not self._stop.wait(self.tick_seconds):
            self._engine.increment_epoch()

    def close(self) -> None:
        """Stop the ticker thread (idempotent)."""
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)


class WasmtimeRuntimeFactory:
    """Creates per-instance Wasmtime runtimes from a shared engine + module.

    Compile once, instantiate many: a single :class:`wasmtime.Engine` and one
    compiled :class:`wasmtime.Module` are created here and shared across every
    :class:`WasmtimeRuntime` this factory produces.
    """

    def __init__(
        self,
        wasm_bytes: bytes,
        *,
        max_memory_pages: int | None = None,
    ) -> None:
        """Compile ``wasm_bytes`` once into a shared engine + module.

        Args:
            wasm_bytes: the compiled OPA policy module.
            max_memory_pages: optional hard cap (in 64 KiB pages) on how large
                any instance's linear memory may grow. ``None`` = unbounded.

        Raises:
            OpaInvalidPolicyError: if the bytes are not a valid WebAssembly
                module, or do not look like an OPA policy.
        """
        if not wasm_bytes:
            raise OpaInvalidPolicyError("policy wasm is empty")
        self._max_memory_pages = max_memory_pages
        # Epoch interruption must be enabled on the Config *before* the Engine is
        # built; it is what makes per-eval deadlines (Store.set_epoch_deadline)
        # able to trap a runaway guest. The ticker thread drives it.
        config = wasmtime.Config()
        config.epoch_interruption = True
        self._engine = wasmtime.Engine(config)
        self._epoch = EpochTicker(self._engine)
        try:
            wasmtime.Module.validate(self._engine, wasm_bytes)
            self._module = wasmtime.Module(self._engine, wasm_bytes)
        except wasmtime.WasmtimeError as exc:
            raise OpaInvalidPolicyError(f"invalid policy wasm: {exc}") from exc
        self._validate_required_env_imports()

    @property
    def engine(self) -> wasmtime.Engine:
        return self._engine

    @property
    def module(self) -> wasmtime.Module:
        return self._module

    @property
    def epoch(self) -> EpochTicker:
        """The shared epoch ticker driving this policy's eval deadlines."""
        return self._epoch

    @property
    def max_memory_pages(self) -> int | None:
        return self._max_memory_pages

    def close(self) -> None:
        """Release factory-owned background resources (the epoch ticker)."""
        self._epoch.close()

    def _validate_required_env_imports(self) -> None:
        """Fail fast if the module does not look like an OPA policy.

        Confirms it imports ``env.memory`` as a memory, rather than discovering
        the problem only at instantiation time.
        """
        imports = {(imp.module, imp.name): imp for imp in self._module.imports}
        if (_ENV_MODULE, _MEMORY_IMPORT) not in imports:
            raise OpaInvalidPolicyError("policy wasm does not import 'env.memory'; not an OPA policy module")
        memory_type = imports[(_ENV_MODULE, _MEMORY_IMPORT)].type
        if not isinstance(memory_type, wasmtime.MemoryType):
            raise OpaInvalidPolicyError("'env.memory' import is not a memory")

    def create_runtime(self, builtin_dispatch: BuiltinDispatch | None = None) -> WasmtimeRuntime:
        """Instantiate a fresh, independent runtime.

        Each call produces a new ``Store``, host ``Memory``, ``Linker`` and
        ``Instance`` — nothing is shared with other runtimes except the engine
        and compiled module.

        Args:
            builtin_dispatch: optional callback invoked for ``opa_builtinN``.
                When ``None``, a stub that raises :class:`OpaBuiltinError` is
                installed (used for low-level runtime tests that never call a
                builtin). Real instances pass the per-instance dispatch.

        Raises:
            OpaInvalidPolicyError: if the module cannot be instantiated (e.g. an
                unsatisfied import).
        """
        return WasmtimeRuntime(
            self._engine,
            self._module,
            builtin_dispatch,
            max_memory_pages=self._max_memory_pages,
        )


class WasmtimeRuntime:
    """One instantiated policy module: Store + host Memory + Instance + exports.

    Not thread-safe. Construct via :meth:`WasmtimeRuntimeFactory.create_runtime`.
    """

    def __init__(
        self,
        engine: wasmtime.Engine,
        module: wasmtime.Module,
        builtin_dispatch: BuiltinDispatch | None,
        *,
        max_memory_pages: int | None = None,
    ) -> None:
        self._builtin_dispatch = builtin_dispatch
        self._max_memory_pages = max_memory_pages
        if max_memory_pages is not None and max_memory_pages < _DEFAULT_MEMORY_PAGES:
            raise OpaInvalidPolicyError(
                f"max_memory_pages={max_memory_pages} is below the minimum of "
                f"{_DEFAULT_MEMORY_PAGES} pages required to instantiate an OPA policy"
            )
        instantiation_ceiling = max_memory_pages if max_memory_pages is not None else _MAX_INSTANTIATION_MEMORY_PAGES
        # Everything after the Store exists can raise (the page-limit guard, memory
        # creation, import registration, instantiation). On any failure, close the
        # Store — and with it the Memory it owns — before re-raising so a runtime
        # whose constructor never completed does not leak its native resources to
        # GC. This matters most on the repeated-invalid-module path.
        initial_pages = _DEFAULT_MEMORY_PAGES
        last_exc: Exception | None = None
        try:
            # Covers the whole retry loop, including failed attempts' own
            # self._store.close() below — see _NATIVE_LIFECYCLE_LOCK above.
            with _NATIVE_LIFECYCLE_LOCK:
                while True:
                    self._store = wasmtime.Store(engine)
                    try:
                        # With epoch_interruption enabled on the engine, a store's default
                        # epoch deadline is "already expired" relative to the ever-advancing
                        # engine epoch, so even instantiation would trap as an interrupt. Arm
                        # a far-future deadline now; the evaluator tightens it per-eval and
                        # this value is restored implicitly on the next arming.
                        self._store.set_epoch_deadline(_NO_DEADLINE_TICKS)
                        # Host-provided memory that the module imports as `env.memory`. A
                        # `max` page count makes an over-allocating policy trap on
                        # `memory.grow` instead of exhausting host RAM. `set_limits` is
                        # belt-and-braces: it caps growth even for memories the guest might
                        # define itself.
                        self._memory = wasmtime.Memory(
                            self._store,
                            wasmtime.MemoryType(wasmtime.Limits(initial_pages, max_memory_pages)),
                        )
                        if max_memory_pages is not None:
                            self._store.set_limits(memory_size=max_memory_pages * _WASM_PAGE_BYTES)
                        linker = wasmtime.Linker(engine)
                        self._register_env_imports(linker, module)
                        self._instance = linker.instantiate(self._store, module)
                        self._exports = self._instance.exports(self._store)
                        break
                    except (wasmtime.WasmtimeError, wasmtime.Trap) as exc:
                        # self._store.close() here, and the outer except's self.close()
                        # below on the ceiling-fail path, both target this same Store —
                        # close() is documented idempotent, so the double-close is safe,
                        # not a bug.
                        self._store.close()
                        if not _is_memory_overflow_trap(exc):
                            raise OpaInvalidPolicyError(f"failed to instantiate policy: {exc}") from exc
                        last_exc = exc
                        if initial_pages >= instantiation_ceiling:
                            raise OpaInvalidPolicyError(f"failed to instantiate policy: {exc}") from exc
                        # See _DEFAULT_MEMORY_PAGES above for why this traps. Double the
                        # initial size and retry, up to `instantiation_ceiling`.
                        initial_pages = min(initial_pages * 2, instantiation_ceiling)
            if last_exc is not None:
                logger.warning("policy needed a larger initial memory (%d pages) to instantiate", initial_pages)
        except BaseException:
            self.close()
            raise

    # -- accessors -----------------------------------------------------------

    @property
    def store(self) -> wasmtime.Store:
        return self._store

    @property
    def memory(self) -> wasmtime.Memory:
        return self._memory

    @property
    def instance(self) -> wasmtime.Instance:
        return self._instance

    def set_epoch_deadline(self, ticks: int) -> None:
        """Arm this store's epoch deadline ``ticks`` epoch-units from now.

        A guest call that runs past the deadline traps with a Wasmtime
        interrupt. Re-armed before every evaluation by the caller. Requires the
        engine to have ``epoch_interruption`` enabled and the shared ticker to
        be running.
        """
        self._store.set_epoch_deadline(ticks)

    def clear_epoch_deadline(self) -> None:
        """Push this store's epoch deadline far into the future (effectively off).

        Called after a successful evaluation so an instance sitting idle in the
        pool carries no about-to-expire deadline into its next use.
        """
        self._store.set_epoch_deadline(_NO_DEADLINE_TICKS)

    def close(self) -> None:
        """Release this runtime's native ``Store`` promptly (idempotent).

        Frees the Store — and with it the Instance and host Memory — without
        waiting for garbage collection. Safe to call more than once; further use
        of the runtime after close is undefined and must not happen.

        Store.close() finalizes natively and synchronously, so it holds
        _NATIVE_LIFECYCLE_LOCK (see above) to avoid racing a concurrent
        construction.
        """
        with _NATIVE_LIFECYCLE_LOCK:
            try:
                self._store.close()
            except Exception:  # pragma: no cover - already closed / nothing to free
                pass

    def exports(self) -> dict[str, object]:
        """Return a copy of the instance's exports mapping (name -> export)."""
        return dict(self._exports)

    def has_export(self, name: str) -> bool:
        return self._exports.get(name) is not None

    def get_export(self, name: str) -> object:
        """Return the named export, or raise if absent.

        Raises:
            OpaInvalidPolicyError: if the export is missing. ABI-level export
                validation (C2) gives richer errors; this is the low-level guard.
        """
        item = self._exports.get(name)
        if item is None:
            raise OpaInvalidPolicyError(f"policy is missing required export {name!r}")
        return item

    def get_func(self, name: str) -> wasmtime.Func:
        """Return the named export as a :class:`wasmtime.Func`.

        Raises:
            OpaInvalidPolicyError: if the export is missing or is not a function.
        """
        item = self.get_export(name)
        if not isinstance(item, wasmtime.Func):
            raise OpaInvalidPolicyError(f"export {name!r} is not a function")
        return item

    # -- host imports --------------------------------------------------------

    def _register_env_imports(self, linker: wasmtime.Linker, module: wasmtime.Module) -> None:
        """Define ``env.memory`` and the OPA host functions on the linker.

        Only imports the module actually declares are defined, so a module that
        (for example) omits ``opa_println`` still instantiates.
        """
        declared = {(imp.module, imp.name) for imp in module.imports}

        linker.define(self._store, _ENV_MODULE, _MEMORY_IMPORT, self._memory)

        i32 = wasmtime.ValType.i32()

        def define_func(name: str, params: list, results: list, fn: Callable) -> None:
            if (_ENV_MODULE, name) not in declared:
                return
            func = wasmtime.Func(self._store, wasmtime.FuncType(params, results), fn, access_caller=True)
            linker.define(self._store, _ENV_MODULE, name, func)

        define_func("opa_abort", [i32], [], self._make_opa_abort())
        define_func("opa_println", [i32], [], self._make_opa_println())

        for name, arity in _BUILTIN_IMPORTS.items():
            # signature: (builtin_id, ctx, arg0..arg{arity-1}) -> i32
            params = [i32, i32] + [i32] * arity
            define_func(name, params, [i32], self._make_opa_builtin(name))

    def _read_cstring_lenient(self, addr: int, limit: int = 4096) -> str:
        """Best-effort read of a NUL-terminated UTF-8 string for diagnostics.

        Used only for ``opa_abort`` / ``opa_println`` messages. Bounded by
        ``limit`` and never raises — a malformed address degrades to a
        placeholder so diagnostics can never crash the host.
        """
        try:
            data = self._memory.read(self._store, addr, addr + limit)
        except Exception:  # pragma: no cover - defensive; wasmtime clamps OOB reads
            # Diagnostics must never raise; degrade gracefully.
            return f"<unreadable message at addr {addr}>"
        nul = data.find(0)
        raw = bytes(data[:nul] if nul != -1 else data)
        return raw.decode("utf-8", errors="replace")

    def _make_opa_abort(self) -> Callable:
        def opa_abort(caller: Caller, addr: int) -> None:
            message = self._read_cstring_lenient(addr)
            logger.debug("opa_abort: %s", message)
            raise OpaAbortError(message)

        return opa_abort

    def _make_opa_println(self) -> Callable:
        def opa_println(caller: Caller, addr: int) -> None:
            message = self._read_cstring_lenient(addr)
            logger.info("opa_println: %s", message)

        return opa_println

    def _make_opa_builtin(self, name: str) -> Callable:
        dispatch = self._builtin_dispatch

        def opa_builtin(caller: Caller, builtin_id: int, ctx: int, *args: int) -> int:
            if dispatch is None:
                raise OpaBuiltinError(
                    f"policy invoked host builtin id {builtin_id} via {name} but no " "builtin dispatch is registered"
                )
            return dispatch(builtin_id, ctx, list(args))

        return opa_builtin
