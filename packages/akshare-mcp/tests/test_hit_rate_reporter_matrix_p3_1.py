"""P3-1 集成单测：HitRateReporter.generate 输出含命中率矩阵（复用 verifications）。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 3 · P3-1
"""

from __future__ import annotations

import asyncio

from akshare_mcp.services.incubation_factory.hit_rate_reporter import HitRateReporter


class _Db:
    async def save_incubation_hit_rate_report(self, *_a, **_k):
        return None

    async def list_strategies(self, *_a, **_k):
        return []

    def __getattr__(self, _name):
        # 任意未知持久化方法都返回 no-op async
        async def _noop(*_a, **_k):
            return None
        return _noop


def test_report_contains_matrix():
    reporter = HitRateReporter()
    strategies = [
        {"id": "s1", "strategy_type": "momentum", "holding_period_bucket": "medium"},
        {"id": "s2", "strategy_type": "momentum", "holding_period_bucket": "medium"},
    ]
    verifications = {
        "s1": {
            "primary_effective_n": 10,
            "primary_hit_rate": 0.6,
            "primary_skill_lcb": 0.0,
            "forward_sharpe": 0.1,
            "strategy_type": "momentum",
            "hit_rate_by_regime": {
                "trend_regime": {"trend_up": {"hit_rate": 0.6, "skill_lcb": 0.0, "n": 10}},
                "vol_regime": {},
                "sentiment_regime": {},
            },
        },
        "s2": {
            "primary_effective_n": 10,
            "primary_hit_rate": 0.8,
            "primary_skill_lcb": 0.1,
            "forward_sharpe": 0.2,
            "strategy_type": "momentum",
            "hit_rate_by_regime": {
                "trend_regime": {"trend_up": {"hit_rate": 0.8, "skill_lcb": 0.1, "n": 10}},
                "vol_regime": {},
                "sentiment_regime": {},
            },
        },
    }

    async def _scenario():
        return await reporter.generate(_Db(), strategies, verifications, {"auto_promoted": 0, "stage_counts": {}})

    report = asyncio.run(_scenario())
    dashboard = report["hit_rate_dashboard"]
    assert "matrix" in dashboard
    matrix = dashboard["matrix"]["matrix"]
    cell = matrix["momentum"]["medium"]["trend_regime"]["trend_up"]
    assert cell["status"] == "ok"
    assert cell["hit_rate"] == 0.7
    assert cell["n"] == 20


def test_report_summary_counts_paper_observation_and_intraday_sync():
    reporter = HitRateReporter()
    strategies = [
        {"id": "incubating-1", "strategy_type": "momentum", "_intake_stage": "incubating"},
        {"id": "paper-1", "strategy_type": "breakout", "_intake_stage": "paper"},
        {"id": "diagnostic-1", "strategy_type": "value", "_intake_stage": "diagnostic"},
    ]

    async def _scenario():
        return await reporter.generate(
            _Db(),
            strategies,
            {},
            {"auto_promoted": 0, "stage_counts": {"warmup": 1}},
            trade_prediction_result={
                "status": "ok",
                "evaluated": 1,
                "intraday_evaluated": 1,
                "intraday_sync": {"attempted": 1, "succeeded": 1, "failed": 0},
            },
        )

    report = asyncio.run(_scenario())
    assert report["summary"]["total_incubating"] == 3
    phase_result = report["trade_prediction_dashboard"]["phase_result"]
    assert phase_result["intraday_sync"]["attempted"] == 1
    assert phase_result["intraday_evaluated"] == 1
