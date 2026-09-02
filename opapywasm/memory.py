"""Linear-memory JSON marshalling (milestone C3).

``MemoryCodec`` converts between Python JSON-compatible values and OPA values
living in a single instance's Wasm linear memory, via the OPA
``opa_json_parse`` / ``opa_json_dump`` ABI functions. It also decodes the
``entrypoints()`` and ``builtins()`` metadata maps, and enforces the size
guards from :class:`~opapywasm.types.PolicyConfig`.

A ``MemoryCodec`` is bound to one instance's :class:`~opapywasm.abi.OpaAbi` and
is not thread-safe. Addresses returned/accepted here are opaque i32 handles into
that one instance's memory and must never be shared across instances.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .errors import OpaInvalidInputError, OpaMemoryError
from .types import JSONInput, JSONValue

if TYPE_CHECKING:
    from .abi import OpaAbi

__all__ = ["MemoryCodec"]

# Compact JSON separators — no whitespace, matching what OPA expects and keeping
# payloads small.
_COMPACT_SEPARATORS = (",", ":")

# Read chunk size when scanning Wasm memory for a NUL terminator.
_CSTRING_CHUNK = 4096


class MemoryCodec:
    """Python ↔ JSON ↔ Wasm-memory marshalling for one instance.

    Args:
        abi: the per-instance :class:`~opapywasm.abi.OpaAbi` this codec drives.
        max_input_bytes / max_data_bytes / max_result_bytes: size guards for
            serialised input, data, and result documents respectively.
        max_cstring_scan_bytes: hard cap on bytes scanned when reading a
            NUL-terminated string out of Wasm memory.
    """

    def __init__(
        self,
        abi: OpaAbi,
        *,
        max_input_bytes: int,
        max_data_bytes: int,
        max_result_bytes: int,
        max_cstring_scan_bytes: int,
    ) -> None:
        self._abi = abi
        self._max_input_bytes = max_input_bytes
        self._max_data_bytes = max_data_bytes
        self._max_result_bytes = max_result_bytes
        self._max_cstring_scan_bytes = max_cstring_scan_bytes

    # -- raw memory ----------------------------------------------------------

    def _check_range(self, addr: int, length: int, *, kind: str) -> None:
        """Validate that ``[addr, addr+length)`` is a sane in-bounds range.

        Guards against negative/zero pointers, negative lengths, integer
        overflow, and reads/writes past the end of linear memory. wasmtime
        ultimately backstops OOB *writes* (they trap) and clamps OOB *reads*, but
        clamping silently returns short data; validating here turns that into a
        clear :class:`OpaMemoryError` and rejects nonsensical addresses before
        touching the guest.
        """
        if addr < 0:
            raise OpaMemoryError(f"{kind}: negative guest address {addr}")
        if length < 0:
            raise OpaMemoryError(f"{kind}: negative length {length}")
        end = addr + length
        mem_len = self._abi.memory_len()
        if end > mem_len:
            raise OpaMemoryError(f"{kind}: range [{addr}, {end}) exceeds guest memory size {mem_len}")

    def _check_pointer(self, addr: int, *, kind: str) -> int:
        """Validate a guest-returned pointer is non-zero and non-negative.

        OPA allocation/serialisation exports signal failure with a NULL (0)
        pointer; treating 0 as a valid address would read from the start of
        linear memory and corrupt results silently.
        """
        if addr <= 0:
            raise OpaMemoryError(f"{kind}: OPA returned an invalid guest pointer {addr}")
        return addr

    def write_bytes(self, data: bytes) -> int:
        """Allocate ``len(data)`` bytes in the instance heap and write ``data``.

        Returns the address of the written buffer.
        """
        addr = self._check_pointer(self._abi.opa_malloc(len(data)), kind="opa_malloc")
        self._check_range(addr, len(data), kind="write_bytes")
        self._abi.memory_write(addr, data)
        return addr

    def read_bytes(self, addr: int, length: int) -> bytes:
        """Read ``length`` bytes starting at ``addr``."""
        self._check_range(addr, length, kind="read_bytes")
        return bytes(self._abi.memory_read(addr, addr + length))

    def read_cstring(self, addr: int) -> bytes:
        """Read a NUL-terminated byte string starting at ``addr``.

        Scans in chunks, bounded by ``max_cstring_scan_bytes`` so a missing
        terminator (corrupted/adversarial memory) cannot cause an unbounded scan.

        Raises:
            OpaMemoryError: if no NUL is found within the scan limit.
        """
        mem_len = self._abi.memory_len()
        if addr < 0 or addr >= mem_len:
            raise OpaMemoryError(f"read_cstring: start address {addr} outside guest memory size {mem_len}")
        out = bytearray()
        pos = addr
        while True:
            remaining = self._max_cstring_scan_bytes - len(out)
            if remaining <= 0:
                break  # hit the scan budget without finding a terminator
            # Bound each read by both the chunk size, the remaining scan budget,
            # and the end of linear memory so a NUL beyond the cap can't sneak in.
            end = min(pos + _CSTRING_CHUNK, pos + remaining, mem_len)
            if end <= pos:
                break  # reached end of linear memory without a terminator
            chunk = self._abi.memory_read(pos, end)
            nul = chunk.find(0)
            if nul != -1:
                out += chunk[:nul]
                return bytes(out)
            out += chunk
            pos = end
        raise OpaMemoryError(
            f"no NUL terminator found within {self._max_cstring_scan_bytes} bytes "
            f"while reading string at addr {addr}"
        )

    # -- JSON ----------------------------------------------------------------

    def _dumps(self, value: JSONValue) -> bytes:
        # allow_nan=False rejects NaN/Infinity/-Infinity: these are not valid
        # JSON and OPA's parser does not accept them, so emitting them would
        # produce a document OPA silently mishandles. Fail fast instead.
        try:
            return json.dumps(value, separators=_COMPACT_SEPARATORS, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise OpaInvalidInputError(f"value is not JSON-serialisable: {exc}") from exc

    def _coerce_to_json_bytes(self, value: JSONInput, *, validate: bool) -> bytes:
        """Normalise an input into compact JSON bytes.

        ``str``/``bytes`` are treated as already-serialised JSON *documents*:
        validated (parsed) when ``validate`` is true, else passed through. To
        send a JSON *string value*, pass a quoted document (``'"hello"'``) or a
        Python object; a bare word like ``hello`` is not valid JSON.
        Anything else is serialised with :func:`json.dumps`.
        """
        if isinstance(value, (str, bytes)):
            raw = value.encode("utf-8") if isinstance(value, str) else value
            if validate:
                try:
                    json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise OpaInvalidInputError(
                        f"input str/bytes is not valid JSON text: {exc}. "
                        "Pass a JSON document (e.g. '\"hello\"' for a string), or a Python object."
                    ) from exc
            return raw
        return self._dumps(value)

    def _check_size(self, raw: bytes, limit: int, kind: str) -> None:
        if len(raw) > limit:
            raise OpaMemoryError(f"{kind} is {len(raw)} bytes, exceeding the {limit}-byte limit")

    def encode_input(self, value: JSONInput, *, limit: int, validate: bool = True) -> bytes:
        """Serialise + size-check ``value`` to JSON bytes **without** touching guest memory.

        Pure Python (no guest calls), so it is safe to call before arming an
        evaluation deadline; the returned bytes are written to guest memory
        separately via :meth:`write_bytes`.
        """
        raw = self._coerce_to_json_bytes(value, validate=validate)
        self._check_size(raw, limit, "input")
        return raw

    def write_json(self, value: JSONInput, *, limit: int, kind: str, validate: bool = True) -> tuple[int, int]:
        """Serialise ``value`` to JSON bytes, size-check, and write to memory.

        Returns ``(addr, length)`` of the raw JSON buffer (not yet an OPA value).
        """
        raw = self._coerce_to_json_bytes(value, validate=validate)
        self._check_size(raw, limit, kind)
        return self.write_bytes(raw), len(raw)

    def read_json_cstring(self, addr: int, *, limit: int | None = None, kind: str = "result") -> JSONValue:
        """Read a NUL-terminated JSON string at ``addr`` and parse it.

        Args:
            addr: address of the NUL-terminated JSON produced by ``opa_json_dump``.
            limit: optional byte cap enforced before parsing (defaults to the
                configured result limit).
        """
        raw = self.read_cstring(addr)
        cap = self._max_result_bytes if limit is None else limit
        self._check_size(raw, cap, kind)
        try:
            return json.loads(raw)  # type: ignore[no-any-return]
        except ValueError as exc:
            raise OpaMemoryError(f"OPA emitted invalid JSON for {kind}: {exc}") from exc

    # -- OPA value conversion ------------------------------------------------

    def python_to_opa_value(self, value: JSONInput, *, limit: int, kind: str, validate: bool = True) -> int:
        """Convert a Python/JSON value into an OPA value; return its address.

        Steps: serialise → write raw bytes → ``opa_json_parse``. The raw string
        buffer is intentionally not freed here — it lives in the per-evaluation
        heap region that the caller resets wholesale (see DataManager/Evaluator).
        """
        raw_addr, raw_len = self.write_json(value, limit=limit, kind=kind, validate=validate)
        return self._check_pointer(self._abi.opa_json_parse(raw_addr, raw_len), kind=f"opa_json_parse:{kind}")

    def python_object_to_opa_value(self, value: JSONValue, *, limit: int, kind: str) -> int:
        """Convert a Python *value* into an OPA value; return its address.

        Unlike :meth:`python_to_opa_value`, a ``str`` here is treated as a JSON
        *string value* (always ``json.dumps``-ed), never as a raw JSON document.
        Used for builtin return values, which are plain Python objects.
        """
        raw = self._dumps(value)
        self._check_size(raw, limit, kind)
        return self._check_pointer(
            self._abi.opa_json_parse(self.write_bytes(raw), len(raw)), kind=f"opa_json_parse:{kind}"
        )

    def opa_value_to_python(self, value_addr: int, *, limit: int | None = None, kind: str = "result") -> JSONValue:
        """Convert an OPA value at ``value_addr`` into a Python value.

        Steps: ``opa_json_dump`` → read NUL-terminated string → ``json.loads``.
        """
        str_addr = self._check_pointer(self._abi.opa_json_dump(value_addr), kind=f"opa_json_dump:{kind}")
        return self.read_json_cstring(str_addr, limit=limit, kind=kind)

    # -- metadata ------------------------------------------------------------

    def decode_entrypoints(self) -> dict[str, int]:
        """Decode ``entrypoints()`` into a ``{name: id}`` mapping."""
        return self._decode_str_int_map(self._abi.entrypoints(), "entrypoints")

    def decode_builtins(self) -> dict[str, int]:
        """Decode ``builtins()`` into a ``{name: id}`` mapping."""
        return self._decode_str_int_map(self._abi.builtins(), "builtins")

    def _decode_str_int_map(self, value_addr: int, kind: str) -> dict[str, int]:
        decoded = self.opa_value_to_python(value_addr, kind=kind)
        if not isinstance(decoded, dict):
            raise OpaMemoryError(f"{kind}() did not decode to an object: {type(decoded).__name__}")
        result: dict[str, int] = {}
        for name, ident in decoded.items():
            if not isinstance(name, str) or not isinstance(ident, int) or isinstance(ident, bool):
                raise OpaMemoryError(f"{kind}() entry {name!r}={ident!r} is not a str->int mapping")
            result[name] = ident
        return result
