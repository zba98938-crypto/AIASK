"""INVERT-DESIGN P3 改动B：PromotionGate 接入孵化流水线的接线测试。

聚焦新接线面 StrategyIncubationPipelineService._evaluate_promotion_gate：
- toggle OFF（默认）→ enabled=False（零变化，不阻断晋升）。
- toggle ON + 前向序列存在 → 跑 DSR，返回 enabled=True + passed/eligible。
- toggle ON + 序列样本不足 → eligible=False（不阻断）。
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


def test_promotion_gate_enabled_runs_dsr(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_PROMOTION_DSR_ENABLED", "1")
    svc = StrategyIncubationPipelineService()
    # 强正偏置序列 → 大概率 DSR 较高；本测只验证接线产出结构，不强断言通过与否。
    import random

    random.seed(11)
    series = [random.gauss(0.002, 0.008) for _ in range(80)]
    db = _FakeDB(series, pool_size=15)
    audit = _run(
        svc._evaluate_promotion_gate(db, "s1", signal_quality={"primary_horizon": 5}, observed_days=40)
    )
    assert audit["enabled"] is True
    assert audit["eligible"] is True
    assert audit["n_trials"] == 15
    assert audit["dsr"] is not None
    assert "passed" in audit


def test_promotion_gate_insufficient_samples_not_eligible(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_PROMOTION_DSR_ENABLED", "1")
    svc = StrategyIncubationPipelineService()
    db = _FakeDB([0.01] * 10, pool_size=5)
    audit = _run(
        svc._evaluate_promotion_gate(db, "s1", signal_quality={"primary_horizon": 5}, observed_days=8)
    )
    assert audit["enabled"] is True
    assert audit["eligible"] is False
    assert audit["passed"] is False
