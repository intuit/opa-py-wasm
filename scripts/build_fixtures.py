#!/usr/bin/env python3
"""Dev-only fixture builder: compile the Rego sources in
``tests/fixtures/rego`` into OPA WebAssembly modules under
``tests/fixtures/wasm``.

This requires the OPA CLI (``opa``) to be installed and on ``PATH``. It is
**never** invoked at runtime or during normal test runs — the compiled
``.wasm`` outputs are committed to the repository so the test suite has no
dependency on the OPA CLI.

Usage::

    python scripts/build_fixtures.py            # build all fixtures
    python scripts/build_fixtures.py allow_true # build a single fixture
    OPA_BIN=/path/to/opa python scripts/build_fixtures.py

``opa build`` emits a bundle ``.tar.gz`` containing ``/policy.wasm``; we extract
just that wasm module and write it next to the others as ``<name>.wasm``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGO_DIR = REPO_ROOT / "tests" / "fixtures" / "rego"
WASM_DIR = REPO_ROOT / "tests" / "fixtures" / "wasm"

# Map each rego source (by stem) to the list of entrypoints to expose.
# Entrypoints are given in OPA "package/rule" form.
FIXTURE_ENTRYPOINTS: dict[str, list[str]] = {
    "allow_true": ["authz/allow"],
    "allow_false": ["authz/allow"],
    "input_based": ["authz/allow"],
    "data_based": ["authz/allow"],
    "multi_entrypoint": ["authz/allow", "authz/deny", "authz/roles"],
    "return_object": ["authz/result"],
    "return_array": ["authz/items"],
    "return_scalar": ["authz/message"],
    "undefined_result": ["authz/allow"],
    "null_result": ["authz/result"],
    "slow_loop": ["authz/result"],
    "default_builtins": ["authz/result"],
    "unsupported_builtin": ["authz/result"],
    "custom_builtin": ["authz/result"],
}

# Fixtures that reference host builtins OPA does not know about at compile time.
# For these we generate a capabilities file = current OPA capabilities plus the
# extra builtin declaration, so `opa build` type-checks the policy.
# Maps fixture stem -> the custom builtin name it invokes.
CUSTOM_BUILTIN_FIXTURES: dict[str, str] = {
    "custom_builtin": "my.custom_builtin",
}


def _opa_bin() -> str:
    opa = os.environ.get("OPA_BIN", "opa")
    resolved = shutil.which(opa)
    if resolved is None:
        raise SystemExit(
            f"OPA CLI not found (looked for {opa!r}). Install it or set OPA_BIN. "
            "See https://www.openpolicyagent.org/docs/latest/#running-opa"
        )
    return resolved


def _opa_version(opa: str) -> str:
    out = subprocess.run([opa, "version"], capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[0] if out.stdout else "unknown"


def _write_custom_capabilities(opa: str, builtin_name: str, dest: Path) -> Path:
    """Write a capabilities file = current OPA caps + one custom builtin decl.

    OPA's compiler type-checks builtin calls, so a policy invoking an unknown
    host builtin (registered only at run time by the SDK) won't compile without
    declaring it here. Generated on the fly — not committed — from
    ``opa capabilities --current``.
    """
    caps_json = subprocess.run([opa, "capabilities", "--current"], capture_output=True, text=True, check=True).stdout
    caps = json.loads(caps_json)
    caps.setdefault("builtins", []).append(
        {
            "name": builtin_name,
            "description": f"Test-only custom host builtin ({builtin_name}).",
            "decl": {
                "type": "function",
                "args": [{"type": "any", "name": "value"}],
                "result": {"type": "any", "name": "result"},
            },
        }
    )
    dest.write_text(json.dumps(caps))
    return dest


def build_one(opa: str, stem: str) -> Path:
    rego = REGO_DIR / f"{stem}.rego"
    if not rego.exists():
        raise SystemExit(f"no such rego source: {rego}")
    entrypoints = FIXTURE_ENTRYPOINTS[stem]

    WASM_DIR.mkdir(parents=True, exist_ok=True)
    out_wasm = WASM_DIR / f"{stem}.wasm"

    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "bundle.tar.gz"
        cmd = [opa, "build", "-t", "wasm"]
        for ep in entrypoints:
            cmd += ["-e", ep]
        if stem in CUSTOM_BUILTIN_FIXTURES:
            caps = _write_custom_capabilities(opa, CUSTOM_BUILTIN_FIXTURES[stem], Path(tmp) / "capabilities.json")
            cmd += ["--capabilities", str(caps)]
        cmd += ["-o", str(bundle), str(rego)]
        subprocess.run(cmd, check=True)

        with tarfile.open(bundle, "r:gz") as tar:
            member = None
            for m in tar.getmembers():
                if m.name.endswith("policy.wasm"):
                    member = m
                    break
            if member is None:
                raise SystemExit(f"bundle for {stem} did not contain policy.wasm")
            extracted = tar.extractfile(member)
            assert extracted is not None
            out_wasm.write_bytes(extracted.read())

    print(f"  {stem:20s} -> {out_wasm.relative_to(REPO_ROOT)} ({out_wasm.stat().st_size} bytes)")
    return out_wasm


def main(argv: list[str]) -> int:
    opa = _opa_bin()
    print(f"Using {opa} ({_opa_version(opa)})")

    stems = argv[1:] if len(argv) > 1 else sorted(FIXTURE_ENTRYPOINTS)
    unknown = [s for s in stems if s not in FIXTURE_ENTRYPOINTS]
    if unknown:
        raise SystemExit(f"unknown fixture(s): {', '.join(unknown)}")

    print(f"Building {len(stems)} fixture(s) into {WASM_DIR.relative_to(REPO_ROOT)}:")
    for stem in stems:
        build_one(opa, stem)
    print("Done. Remember to commit the generated .wasm files.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
