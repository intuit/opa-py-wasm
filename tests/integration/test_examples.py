"""Smoke tests for the runnable ``examples/`` scripts.

Runs each example as a subprocess so they can't silently rot as the API evolves.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_wasm]

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"


@pytest.mark.parametrize("script", ["basic_authz.py", "pooled_eval.py", "custom_builtin.py"])
def test_example_runs(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.requires_yaml
def test_yaml_example_runs() -> None:
    result = subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / "yaml_builtin.py")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "valid_yaml: True" in result.stdout
