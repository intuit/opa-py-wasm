"""Basic authorization: load a policy, set data, evaluate input.

Run from the repo root:

    uv run python examples/basic_authz.py
"""

from __future__ import annotations

from pathlib import Path

from opapywasm import OpaWasmPolicy, PolicyConfig

# A committed test fixture: allow iff data.roles[input.user] == "admin".
WASM = Path(__file__).parent.parent / "tests" / "fixtures" / "wasm" / "data_based.wasm"


def main() -> None:
    policy = OpaWasmPolicy.from_wasm_file(
        WASM,
        config=PolicyConfig(default_entrypoint="authz/allow"),
    )
    policy.set_data({"roles": {"alice": "admin", "bob": "viewer"}})

    for user in ("alice", "bob", "carol"):
        decision = policy.evaluate({"user": user})
        print(f"{user:6s} -> allow={decision}")

    # evaluate_raw returns the full OPA result set.
    print("raw(alice):", policy.evaluate_raw({"user": "alice"}))


if __name__ == "__main__":
    main()
