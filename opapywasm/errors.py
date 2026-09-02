"""Typed exception hierarchy for :mod:`opapywasm`.

Every public code path raises one of these typed errors rather than a generic
``RuntimeError`` / ``ValueError``. Callers can catch :class:`OpaWasmError` to
handle any failure originating from this SDK.

The hierarchy is intentionally flat: every concrete error derives directly from
:class:`OpaWasmError` so ``except OpaWasmError`` is a reliable catch-all.
"""

from __future__ import annotations

__all__ = [
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


class OpaWasmError(Exception):
    """Base class for every error raised by :mod:`opapywasm`."""


class OpaInvalidPolicyError(OpaWasmError):
    """The supplied bytes are not a valid/instantiable OPA policy module.

    Raised when the WebAssembly module fails to compile, is missing required
    imports/exports, or is otherwise structurally unusable as an OPA policy.
    """


class OpaAbiError(OpaWasmError):
    """The policy module's OPA Wasm ABI is missing, malformed, or unsupported.

    Raised on ABI version mismatch or when a required ABI export is absent.
    """


class OpaAbortError(OpaWasmError):
    """The policy invoked ``env.opa_abort`` with a diagnostic message.

    The message read from Wasm memory is preserved as the exception text.
    """


class OpaEvaluationError(OpaWasmError):
    """A policy evaluation failed for a reason not covered by a more specific error."""


class OpaBuiltinError(OpaWasmError):
    """A host builtin could not be dispatched or raised while executing.

    Raised when an unknown builtin id is requested, when a required builtin is
    unregistered, or when a registered builtin callable itself fails. The
    offending builtin name is included in the message where known.
    """


class OpaMemoryError(OpaWasmError):
    """A Wasm linear-memory operation failed or exceeded a configured guard.

    Raised for oversize input/data/result payloads and for unterminated or
    over-long C-strings encountered while reading Wasm memory.
    """


class OpaPoolTimeoutError(OpaWasmError):
    """Borrowing an instance from the pool timed out.

    Raised when no :class:`~opapywasm.instance.OpaPolicyInstance` became
    available within ``borrow_timeout_seconds``.
    """


class OpaPolicyClosedError(OpaWasmError):
    """The policy (and its pool) has been closed and can no longer be used."""


class OpaInvalidInputError(OpaWasmError):
    """The supplied ``input`` (or ``data``) value could not be marshalled.

    Raised when a raw JSON string is malformed or a Python value is not
    JSON-serialisable.
    """


class OpaEntrypointError(OpaWasmError):
    """The requested entrypoint name or id does not exist in the policy."""


class OpaDataError(OpaWasmError):
    """External ``data`` could not be loaded into a policy instance."""
