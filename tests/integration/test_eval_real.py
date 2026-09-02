"""C4 integration tests: end-to-end evaluation via OpaWasmPolicy.

Exercises the full stack (runtime → abi → codec → data → evaluator → facade)
against real compiled OPA policies.
"""

from __future__ import annotations

import io

import pytest

from opapywasm import OpaWasmPolicy, PolicyConfig
from opapywasm.errors import OpaEntrypointError, OpaEvaluationError, OpaPolicyClosedError

pytestmark = [pytest.mark.integration, pytest.mark.requires_wasm]


def _policy(load_wasm, name: str, config: PolicyConfig | None = None) -> OpaWasmPolicy:
    return OpaWasmPolicy.from_wasm_bytes(load_wasm(name), config)


class TestConstructors:
    def test_from_wasm_file(self, wasm_dir) -> None:
        p = OpaWasmPolicy.from_wasm_file(wasm_dir / "allow_true.wasm")
        assert p.evaluate({}) is True

    def test_from_wasm_stream(self, wasm_dir) -> None:
        with open(wasm_dir / "allow_true.wasm", "rb") as fh:
            p = OpaWasmPolicy.from_wasm_stream(io.BytesIO(fh.read()))
        assert p.evaluate({}) is True

    def test_abi_version_property(self, load_wasm) -> None:
        p = OpaWasmPolicy.from_wasm_bytes(load_wasm("allow_true"))
        assert p.abi_version[0] == 1

    def test_builtins_property_lists_required_host_builtins(self, load_wasm) -> None:
        p = OpaWasmPolicy.from_wasm_bytes(load_wasm("default_builtins"))
        assert "sprintf" in p.builtins


class TestBasicEval:
    def test_allow_true(self, load_wasm) -> None:
        assert _policy(load_wasm, "allow_true").evaluate({}) is True

    def test_allow_false(self, load_wasm) -> None:
        assert _policy(load_wasm, "allow_false").evaluate({}) is False

    def test_input_based(self, load_wasm) -> None:
        p = _policy(load_wasm, "input_based")
        assert p.evaluate({"user": "alice", "action": "read"}) is True
        assert p.evaluate({"user": "alice", "action": "write"}) is False

    def test_evaluate_raw_shape(self, load_wasm) -> None:
        assert _policy(load_wasm, "allow_true").evaluate_raw({}) == [{"result": True}]


class TestDataEval:
    def test_data_drives_decision(self, load_wasm) -> None:
        p = _policy(load_wasm, "data_based")
        p.set_data({"roles": {"alice": "admin", "bob": "viewer"}})
        assert p.evaluate({"user": "alice"}) is True
        assert p.evaluate({"user": "bob"}) is False
        assert p.evaluate({"user": "carol"}) is False

    def test_set_data_updates_decision_and_version(self, load_wasm) -> None:
        p = _policy(load_wasm, "data_based")
        assert p.data_version == 0
        p.set_data({"roles": {"bob": "viewer"}})
        assert p.evaluate({"user": "bob"}) is False
        p.set_data({"roles": {"bob": "admin"}})
        assert p.evaluate({"user": "bob"}) is True
        assert p.data_version == 2

    def test_no_state_bleed_across_repeated_evals(self, load_wasm) -> None:
        p = _policy(load_wasm, "data_based")
        p.set_data({"roles": {"alice": "admin"}})
        for _ in range(50):
            assert p.evaluate({"user": "alice"}) is True
            assert p.evaluate({"user": "nobody"}) is False


class TestEntrypoints:
    def test_enumerate(self, load_wasm) -> None:
        assert _policy(load_wasm, "multi_entrypoint").entrypoints == {
            "authz/allow": 0,
            "authz/deny": 1,
            "authz/roles": 2,
        }

    def test_select_by_name(self, load_wasm) -> None:
        p = _policy(load_wasm, "multi_entrypoint")
        assert p.evaluate({"action": "read"}, entrypoint="authz/allow") is True
        assert p.evaluate({"action": "delete"}, entrypoint="authz/deny") is True

    def test_select_by_id(self, load_wasm) -> None:
        p = _policy(load_wasm, "multi_entrypoint")
        assert p.evaluate({"action": "delete"}, entrypoint=1) is True

    def test_default_entrypoint_config(self, load_wasm) -> None:
        p = _policy(load_wasm, "multi_entrypoint", PolicyConfig(default_entrypoint="authz/deny"))
        assert p.evaluate({"action": "delete"}) is True

    def test_unknown_name_raises(self, load_wasm) -> None:
        with pytest.raises(OpaEntrypointError, match="unknown entrypoint"):
            _policy(load_wasm, "multi_entrypoint").evaluate({}, entrypoint="authz/missing")

    def test_unknown_id_raises(self, load_wasm) -> None:
        with pytest.raises(OpaEntrypointError, match="unknown entrypoint id"):
            _policy(load_wasm, "multi_entrypoint").evaluate({}, entrypoint=99)

    def test_bool_entrypoint_rejected(self, load_wasm) -> None:
        # bool is an int subclass; it must not be accepted as an entrypoint id.
        with pytest.raises(OpaEntrypointError, match="invalid entrypoint"):
            _policy(load_wasm, "allow_true").evaluate({}, entrypoint=True)


class TestResultShapes:
    def test_object(self, load_wasm) -> None:
        assert _policy(load_wasm, "return_object").evaluate({"user": "x", "action": "read"}) == {
            "allowed": True,
            "user": "x",
        }

    def test_array(self, load_wasm) -> None:
        assert _policy(load_wasm, "return_array").evaluate({}) == [1, 2, 3]

    def test_scalar(self, load_wasm) -> None:
        assert _policy(load_wasm, "return_scalar").evaluate({}) == "hello"


class TestUndefined:
    def test_undefined_returns_sentinel(self, load_wasm) -> None:
        from opapywasm import UNDEFINED

        result = _policy(load_wasm, "undefined_result").evaluate({"enabled": False})
        assert result is UNDEFINED
        assert not result  # sentinel is falsy

    def test_defined_returns_value(self, load_wasm) -> None:
        assert _policy(load_wasm, "undefined_result").evaluate({"enabled": True}) is True

    def test_raw_undefined_is_empty_list(self, load_wasm) -> None:
        assert _policy(load_wasm, "undefined_result").evaluate_raw({"enabled": False}) == []

    def test_strict_result_raises(self, load_wasm) -> None:
        p = _policy(load_wasm, "undefined_result", PolicyConfig(strict_result=True))
        with pytest.raises(OpaEvaluationError, match="undefined"):
            p.evaluate({"enabled": False})


class TestInstanceAndSimplify:
    def test_instance_exposes_abi_and_codec(self, load_wasm) -> None:
        from opapywasm.abi import OpaAbi, OpaAbiSpec
        from opapywasm.instance import OpaPolicyInstance
        from opapywasm.memory import MemoryCodec
        from opapywasm.runtime import WasmtimeRuntimeFactory

        factory = WasmtimeRuntimeFactory(load_wasm("allow_true"))
        inst = OpaPolicyInstance(factory, PolicyConfig(), OpaAbiSpec())
        assert isinstance(inst.abi, OpaAbi)
        assert isinstance(inst.codec, MemoryCodec)

    def test_simplify_unexpected_shape_raises_by_default(self, load_wasm) -> None:
        # A non-empty result set whose shape is not [{"result": ...}] is a
        # malformed/unexpected shape: by default (strict_result_shape=True) this
        # raises rather than silently collapsing to undefined, so ABI corruption
        # is not hidden.
        p = _policy(load_wasm, "allow_true")
        with pytest.raises(OpaEvaluationError, match="unexpected result shape"):
            p._simplify_result([{"other": 1}])
        with pytest.raises(OpaEvaluationError, match="unexpected result shape"):
            p._simplify_result("not-a-list")

    def test_simplify_unexpected_shape_lenient_returns_undefined(self, load_wasm) -> None:
        from opapywasm import UNDEFINED

        p = _policy(load_wasm, "allow_true", PolicyConfig(strict_result_shape=False))
        assert p._simplify_result([{"other": 1}]) is UNDEFINED
        assert p._simplify_result("not-a-list") is UNDEFINED

    def test_simplify_unexpected_shape_strict_result_raises(self, load_wasm) -> None:
        # With strict_result_shape off but strict_result on, an unexpected shape
        # is reported as an undefined/empty decision rather than the sentinel.
        p = _policy(load_wasm, "allow_true", PolicyConfig(strict_result_shape=False, strict_result=True))
        with pytest.raises(OpaEvaluationError, match="undefined/empty"):
            p._simplify_result([{"other": 1}])


class TestLifecycle:
    def test_close_blocks_further_eval(self, load_wasm) -> None:
        p = _policy(load_wasm, "allow_true")
        p.close()
        with pytest.raises(OpaPolicyClosedError):
            p.evaluate({})

    def test_context_manager_closes(self, load_wasm) -> None:
        with _policy(load_wasm, "allow_true") as p:
            assert p.evaluate({}) is True
        with pytest.raises(OpaPolicyClosedError):
            p.evaluate({})

    def test_raw_json_string_input(self, load_wasm) -> None:
        p = _policy(load_wasm, "input_based")
        assert p.evaluate('{"user":"alice","action":"read"}') is True
