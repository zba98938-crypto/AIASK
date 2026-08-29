from __future__ import annotations

import asyncio
from datetime import date

import pytest

from akshare_mcp.services.strategy_lifecycle_shared.overview import build_incubation_overview
from akshare_mcp.services.strategy_lifecycle_shared.overview import _signal_stats_cache_signature


class _OverviewCacheDb:
    signal_stats = {}

    async def get_latest_strategy_quality_report(self, strategy_id: str):
        assert strategy_id == "strategy-cache"
        return {"updated_at": "2026-04-21T08:00:00+00:00"}

    async def get_latest_execution_audit_snapshot(self, strategy_id: str):
        assert strategy_id == "strategy-cache"
        return {
            "snapshot_id": "eas-cache",
            "as_of": date.today().isoformat(),
            "correlation_id": "corr-cache",
            "factory_run_id": "factory-cache",
            "verdict": {"status": "passed", "reasons": [], "hard_gate_passed": True},
        }

    async def get_latest_strategy_closure_snapshot(self, strategy_id: str, snapshot_type: str = "incubation_overview"):
        assert strategy_id == "strategy-cache"
        assert snapshot_type == "incubation_overview"
        return {
            "snapshot_id": "cls-cache",
            "as_of": date.today().isoformat(),
            "metadata": {
                "strategy_status": "incubating",
                "quality_report_updated_at": "2026-04-21T08:00:00+00:00",
                "execution_audit_snapshot_id": "eas-cache",
                "signal_stats_signature": _signal_stats_cache_signature(self.signal_stats),
            },
            "snapshot": {
                "strategy_id": strategy_id,
                "status": "incubating",
                "pipeline_stage": "candidate",
                "promotion_gate_status": "passed",
                "execution_audit_snapshot": {
                    "snapshot_id": "eas-cache",
                },
            },
        }

    async def get_strategy_metrics(self, strategy_id: str):
        raise AssertionError("cached overview should not recompute metrics")

    async def get_signal_stats(self, strategy_id: str):
        assert strategy_id == "strategy-cache"
        return dict(self.signal_stats)


def test_incubation_overview_prefers_fresh_closure_snapshot():
    result = asyncio.run(
        build_incubation_overview(
            _OverviewCacheDb(),
            {"id": "strategy-cache", "status": "incubating"},
        )
    )

    assert result["strategy_id"] == "strategy-cache"
    assert result["cached"] is True
    assert result["recomputed"] is False
    assert result["closure_snapshot_id"] == "cls-cache"
    assert result["execution_audit_snapshot_id"] == "eas-cache"
    assert result["correlation_id"] == "corr-cache"
    assert result["factory_run_id"] == "factory-cache"


class _StaleSignalCacheDb(_OverviewCacheDb):
    signal_stats = {
        "raw_signal_count": 1,
        "signals_with_forward_returns_count": 1,
        "observed_forward_return_count": 2,
        "coverage_ratio": 1.0,
        "sample_count": {1: 1, 5: 1},
        "effective_n": {1: 1, 5: 1},
        "hit_rate": {1: 1.0, 5: 1.0},
    }

    async def get_latest_strategy_closure_snapshot(self, strategy_id: str, snapshot_type: str = "incubation_overview"):
        cached = await super().get_latest_strategy_closure_snapshot(strategy_id, snapshot_type=snapshot_type)
        cached["metadata"]["signal_stats_signature"] = {}
        return cached

    async def get_strategy_metrics(self, strategy_id: str):
        raise AssertionError("stale overview should recompute metrics")


def test_incubation_overview_recomputes_when_signal_stats_signature_changes():
    with pytest.raises(AssertionError, match="stale overview should recompute metrics"):
        asyncio.run(
            build_incubation_overview(
                _StaleSignalCacheDb(),
                {"id": "strategy-cache", "status": "incubating"},
            )
        )
