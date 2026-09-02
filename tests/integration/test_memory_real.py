"""C3 integration tests: MemoryCodec against real compiled OPA policies.

Round-trips and metadata decoding need a real OPA heap and the real
opa_json_parse / opa_json_dump / entrypoints / builtins exports.
"""

from __future__ import annotations

import pytest

from opapywasm.abi import OpaAbi
from opapywasm.errors import OpaInvalidInputError, OpaMemoryError
from opapywasm.memory import MemoryCodec
from opapywasm.runtime import WasmtimeRuntimeFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_wasm]

_LIMIT = 1_000_000


def _codec(load_wasm, name: str, **overrides) -> MemoryCodec:
    rt = WasmtimeRuntimeFactory(load_wasm(name)).create_runtime()
    kwargs = dict(
        max_input_bytes=_LIMIT,
        max_data_bytes=_LIMIT,
        max_result_bytes=_LIMIT,
        max_cstring_scan_bytes=_LIMIT,
    )
    kwargs.update(overrides)
    return MemoryCodec(OpaAbi(rt), **kwargs)


class TestRoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            {"user": "alice", "n": 3.5, "ok": True, "nil": None, "arr": [1, 2, [3]]},
            [1, 2, 3],
            {},
            [],
            42,
            3.14,
            True,
            False,
            None,
            {"unicode": "héllo • 世界"},
            {"deep": {"a": {"b": {"c": [1, {"d": 2}]}}}},
        ],
    )
    def test_python_value_round_trips(self, load_wasm, value) -> None:
        c = _codec(load_wasm, "allow_true")
        addr = c.python_to_opa_value(value, limit=_LIMIT, kind="input")
        assert c.opa_value_to_python(addr) == value

    def test_raw_json_string_document(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true")
        addr = c.python_to_opa_value('{"a":1,"b":[2,3]}', limit=_LIMIT, kind="input")
        assert c.opa_value_to_python(addr) == {"a": 1, "b": [2, 3]}

    def test_json_string_value_must_be_quoted(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true")
        addr = c.python_to_opa_value('"hello"', limit=_LIMIT, kind="input")
        assert c.opa_value_to_python(addr) == "hello"

    def test_validate_false_skips_json_check(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true")
        addr = c.python_to_opa_value('{"a":1}', limit=_LIMIT, kind="input", validate=False)
        assert c.opa_value_to_python(addr) == {"a": 1}


class TestInputValidation:
    def test_invalid_json_string_raises(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true")
        with pytest.raises(OpaInvalidInputError, match="valid JSON"):
            c.python_to_opa_value("{not json", limit=_LIMIT, kind="input")

    def test_non_serialisable_python_raises(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true")
        with pytest.raises(OpaInvalidInputError, match="serialis"):
            c.python_to_opa_value({1, 2, 3}, limit=_LIMIT, kind="input")  # type: ignore[arg-type]


class TestSizeGuards:
    def test_oversize_python_value_raises(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true")
        with pytest.raises(OpaMemoryError, match="exceeding"):
            c.python_to_opa_value(["x"] * 1000, limit=10, kind="input")

    def test_oversize_raw_json_raises(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true")
        with pytest.raises(OpaMemoryError, match="exceeding"):
            c.python_to_opa_value('"' + "x" * 1000 + '"', limit=10, kind="input")


class TestCStringGuard:
    def test_missing_terminator_within_cap_raises(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true", max_cstring_scan_bytes=8)
        abi = c._abi
        addr = abi.opa_malloc(64)
        abi.memory_write(addr, b"A" * 32)  # 32 non-NUL bytes, cap is 8
        with pytest.raises(OpaMemoryError, match="NUL terminator"):
            c.read_cstring(addr)

    def test_terminator_within_cap_reads(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true", max_cstring_scan_bytes=8)
        abi = c._abi
        addr = abi.opa_malloc(16)
        abi.memory_write(addr, b"hi\x00")
        assert c.read_cstring(addr) == b"hi"


class TestMetadata:
    def test_decode_entrypoints_multi(self, load_wasm) -> None:
        c = _codec(load_wasm, "multi_entrypoint")
        assert c.decode_entrypoints() == {"authz/allow": 0, "authz/deny": 1, "authz/roles": 2}

    def test_decode_entrypoints_single(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true")
        assert c.decode_entrypoints() == {"authz/allow": 0}

    def test_decode_builtins_empty_for_simple_policy(self, load_wasm) -> None:
        c = _codec(load_wasm, "allow_true")
        assert c.decode_builtins() == {}

    def test_decode_builtins_lists_host_builtins(self, load_wasm) -> None:
        # OPA only delegates builtins it can't evaluate itself; sprintf and
        # yaml.is_valid are host builtins, json.is_valid is compiled inline.
        c = _codec(load_wasm, "default_builtins")
        builtins = c.decode_builtins()
        assert "sprintf" in builtins
        assert all(isinstance(v, int) for v in builtins.values())

    def test_decode_builtins_unsupported(self, load_wasm) -> None:
        c = _codec(load_wasm, "unsupported_builtin")
        assert c.decode_builtins() == {"http.send": 0}
