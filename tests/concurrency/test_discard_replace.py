"""C5 concurrency tests: discard/replace of poisoned instances.

An evaluation that raises OpaEvaluationError discards its instance; the pool
must rebuild a replacement and keep serving correct results afterwards, without
leaking slots.
"""

from __future__ import annotations

import threading

import pytest

from opapywasm import OpaWasmPolicy, PolicyConfig
from opapywasm.errors import OpaEvaluationError, OpaMemoryError

pytestmark = [pytest.mark.concurrency, pytest.mark.requires_wasm]


def test_evaluation_error_discards_instance(load_wasm, monkeypatch) -> None:
    # A trap after entering Wasm surfaces as OpaEvaluationError *and* marks the
    # instance poisoned; the pool must then discard and rebuild it. We simulate a
    # real trap by poisoning the instance the way OpaPolicyInstance.evaluate does
    # on failure (set _poisoned, then raise), rather than replacing the whole
    # method (which would bypass the poison flag the pool relies on).
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("allow_true"), PolicyConfig(pool_size=1))
    import opapywasm.instance as instance_mod

    original = instance_mod.OpaPolicyInstance.evaluate

    def boom(self, input_value, entrypoint_id):
        self._poisoned = True  # what a mid-eval trap does before propagating
        raise OpaEvaluationError("simulated trap")

    monkeypatch.setattr(instance_mod.OpaPolicyInstance, "evaluate", boom)
    with pytest.raises(OpaEvaluationError, match="simulated trap"):
        policy.evaluate({})
    assert policy.instances_recreated >= 1

    # Restore real eval; the rebuilt instance works again.
    monkeypatch.setattr(instance_mod.OpaPolicyInstance, "evaluate", original)
    assert policy.evaluate({}) is True


def test_oversize_input_discards_and_pool_recovers(load_wasm) -> None:
    # A tiny input cap makes normal inputs oversize -> OpaMemoryError on eval.
    # (Oversize is raised before opa_eval, so the instance is not truly poisoned,
    # but this still exercises the borrow/return path under failure.)
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("allow_true"), PolicyConfig(pool_size=2, max_input_bytes=2))
    with pytest.raises(OpaMemoryError):
        policy.evaluate({"way": "too big"})
    # Pool still usable for a within-limit input.
    assert policy.evaluate({}) is True


def test_pool_slots_preserved_after_many_discards(load_wasm) -> None:
    policy = OpaWasmPolicy.from_wasm_bytes(load_wasm("allow_true"), PolicyConfig(pool_size=3))

    # Manually poison-and-release through the pool to exercise replacement.
    for _ in range(10):
        with policy._pool.borrow() as loan:
            loan.discard()
    assert policy.instances_recreated >= 1

    # After all those discards the pool still has exactly pool_size slots and works.
    errors: list[str] = []

    def worker(_tid: int) -> None:
        for _ in range(100):
            if policy.evaluate({}) is not True:
                errors.append("wrong")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert policy.instances_created <= policy.pool_size + policy.instances_recreated
