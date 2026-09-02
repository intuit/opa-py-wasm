"""Host builtin registry and defaults.

``BuiltinRegistry`` holds host-provided builtin callables. Registration mutates
under a lock and publishes an immutable snapshot; dispatch reads the snapshot
without holding the lock so user code never runs under the registry lock.

.. important::
   A registered builtin callable **may execute concurrently** — the same
   callable is shared by every pooled instance and is invoked on whichever
   application thread is doing an evaluation. Custom builtins must therefore be
   **thread-safe**: no unsynchronised shared mutable state, and they should be
   bounded (they run inside a policy's evaluation deadline) and must not retain
   guest pointers. Keep them pure/functional where possible; guard any shared
   state yourself.

Default builtins: ``sprintf``, ``json.is_valid``, ``yaml.is_valid``,
``yaml.marshal``, ``yaml.unmarshal`` (all thread-safe). The YAML builtins
require the optional ``opa-py-wasm[yaml]`` extra; if it is not installed, invoking
one raises :class:`~opapywasm.errors.OpaBuiltinError` with a clear message
rather than failing at import time.

Only builtins OPA cannot evaluate inline are delegated to the host, so a policy
that merely uses (say) ``json.is_valid`` may never call the host at all.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from types import MappingProxyType

from .errors import OpaBuiltinError
from .types import BuiltinCallable, JSONValue

__all__ = ["BuiltinRegistry", "default_builtins"]


# -- default builtin implementations ----------------------------------------

# Go fmt verbs OPA may emit in sprintf, mapped to Python %-format equivalents.
# %v (default format) and %+v/%#v are rendered via str(); the rest map directly.
_GO_VERB = re.compile(r"%[#+ 0-9.\-]*[vTtbcdoqxXUeEfFgGsp%]")


def _sprintf(fmt: str, operands: list[JSONValue]) -> str:
    """OPA ``sprintf(format, array)`` — Go-style formatting, **best-effort**.

    Covers the common Go ``fmt`` verbs (``%d %s %f %g %x %X %o %b %q %c %t %v %T``
    and flag/width/precision variants) by mapping them onto Python ``%``
    formatting; ``%v`` renders containers as JSON and booleans as ``true``/
    ``false`` so output is sensible for any JSON value. This is **not** a
    complete Go ``fmt`` reimplementation — exotic verbs and precise Go
    rounding/width edge cases may differ. See
    ``docs/parity_with_opa_java_wasm.md`` and the differential vectors in
    ``tests/integration/test_sprintf_parity.py``.
    """
    if not isinstance(fmt, str):
        raise OpaBuiltinError("sprintf: format must be a string")
    if not isinstance(operands, list):
        raise OpaBuiltinError("sprintf: second argument must be an array")

    it = iter(operands)
    out: list[str] = []
    pos = 0
    for match in _GO_VERB.finditer(fmt):
        out.append(fmt[pos : match.start()])
        pos = match.end()
        verb = match.group()
        if verb == "%%":
            out.append("%")
            continue
        try:
            operand = next(it)
        except StopIteration as exc:
            raise OpaBuiltinError("sprintf: not enough arguments for format") from exc
        out.append(_format_verb(verb, operand))
    out.append(fmt[pos:])
    return "".join(out)


def _format_verb(verb: str, operand: JSONValue) -> str:
    conv = verb[-1]
    if conv == "T":
        # %T: Go type name. We can only approximate from the JSON value.
        return type(operand).__name__
    if conv == "v":
        # %v: default representation. Render containers as JSON with Go-style
        # spacing (", " / ": ") to match OPA's output, and bools as true/false.
        if isinstance(operand, (dict, list)):
            return json.dumps(operand, separators=(", ", ": "))
        if isinstance(operand, bool):
            return "true" if operand else "false"
        return str(operand)
    if conv == "t":
        return "true" if operand else "false"
    if conv == "q":
        # %q: double-quoted string. json.dumps gives Go-compatible quoting for
        # plain strings; non-strings fall back to a quoted repr.
        if isinstance(operand, str):
            return json.dumps(operand)
        return json.dumps(str(operand))
    try:
        return verb % operand
    except (TypeError, ValueError):
        return str(operand)


def _json_is_valid(value: JSONValue) -> bool:
    """``json.is_valid(str)`` — true iff the string parses as JSON."""
    if not isinstance(value, str):
        return False
    try:
        json.loads(value)
    except ValueError:
        return False
    return True


def _require_yaml() -> object:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - exercised via error message test
        raise OpaBuiltinError("YAML builtins require the optional dependency; install opa-py-wasm[yaml]") from exc
    return yaml


def _yaml_is_valid(value: JSONValue) -> bool:
    """``yaml.is_valid(str)`` — true iff the string parses as YAML."""
    yaml = _require_yaml()
    if not isinstance(value, str):
        return False
    try:
        yaml.safe_load(value)  # type: ignore[attr-defined]
    except Exception:  # any parse error means "not valid"
        return False
    return True


def _yaml_marshal(value: JSONValue) -> str:
    """``yaml.marshal(x)`` — serialise a value to a YAML document string."""
    yaml = _require_yaml()
    return yaml.safe_dump(value, default_flow_style=False, sort_keys=False)  # type: ignore[attr-defined,no-any-return]


def _yaml_unmarshal(value: JSONValue) -> JSONValue:
    """``yaml.unmarshal(str)`` — parse a YAML document into a value."""
    yaml = _require_yaml()
    if not isinstance(value, str):
        raise OpaBuiltinError("yaml.unmarshal: argument must be a string")
    try:
        return yaml.safe_load(value)  # type: ignore[attr-defined,no-any-return]
    except Exception as exc:
        raise OpaBuiltinError(f"yaml.unmarshal: invalid YAML: {exc}") from exc


def default_builtins() -> dict[str, BuiltinCallable]:
    """Return the SDK's default host builtins as a ``{name: callable}`` map.

    YAML entries are always present; they raise a typed error at call time if
    PyYAML is not installed, rather than being silently absent.
    """
    return {
        "sprintf": _sprintf,
        "json.is_valid": _json_is_valid,
        "yaml.is_valid": _yaml_is_valid,
        "yaml.marshal": _yaml_marshal,
        "yaml.unmarshal": _yaml_unmarshal,
    }


# -- registry ----------------------------------------------------------------


class BuiltinRegistry:
    """Thread-safe registry of host builtin callables.

    Registration takes a lock and republishes an immutable snapshot dict.
    :meth:`snapshot` returns that dict for dispatch to read without holding the
    lock, so a (potentially slow or re-entrant) user callable never runs under
    the registry lock.
    """

    def __init__(self, initial: Mapping[str, BuiltinCallable] | None = None) -> None:
        self._lock = threading.Lock()
        self._builtins: dict[str, BuiltinCallable] = dict(initial or {})

    def register(self, name: str, fn: BuiltinCallable, *, replace: bool = True) -> None:
        """Register (or replace) a builtin by name.

        Args:
            replace: if ``False`` and ``name`` is already registered, raise.
        """
        if not callable(fn):
            raise OpaBuiltinError(f"builtin {name!r} is not callable")
        with self._lock:
            if not replace and name in self._builtins:
                raise OpaBuiltinError(f"builtin {name!r} is already registered")
            # Copy-on-write: publish a new dict so existing snapshots are stable.
            updated = dict(self._builtins)
            updated[name] = fn
            self._builtins = updated

    def unregister(self, name: str) -> None:
        with self._lock:
            if name not in self._builtins:
                raise OpaBuiltinError(f"builtin {name!r} is not registered")
            updated = dict(self._builtins)
            del updated[name]
            self._builtins = updated

    def snapshot(self) -> Mapping[str, BuiltinCallable]:
        """Return the current immutable snapshot for lock-free dispatch.

        The returned mapping is a read-only :class:`~types.MappingProxyType` view
        of the currently-published dict, so the "existing snapshots are stable"
        copy-on-write guarantee is enforced structurally: a caller cannot mutate
        the live registry through the snapshot, and a later ``register`` /
        ``unregister`` republishes a *new* dict, leaving this view unchanged.
        """
        with self._lock:
            return MappingProxyType(self._builtins)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._builtins)
