"""C7 unit tests: BuiltinRegistry and the default builtin implementations."""

from __future__ import annotations

import pytest

from opapywasm.builtins import BuiltinRegistry, default_builtins
from opapywasm.errors import OpaBuiltinError


class TestRegistry:
    def test_seeded_with_initial(self) -> None:
        reg = BuiltinRegistry({"a": lambda: 1})
        assert reg.names() == ["a"]

    def test_register_and_snapshot(self) -> None:
        reg = BuiltinRegistry()
        reg.register("my.fn", lambda x: x)
        assert "my.fn" in reg.snapshot()

    def test_snapshot_is_stable_across_registration(self) -> None:
        # Copy-on-write: a snapshot taken earlier must not see later mutations.
        reg = BuiltinRegistry({"a": lambda: 1})
        snap = reg.snapshot()
        reg.register("b", lambda: 2)
        assert "b" not in snap
        assert "b" in reg.snapshot()

    def test_snapshot_is_read_only(self) -> None:
        # The published snapshot is a MappingProxyType view, so a caller cannot
        # mutate the live registry through it — the copy-on-write "snapshots are
        # stable" guarantee is structural, not merely by convention.
        reg = BuiltinRegistry({"a": lambda: 1})
        snap = reg.snapshot()
        with pytest.raises(TypeError):
            snap["b"] = lambda: 2  # type: ignore[index]
        assert "b" not in reg.snapshot()

    def test_register_non_callable_raises(self) -> None:
        with pytest.raises(OpaBuiltinError, match="not callable"):
            BuiltinRegistry().register("x", 123)  # type: ignore[arg-type]

    def test_no_replace_conflict_raises(self) -> None:
        reg = BuiltinRegistry({"a": lambda: 1})
        with pytest.raises(OpaBuiltinError, match="already registered"):
            reg.register("a", lambda: 2, replace=False)

    def test_replace_by_default(self) -> None:
        reg = BuiltinRegistry({"a": lambda: 1})
        reg.register("a", lambda: 2)
        assert reg.snapshot()["a"]() == 2

    def test_unregister(self) -> None:
        reg = BuiltinRegistry({"a": lambda: 1})
        reg.unregister("a")
        assert reg.names() == []

    def test_unregister_missing_raises(self) -> None:
        with pytest.raises(OpaBuiltinError, match="not registered"):
            BuiltinRegistry().unregister("ghost")


class TestSprintf:
    def setup_method(self) -> None:
        self.sprintf = default_builtins()["sprintf"]

    def test_string_verb(self) -> None:
        assert self.sprintf("hello %s", ["world"]) == "hello world"

    def test_int_verb(self) -> None:
        assert self.sprintf("n=%d", [42]) == "n=42"

    def test_default_verb_scalar(self) -> None:
        assert self.sprintf("v=%v", ["x"]) == "v=x"

    def test_default_verb_container(self) -> None:
        # %v renders containers as JSON with Go-style spacing to match OPA.
        assert self.sprintf("v=%v", [{"k": 1}]) == 'v={"k": 1}'

    def test_bool_verb(self) -> None:
        assert self.sprintf("%v", [True]) == "true"

    def test_percent_literal(self) -> None:
        assert self.sprintf("100%%", []) == "100%"

    def test_multiple_operands(self) -> None:
        assert self.sprintf("%s=%d", ["age", 30]) == "age=30"

    def test_not_enough_args_raises(self) -> None:
        with pytest.raises(OpaBuiltinError, match="not enough"):
            self.sprintf("%s %s", ["only-one"])

    def test_non_string_format_raises(self) -> None:
        with pytest.raises(OpaBuiltinError, match="format must be a string"):
            self.sprintf(123, [])

    def test_non_list_operands_raises(self) -> None:
        with pytest.raises(OpaBuiltinError, match="must be an array"):
            self.sprintf("%s", "not-a-list")

    def test_type_verb(self) -> None:
        assert self.sprintf("%T", [42]) == "int"

    def test_bool_t_verb(self) -> None:
        assert self.sprintf("%t", [False]) == "false"

    def test_verb_fallback_on_type_mismatch(self) -> None:
        # %d with a string operand can't format; fall back to str().
        assert self.sprintf("%d", ["notnum"]) == "notnum"


class TestJsonIsValid:
    def setup_method(self) -> None:
        self.fn = default_builtins()["json.is_valid"]

    @pytest.mark.parametrize("doc,expected", [('{"a":1}', True), ("[1,2]", True), ("nope", False), ("", False)])
    def test_validity(self, doc: str, expected: bool) -> None:
        assert self.fn(doc) is expected

    def test_non_string_is_false(self) -> None:
        assert self.fn(123) is False


@pytest.mark.requires_yaml
class TestYamlBuiltins:
    def test_is_valid(self) -> None:
        fn = default_builtins()["yaml.is_valid"]
        assert fn("a: 1") is True
        assert fn("a: [unclosed") is False

    def test_is_valid_non_string_is_false(self) -> None:
        assert default_builtins()["yaml.is_valid"](123) is False

    def test_marshal_unmarshal_round_trip(self) -> None:
        marshal = default_builtins()["yaml.marshal"]
        unmarshal = default_builtins()["yaml.unmarshal"]
        value = {"a": 1, "b": [2, 3], "c": {"d": True}}
        assert unmarshal(marshal(value)) == value

    def test_unmarshal_non_string_raises(self) -> None:
        with pytest.raises(OpaBuiltinError, match="must be a string"):
            default_builtins()["yaml.unmarshal"](123)

    def test_unmarshal_invalid_raises(self) -> None:
        with pytest.raises(OpaBuiltinError, match="invalid YAML"):
            default_builtins()["yaml.unmarshal"]("a: [unclosed")
