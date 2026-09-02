"""Per-instance data lifecycle and heap checkpointing (milestone C4).

``DataManager`` owns the long-lived external ``data`` document for a single
instance. It records the initial heap pointer, loads ``data`` into the OPA
heap, and checkpoints the post-data heap pointer so every short-lived
evaluation can reset the heap back to that checkpoint.

Heap model (verified against real OPA fixtures):

* At construction the heap pointer is at ``initial_heap_ptr``.
* Loading data resets the heap to ``initial_heap_ptr``, parses the data document
  into an OPA value (``data_addr``), then records ``data_heap_ptr`` = the heap
  pointer *after* the data value. Everything below ``data_heap_ptr`` is the
  long-lived data region.
* Each evaluation resets the heap to ``data_heap_ptr`` before writing its
  short-lived input, so per-request allocations never accumulate and never
  corrupt the data region.

Bound to one instance; not thread-safe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import OpaDataError
from .types import JSONInput

if TYPE_CHECKING:
    from .abi import OpaAbi
    from .memory import MemoryCodec

__all__ = ["DataManager"]

# OPA value address 0 means "no data document" to opa_eval.
_NO_DATA_ADDR = 0


class DataManager:
    """Owns one instance's external ``data`` document and heap checkpoints.

    Args:
        abi: the per-instance ABI adapter.
        codec: the per-instance memory codec.
        max_data_bytes: size guard for the serialised data document.
    """

    def __init__(self, abi: OpaAbi, codec: MemoryCodec, *, max_data_bytes: int) -> None:
        self._abi = abi
        self._codec = codec
        self._max_data_bytes = max_data_bytes

        # Heap pointer before any data is loaded — the reset floor for a reload.
        self._initial_heap_ptr = abi.opa_heap_ptr_get()
        # Address of the loaded data OPA value (0 == no data).
        self._data_addr = _NO_DATA_ADDR
        # Heap pointer just past the loaded data; per-eval scratch starts here.
        self._data_heap_ptr = self._initial_heap_ptr

    @property
    def data_addr(self) -> int:
        """OPA value address of the current data document (0 if none)."""
        return self._data_addr

    @property
    def data_heap_ptr(self) -> int:
        """Heap pointer just past the data region; the per-eval reset point."""
        return self._data_heap_ptr

    def reset_heap_to_checkpoint(self) -> None:
        """Reset the heap to the post-data checkpoint.

        Called after a successful evaluation (and before the next one) to discard
        per-request allocations while preserving the loaded data. It is skipped
        when an evaluation traps mid-flight, because the instance is then poisoned
        and discarded and calling back into a trapped guest could trap again (see
        ``Evaluator._reset_heap_if_safe``).
        """
        self._abi.opa_heap_ptr_set(self._data_heap_ptr)

    def load_data(self, data: JSONInput | None) -> None:
        """Load (or clear) the external data document for this instance.

        Resets the heap to ``initial_heap_ptr`` first so repeated ``load_data``
        calls do not leak the previous document, then parses the new data and
        records the post-data heap checkpoint.

        Args:
            data: a JSON-compatible object / JSON string / bytes, or ``None`` to
                clear the data document.

        Raises:
            OpaDataError: if the data cannot be marshalled into an OPA value.
        """
        self._abi.opa_heap_ptr_set(self._initial_heap_ptr)
        if data is None:
            self._data_addr = _NO_DATA_ADDR
            self._data_heap_ptr = self._initial_heap_ptr
            return
        try:
            self._data_addr = self._codec.python_to_opa_value(data, limit=self._max_data_bytes, kind="data")
        except Exception as exc:
            # Leave the instance with no data rather than a half-loaded heap.
            self._abi.opa_heap_ptr_set(self._initial_heap_ptr)
            self._data_addr = _NO_DATA_ADDR
            self._data_heap_ptr = self._initial_heap_ptr
            if isinstance(exc, OpaDataError):
                raise
            raise OpaDataError(f"failed to load data: {exc}") from exc
        self._data_heap_ptr = self._abi.opa_heap_ptr_get()
