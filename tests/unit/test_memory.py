"""C3 unit tests: MemoryCodec logic that needs no real OPA heap.

Uses a fake ABI backed by a Python ``bytearray`` so we can drive read/write,
c-string scanning, and metadata-shape validation deterministically. Behaviour
against a real OPA heap is covered by tests/integration/test_memory_real.py.
"""

from __future__ import annotations

import json

import pytest

from opapywasm.errors import OpaInvalidInputError, OpaMemoryError
from opapywasm.memory import MemoryCodec


class FakeAbi:
    """Minimal in-memory stand-in for OpaAbi.

    Backs a flat bytearray; ``opa_json_parse`` stores the raw bytes at a handle
    and ``opa_json_dump`` writes them back NUL-terminated, so the codec's
    parse/dump/round-trip paths can run without a Wasm instance.
    """

    def __init__(self, size: int = 1 << 20) -> None:
        self._mem = bytearray(size)
        self._brk = 8  # bump allocator cursor (skip 0 so addr 0 is falsy-safe)
        self._values: dict[int, bytes] = {}
        self._meta_addr: int | None = None

    # allocation / memory
    def opa_malloc(self, size: int) -> int:
        addr = self._brk
        self._brk += max(size, 1)
        return addr

    def memory_write(self, addr: int, data: bytes) -> None:
        self._mem[addr : addr + len(data)] = data

    def memory_read(self, start: int, stop: int) -> bytearray:
        return self._mem[start:stop]

    def memory_len(self) -> int:
        return len(self._mem)

    # json parse/dump modelled as: value handle -> raw json bytes
    def opa_json_parse(self, addr: int, length: int) -> int:
        raw = bytes(self._mem[addr : addr + length])
        handle = self.opa_malloc(1)
        self._values[handle] = raw
        return handle

    def opa_json_dump(self, value_addr: int) -> int:
        raw = self._values[value_addr]
        out = self.opa_malloc(len(raw) + 1)
        self.memory_write(out, raw + b"\x00")
        return out

    # metadata: return a preset handle carrying a JSON object
    def set_metadata(self, obj) -> None:
        raw = json.dumps(obj).encode()
        self._meta_addr = self.opa_malloc(1)
        self._values[self._meta_addr] = raw

    def entrypoints(self) -> int:
        assert self._meta_addr is not None
        return self._meta_addr

    builtins = entrypoints


def make_codec(abi: FakeAbi | None = None, **overrides) -> MemoryCodec:
    kwargs = dict(
        max_input_bytes=1000,
        max_data_bytes=1000,
        max_result_bytes=1000,
        max_cstring_scan_bytes=64,
    )
    kwargs.update(overrides)
    return MemoryCodec(abi or FakeAbi(), **kwargs)  # type: ignore[arg-type]


class TestRawBytes:
    def test_write_then_read_bytes(self) -> None:
        c = make_codec()
        addr = c.write_bytes(b"hello world")
        assert c.read_bytes(addr, 11) == b"hello world"

    def test_read_negative_length_raises(self) -> None:
        c = make_codec()
        with pytest.raises(OpaMemoryError, match="negative"):
            c.read_bytes(0, -1)


class TestCStringScan:
    def test_reads_up_to_nul(self) -> None:
        c = make_codec()
        addr = c.write_bytes(b"abc\x00defence")
        assert c.read_cstring(addr) == b"abc"

    def test_scan_guard_caps_total_bytes(self) -> None:
        # No NUL within the cap -> raise, even though a NUL exists far beyond it.
        abi = FakeAbi()
        c = make_codec(abi, max_cstring_scan_bytes=8)
        addr = abi.opa_malloc(4096)
        abi.memory_write(addr, b"A" * 100 + b"\x00")
        with pytest.raises(OpaMemoryError, match="NUL terminator"):
            c.read_cstring(addr)

    def test_scan_stops_at_end_of_memory(self) -> None:
        abi = FakeAbi(size=32)
        c = make_codec(abi, max_cstring_scan_bytes=1000)
        addr = abi.opa_malloc(1)
        abi.memory_write(addr, b"B" * (abi.memory_len() - addr))  # fill to the end, no NUL
        with pytest.raises(OpaMemoryError, match="NUL terminator"):
            c.read_cstring(addr)


class TestJSONCoercion:
    def test_python_object_serialised_compactly(self) -> None:
        c = make_codec()
        addr, length = c.write_json({"a": 1, "b": 2}, limit=1000, kind="input")
        assert c.read_bytes(addr, length) == b'{"a":1,"b":2}'

    def test_invalid_json_string_raises(self) -> None:
        c = make_codec()
        with pytest.raises(OpaInvalidInputError, match="valid JSON"):
            c.write_json("{bad", limit=1000, kind="input")

    def test_non_serialisable_raises(self) -> None:
        c = make_codec()
        with pytest.raises(OpaInvalidInputError, match="serialis"):
            c.write_json(object(), limit=1000, kind="input")  # type: ignore[arg-type]

    def test_oversize_raises_with_kind(self) -> None:
        c = make_codec()
        with pytest.raises(OpaMemoryError, match=r"input is .* exceeding"):
            c.write_json({"x": "y" * 2000}, limit=50, kind="input")

    def test_bytes_input_accepted(self) -> None:
        c = make_codec()
        addr, length = c.write_json(b'{"k":1}', limit=1000, kind="data")
        assert c.read_bytes(addr, length) == b'{"k":1}'


class TestValueConversion:
    def test_round_trip_through_fake_abi(self) -> None:
        c = make_codec()
        cases: list[object] = [{"x": [1, 2, 3]}, "str-doc-via-quotes", 7, None, True]
        for value in cases:
            # str values are sent as quoted JSON documents; others pass as-is.
            payload = json.dumps(value) if isinstance(value, str) else value
            addr = c.python_to_opa_value(payload, limit=1000, kind="input")  # type: ignore[arg-type]
            assert c.opa_value_to_python(addr) == value

    def test_result_json_parse_failure_raises(self) -> None:
        abi = FakeAbi()
        c = make_codec(abi)
        # Craft a value handle whose bytes are not valid JSON.
        handle = abi.opa_malloc(1)
        abi._values[handle] = b"{definitely not json"
        with pytest.raises(OpaMemoryError, match="invalid JSON"):
            c.opa_value_to_python(handle)


class TestMetadataDecoding:
    def test_decodes_str_int_map(self) -> None:
        abi = FakeAbi()
        abi.set_metadata({"authz/allow": 0, "authz/deny": 1})
        c = make_codec(abi)
        assert c.decode_entrypoints() == {"authz/allow": 0, "authz/deny": 1}

    def test_non_object_metadata_raises(self) -> None:
        abi = FakeAbi()
        abi.set_metadata([1, 2, 3])
        c = make_codec(abi)
        with pytest.raises(OpaMemoryError, match="did not decode to an object"):
            c.decode_entrypoints()

    def test_non_int_value_raises(self) -> None:
        abi = FakeAbi()
        abi.set_metadata({"authz/allow": "zero"})
        c = make_codec(abi)
        with pytest.raises(OpaMemoryError, match="not a str->int"):
            c.decode_entrypoints()

    def test_bool_value_rejected_as_int(self) -> None:
        # bools are ints in Python; the decoder must reject them as ids.
        abi = FakeAbi()
        abi.set_metadata({"authz/allow": True})
        c = make_codec(abi)
        with pytest.raises(OpaMemoryError, match="not a str->int"):
            c.decode_entrypoints()
