"""Single-evaluation driver (milestone C4).

``Evaluator`` runs exactly one policy evaluation against one exclusively-held
instance: marshal ``input``, invoke the ``opa_eval`` fast path, read and parse
the result, and reset the heap.

Bound to one instance; not thread-safe. Concurrency is provided by the pool,
which hands out instances exclusively.

The evaluation recipe (verified against real OPA fixtures):

1. Reset the heap to the data checkpoint (discard any prior per-request state).
2. Write the input as raw JSON into the heap.
3. Capture the heap pointer *after* the input — ``opa_eval`` uses it as the base
   for its scratch allocations, so it must sit past the input buffer or the
   input gets clobbered.
4. Arm the per-eval epoch deadline (if configured) and call
   ``opa_eval(entrypoint_id, data_addr, input_addr, input_len, heap_ptr)``.
5. Read the NUL-terminated JSON result and parse it.
6. On success, reset the heap to the data checkpoint. On a mid-eval trap the
   instance is poisoned and discarded, so the reset is skipped (calling back
   into a trapped/interrupted guest would trap again — see
   ``_reset_heap_if_safe``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .errors import OpaEvaluationError, OpaInvalidInputError, OpaMemoryError, OpaWasmError
from .types import JSONInput, JSONValue

# Typed SDK errors that already carry precise context and should propagate
# unwrapped rather than being re-wrapped as a generic OpaEvaluationError.
_PASSTHROUGH_ERRORS = (OpaEvaluationError, OpaInvalidInputError, OpaMemoryError)


def _is_epoch_interrupt(exc: BaseException) -> bool:
    """True if ``exc`` is (or wraps) a Wasmtime epoch-interruption trap.

    Epoch deadlines surface as a ``wasmtime.Trap`` whose message mentions an
    interrupt. We match on the message text because wasmtime.py does not expose
    a typed trap-code enum stably across versions.
    """
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).lower()
        if "interrupt" in text or "epoch" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _find_typed_in_chain(exc: BaseException) -> OpaWasmError | None:
    """Return the first typed SDK error in ``exc``'s cause/context chain.

    wasmtime wraps a Python exception raised inside a host function into a trap;
    the original (e.g. ``OpaBuiltinError``) survives on ``__cause__`` /
    ``__context__``. Walk the chain so it can be re-raised directly.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, OpaWasmError):
            return cur
        cur = cur.__cause__ or cur.__context__
    return None


if TYPE_CHECKING:
    from .abi import OpaAbi
    from .data import DataManager
    from .memory import MemoryCodec

__all__ = ["Evaluator"]


class Evaluator:
    """Runs one evaluation on one exclusively-held instance.

    Args:
        abi: the per-instance ABI adapter (must support the ``opa_eval`` path).
        codec: the per-instance memory codec.
        data_manager: the per-instance data/heap manager.
        max_input_bytes / max_result_bytes: size guards.
        validate_input: whether raw JSON string/bytes inputs are validated.
    """

    def __init__(
        self,
        abi: OpaAbi,
        codec: MemoryCodec,
        data_manager: DataManager,
        *,
        max_input_bytes: int,
        max_result_bytes: int,
        validate_input: bool = True,
        deadline_ticks: int | None = None,
        on_enter_wasm: Callable[[], None] | None = None,
    ) -> None:
        self._abi = abi
        self._codec = codec
        self._data = data_manager
        self._max_input_bytes = max_input_bytes
        self._max_result_bytes = max_result_bytes
        self._validate_input = validate_input
        # Epoch-deadline tick budget armed before each opa_eval; None disables.
        self._deadline_ticks = deadline_ticks
        # Called once per evaluation the instant guest execution begins, so the
        # owner can mark the instance poisoned if anything from that point on
        # fails (the guest heap/allocator state is then untrustworthy).
        self._on_enter_wasm = on_enter_wasm

    def evaluate(self, input_value: JSONInput, entrypoint_id: int) -> JSONValue:
        """Evaluate ``entrypoint_id`` with ``input_value``; return the raw result set.

        The result is the OPA result set, typically ``[{"result": ...}]`` or
        ``[]`` for an undefined decision.

        Raises:
            OpaEvaluationError: if the ABI lacks the ``opa_eval`` fast path or
                the underlying evaluation traps.
        """
        # opa_eval availability is guaranteed by OpaAbi construction (a module
        # lacking it is rejected up front), so no capability check is needed.
        entered_wasm = False
        try:
            # 1. Serialise + validate the input in Python *first*. This can raise
            #    (bad JSON / oversize) but touches no guest state, so it must not
            #    arm the deadline or poison the instance.
            input_bytes = self._codec.encode_input(
                input_value, limit=self._max_input_bytes, validate=self._validate_input
            )
            # 2. Arm a fresh per-eval deadline *before the first guest call*. The
            #    deadline is relative to the current engine epoch, so it must be
            #    (re-)armed each evaluation — otherwise a stale deadline left on an
            #    idle instance could expire and trap the heap reset below. From
            #    here on every guest call is under a live deadline and any failure
            #    may leave guest state inconsistent, so we mark entered_wasm.
            entered_wasm = True
            if self._on_enter_wasm is not None:
                self._on_enter_wasm()
            self._arm_deadline()
            # 3. Discard any prior per-request allocations, write the input, and
            #    capture the heap base *after* the input (opa_eval scratch must
            #    sit past the input buffer or it clobbers the input).
            self._data.reset_heap_to_checkpoint()
            input_addr = self._codec.write_bytes(input_bytes)
            heap_ptr = self._abi.opa_heap_ptr_get()
            # 4. Evaluate.
            result_addr = self._abi.opa_eval(
                entrypoint_id,
                self._data.data_addr,
                input_addr,
                len(input_bytes),
                heap_ptr,
            )
            # 5. Read + parse the NUL-terminated JSON result.
            result = self._codec.read_json_cstring(result_addr, limit=self._max_result_bytes, kind="result")
            # 6. Restore the heap to the data checkpoint, discarding this eval's
            #    scratch, and disarm the deadline so an idle instance carries no
            #    live deadline. Done on the success path only — a trapped guest is
            #    poisoned and discarded, so it skips both (see _reset_heap_if_safe).
            self._data.reset_heap_to_checkpoint()
            self._disarm_deadline()
            return result
        except _PASSTHROUGH_ERRORS:
            self._reset_heap_if_safe(entered_wasm)
            raise
        except Exception as exc:
            self._reset_heap_if_safe(entered_wasm)
            # A typed SDK error raised inside a host builtin is wrapped by
            # wasmtime into a trap; recover it from the exception chain so the
            # caller sees e.g. OpaBuiltinError, not a generic OpaEvaluationError.
            typed = _find_typed_in_chain(exc)
            if typed is not None:
                raise typed from exc
            if entered_wasm and _is_epoch_interrupt(exc):
                raise OpaEvaluationError(
                    "evaluation exceeded its time limit (eval_timeout_seconds) and was interrupted"
                ) from exc
            raise OpaEvaluationError(f"evaluation failed: {exc}") from exc

    def _arm_deadline(self) -> None:
        """Arm this evaluation's epoch deadline (no-op when timeouts disabled)."""
        if self._deadline_ticks is not None:
            self._abi.set_epoch_deadline(self._deadline_ticks)

    def _disarm_deadline(self) -> None:
        """Clear any live deadline after a successful eval.

        Leaves the store with a far-future deadline so an instance sitting idle
        in the pool never carries an about-to-expire deadline into its next use.
        """
        if self._deadline_ticks is not None:
            self._abi.clear_epoch_deadline()

    def _reset_heap_if_safe(self, entered_wasm: bool) -> None:
        """Restore the heap to the data checkpoint after a *successful* or
        pre-execution failure.

        If the guest trapped mid-eval (``entered_wasm`` and an exception is in
        flight), the instance is already poisoned and about to be discarded, and
        — critically — an epoch-interrupt trap leaves the store's deadline
        expired, so calling back into the guest here would trap *again* and mask
        the real error. In that case we skip the reset entirely.
        """
        if entered_wasm:
            return
        self._data.reset_heap_to_checkpoint()
