"""OPA Wasm ABI adapter (milestone C2).

Two layers:

``OpaAbiSpec``
    Immutable, shareable knowledge about the OPA Wasm ABI: required export
    names, version-support policy, and capability rules. Safe to share across
    instances.

``OpaAbi``
    A per-instance adapter *bound* to one :class:`~opapywasm.runtime.WasmtimeRuntime`
    (its ``Store`` / ``Instance`` / ``Memory`` / exports). Never shared across
    instances — OPA value addresses and heap state are instance-local.

ABI facts (names, signatures, version semantics) come from the OPA Wasm ABI
documentation; see docs/opa_abi.md. Where the module varies by ABI version we
inspect exports and fail with :class:`OpaAbiError` rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import wasmtime

from .errors import OpaAbiError

if TYPE_CHECKING:
    from .runtime import WasmtimeRuntime

__all__ = ["OpaAbiSpec", "OpaAbi"]

# ABI version globals exported by every OPA policy module.
_ABI_VERSION_GLOBAL = "opa_wasm_abi_version"
_ABI_MINOR_GLOBAL = "opa_wasm_abi_minor_version"

# Exports required of every supported OPA policy, independent of ABI minor.
_REQUIRED_EXPORTS: tuple[str, ...] = (
    "opa_malloc",
    "opa_free",
    "opa_json_parse",
    "opa_json_dump",
    "opa_heap_ptr_get",
    "opa_heap_ptr_set",
    "entrypoints",
    "builtins",
)

# Capability-gating exports. Presence of these toggles the corresponding flag.
_OPA_EVAL_EXPORT = "opa_eval"
_EVAL_CTX_EXPORTS: tuple[str, ...] = (
    "opa_eval_ctx_new",
    "opa_eval_ctx_set_input",
    "opa_eval_ctx_set_data",
    "opa_eval_ctx_set_entrypoint",
    "eval",
    "opa_eval_ctx_get_result",
)
_HEAP_STASH_EXPORTS: tuple[str, ...] = (
    "opa_heap_blocks_stash",
    "opa_heap_blocks_restore",
    "opa_heap_stash_clear",
)
_VALUE_FREE_EXPORT = "opa_value_free"

# Expected Wasm signatures of the exports we actually call, as
# ``(param_valtypes, result_valtypes)`` in ``i32`` terms. Validated at
# construction so a mistyped export fails fast with a clear error rather than
# trapping mid-evaluation. OPA emits everything as i32 (memory offsets / sizes).
_I32 = "i32"
_EXPECTED_SIGNATURES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "opa_malloc": ((_I32,), (_I32,)),
    "opa_free": ((_I32,), ()),
    "opa_json_parse": ((_I32, _I32), (_I32,)),
    "opa_json_dump": ((_I32,), (_I32,)),
    "opa_heap_ptr_get": ((), (_I32,)),
    "opa_heap_ptr_set": ((_I32,), ()),
    "entrypoints": ((), (_I32,)),
    "builtins": ((), (_I32,)),
    # opa_eval(reserved, entrypoint, data, input, input_len, heap_ptr, format)
    "opa_eval": ((_I32,) * 7, (_I32,)),
}


@dataclass(frozen=True)
class OpaAbiSpec:
    """Immutable, shareable ABI policy for a compiled OPA module.

    Encodes which ABI versions this SDK supports and which exports are required.
    Holds no per-instance state, so a single spec is shared by every instance of
    a policy.

    Attributes:
        min_major / max_major: supported ABI major-version range (inclusive).
        min_minor_for_eval: the ABI minor version at which the one-shot
            ``opa_eval`` fast path becomes available. MVP requires ABI >= 1.2.
        required_exports: exports every supported policy must provide.
    """

    min_major: int = 1
    max_major: int = 1
    min_minor_for_eval: int = 2
    required_exports: tuple[str, ...] = field(default=_REQUIRED_EXPORTS)

    def check_version(self, major: int, minor: int) -> None:
        """Validate a detected (major, minor) ABI version against this spec.

        Raises:
            OpaAbiError: if the major version is outside the supported range.
        """
        if not (self.min_major <= major <= self.max_major):
            raise OpaAbiError(
                f"unsupported OPA Wasm ABI major version {major}.{minor}; " f"this SDK supports {self.min_major}.x"
            )

    def requires_eval_fast_path(self, minor: int) -> bool:
        """Whether the detected ABI minor version implies the ``opa_eval`` path."""
        return minor >= self.min_minor_for_eval


class OpaAbi:
    """Per-instance adapter binding an OPA module's ABI to one runtime.

    On construction it reads the ABI version globals, validates them against the
    :class:`OpaAbiSpec`, verifies required exports are present, and computes
    capability flags. It then offers typed wrappers around the OPA exports.

    Not thread-safe; bound to exactly one instance.
    """

    def __init__(self, runtime: WasmtimeRuntime, spec: OpaAbiSpec | None = None) -> None:
        self._rt = runtime
        self._spec = spec or OpaAbiSpec()

        self._abi_major, self._abi_minor = self._read_version()
        self._spec.check_version(self._abi_major, self._abi_minor)
        self._validate_required_exports()

        # Capability flags — derived once from the exports present.
        self._supports_opa_eval = runtime.has_export(_OPA_EVAL_EXPORT) and self._spec.requires_eval_fast_path(
            self._abi_minor
        )
        self._supports_eval_context = all(runtime.has_export(name) for name in _EVAL_CTX_EXPORTS)
        self._supports_heap_stash = all(runtime.has_export(name) for name in _HEAP_STASH_EXPORTS)
        self._supports_value_free = runtime.has_export(_VALUE_FREE_EXPORT)

        # This SDK evaluates exclusively via the opa_eval fast path (ABI >= 1.2).
        # The eval-context fallback is deliberately not implemented, so we fail
        # fast at construction if it is unavailable rather than letting a
        # context-only module construct and then error on first evaluate().
        if not self._supports_opa_eval:
            raise OpaAbiError(
                f"policy ABI {self._abi_major}.{self._abi_minor} does not expose the opa_eval "
                "fast path (requires ABI >= 1.2 with an 'opa_eval' export); this SDK does not "
                "implement the eval-context fallback"
            )

        # Validate the signatures of the exports we call, so a mistyped export is
        # a clear construction-time error, not a mid-eval trap.
        self._validate_export_signatures()

    # -- version & capabilities ---------------------------------------------

    @property
    def abi_version(self) -> tuple[int, int]:
        """Detected ``(major, minor)`` ABI version."""
        return (self._abi_major, self._abi_minor)

    @property
    def supports_opa_eval(self) -> bool:
        return self._supports_opa_eval

    @property
    def supports_eval_context(self) -> bool:
        return self._supports_eval_context

    @property
    def supports_heap_stash(self) -> bool:
        return self._supports_heap_stash

    @property
    def supports_value_free(self) -> bool:
        return self._supports_value_free

    # -- raw memory access (for the per-instance MemoryCodec) ----------------

    def memory_write(self, addr: int, data: bytes) -> None:
        """Write ``data`` into this instance's Wasm memory at ``addr``."""
        self._rt.memory.write(self._rt.store, data, addr)

    def memory_read(self, start: int, stop: int) -> bytearray:
        """Read this instance's Wasm memory over ``[start, stop)``."""
        data: bytearray = self._rt.memory.read(self._rt.store, start, stop)
        return data

    def memory_len(self) -> int:
        """Current size of this instance's Wasm memory, in bytes."""
        return int(self._rt.memory.data_len(self._rt.store))

    def set_epoch_deadline(self, ticks: int) -> None:
        """Arm this instance's per-evaluation epoch deadline (see runtime)."""
        self._rt.set_epoch_deadline(ticks)

    def clear_epoch_deadline(self) -> None:
        """Clear the per-evaluation deadline after a successful eval (see runtime)."""
        self._rt.clear_epoch_deadline()

    def _read_version(self) -> tuple[int, int]:
        """Read the ABI version globals.

        The minor global was added in ABI 1.1; if it is absent the minor version
        is treated as 0.

        Raises:
            OpaAbiError: if the mandatory major-version global is missing or not
                an integer global.
        """
        major_global = self._rt.exports().get(_ABI_VERSION_GLOBAL)
        if not isinstance(major_global, wasmtime.Global):
            raise OpaAbiError(f"policy is missing the {_ABI_VERSION_GLOBAL!r} ABI global; not an OPA policy")
        major = major_global.value(self._rt.store)

        minor_global = self._rt.exports().get(_ABI_MINOR_GLOBAL)
        minor = minor_global.value(self._rt.store) if isinstance(minor_global, wasmtime.Global) else 0

        if not isinstance(major, int) or not isinstance(minor, int):
            raise OpaAbiError("OPA ABI version globals are not integers")
        return major, minor

    def _validate_required_exports(self) -> None:
        missing = [name for name in self._spec.required_exports if not self._rt.has_export(name)]
        if missing:
            raise OpaAbiError(
                f"policy is missing required OPA ABI export(s): {', '.join(sorted(missing))} "
                f"(ABI {self._abi_major}.{self._abi_minor})"
            )

    def _validate_export_signatures(self) -> None:
        """Check that every export we call has the expected ``i32`` signature.

        A signature mismatch means the module is not the OPA ABI we target;
        surfacing it here (rather than as an opaque trap during evaluation) makes
        the failure actionable.
        """
        for name, (want_params, want_results) in _EXPECTED_SIGNATURES.items():
            if not self._rt.has_export(name):
                # opa_eval presence is already enforced; other names are in the
                # required-export set. Anything genuinely absent was caught above.
                continue
            func = self._rt.get_func(name)
            func_type = func.type(self._rt.store)
            got_params = tuple(str(v) for v in func_type.params)
            got_results = tuple(str(v) for v in func_type.results)
            if got_params != want_params or got_results != want_results:
                raise OpaAbiError(
                    f"OPA export {name!r} has unexpected signature "
                    f"{got_params}->{got_results}; expected {want_params}->{want_results}"
                )

    # -- typed export wrappers ----------------------------------------------
    #
    # Each wrapper calls the corresponding OPA export with the instance's store.
    # Addresses are opaque i32 handles into this instance's Wasm memory; callers
    # (MemoryCodec, DataManager, Evaluator) must never share them across
    # instances.

    def _call(self, name: str, *args: int) -> int:
        func = self._rt.get_func(name)
        return int(func(self._rt.store, *args))

    def opa_malloc(self, size: int) -> int:
        """Allocate ``size`` bytes in the instance heap; return the address."""
        return self._call("opa_malloc", size)

    def opa_free(self, addr: int) -> None:
        self._rt.get_func("opa_free")(self._rt.store, addr)

    def opa_json_parse(self, addr: int, length: int) -> int:
        """Parse JSON bytes at ``addr`` into an OPA value; return its address."""
        return self._call("opa_json_parse", addr, length)

    def opa_json_dump(self, value_addr: int) -> int:
        """Dump an OPA value to a NUL-terminated JSON string; return its address."""
        return self._call("opa_json_dump", value_addr)

    def opa_heap_ptr_get(self) -> int:
        return self._call("opa_heap_ptr_get")

    def opa_heap_ptr_set(self, addr: int) -> None:
        self._rt.get_func("opa_heap_ptr_set")(self._rt.store, addr)

    def entrypoints(self) -> int:
        """Return the address of the OPA value describing entrypoints (name->id)."""
        return self._call("entrypoints")

    def builtins(self) -> int:
        """Return the address of the OPA value describing builtins (name->id)."""
        return self._call("builtins")

    def opa_eval(
        self,
        entrypoint_id: int,
        data_addr: int,
        input_addr: int,
        input_len: int,
        heap_ptr: int,
        *,
        reserved: int = 0,
        result_format: int = 0,
    ) -> int:
        """One-shot evaluation (ABI 1.2+ fast path).

        Signature (per the OPA Wasm ABI):
        ``opa_eval(reserved, entrypoint_id, data, input, input_len, heap_ptr, format)``
        returning the address of a NUL-terminated JSON result string.

        The ``opa_eval`` fast path is guaranteed present: :class:`OpaAbi`
        construction rejects any module lacking it, so no runtime guard is
        needed here.
        """
        return self._call(
            _OPA_EVAL_EXPORT,
            reserved,
            entrypoint_id,
            data_addr,
            input_addr,
            input_len,
            heap_ptr,
            result_format,
        )
