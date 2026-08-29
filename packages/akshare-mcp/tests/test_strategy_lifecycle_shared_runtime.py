from __future__ import annotations

import asyncio

from akshare_mcp.services.strategy_lifecycle_shared.confidence import evaluate_execution_audit_gate
from akshare_mcp.services.strategy_lifecycle_shared.execution_quality import _build_execution_quality_snapshot
from akshare_mcp.services.strategy_lifecycle_shared.incubation import resolve_incubation_pipeline_stage
from akshare_mcp.services.strategy_lifecycle_shared.overview import _resolve_risk_hard_gate
from akshare_mcp.services.strategy_lifecycle_shared.prediction_trace import _build_execution_lineage


class _SignalEvidenceDb:
    async def list_strategy_signal_evidence(
        self,
        *,
        signal_id: str | None = None,
        strategy_id: str | None = None,
        limit: int = 200,
    ):
        # match the real DB keyword-only signature so the test catches
        # any future caller that regresses to positional args
        assert signal_id is None
        assert strategy_id == "strategy-runtime"
        assert limit >= 2
        return [
            {
                "signal_id": "sig-1",
                "signal_date": "2026-04-15",
                "signal_ts": "2026-04-15T10:00:00+00:00",
                "created_at": "2026-04-15T10:00:01+00:00",
                "applied_claim_id": "claim-a",
                "applied_trade_step_id": "trade-step-1",
                "runtime_action_reason": "reduce_risk",
                "runtime_action_source": "runtime_control",
            },
            {
                "signal_id": "sig-2",
                "signal_date": "2026-04-16",
                "signal_ts": "2026-04-16T10:00:00+00:00",
                "created_at": "2026-04-16T10:00:01+00:00",
                "applied_claim_id": "claim-a",
                "runtime_action_reason": "freeze_reentry",
                "runtime_action_source": "risk_event",
            },
        ]


def test_execution_audit_gate_drives_incubation_stage_without_name_errors():
    audit_summary = {
        "realized_trade_count": 28,
        "mapped_position_count": 28,
        "order_count": 40,
        "filled_order_count": 36,
        "trade_count": 32,
        "nav_observation_days": 18,
        "trade_expectancy": 0.03,
        "pnl_conversion_efficiency": 0.18,
        "execution_conversion_efficiency": 0.42,
    }
    status, reasons, passes, metrics = evaluate_execution_audit_gate(audit_summary)
    assert status == "passed"
    assert reasons == []
    assert passes["realized_trade_count"] is True
    assert metrics["realized_trade_count"] == 28

    stage = resolve_incubation_pipeline_stage(
        {
            "primary_effective_n": 72,
            "secondary_effective_n": 36,
            "primary_skill_lcb": 0.04,
            "secondary_skill_lcb": 0.02,
            "recent_primary_skill_lcb": 0.01,
            "coverage_ratio": 0.85,
            "stability_gap": 0.03,
        },
        audit_summary=audit_summary,
    )
    assert stage == "graduation_ready"


def _low_freq_quality(primary_n: int, secondary_n: int) -> dict:
    return {
        "primary_effective_n": primary_n,
        "secondary_effective_n": secondary_n,
        "primary_skill_lcb": 0.04,
        "secondary_skill_lcb": 0.02,
        "recent_primary_skill_lcb": 0.01,
        "coverage_ratio": 0.85,
        "stability_gap": 0.03,
    }


def test_long_horizon_graduates_on_lower_sample_floor():
    """P1: 低频(long)策略 n=32 应可毕业(自适应门 30),而非被短线门 60 卡死。"""
    passed_audit = {
        "audit_grade": True,
        "realized_trade_count": 24,
        "trade_expectancy": 0.03,
        "pnl_conversion_efficiency": 0.02,
        "execution_conversion_efficiency": 1.0,
    }
    stage = resolve_incubation_pipeline_stage(
        _low_freq_quality(32, 16),
        audit_summary=passed_audit,
        holding_bucket="long",
    )
    assert stage == "graduation_ready"


def test_long_horizon_still_blocks_below_adaptive_floor():
    """低频门是 30,n=10 仍应停在 warmup —— 不是无限放水。"""
    stage = resolve_incubation_pipeline_stage(
        _low_freq_quality(10, 5),
        holding_bucket="long",
    )
    assert stage == "warmup"


def test_default_bucket_keeps_strict_sixty_floor():
    """默认(短线/未指定)仍要求 n>=60 —— 不破坏既有严格门。"""
    stage = resolve_incubation_pipeline_stage(_low_freq_quality(32, 16))
    # n=32 < 60(短线门),不满足 graduation;skill_lcb>0 且 coverage 高 → candidate(待 audit)
    assert stage != "graduation_ready"


def test_low_freq_does_not_relax_skill_significance():
    """低频自适应只放宽样本量,不放宽 skill_lcb>0 —— 负 skill 仍不能毕业。"""
    quality = _low_freq_quality(32, 16)
    quality["primary_skill_lcb"] = -0.01  # 统计不显著
    quality["recent_primary_skill_lcb"] = -0.01
    stage = resolve_incubation_pipeline_stage(
        quality,
        holding_bucket="long",
    )
    assert stage != "graduation_ready"
    audit_summary = {
        "strategy_type": "margin_divergence",
        "realized_trade_count": 5,
        "mapped_position_count": 5,
        "order_count": 8,
        "filled_order_count": 8,
        "trade_count": 8,
        "nav_observation_days": 12,
        "trade_expectancy": 0.03,
        "pnl_conversion_efficiency": 0.02,
        "execution_conversion_efficiency": 1.0,
    }
    status, reasons, passes, metrics = evaluate_execution_audit_gate(audit_summary)

    assert status == "bootstrap_ready"
    assert reasons == ["realized_trade_count<20"]
    assert passes["bootstrap_trade_count"] is True
    assert passes["realized_trade_count"] is False
    assert metrics["bootstrap_trade_floor"] == 3
    assert metrics["required_trade_count"] == 20


def test_execution_quality_snapshot_keeps_bootstrap_ready_as_sample_gap_evidence():
    snapshot = _build_execution_quality_snapshot(
        {
            "execution_audit_gate_status": "bootstrap_ready",
            "evidence_gap_codes": [],
            "audit": {"realized_trade_count": 5},
        }
    )

    assert snapshot["status"] == "insufficient_evidence"
    assert "execution_audit_gate:bootstrap_ready" in snapshot["evidence_gap_codes"]


def test_prediction_trace_execution_lineage_uses_runtime_helpers():
    lineage = asyncio.run(_build_execution_lineage(_SignalEvidenceDb(), "strategy-runtime"))

    assert lineage["signal_evidence_count"] == 2
    assert lineage["runtime_action_count"] == 2
    assert lineage["unmapped_runtime_action_count"] == 1
    assert lineage["recent_signal_ids"] == ["sig-2", "sig-1"]
    assert lineage["runtime_action_reason_counts"] == {
        "freeze_reentry": 1,
        "reduce_risk": 1,
    }


def test_overview_risk_gate_helper_resolves_contracts_without_name_errors():
    result = _resolve_risk_hard_gate(
        {
            "drawdown_invalidation_contract": {
                "apply_as_hard_gate": True,
                "review_drawdown_pct": 0.05,
                "kill_drawdown_pct": 0.07,
            },
            "parameter_coherence_audit": {
                "blockers": ["missing_stop_loss_mapping"],
            },
        },
        max_drawdown=0.08,
    )

    assert result["status"] == "kill_switch"
    assert "parameter_coherence:missing_stop_loss_mapping" in result["reasons"]
    assert "max_drawdown>=7%" in result["reasons"]
