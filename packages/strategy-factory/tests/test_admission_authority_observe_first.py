from __future__ import annotations

from typing import Any

import pytest

from _admission_helpers import (
    _clear_dev_v1_env,
    _formal_runtime,
    _ready_trade_prediction_fields,
    _resolve,
    _resolve_with_real_resolver,
    _runtime_ready_candidate,
)

def test_dev_v1_p0_d_grade_observe_disabled_by_default() -> None:
    """toggle 默认 OFF: D 级 + Gate passed 仍归 deferred_submission,行为与 V4 修改前一致。"""
    gate = {
        "passed": True,
        "validation_grade": "D",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": False,
    }
    candidate = _runtime_ready_candidate("d-grade-disabled")
    result = _resolve_with_real_resolver(gate, candidate)

    assert result["submission_lane"] == "deferred_submission"
    assert result["runtime_bootstrap_eligible"] is False
    assert result["runtime_bootstrap_reason"] == "validation_grade_d_not_allowed_for_runtime"
    assert result["formal_track_eligible"] is False


def test_dev_v1_p0_d_grade_observe_enabled_routes_to_observe(monkeypatch) -> None:
    """toggle ON: D 级 + Gate passed 候选走 observe_incubation,budget_tier=micro,
    formal 严格性零变化(formal_track_eligible 仍为 False)。
    """
    monkeypatch.setenv("STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED", "1")

    gate = {
        "passed": True,
        "validation_grade": "D",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": False,
    }
    candidate = _runtime_ready_candidate("d-grade-enabled")
    result = _resolve_with_real_resolver(gate, candidate)

    assert result["submission_lane"] == "observe_incubation"
    assert result["final_status"] == "submitted"
    assert result["runtime_bootstrap_eligible"] is True
    assert result["runtime_bootstrap_reason"] == "d_grade_observe_only_micro_budget"
    assert result["runtime_bootstrap_budget_tier"] == "micro"
    # formal 严格性零变化
    assert result["formal_track_eligible"] is False
    assert "strict_incubation_pass_required_for_formal_track" in result["formal_track_blockers"]
    assert result["admission_decision"] == "observe_only"


def test_gate3_statistical_failure_routes_to_observe_when_observe_first_requested() -> None:
    gate = {
        "passed": False,
        "validation_grade": "C",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": False,
        "reason_codes": [
            "insufficient_statistical_evidence",
            "weak_bootstrap_ci_lower",
            "factory_policy_backtest_trade_count_0_4",
            "profit_factor_0_860_1_000",
        ],
        "admission_block_reasons": [
            "insufficient_statistical_evidence",
            "weak_bootstrap_ci_lower",
            "factory_policy_backtest_trade_count_0_4",
        ],
    }
    candidate = _runtime_ready_candidate("gate3-audit-only", observe_first=True)

    result = _resolve_with_real_resolver(gate, candidate)

    assert result["submission_lane"] == "observe_incubation"
    assert result["final_status"] == "submitted"
    assert result["runtime_bootstrap_eligible"] is True
    assert result["wide_intake_admitted"] is True
    assert result["runtime_bootstrap_reason"] == "wide_intake_observe_gate3_not_required"
    assert result["admission_decision"] == "observe_only"
    assert result["formal_track_eligible"] is False
    assert result["pre_observe_hard_reject_reasons"] == []


def test_derived_sparse_trade_prediction_contract_routes_to_observe_first() -> None:
    gate = {
        "passed": False,
        "validation_grade": "C",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": False,
        "reason_codes": ["insufficient_statistical_evidence"],
    }
    candidate = {
        "id": "missing-trade-prediction",
        "strategy_type": "volatility_breakout",
        "target_symbols": ["600000"],
        "observe_first_intake": True,
        "incubation_budget": {
            "track": "observe_incubation",
            "budget_tier": "micro",
            "observe_first_intake": True,
        },
        "params": {
            "observe_first_intake": True,
            "holding_horizon": {"horizon": "next_day"},
            "trade_plan": {"entry_bias": "long", "holding_horizon": "next_day"},
            "risk_rules": {"max_drawdown": 0.12},
            "execution_assumptions": {"order_style": "paper"},
            "trade_prediction_contract": {
                "contract_source": "derived_from_legacy_contract",
            },
            "trade_prediction_contract_status": "rejected",
            "trade_prediction_contract_reject_reasons": [
                "missing:direction",
                "missing:confidence",
            ],
            "trade_prediction_contract_missing_fields": [
                "direction",
                "confidence",
            ],
        },
    }

    result = _resolve_with_real_resolver(gate, candidate)

    assert result["submission_lane"] == "observe_incubation"
    assert result["final_status"] == "submitted"
    assert result["runtime_bootstrap_eligible"] is True
    assert result["wide_intake_admitted"] is True
    assert result["runtime_bootstrap_reason"] == "wide_intake_observe_gate3_not_required"
    assert result["pre_observe_hard_reject_reasons"] == []
    assert result["trade_prediction_contract_observation_gap"] is True
    assert result["diagnostic_only"] is True
    assert result["execution_readiness_tier"] == "observe_diagnostic_only"
    assert result["formal_track_eligible"] is False
    assert "diagnostic_only_not_allowed_for_incubation" in result["formal_track_blockers"]
    assert "diagnostic_only_runtime" not in result["formal_track_blockers"]
