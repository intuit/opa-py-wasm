"""Register and invoke a custom host builtin.

The ``custom_builtin`` fixture calls ``my.custom_builtin(input.value)``; the host
supplies the implementation.

    uv run python examples/custom_builtin.py
"""

from __future__ import annotations

from pathlib import Path

from opapywasm import OpaWasmPolicy
from opapywasm.types import JSONValue

WASM = Path(__file__).parent.parent / "tests" / "fixtures" / "wasm" / "custom_builtin.wasm"


def double_it(value: JSONValue) -> JSONValue:
    """A trivial custom builtin: receives a decoded Python value, returns JSON."""
    if not isinstance(value, (int, float)):
        return {"error": "expected a number"}
    return {"doubled": value * 2, "original": value}


def main() -> None:
    policy = OpaWasmPolicy.from_wasm_file(WASM)
    print("required builtins:", policy.builtins)

    policy.register_builtin("my.custom_builtin", double_it)
    print("registered builtins:", policy.registered_builtins)

    print("result:", policy.evaluate({"value": 21}))


if __name__ == "__main__":
    main()
