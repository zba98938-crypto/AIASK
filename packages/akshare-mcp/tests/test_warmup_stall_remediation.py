"""Tests for P-1 warmup-stall remediation in observe-pool governance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from akshare_mcp.services.strategy_lifecycle_shared.incubation import (
    resolve_incubation_action_plan,
)


class _NoMetricsDB:
    """db stub without incubation metrics → stage_clock_days falls back to created_at."""

    pass


def _old_strategy(days_ago: int) -> dict:
    created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"id": "obs-stall-1", "created_at": created}


@pytest.mark.asyncio
async def test_warmup_stall_low_evidence_freezes_after_threshold() -> None:
    """有信号但滞留 >=45 天且 effective_n<12 → freeze_and_revise(释放 observe 池)。"""
    plan = await resolve_incubation_action_plan(
        _NoMetricsDB(),
        _old_strategy(50),
        pipeline_stage="warmup",
        signal_quality={"primary_effective_n": 6, "coverage_ratio": 0.4},
        total_signals=8,  # 有信号,区别于 signal_vacuum
        validation_grade="B",
    )
    assert plan["remediation_action"] == "freeze_and_revise"
    assert plan["remediation_reason"] == "warmup_stall_low_evidence"
    assert plan["revision_required"] is True


@pytest.mark.asyncio
async def test_warmup_recent_sample_not_frozen() -> None:
    """滞留天数不够(刚入池) → 不冻结,继续观察。"""
    plan = await resolve_incubation_action_plan(
        _NoMetricsDB(),
        _old_strategy(10),
        pipeline_stage="warmup",
        signal_quality={"primary_effective_n": 6, "coverage_ratio": 0.4},
        total_signals=8,
        validation_grade="B",
    )
    assert plan["remediation_reason"] != "warmup_stall_low_evidence"
    assert plan["revision_required"] is False


@pytest.mark.asyncio
async def test_warmup_stall_with_growing_evidence_not_frozen() -> None:
    """滞留久但 effective_n 已达标(仍在积累且证据足) → 不冻结,避免误杀好样本。"""
    plan = await resolve_incubation_action_plan(
        _NoMetricsDB(),
        _old_strategy(60),
        pipeline_stage="warmup",
        signal_quality={"primary_effective_n": 18, "coverage_ratio": 0.6},
        total_signals=25,
        validation_grade="B",
    )
    assert plan["remediation_reason"] != "warmup_stall_low_evidence"


@pytest.mark.asyncio
async def test_signal_vacuum_still_takes_precedence_for_zero_signals() -> None:
    """0 信号样本仍走原 signal_vacuum 路径,不被新 warmup_stall 逻辑接管。"""
    plan = await resolve_incubation_action_plan(
        _NoMetricsDB(),
        _old_strategy(50),
        pipeline_stage="warmup",
        signal_quality={"primary_effective_n": 0},
        total_signals=0,
        validation_grade="B",
    )
    assert plan["remediation_reason"] == "signal_vacuum"
