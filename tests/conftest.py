"""Shared pytest fixtures and helpers for the opapywasm test suite.

Provides:

* ``WASM_DIR`` / ``wasm_path`` — locate committed compiled ``.wasm`` fixtures.
* ``requires_wasm`` autouse handling — tests marked ``@pytest.mark.requires_wasm``
  are skipped when the compiled fixtures are absent (e.g. OPA CLI not yet run),
  so a fresh checkout still has a green unit-test run.
* ``requires_yaml`` — skip YAML-builtin tests when PyYAML is not installed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
WASM_DIR = FIXTURES_DIR / "wasm"
REGO_DIR = FIXTURES_DIR / "rego"

_YAML_AVAILABLE = importlib.util.find_spec("yaml") is not None


def wasm_bytes(name: str) -> bytes:
    """Return the compiled bytes for fixture ``name`` (without ``.wasm``)."""
    path = WASM_DIR / f"{name}.wasm"
    if not path.exists():
        pytest.skip(f"compiled wasm fixture {path.name!r} not present; run scripts/build_fixtures.py")
    return path.read_bytes()


@pytest.fixture
def wasm_dir() -> Path:
    return WASM_DIR


@pytest.fixture
def load_wasm():
    """Factory fixture returning the bytes of a named wasm fixture."""
    return wasm_bytes


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    have_any_wasm = WASM_DIR.exists() and any(WASM_DIR.glob("*.wasm"))
    skip_wasm = pytest.mark.skip(reason="no compiled wasm fixtures; run scripts/build_fixtures.py")
    skip_yaml = pytest.mark.skip(reason="PyYAML not installed (install opa-py-wasm[yaml])")
    for item in items:
        if "requires_wasm" in item.keywords and not have_any_wasm:
            item.add_marker(skip_wasm)
        if "requires_yaml" in item.keywords and not _YAML_AVAILABLE:
            item.add_marker(skip_yaml)
