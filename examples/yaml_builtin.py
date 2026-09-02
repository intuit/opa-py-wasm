"""Use the default YAML builtins (requires the ``opa-py-wasm[yaml]`` extra).

The ``default_builtins`` fixture calls sprintf, json.is_valid and yaml.is_valid.

    uv run python examples/yaml_builtin.py

If PyYAML is not installed, invoking a YAML builtin raises a clear
OpaBuiltinError telling you to install the extra.
"""

from __future__ import annotations

from pathlib import Path

from opapywasm import OpaWasmPolicy
from opapywasm.errors import OpaBuiltinError

WASM = Path(__file__).parent.parent / "tests" / "fixtures" / "wasm" / "default_builtins.wasm"


def main() -> None:
    policy = OpaWasmPolicy.from_wasm_file(WASM)
    try:
        result = policy.evaluate({"name": "world", "doc": "a: 1\nb: [2, 3]"})
    except OpaBuiltinError as exc:
        print("YAML builtin unavailable:", exc)
        print("Install with:  uv add 'opa-py-wasm[yaml]'")
        return

    assert isinstance(result, dict)
    print("greeting  :", result["greeting"])
    print("valid_yaml:", result["valid_yaml"])
    print("valid_json:", result["valid_json"])


if __name__ == "__main__":
    main()
