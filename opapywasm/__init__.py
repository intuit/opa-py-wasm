"""opapywasm — in-process, thread-safe Python SDK for evaluating OPA policies
compiled to WebAssembly, built on ``wasmtime.py``.

Public API (see :class:`~opapywasm.policy.OpaWasmPolicy`)::

    from opapywasm import OpaWasmPolicy, PolicyConfig

    policy = OpaWasmPolicy.from_wasm_file("policy.wasm")
    policy.set_data({"roles": {"alice": "admin"}})
    decision = policy.evaluate({"user": "alice", "action": "delete"})

The ``OpaWasmPolicy`` facade owns a bounded, thread-safe pool of single-use
Wasm instances; application threads own request-level concurrency.
"""

from __future__ import annotations

from .builtins import BuiltinRegistry
from .errors import (
    OpaAbiError,
    OpaAbortError,
    OpaBuiltinError,
    OpaDataError,
    OpaEntrypointError,
    OpaEvaluationError,
    OpaInvalidInputError,
    OpaInvalidPolicyError,
    OpaMemoryError,
    OpaPolicyClosedError,
    OpaPoolTimeoutError,
    OpaWasmError,
)
from .policy import OpaWasmPolicy
from .types import UNDEFINED, BuiltinCallable, JSONInput, JSONValue, PolicyConfig, Undefined

__all__ = [
    # Facade — a bounded, thread-safe pool of policy instances.
    "OpaWasmPolicy",
    "BuiltinRegistry",
    # Configuration & types
    "PolicyConfig",
    "JSONValue",
    "JSONInput",
    "BuiltinCallable",
    "Undefined",
    "UNDEFINED",
    # Errors
    "OpaWasmError",
    "OpaInvalidPolicyError",
    "OpaAbiError",
    "OpaAbortError",
    "OpaEvaluationError",
    "OpaBuiltinError",
    "OpaMemoryError",
    "OpaPoolTimeoutError",
    "OpaPolicyClosedError",
    "OpaInvalidInputError",
    "OpaEntrypointError",
    "OpaDataError",
]

# Sourced from installed package metadata so it can never drift from
# pyproject.toml. Falls back to "0.0.0+unknown" when running from a source tree
# that was never installed (e.g. some editable/CI layouts).
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        __version__ = _pkg_version("opa-py-wasm")
    except PackageNotFoundError:  # pragma: no cover - only in an uninstalled tree
        __version__ = "0.0.0+unknown"
except ImportError:  # pragma: no cover - importlib.metadata always present on 3.10+
    __version__ = "0.0.0+unknown"
