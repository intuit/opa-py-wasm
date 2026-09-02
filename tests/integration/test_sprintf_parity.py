"""Differential parity for the host ``sprintf`` builtin.

The SDK's ``sprintf`` maps Go ``fmt`` verbs onto Python formatting and is
documented as best-effort. These vectors pin the behaviour we *do* claim to
match, and — when the OPA CLI is available — compare directly against native
``opa eval`` so drift is caught. Vectors that Go and Python format differently
are intentionally excluded (see docs/parity_with_opa_java_wasm.md).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from opapywasm.builtins import _sprintf


def _find_opa() -> str | None:
    """Locate the OPA CLI: $OPA_BIN, then the repo's .tools/opa, then PATH."""
    env_bin = os.environ.get("OPA_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin
    local = Path(__file__).resolve().parents[2] / ".tools" / "opa"
    if local.exists():
        return str(local)
    return shutil.which("opa")


# (format, operands, expected) — expected is the Go/OPA output we target. Every
# vector here is verified to match native `opa eval` (see the differential test
# below). Vectors where Python and Go formatting genuinely diverge are excluded
# and documented in docs/parity_with_opa_java_wasm.md.
VECTORS: list[tuple[str, list, str]] = [
    ("%s", ["hello"], "hello"),
    ("%d apples", [3], "3 apples"),
    ("%d + %d = %d", [1, 2, 3], "1 + 2 = 3"),
    ("%x", [255], "ff"),
    ("%X", [255], "FF"),
    ("%o", [8], "10"),
    ("100%%", [], "100%"),
    ("%v", [42], "42"),
    ("%v", [True], "true"),
    ("%v", [[1, 2, 3]], "[1, 2, 3]"),
    ("%v", [{"a": 1}], '{"a": 1}'),
    ("%q", ["hi"], '"hi"'),
    ("%5d", [3], "    3"),
    ("%05d", [42], "00042"),
    ("%.2f", [3.14159], "3.14"),
]


@pytest.mark.parametrize("fmt,operands,expected", VECTORS)
def test_sprintf_matches_expected(fmt: str, operands: list, expected: str) -> None:
    assert _sprintf(fmt, operands) == expected


_OPA = _find_opa()


@pytest.mark.skipif(_OPA is None, reason="OPA CLI not found ($OPA_BIN / .tools/opa / PATH)")
@pytest.mark.parametrize("fmt,operands,expected", VECTORS)
def test_sprintf_matches_native_opa(fmt: str, operands: list, expected: str) -> None:
    # Evaluate `sprintf(fmt, operands)` with the real OPA CLI and compare.
    query = f"sprintf({json.dumps(fmt)}, {json.dumps(operands)})"
    proc = subprocess.run(
        [_OPA, "eval", "-f", "json", query],  # type: ignore[list-item]
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    native = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
    assert _sprintf(fmt, operands) == native == expected
