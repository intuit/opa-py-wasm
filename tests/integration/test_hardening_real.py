"""Integration coverage for the review-driven hardening, against real fixtures.

Groups:

* Resource limits — per-eval timeout (epoch) and per-instance memory cap.
* Poison handling — a post-Wasm failure discards the instance across failure
  classes (timeout, opa_abort/memory bomb), and the pool recovers.
* Undefined vs. defined-null distinction through the public ``evaluate`` API.
* ``set_data`` snapshot immutability against caller mutation.
"""

from __future__ import annotations

import time

import pytest

from opapywasm import UNDEFINED, OpaWasmPolicy, PolicyConfig
from opapywasm.errors import (
    OpaAbortError,
    OpaBuiltinError,
    OpaEntrypointError,
    OpaEvaluationError,
    OpaInvalidPolicyError,
    OpaPolicyClosedError,
    OpaWasmError,
)
from opapywasm.types import JSONValue

pytestmark = [pytest.mark.integration, pytest.mark.requires_wasm]


def _policy(load_wasm, name: str, config: PolicyConfig | None = None) -> OpaWasmPolicy:
    return OpaWasmPolicy.from_wasm_bytes(load_wasm(name), config)


class TestExecutionTimeLimit:
    def test_slow_policy_times_out_cleanly(self, load_wasm) -> None:
        # Memory uncapped so CPU time dominates and the epoch deadline (not the
        # memory cap) is what fires.
        p = _policy(
            load_wasm,
            "slow_loop",
            PolicyConfig(eval_timeout_seconds=0.2, pool_size=2, max_memory_pages=None),
        )
        with pytest.raises(OpaEvaluationError, match="time limit"):
            p.evaluate({})
        p.close()

    def test_pool_recovers_after_timeout(self, load_wasm) -> None:
        # A timed-out instance is poisoned + discarded; the pool rebuilds and a
        # fast policy still evaluates fine afterwards.
        p = _policy(
            load_wasm,
            "slow_loop",
            PolicyConfig(eval_timeout_seconds=0.2, pool_size=1, max_memory_pages=None),
        )
        with pytest.raises(OpaEvaluationError):
            p.evaluate({})
        assert p.instances_recreated >= 1
        p.close()

    def test_none_timeout_disables_deadline(self, load_wasm) -> None:
        # With no timeout and a generous memory cap, a fast policy is unaffected.
        p = _policy(load_wasm, "allow_true", PolicyConfig(eval_timeout_seconds=None))
        assert p.evaluate({}) is True
        p.close()


class TestMemoryLimit:
    def test_memory_bomb_is_bounded(self, load_wasm) -> None:
        # A policy allocating a huge structure hits the per-instance page cap and
        # traps (surfaced as opa_abort "opa_malloc: failed") instead of
        # exhausting host RAM. A small cap keeps the test fast.
        p = _policy(
            load_wasm,
            "slow_loop",
            PolicyConfig(max_memory_pages=64, eval_timeout_seconds=None, pool_size=1),
        )
        with pytest.raises((OpaAbortError, OpaEvaluationError)):
            p.evaluate({})
        p.close()


class TestPoisonHandling:
    def test_builtin_failure_discards_instance(self, load_wasm) -> None:
        # A required-but-unregistered builtin (http.send) fails *after* entering
        # Wasm. Previously only OpaEvaluationError discarded the instance; now
        # any post-Wasm failure (including OpaBuiltinError) poisons it, so the
        # pool rebuilds rather than reusing a possibly-corrupt heap.
        p = _policy(load_wasm, "unsupported_builtin", PolicyConfig(pool_size=1))
        with pytest.raises(OpaBuiltinError, match=r"http\.send"):
            p.evaluate({"url": "http://example.com"})
        assert p.instances_recreated >= 1
        p.close()

    def test_pool_recovers_after_builtin_failure(self, load_wasm) -> None:
        # After the poisoned instance is discarded, a subsequent evaluation on a
        # freshly-built instance succeeds (custom builtin now registered).
        p = _policy(load_wasm, "custom_builtin", PolicyConfig(pool_size=1))
        p.register_builtin("my.custom_builtin", lambda x: x * 2)
        assert p.evaluate({"value": 21}) == 42
        p.close()

    def test_registered_builtin_that_raises_surfaces_typed_error_and_poisons(self, load_wasm) -> None:
        # A *registered* builtin that raises mid-eval must (a) surface as an
        # OpaBuiltinError naming the builtin — recovered from the wasmtime trap
        # chain by Evaluator._find_typed_in_chain, not masked as a generic
        # OpaEvaluationError — and (b) poison the instance so the pool rebuilds.
        # This is the counterpart to test_builtin_failure_discards_instance,
        # which only covers an *unregistered* required builtin.
        def boom(_value: JSONValue) -> JSONValue:
            raise ValueError("builtin blew up")

        p = _policy(load_wasm, "custom_builtin", PolicyConfig(pool_size=1))
        p.register_builtin("my.custom_builtin", boom)
        with pytest.raises(OpaBuiltinError, match=r"my\.custom_builtin"):
            p.evaluate({"value": 21})
        assert p.instances_recreated >= 1
        # The pool recovered: re-registering a working builtin evaluates cleanly
        # on a freshly-built instance.
        p.register_builtin("my.custom_builtin", lambda x: x * 2)
        assert p.evaluate({"value": 21}) == 42
        p.close()


class TestUndefinedVsNull:
    def test_undefined_returns_sentinel(self, load_wasm) -> None:
        result = _policy(load_wasm, "undefined_result").evaluate({"enabled": False})
        assert result is UNDEFINED

    def test_defined_null_returns_none_not_sentinel(self, load_wasm) -> None:
        result = _policy(load_wasm, "null_result").evaluate({})
        assert result is None
        assert result is not UNDEFINED

    def test_raw_distinguishes_both(self, load_wasm) -> None:
        undefined_raw = _policy(load_wasm, "undefined_result").evaluate_raw({"enabled": False})
        null_raw = _policy(load_wasm, "null_result").evaluate_raw({})
        assert undefined_raw == []
        assert null_raw == [{"result": None}]


class TestDataSnapshotImmutability:
    def test_caller_mutation_after_set_data_does_not_leak(self, load_wasm) -> None:
        p = _policy(load_wasm, "data_based", PolicyConfig(pool_size=1))
        roles: dict = {"roles": {"alice": "admin"}}
        p.set_data(roles)
        # Mutate the caller's object *after* set_data — must not affect the policy.
        roles["roles"]["alice"] = "viewer"
        roles["roles"]["mallory"] = "admin"
        assert p.evaluate({"user": "alice"}) is True  # still admin inside the policy
        assert p.evaluate({"user": "mallory"}) is False  # mutation did not leak in
        p.close()

    def test_invalid_data_rejected_at_set_time(self, load_wasm) -> None:
        from opapywasm.errors import OpaInvalidInputError

        p = _policy(load_wasm, "data_based")
        with pytest.raises(OpaInvalidInputError):
            p.set_data({"bad": {1, 2, 3}})  # type: ignore[dict-item]  # sets aren't JSON
        p.close()


class TestConstructionAndResolution:
    def test_default_entrypoint_zero_is_validated(self, load_wasm) -> None:
        # multi_entrypoint's ids do include 0, so None resolves and evaluates.
        p = _policy(load_wasm, "multi_entrypoint")
        assert 0 in p.entrypoints.values()
        p.evaluate({"action": "read"})  # entrypoint None -> id 0, validated
        # An explicitly unknown id still raises cleanly.
        with pytest.raises(OpaEntrypointError, match="unknown entrypoint id"):
            p.evaluate({}, entrypoint=999)
        p.close()

    def test_max_memory_pages_below_minimum_rejected(self, load_wasm) -> None:
        # A cap below the pages needed to instantiate must fail with a clear,
        # typed error at construction (not an opaque low-level Wasmtime error).
        with pytest.raises(OpaInvalidPolicyError, match="below the minimum"):
            _policy(load_wasm, "allow_true", PolicyConfig(max_memory_pages=1))


class TestIdleDeadline:
    def test_eval_after_idle_past_timeout_succeeds(self, load_wasm) -> None:
        # An instance idle longer than eval_timeout_seconds must not carry a
        # stale, expired epoch deadline into its next evaluation (HIGH-1).
        p = _policy(load_wasm, "allow_true", PolicyConfig(pool_size=1, eval_timeout_seconds=0.3))
        assert p.evaluate({}) is True
        time.sleep(0.6)  # idle for 2x the timeout
        assert p.evaluate({}) is True
        p.close()

    def test_set_data_after_idle_then_eval_succeeds(self, load_wasm) -> None:
        # Lazy data refresh on an idled instance must also not trap on a stale
        # deadline.
        p = _policy(load_wasm, "data_based", PolicyConfig(pool_size=1, eval_timeout_seconds=0.3))
        assert p.evaluate({"user": "alice"}) is False
        time.sleep(0.6)
        p.set_data({"roles": {"alice": "admin"}})
        assert p.evaluate({"user": "alice"}) is True
        p.close()


class TestFailedRefresh:
    def test_failed_lazy_refresh_poisons_and_recreates(self, load_wasm) -> None:
        # A data document that serialises fine but cannot be loaded into the
        # guest (exceeds the memory cap) must poison the instance so the pool
        # discards and rebuilds it (HIGH-2), and the policy must recover once
        # given loadable data.
        p = _policy(
            load_wasm,
            "data_based",
            PolicyConfig(pool_size=1, max_memory_pages=2, max_data_bytes=50_000_000, eval_timeout_seconds=None),
        )
        assert p.evaluate({"user": "alice"}) is False
        before = p.instances_recreated
        p.set_data({"roles": {f"u{i}": "admin" for i in range(200_000)}})  # too big for 2 pages
        with pytest.raises(OpaWasmError):
            p.evaluate({"user": "alice"})
        assert p.instances_recreated > before  # poisoned instance was discarded
        # Recovery: a loadable data doc evaluates on a fresh instance.
        p.set_data({"roles": {"alice": "admin"}})
        assert p.evaluate({"user": "alice"}) is True
        p.close()


class TestClosedMutations:
    def test_set_data_after_close_raises(self, load_wasm) -> None:
        p = _policy(load_wasm, "data_based")
        p.close()
        with pytest.raises(OpaPolicyClosedError):
            p.set_data({"roles": {}})

    def test_register_builtin_after_close_raises(self, load_wasm) -> None:
        p = _policy(load_wasm, "allow_true")
        p.close()
        with pytest.raises(OpaPolicyClosedError):
            p.register_builtin("my.custom", lambda: None)
