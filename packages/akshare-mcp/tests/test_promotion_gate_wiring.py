"""PromotionGate wiring in the incubation pipeline.

The A-class promotion gate was removed: the DSR verdict is still computed and
returned as evidence, but it no longer participates in any decision, so
``enabled`` is always False and the caller cannot downgrade on it. The env
toggle is deliberately ignored.
"""

from __future__ import annotations

import asyncio

from akshare_mcp.services.incubation_pipeline import StrategyIncubationPipelineService


class _FakeDB:
    def __init__(self, series, pool_size=10):
        self._series = series
        self._pool_size = pool_size

    async def list_signal_forward_returns(self, strategy_id, *, forward_days=5, lookback_days=None):
        return [{"actual_return": x} for x in self._series]

    async def list_strategies(self, status=None, limit=1000):
        return [{"id": f"s{i}"} for i in range(self._pool_size)]


def _run(coro):
    return asyncio.run(coro)


def test_promotion_gate_disabled_by_default(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_PROMOTION_DSR_ENABLED", raising=False)
    svc = StrategyIncubationPipelineService()
    db = _FakeDB([0.01] * 60)
    audit = _run(
        svc._evaluate_promotion_gate(db, "s1", signal_quality={"primary_horizon": 5}, observed_days=40)
    )
    assert audit["enabled"] is False


def test_promotion_gate_is_measured_but_never_participates(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_PROMOTION_DSR_ENABLED", "1")
    svc = StrategyIncubationPipelineService()
    import random

    random.seed(11)
    series = [random.gauss(0.002, 0.008) for _ in range(80)]
    db = _FakeDB(series, pool_size=15)
    audit = _run(
        svc._evaluate_promotion_gate(db, "s1", signal_quality={"primary_horizon": 5}, observed_days=40)
    )
    # The measurement is still produced...
    assert audit["n_trials"] == 15
    assert audit["dsr"] is not None
    assert "passed" in audit
    assert "eligible" in audit
    # ...but it does not participate in the decision, whatever the env says.
    assert audit["enabled"] is False
    assert audit["diagnostic_only"] is True


def test_promotion_gate_insufficient_samples_still_cannot_block(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_PROMOTION_DSR_ENABLED", "1")
    svc = StrategyIncubationPipelineService()
    db = _FakeDB([0.01] * 10, pool_size=5)
    audit = _run(
        svc._evaluate_promotion_gate(db, "s1", signal_quality={"primary_horizon": 5}, observed_days=8)
    )
    # A thin sample is still reported as not eligible, but it cannot hold a
    # strategy back any more because the verdict no longer participates.
    assert audit["enabled"] is False
    assert audit["diagnostic_only"] is True
    assert audit["eligible"] is False
    assert audit["passed"] is False
