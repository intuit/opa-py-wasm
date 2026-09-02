"""Unit coverage for the review-driven hardening changes.

Covers the pure-Python behaviours that do not need a compiled fixture:

* ``UNDEFINED`` sentinel semantics.
* New ``PolicyConfig`` validation (``eval_timeout_seconds`` / ``max_memory_pages``).
* NaN/Infinity rejection in the codec.
* Centralised guest-pointer / bounds validation in the codec.
* ``__version__`` sourced from package metadata.
"""

from __future__ import annotations

import math

import pytest

from opapywasm import UNDEFINED, PolicyConfig, Undefined
from opapywasm.errors import OpaInvalidInputError, OpaMemoryError
from opapywasm.memory import MemoryCodec


class TestUndefinedSentinel:
    def test_singleton_identity(self) -> None:
        assert UNDEFINED is Undefined.token

    def test_is_falsy(self) -> None:
        assert not UNDEFINED
        assert bool(UNDEFINED) is False

    def test_repr(self) -> None:
        assert repr(UNDEFINED) == "UNDEFINED"

    def test_distinct_from_none(self) -> None:
        assert UNDEFINED is not None


class TestConfigValidation:
    def test_eval_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="eval_timeout_seconds"):
            PolicyConfig(eval_timeout_seconds=0)
        with pytest.raises(ValueError, match="eval_timeout_seconds"):
            PolicyConfig(eval_timeout_seconds=-1)

    def test_eval_timeout_none_allowed(self) -> None:
        assert PolicyConfig(eval_timeout_seconds=None).eval_timeout_seconds is None

    def test_max_memory_pages_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_memory_pages"):
            PolicyConfig(max_memory_pages=0)

    def test_max_memory_pages_none_allowed(self) -> None:
        assert PolicyConfig(max_memory_pages=None).max_memory_pages is None

    def test_defaults_are_bounded(self) -> None:
        cfg = PolicyConfig()
        assert cfg.max_memory_pages is not None
        assert cfg.eval_timeout_seconds is not None
        assert cfg.strict_result_shape is True


class _FakeAbi:
    """Minimal ABI stand-in for exercising codec bounds/pointer checks."""

    def __init__(self, *, mem_len: int = 1024, malloc_addr: int = 16) -> None:
        self._mem = bytearray(mem_len)
        self._malloc_addr = malloc_addr

    def memory_len(self) -> int:
        return len(self._mem)

    def memory_write(self, addr: int, data: bytes) -> None:
        self._mem[addr : addr + len(data)] = data

    def memory_read(self, start: int, stop: int) -> bytearray:
        return self._mem[start:stop]

    def opa_malloc(self, size: int) -> int:
        return self._malloc_addr


def _codec(abi: _FakeAbi) -> MemoryCodec:
    return MemoryCodec(
        abi,  # type: ignore[arg-type]
        max_input_bytes=1000,
        max_data_bytes=1000,
        max_result_bytes=1000,
        max_cstring_scan_bytes=1000,
    )


class TestNaNRejection:
    def test_nan_rejected(self) -> None:
        codec = _codec(_FakeAbi())
        with pytest.raises(OpaInvalidInputError):
            codec.python_object_to_opa_value(float("nan"), limit=1000, kind="x")

    def test_infinity_rejected(self) -> None:
        codec = _codec(_FakeAbi())
        with pytest.raises(OpaInvalidInputError):
            codec.python_object_to_opa_value(math.inf, limit=1000, kind="x")


class TestBoundsChecks:
    def test_read_negative_address_rejected(self) -> None:
        codec = _codec(_FakeAbi())
        with pytest.raises(OpaMemoryError, match="negative"):
            codec.read_bytes(-1, 4)

    def test_read_negative_length_rejected(self) -> None:
        codec = _codec(_FakeAbi())
        with pytest.raises(OpaMemoryError, match="negative length"):
            codec.read_bytes(0, -4)

    def test_read_past_end_rejected(self) -> None:
        codec = _codec(_FakeAbi(mem_len=64))
        with pytest.raises(OpaMemoryError, match="exceeds guest memory"):
            codec.read_bytes(60, 100)

    def test_zero_malloc_pointer_rejected(self) -> None:
        codec = _codec(_FakeAbi(malloc_addr=0))
        with pytest.raises(OpaMemoryError, match="invalid guest pointer"):
            codec.write_bytes(b"hello")

    def test_cstring_start_out_of_range_rejected(self) -> None:
        codec = _codec(_FakeAbi(mem_len=64))
        with pytest.raises(OpaMemoryError, match="outside guest memory"):
            codec.read_cstring(1000)


class TestVersionMetadata:
    def test_version_is_a_string_from_metadata(self) -> None:
        import opapywasm

        assert isinstance(opapywasm.__version__, str)
        # Must not be the old hardcoded value that drifted from pyproject.
        assert opapywasm.__version__ != "0.1.0a0"


class TestDataSerialisation:
    """Covers OpaWasmPolicy._serialise_data branches without a real fixture."""

    def _bare_policy(self):
        # Build just enough of the facade to call _serialise_data: it only reads
        # self._config, so we construct via __new__ and attach a config (no wasm
        # compilation, so no fixture needed).
        from opapywasm.policy import OpaWasmPolicy

        p = OpaWasmPolicy.__new__(OpaWasmPolicy)
        p._config = PolicyConfig(max_data_bytes=32)
        return p

    def test_none_returns_none(self) -> None:
        assert self._bare_policy()._serialise_data(None) is None

    def test_dict_serialised_to_bytes(self) -> None:
        assert self._bare_policy()._serialise_data({"a": 1}) == b'{"a":1}'

    def test_str_document_validated(self) -> None:
        assert self._bare_policy()._serialise_data('{"a":1}') == b'{"a":1}'

    def test_invalid_str_rejected(self) -> None:
        with pytest.raises(OpaInvalidInputError, match="not valid JSON"):
            self._bare_policy()._serialise_data("{not json")

    def test_non_serialisable_rejected(self) -> None:
        with pytest.raises(OpaInvalidInputError, match="not JSON-serialisable"):
            self._bare_policy()._serialise_data({"x": {1, 2}})

    def test_oversize_rejected(self) -> None:
        with pytest.raises(OpaInvalidInputError, match="exceeding"):
            self._bare_policy()._serialise_data({"x": "y" * 100})
