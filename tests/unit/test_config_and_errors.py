"""Unit tests for the error taxonomy and PolicyConfig.

(Module-import / scaffolding smoke tests were dropped once the real C1/C2 tests
began importing and exercising these modules directly.)
"""

from __future__ import annotations

import dataclasses

import pytest

import opapywasm
from opapywasm import PolicyConfig
from opapywasm.errors import (
    OpaAbiError,
    OpaBuiltinError,
    OpaPoolTimeoutError,
    OpaWasmError,
)


class TestErrorTaxonomy:
    def test_all_errors_subclass_base(self) -> None:
        # Every exported ``*Error`` type must derive from OpaWasmError.
        for name in opapywasm.__all__:
            obj = getattr(opapywasm, name)
            if isinstance(obj, type) and name.endswith("Error") and name != "OpaWasmError":
                assert issubclass(obj, OpaWasmError), name

    def test_specific_errors_are_distinct(self) -> None:
        assert not issubclass(OpaAbiError, OpaBuiltinError)
        assert not issubclass(OpaBuiltinError, OpaAbiError)
        assert issubclass(OpaPoolTimeoutError, OpaWasmError)


class TestPolicyConfig:
    def test_defaults(self) -> None:
        cfg = PolicyConfig()
        assert cfg.pool_size == 4
        assert cfg.borrow_timeout_seconds == 5.0
        assert cfg.strict_result is False

    def test_is_frozen(self) -> None:
        cfg = PolicyConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.pool_size = 10  # type: ignore[misc]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"pool_size": 0},
            {"borrow_timeout_seconds": -1.0},
            {"max_input_bytes": 0},
            {"max_result_bytes": -5},
        ],
    )
    def test_invalid_config_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            PolicyConfig(**kwargs)

    def test_none_timeout_allowed(self) -> None:
        assert PolicyConfig(borrow_timeout_seconds=None).borrow_timeout_seconds is None
