"""Shared types and configuration for :mod:`opapywasm`.

Contains no runtime Wasm logic — only value types, the public
:class:`PolicyConfig`, and type aliases used across the package.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Union

__all__ = [
    "JSONValue",
    "JSONInput",
    "BuiltinCallable",
    "PolicyConfig",
    "Undefined",
    "UNDEFINED",
]


class Undefined(Enum):
    """Sentinel type for an *undefined* OPA decision (an empty result set).

    A single member, :data:`UNDEFINED`, is returned by
    :meth:`~opapywasm.policy.OpaWasmPolicy.evaluate` when the policy produces no
    result. It is deliberately distinct from ``None`` so that a policy which
    genuinely returns JSON ``null`` (decoded to ``None``) can be told apart from
    one that is simply undefined — see the OPA distinction between an undefined
    document and a defined ``null`` value.

    Implemented as a single-member :class:`enum.Enum` so it is a true singleton
    (``UNDEFINED is UNDEFINED``), has a readable ``repr``, and type-narrows
    cleanly (``value is UNDEFINED``).
    """

    token = 0

    def __repr__(self) -> str:
        return "UNDEFINED"

    def __bool__(self) -> bool:
        # Undefined is falsy, so `if policy.evaluate(...):` behaves intuitively.
        return False


#: The singleton undefined-decision sentinel (see :class:`Undefined`).
UNDEFINED = Undefined.token

# A JSON-compatible Python value. ``mypy`` cannot express a truly recursive
# alias cleanly across all supported versions, so the containers use the
# forward string form which resolves recursively. ``Union`` is used (rather
# than ``X | Y``) because these are runtime alias values, and a self-referential
# ``|`` alias is awkward to spell portably.
JSONValue = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]

# The wrapper accepts either an already-serialised JSON document (``str`` /
# ``bytes``) or a JSON-compatible Python object for ``input`` and ``data``.
JSONInput = Union[JSONValue, str, bytes]

# A host builtin: receives already-decoded Python arguments and returns a
# JSON-compatible Python value. Arity is fixed at registration/dispatch time by
# the number of OPA value addresses supplied to ``opa_builtinN``.
BuiltinCallable = Callable[..., JSONValue]


@dataclass(frozen=True)
class PolicyConfig:
    """Immutable configuration for an :class:`~opapywasm.policy.OpaWasmPolicy`.

    Attributes:
        pool_size:
            Number of :class:`~opapywasm.instance.OpaPolicyInstance` objects to
            pre-instantiate and pool. Bounds the maximum concurrent evaluations.
        default_entrypoint:
            Entrypoint (name or numeric id) used when ``evaluate`` is called
            without an explicit ``entrypoint``. ``None`` means the policy's
            default entrypoint (id ``0``) is used.
        borrow_timeout_seconds:
            Maximum time to wait for a free pooled instance before raising
            :class:`~opapywasm.errors.OpaPoolTimeoutError`. ``None`` blocks
            forever.
        strict_result:
            When ``True``, ``evaluate`` raises rather than returning the
            ``UNDEFINED`` sentinel if the OPA result set is empty (undefined
            decision). When ``False``, an undefined decision returns
            :data:`~opapywasm.types.UNDEFINED` (which is distinct from a defined
            JSON ``null``, decoded as Python ``None``).
        max_input_bytes:
            Reject serialised ``input`` larger than this many bytes.
        max_data_bytes:
            Reject serialised ``data`` larger than this many bytes.
        max_result_bytes:
            Reject OPA result documents larger than this many bytes.
        max_cstring_scan_bytes:
            Upper bound on the number of bytes scanned when reading a
            NUL-terminated string from Wasm memory. Guards against unbounded
            scans on corrupted/adversarial memory.
        parse_raw_json_input:
            When ``True`` (default), a ``str``/``bytes`` ``input`` is validated
            as JSON before being written to Wasm memory. When ``False`` it is
            passed through verbatim (faster, but a malformed document surfaces
            as an OPA-side error).
        max_memory_pages:
            Hard cap on the size (in 64 KiB Wasm pages) the instance's linear
            memory may grow to. A policy that tries to allocate beyond this traps
            rather than exhausting host RAM. ``None`` leaves the memory
            unbounded. Guards against memory-bomb policies (per-instance worst
            case ≈ ``pool_size * max_memory_pages * 64 KiB``). Must be at least
            2 (the pages needed to instantiate an OPA policy); a smaller value is
            rejected at construction with :class:`~opapywasm.errors.OpaInvalidPolicyError`.
        eval_timeout_seconds:
            Wall-clock deadline for a single :meth:`evaluate` call, enforced via
            Wasmtime epoch interruption. A policy that loops forever traps with
            :class:`~opapywasm.errors.OpaEvaluationError` instead of hanging the
            calling thread. ``None`` disables the deadline. Enforcement is
            best-effort at ~10 ms granularity. **This bounds guest Wasm execution
            only — it cannot interrupt a Python host builtin** (see
            :meth:`~opapywasm.policy.OpaWasmPolicy.register_builtin`); a builtin
            that blocks must enforce its own timeout.
        strict_result_shape:
            When ``True``, a *non-empty* result set whose shape is not the
            expected ``[{"result": ...}]`` raises
            :class:`~opapywasm.errors.OpaEvaluationError` rather than being
            treated as undefined. Catches ABI corruption / unexpected shapes.
    """

    pool_size: int = 4
    default_entrypoint: str | int | None = None
    borrow_timeout_seconds: float | None = 5.0
    strict_result: bool = False
    max_input_bytes: int = 1_000_000
    max_data_bytes: int = 10_000_000
    max_result_bytes: int = 10_000_000
    max_cstring_scan_bytes: int = 10_000_000
    parse_raw_json_input: bool = True
    max_memory_pages: int | None = 4096  # 256 MiB per instance
    eval_timeout_seconds: float | None = 30.0
    strict_result_shape: bool = True

    def __post_init__(self) -> None:
        if self.pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        if self.borrow_timeout_seconds is not None and self.borrow_timeout_seconds < 0:
            raise ValueError("borrow_timeout_seconds must be >= 0 or None")
        if self.eval_timeout_seconds is not None and self.eval_timeout_seconds <= 0:
            raise ValueError("eval_timeout_seconds must be > 0 or None")
        if self.max_memory_pages is not None and self.max_memory_pages < 1:
            raise ValueError("max_memory_pages must be >= 1 or None")
        for name in (
            "max_input_bytes",
            "max_data_bytes",
            "max_result_bytes",
            "max_cstring_scan_bytes",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
