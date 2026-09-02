"""C7 integration tests: builtin dispatch through real compiled policies."""

from __future__ import annotations

import pytest

from opapywasm import OpaWasmPolicy
from opapywasm.errors import OpaBuiltinError

pytestmark = [pytest.mark.integration, pytest.mark.requires_wasm]


def _policy(load_wasm, name: str) -> OpaWasmPolicy:
    return OpaWasmPolicy.from_wasm_bytes(load_wasm(name))


class TestDefaultBuiltins:
    def test_defaults_registered(self, load_wasm) -> None:
        p = _policy(load_wasm, "allow_true")
        for name in ("sprintf", "json.is_valid", "yaml.is_valid", "yaml.marshal", "yaml.unmarshal"):
            assert name in p.registered_builtins

    @pytest.mark.requires_yaml
    def test_default_builtins_policy_evaluates(self, load_wasm) -> None:
        p = _policy(load_wasm, "default_builtins")
        result = p.evaluate({"name": "world", "doc": "a: 1"})
        assert isinstance(result, dict)
        assert result["greeting"] == "hello world"
        assert result["valid_yaml"] is True

    @pytest.mark.requires_yaml
    def test_invalid_yaml_doc(self, load_wasm) -> None:
        p = _policy(load_wasm, "default_builtins")
        result = p.evaluate({"name": "bob", "doc": "a: [unclosed"})
        assert isinstance(result, dict)
        assert result["valid_yaml"] is False


class TestCustomBuiltin:
    def test_register_and_invoke(self, load_wasm) -> None:
        p = _policy(load_wasm, "custom_builtin")
        p.register_builtin("my.custom_builtin", lambda x: {"doubled": x * 2})
        assert p.evaluate({"value": 21}) == {"doubled": 42}

    def test_custom_builtin_required_metadata(self, load_wasm) -> None:
        p = _policy(load_wasm, "custom_builtin")
        assert "my.custom_builtin" in p.builtins

    def test_unregistered_custom_raises_naming_it(self, load_wasm) -> None:
        p = _policy(load_wasm, "custom_builtin")
        with pytest.raises(OpaBuiltinError, match=r"my\.custom_builtin"):
            p.evaluate({"value": 1})

    def test_user_exception_wrapped_with_context(self, load_wasm) -> None:
        p = _policy(load_wasm, "custom_builtin")

        def boom(_x: object) -> object:
            raise ValueError("kaboom")

        p.register_builtin("my.custom_builtin", boom)  # type: ignore[arg-type]
        with pytest.raises(OpaBuiltinError, match="raised: kaboom"):
            p.evaluate({"value": 1})

    def test_non_json_result_raises(self, load_wasm) -> None:
        p = _policy(load_wasm, "custom_builtin")
        # A set is not JSON-serialisable; the dispatcher must reject the result.
        p.register_builtin("my.custom_builtin", lambda _x: {1, 2, 3})  # type: ignore[arg-type,return-value]
        with pytest.raises(OpaBuiltinError, match="not JSON-compatible"):
            p.evaluate({"value": 1})


class TestDispatchInternals:
    """Directly exercise the instance-level dispatch error branches."""

    def _instance(self, load_wasm, name: str):
        from opapywasm.abi import OpaAbiSpec
        from opapywasm.builtins import BuiltinRegistry, default_builtins
        from opapywasm.instance import OpaPolicyInstance
        from opapywasm.runtime import WasmtimeRuntimeFactory
        from opapywasm.types import PolicyConfig

        factory = WasmtimeRuntimeFactory(load_wasm(name))
        return OpaPolicyInstance(factory, PolicyConfig(), OpaAbiSpec(), builtins=BuiltinRegistry(default_builtins()))

    def test_unknown_builtin_id_raises(self, load_wasm) -> None:
        inst = self._instance(load_wasm, "allow_true")
        with pytest.raises(OpaBuiltinError, match="unknown builtin id"):
            inst._dispatch_builtin(9999, 0, [])

    def test_arg_decode_failure_wrapped(self, load_wasm) -> None:
        inst = self._instance(load_wasm, "custom_builtin")
        inst._builtins.register("my.custom_builtin", lambda x: x)
        # A bogus arg address makes opa_json_dump / decode fail.
        with pytest.raises(OpaBuiltinError, match="decode arguments"):
            inst._dispatch_builtin(inst.builtins()["my.custom_builtin"], 0, [2_000_000_000])


class TestUnsupportedBuiltin:
    def test_http_send_not_registered_by_default(self, load_wasm) -> None:
        p = _policy(load_wasm, "unsupported_builtin")
        assert "http.send" not in p.registered_builtins

    def test_http_send_raises_naming_it(self, load_wasm) -> None:
        p = _policy(load_wasm, "unsupported_builtin")
        with pytest.raises(OpaBuiltinError, match=r"http\.send"):
            p.evaluate({"url": "http://example.com"})
