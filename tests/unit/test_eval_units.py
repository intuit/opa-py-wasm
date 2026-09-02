"""C4 unit tests: DataManager, Evaluator, and entrypoint/result logic.

Driven by a fake ABI + codec so heap checkpointing, the finally-reset contract,
and error wrapping can be tested deterministically without a Wasm instance.
End-to-end behaviour against real policies lives in
tests/integration/test_eval_real.py.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from opapywasm.abi import OpaAbi
from opapywasm.data import DataManager
from opapywasm.errors import OpaDataError, OpaEvaluationError, OpaInvalidInputError
from opapywasm.evaluator import Evaluator
from opapywasm.memory import MemoryCodec


def _dm(abi: Any, codec: Any, **kw: Any) -> DataManager:
    """Build a DataManager from test doubles (casts around the strict types)."""
    return DataManager(cast(OpaAbi, abi), cast(MemoryCodec, codec), **kw)


def _ev(abi: Any, codec: Any, dm: DataManager, **kw: Any) -> Evaluator:
    return Evaluator(cast(OpaAbi, abi), cast(MemoryCodec, codec), dm, **kw)


class FakeAbi:
    """Fake ABI exposing just what DataManager/Evaluator use."""

    def __init__(self, *, supports_opa_eval: bool = True) -> None:
        self.supports_opa_eval = supports_opa_eval
        self._heap = 100
        self.heap_sets: list[int] = []
        self.eval_calls: list[tuple] = []
        self.raise_on_eval: Exception | None = None

    def opa_heap_ptr_get(self) -> int:
        return self._heap

    def opa_heap_ptr_set(self, addr: int) -> None:
        self._heap = addr
        self.heap_sets.append(addr)

    def opa_eval(self, entrypoint_id, data_addr, input_addr, input_len, heap_ptr):
        self.eval_calls.append((entrypoint_id, data_addr, input_addr, input_len, heap_ptr))
        if self.raise_on_eval is not None:
            raise self.raise_on_eval
        return 999  # pretend result address


class FakeCodec:
    """Fake codec: tracks writes, simulates data-value allocation and results.

    Mirrors the codec surface the current Evaluator uses: ``encode_input``
    (pure serialise, no guest state) followed by ``write_bytes`` (heap write),
    plus ``python_to_opa_value`` for the data path and ``read_json_cstring`` for
    the result. ``raise_on_write`` fires from ``encode_input``/``write_bytes``/
    ``python_to_opa_value`` so both the pre-guest and data paths can be faulted.
    """

    def __init__(self) -> None:
        self.result: object = [{"result": True}]
        self.raise_on_write: Exception | None = None
        self._next = 200

    def python_to_opa_value(self, value, *, limit, kind, validate=True):
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self._next += 10
        return self._next  # pretend data value address

    def encode_input(self, value, *, limit, validate=True):
        # Pure Python serialise; the Evaluator calls this before arming the
        # deadline, so a raise here models a pre-guest input failure.
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return b'{"fake":"input"}'

    def write_bytes(self, data):
        self._next += 10
        return self._next  # pretend heap address of the written buffer

    def read_json_cstring(self, addr, *, limit=None, kind="result"):
        return self.result


class TestDataManager:
    def test_initial_checkpoint_is_heap_ptr(self) -> None:
        abi, codec = FakeAbi(), FakeCodec()
        dm = _dm(abi, codec, max_data_bytes=1000)
        assert dm.data_addr == 0
        assert dm.data_heap_ptr == 100

    def test_load_data_sets_addr_and_advances_checkpoint(self) -> None:
        abi, codec = FakeAbi(), FakeCodec()
        dm = _dm(abi, codec, max_data_bytes=1000)
        abi._heap = 250  # pretend the value parse advanced the heap
        codec._next = 300
        dm.load_data({"a": 1})
        assert dm.data_addr > 0
        # checkpoint recorded from heap AFTER the data value
        assert dm.data_heap_ptr == abi._heap

    def test_load_none_clears_data(self) -> None:
        abi, codec = FakeAbi(), FakeCodec()
        dm = _dm(abi, codec, max_data_bytes=1000)
        dm.load_data({"a": 1})
        dm.load_data(None)
        assert dm.data_addr == 0

    def test_reload_resets_to_initial_first(self) -> None:
        abi, codec = FakeAbi(), FakeCodec()
        dm = _dm(abi, codec, max_data_bytes=1000)
        dm.load_data({"a": 1})
        abi.heap_sets.clear()
        dm.load_data({"b": 2})
        # First action of a reload is resetting to the initial heap ptr (100).
        assert abi.heap_sets[0] == 100

    def test_reset_heap_to_checkpoint(self) -> None:
        abi, codec = FakeAbi(), FakeCodec()
        dm = _dm(abi, codec, max_data_bytes=1000)
        abi._heap = 250
        codec._next = 300
        dm.load_data({"a": 1})
        checkpoint = dm.data_heap_ptr
        abi._heap = 9999
        dm.reset_heap_to_checkpoint()
        assert abi._heap == checkpoint

    def test_load_failure_wraps_in_opa_data_error(self) -> None:
        abi, codec = FakeAbi(), FakeCodec()
        codec.raise_on_write = ValueError("boom")
        dm = _dm(abi, codec, max_data_bytes=1000)
        with pytest.raises(OpaDataError, match="failed to load data"):
            dm.load_data({"a": 1})
        # instance left with no data, heap reset to initial
        assert dm.data_addr == 0
        assert dm.data_heap_ptr == 100

    def test_load_failure_typed_data_error_not_double_wrapped(self) -> None:
        # If the codec itself raises OpaDataError, it propagates unchanged.
        abi, codec = FakeAbi(), FakeCodec()
        codec.raise_on_write = OpaDataError("already typed")
        dm = _dm(abi, codec, max_data_bytes=1000)
        with pytest.raises(OpaDataError, match="already typed"):
            dm.load_data({"a": 1})

    def test_load_failure_surfaces_as_data_error_with_cause(self) -> None:
        # A marshalling failure while loading data surfaces as OpaDataError
        # (the operation-specific type), chaining the underlying cause.
        abi, codec = FakeAbi(), FakeCodec()
        codec.raise_on_write = OpaInvalidInputError("bad json")
        dm = _dm(abi, codec, max_data_bytes=1000)
        with pytest.raises(OpaDataError) as exc_info:
            dm.load_data("{bad")
        assert isinstance(exc_info.value.__cause__, OpaInvalidInputError)


class TestEvaluator:
    def _make(self, abi: FakeAbi, codec: FakeCodec) -> Evaluator:
        dm = _dm(abi, codec, max_data_bytes=1000)
        dm.load_data({"seed": 1})
        return _ev(abi, codec, dm, max_input_bytes=1000, max_result_bytes=1000)

    def test_returns_raw_result_set(self) -> None:
        abi, codec = FakeAbi(), FakeCodec()
        ev = self._make(abi, codec)
        assert ev.evaluate({"user": "x"}, 0) == [{"result": True}]

    def test_heap_reset_happens_in_finally_on_success(self) -> None:
        abi, codec = FakeAbi(), FakeCodec()
        dm = _dm(abi, codec, max_data_bytes=1000)
        dm.load_data({"seed": 1})
        checkpoint = dm.data_heap_ptr
        ev = _ev(abi, codec, dm, max_input_bytes=1000, max_result_bytes=1000)
        abi.heap_sets.clear()
        ev.evaluate({"user": "x"}, 0)
        # last heap set restores the checkpoint (the finally)
        assert abi.heap_sets[-1] == checkpoint

    def test_heap_reset_happens_in_finally_on_failure(self) -> None:
        abi, codec = FakeAbi(), FakeCodec()
        dm = _dm(abi, codec, max_data_bytes=1000)
        dm.load_data({"seed": 1})
        checkpoint = dm.data_heap_ptr
        ev = _ev(abi, codec, dm, max_input_bytes=1000, max_result_bytes=1000)
        abi.raise_on_eval = RuntimeError("trap")
        abi.heap_sets.clear()
        with pytest.raises(OpaEvaluationError, match="evaluation failed"):
            ev.evaluate({"user": "x"}, 0)
        assert abi.heap_sets[-1] == checkpoint

    def test_heap_ptr_captured_after_input_write(self) -> None:
        # The heap_ptr passed to opa_eval must be read AFTER the input write,
        # i.e. it should equal the codec-advanced heap, not the checkpoint.
        abi, codec = FakeAbi(), FakeCodec()
        dm = _dm(abi, codec, max_data_bytes=1000)
        dm.load_data({"seed": 1})
        ev = _ev(abi, codec, dm, max_input_bytes=1000, max_result_bytes=1000)

        # Model the input write advancing the heap: opa_eval must receive the
        # heap pointer read *after* the input buffer is written, not the
        # checkpoint, so its scratch sits past the input.
        original_write = codec.write_bytes

        def advancing_write(*a, **k):
            addr = original_write(*a, **k)
            abi._heap += 500
            return addr

        codec.write_bytes = advancing_write  # type: ignore[method-assign]
        checkpoint = dm.data_heap_ptr
        ev.evaluate({"user": "x"}, 0)
        _, _, _, _, heap_ptr = abi.eval_calls[-1]
        assert heap_ptr == checkpoint + 500

    def test_invalid_input_propagates_unwrapped(self) -> None:
        abi, codec = FakeAbi(), FakeCodec()
        ev = self._make(abi, codec)
        codec.raise_on_write = OpaInvalidInputError("bad")
        with pytest.raises(OpaInvalidInputError):
            ev.evaluate("{bad", 0)
