"""Single Wasm instance wrapper.

``OpaPolicyInstance`` bundles everything that is instance-local and therefore
must never be shared across threads: the :class:`~opapywasm.runtime.WasmtimeRuntime`
(``Store`` / ``Memory`` / ``Instance`` / exports), the
:class:`~opapywasm.abi.OpaAbi`, :class:`~opapywasm.memory.MemoryCodec`,
:class:`~opapywasm.data.DataManager` and :class:`~opapywasm.evaluator.Evaluator`.

An instance is safe to use *only* when borrowed exclusively from the pool. It
carries a ``data_version`` used to detect and repair staleness after a
policy-level ``set_data``, and a ``poisoned`` flag the pool checks on release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .abi import OpaAbi
from .builtins import BuiltinRegistry
from .data import DataManager
from .errors import OpaBuiltinError
from .evaluator import Evaluator
from .memory import MemoryCodec
from .types import JSONInput, JSONValue

if TYPE_CHECKING:
    from .abi import OpaAbiSpec
    from .runtime import EpochTicker, WasmtimeRuntimeFactory
    from .types import PolicyConfig

__all__ = ["OpaPolicyInstance"]


class OpaPolicyInstance:
    """One fully-wired, single-threaded OPA policy instance.

    Construction instantiates a fresh runtime from the shared factory, binds the
    ABI, codec, data manager and evaluator, and loads the initial data document.

    Args:
        factory: the shared engine/module factory.
        config: the policy configuration (size guards, validation flags).
        abi_spec: the shared, immutable ABI spec.
        initial_data: data document to load at construction (may be ``None``).
        data_version: the policy-level data version this instance starts at.
        builtins: shared registry whose snapshot backs ``opa_builtinN`` dispatch.
    """

    def __init__(
        self,
        factory: WasmtimeRuntimeFactory,
        config: PolicyConfig,
        abi_spec: OpaAbiSpec,
        *,
        initial_data: JSONInput | None = None,
        data_version: int = 0,
        builtins: BuiltinRegistry | None = None,
        epoch: EpochTicker | None = None,
    ) -> None:
        self._config = config
        self._builtins = builtins if builtins is not None else BuiltinRegistry()
        # Once an evaluation enters Wasm and fails, the guest heap/allocator may
        # be in an unknown state; the instance is marked poisoned and the pool
        # discards it instead of reusing it. See `evaluate`.
        self._poisoned = False
        self._entered_wasm = False
        # The runtime needs the dispatch callback at creation time, but the
        # dispatch reads self._codec / self._builtin_id_to_name — which are set
        # just below, before any evaluation can occur. A bound method closes the
        # loop safely.
        self._runtime = factory.create_runtime(builtin_dispatch=self._dispatch_builtin)
        # Everything after the runtime exists can fail (ABI validation, metadata
        # decode, data load). On failure, close the runtime's native Store before
        # re-raising so a partially-built instance never leaks it to GC.
        try:
            self._abi = OpaAbi(self._runtime, abi_spec)
            self._codec = MemoryCodec(
                self._abi,
                max_input_bytes=config.max_input_bytes,
                max_data_bytes=config.max_data_bytes,
                max_result_bytes=config.max_result_bytes,
                max_cstring_scan_bytes=config.max_cstring_scan_bytes,
            )
            self._data = DataManager(self._abi, self._codec, max_data_bytes=config.max_data_bytes)
            # Convert the wall-clock eval timeout into an epoch-tick budget.
            deadline_ticks: int | None = None
            if epoch is not None and config.eval_timeout_seconds is not None:
                deadline_ticks = epoch.deadline_ticks(config.eval_timeout_seconds)
            self._evaluator = Evaluator(
                self._abi,
                self._codec,
                self._data,
                max_input_bytes=config.max_input_bytes,
                max_result_bytes=config.max_result_bytes,
                validate_input=config.parse_raw_json_input,
                deadline_ticks=deadline_ticks,
                on_enter_wasm=self._mark_entered_wasm,
            )
            # id -> name for opa_builtinN dispatch (built from this module's metadata).
            self._builtin_id_to_name = {ident: name for name, ident in self._codec.decode_builtins().items()}
            self._data_version = data_version
            self._data.load_data(initial_data)
        except BaseException:
            self._runtime.close()
            raise
        # Start the shared epoch ticker only *after* the instance is fully and
        # successfully built, so a construction failure never leaves a ticker
        # thread running. It spins up only for policies with a timeout that
        # actually build an instance, and is idempotent across instances.
        if deadline_ticks is not None and epoch is not None:
            epoch.ensure_running()

    # -- metadata ------------------------------------------------------------

    @property
    def abi(self) -> OpaAbi:
        return self._abi

    @property
    def codec(self) -> MemoryCodec:
        return self._codec

    @property
    def data_version(self) -> int:
        """Version tag of the data currently loaded in this instance."""
        return self._data_version

    def entrypoints(self) -> dict[str, int]:
        """Return this policy's ``{entrypoint_name: id}`` map."""
        return self._codec.decode_entrypoints()

    def builtins(self) -> dict[str, int]:
        """Return this policy's required-builtin ``{name: id}`` map."""
        return self._codec.decode_builtins()

    # -- data ----------------------------------------------------------------

    def reload_data(self, data: JSONInput | None, data_version: int) -> None:
        """Replace this instance's data document and update its version tag.

        Reloading calls guest exports (heap reset + parse). If any of them fails
        — a trap, memory cap, or allocator error — the instance's guest state is
        uncertain, so it is marked :attr:`poisoned` before the error propagates;
        the pool then discards and rebuilds it rather than circulating an
        instance left with no data. The version is bumped only on success, so a
        transient failure is simply retried on a fresh instance.
        """
        try:
            self._data.load_data(data)
        except Exception:
            self._poisoned = True
            raise
        self._data_version = data_version

    # -- evaluation ----------------------------------------------------------

    @property
    def poisoned(self) -> bool:
        """True if a prior evaluation failed after entering Wasm.

        A poisoned instance must not be reused: its guest heap / allocator may
        be in an inconsistent state. The pool discards it on release.
        """
        return self._poisoned

    def evaluate(self, input_value: JSONInput, entrypoint_id: int) -> JSONValue:
        """Evaluate ``entrypoint_id`` with ``input_value``; return the raw result set.

        On any failure that occurs *after* guest execution has begun (a trap,
        ``opa_abort``, a host-builtin error, a mid-eval memory error, or a failed
        heap reset), the instance is marked :attr:`poisoned` before the error
        propagates, so the pool will discard rather than reuse it. Pre-execution
        failures (input serialisation / size guards) leave the instance clean.
        """
        self._entered_wasm = False
        try:
            return self._evaluator.evaluate(input_value, entrypoint_id)
        except Exception:
            if self._entered_wasm:
                self._poisoned = True
            raise

    def _mark_entered_wasm(self) -> None:
        self._entered_wasm = True

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Release this instance's native runtime (Store/Memory/Instance).

        Idempotent. Called by the pool when discarding an instance and by the
        facade for the metadata probe, so native resources are freed promptly
        rather than at garbage-collection time.
        """
        self._runtime.close()

    # -- builtin dispatch ----------------------------------------------------

    def _dispatch_builtin(self, builtin_id: int, ctx: int, arg_addrs: list[int]) -> int:
        """Host callback invoked by ``opa_builtinN`` during evaluation.

        1. Resolve the builtin id to a name (from this module's metadata).
        2. Look it up in the registry snapshot (lock not held while calling).
        3. Decode OPA arg values to Python, call the builtin, re-encode the
           result as an OPA value, and return its address.

        Any failure surfaces as :class:`OpaBuiltinError` naming the builtin, so
        the enclosing evaluation fails with actionable context rather than a
        bare Wasm trap.
        """
        name = self._builtin_id_to_name.get(builtin_id)
        if name is None:
            raise OpaBuiltinError(f"policy invoked unknown builtin id {builtin_id}")

        snapshot = self._builtins.snapshot()
        fn = snapshot.get(name)
        if fn is None:
            raise OpaBuiltinError(
                f"policy requires builtin {name!r} but it is not registered; "
                "register it via OpaWasmPolicy.register_builtin"
            )

        try:
            args = [self._codec.opa_value_to_python(addr, kind=f"builtin:{name}:arg") for addr in arg_addrs]
        except OpaBuiltinError:
            raise
        except Exception as exc:
            raise OpaBuiltinError(f"builtin {name!r}: failed to decode arguments: {exc}") from exc

        try:
            result = fn(*args)
        except OpaBuiltinError:
            raise
        except Exception as exc:
            raise OpaBuiltinError(f"builtin {name!r} raised: {exc}") from exc

        try:
            return self._codec.python_object_to_opa_value(
                result, limit=self._config.max_result_bytes, kind=f"builtin:{name}:result"
            )
        except OpaBuiltinError:
            raise
        except Exception as exc:
            raise OpaBuiltinError(f"builtin {name!r}: result is not JSON-compatible: {exc}") from exc
