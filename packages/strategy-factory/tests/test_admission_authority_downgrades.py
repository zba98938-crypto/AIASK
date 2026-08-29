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

def test_ready_trade_prediction_contract_downgrades_semantic_conflict_rule_gap_for_observe_first() -> None:
    gate = {
        "passed": False,
        "validation_grade": "A",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": False,
        "reason_codes": [
            "prediction_contract_conflict_resolution_rule_missing",
            "weak_wf_ic_ir",
        ],
        "hard_fail_reasons": [
            "prediction_contract_conflict_resolution_rule_missing",
        ],
    }
    candidate = _runtime_ready_candidate("semantic-conflict-gap", observe_first=True)

    result = _resolve_with_real_resolver(gate, candidate)

    assert result["submission_lane"] == "observe_incubation"
    assert result["submission_action_type"] == "paper"
    assert result["submission_action_type"] == "paper"
    assert result["final_status"] == "submitted"
    assert result["runtime_bootstrap_eligible"] is True
    assert result["wide_intake_admitted"] is True
    assert result["runtime_bootstrap_reason"] == "wide_intake_observe_gate3_not_required"
    assert result["pre_observe_hard_reject_reasons"] == []
    assert result["trade_prediction_contract_status"] == "ready"
    assert result["trade_prediction_contract_hash"]
    assert result["formal_track_eligible"] is False


def test_formal_request_preserves_planned_lane_and_canonical_blockers_when_deferred() -> None:
    def _blocked_runtime(_gate: dict[str, Any], *, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "runtime_bootstrap_eligible": False,
            "runtime_bootstrap_reason": "missing_executable_contract",
            "runtime_bootstrap_budget_tier": None,
            "execution_semantic_gap": True,
            "execution_semantic_gap_reasons": ["missing_executable_contract"],
            "semantic_runtime_match": False,
            "proxy_runtime_used": False,
            "diagnostic_only": True,
            "execution_readiness_tier": "missing",
            "trade_prediction_contract_status": "missing",
            "trade_prediction_contract_observation_gap": False,
        }

    from strategy_factory.application.services.admission_authority import SubmissionAdmissionAuthority

    result = SubmissionAdmissionAuthority.resolve(
        {
            "passed": True,
            "validation_grade": "A",
            "research_candidate_ready": True,
            "live_candidate_ready": False,
            "strict_incubation_ready": True,
        },
        candidate=_runtime_ready_candidate("formal-deferred"),
        refresh_existing=False,
        existing_status="draft",
        incubation_budget_track="formal_incubation",
        runtime_bootstrap_resolver=_blocked_runtime,
    )

    assert result["submission_lane"] == "deferred_submission"
    assert result["planned_submission_lane"] == "formal_incubation"
    assert result["submission_action"]["planned_submission_lane"] == "formal_incubation"
    assert result["formal_track_requested"] is True
    assert result["formal_track_eligible"] is False
    assert "execution_readiness_tier:missing_executable_contract" in result["formal_track_blockers"]
    assert "diagnostic_only_not_allowed_for_incubation" in result["formal_track_blockers"]
    assert "diagnostic_only_runtime" not in result["formal_track_blockers"]
    assert result["submission_action"]["incubation_budget_track"] == "formal_incubation"


def test_ready_trade_prediction_contract_downgrades_evidence_audit_conflict_rule_gap_for_observe_first() -> None:
    gate = {
        "passed": False,
        "validation_grade": "A",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": False,
        "reason_codes": ["weak_wf_ic_ir"],
    }
    candidate = _runtime_ready_candidate(
        "semantic-audit-conflict-gap",
        observe_first=True,
        evidence_alignment_audit={
            "hard_fail_reasons": [
                "prediction_contract_conflict_resolution_rule_missing",
            ],
        },
    )

    result = _resolve_with_real_resolver(gate, candidate)

    assert result["submission_lane"] == "observe_incubation"
    assert result["submission_action_type"] == "paper"
    assert result["final_status"] == "submitted"
    assert result["runtime_bootstrap_eligible"] is True
    assert result["wide_intake_admitted"] is True
    assert result["runtime_bootstrap_reason"] == "wide_intake_observe_gate3_not_required"
    assert result["pre_observe_hard_reject_reasons"] == []
    assert result["trade_prediction_contract_status"] == "ready"
    assert result["formal_track_eligible"] is False


def test_real_resolver_reuses_gate_runtime_context_for_formal_blockers() -> None:
    gate = {
        "passed": True,
        "validation_grade": "A",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": False,
        "semantic_runtime_match": False,
        "runtime_family_data_source": "price_proxy_runtime",
        "proxy_runtime_used": True,
        "diagnostic_only": True,
        "execution_readiness_tier": "observe_diagnostic_only",
    }
    candidate = _runtime_ready_candidate(
        "gate-runtime-context",
        strategy_type="quality_factor",
    )

    result = _resolve_with_real_resolver(gate, candidate)

    assert result["submission_lane"] == "deferred_submission"
    assert result["submission_action_type"] == "research_only"
    assert result["runtime_bootstrap_reason"] == "proxy_runtime_observe_only"
    assert result["runtime_family_data_source"] == "price_proxy_runtime"
    assert result["proxy_runtime_used"] is True
    assert result["diagnostic_only"] is True
    assert result["execution_readiness_tier"] == "observe_diagnostic_only"
    assert result["formal_track_requested"] is True
    assert result["formal_track_eligible"] is False
    assert "semantic_runtime_mismatch" in result["formal_track_blockers"]
    assert "proxy_runtime_not_allowed_for_formal_incubation" in result["formal_track_blockers"]
    assert "diagnostic_only_not_allowed_for_incubation" in result["formal_track_blockers"]
    assert "diagnostic_only_runtime" not in result["formal_track_blockers"]
    assert "execution_readiness_tier:observe_diagnostic_only" in result["formal_track_blockers"]


def test_observe_first_strict_ready_formal_runtime_auto_corrects_to_formal() -> None:
    gate = {
        "passed": True,
        "validation_grade": "A",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": True,
        "incubation_pass_mode": "strict",
        "semantic_runtime_match": True,
        "runtime_family_data_source": "market_data_runtime",
        "proxy_runtime_used": False,
        "diagnostic_only": False,
        "execution_readiness_tier": "formal_runtime_ready",
        "trade_prediction_contract_status": "ready",
        "trade_prediction_contract_observation_gap": False,
    }
    candidate = _runtime_ready_candidate(
        "observe-first-formal-correction",
        observe_first=True,
        strategy_type="quality_factor",
    )

    result = _resolve_with_real_resolver(gate, candidate, track="observe_incubation")

    assert result["submission_lane"] == "formal_incubation"
    assert result["final_status"] == "incubating"
    assert result["formal_track_requested"] is True
    assert result["formal_track_auto_corrected"] is True
    assert result["formal_track_eligible"] is True
    assert result["submission_action_trigger"] == "strict_incubation_ready_and_observe_first_formal_correction"
    assert result["admission_decision"] == "accept"


def test_deferred_strict_ready_formal_runtime_auto_corrects_to_formal() -> None:
    gate = {
        "passed": True,
        "validation_grade": "B",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": True,
        "incubation_pass_mode": "strict",
        "semantic_runtime_match": True,
        "runtime_family_data_source": "market_data_runtime",
        "proxy_runtime_used": False,
        "diagnostic_only": False,
        "execution_readiness_tier": "formal_runtime_ready",
        "trade_prediction_contract_status": "ready",
        "trade_prediction_contract_observation_gap": False,
    }
    candidate = _runtime_ready_candidate(
        "deferred-formal-correction",
        strategy_type="quality_factor",
        incubation_budget={"track": "deferred_budget_queue"},
    )

    result = _resolve_with_real_resolver(gate, candidate, track="deferred_budget_queue")

    assert result["submission_lane"] == "formal_incubation"
    assert result["final_status"] == "incubating"
    assert result["planned_submission_lane"] == "formal_incubation"
    assert result["formal_track_requested"] is True
    assert result["formal_track_auto_corrected"] is True
    assert result["formal_track_eligible"] is True
    assert result["observe_first_intake_requested"] is False
    assert result["formal_auto_correction_source_track"] == "deferred_budget_queue"
    assert result["submission_action_trigger"] == "strict_incubation_ready_and_runtime_formal_correction"
    assert result["admission_decision"] == "accept"


def test_deferred_proxy_runtime_is_not_auto_corrected_to_formal() -> None:
    gate = {
        "passed": True,
        "validation_grade": "B",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": True,
        "incubation_pass_mode": "strict",
        "semantic_runtime_match": False,
        "runtime_family_data_source": "price_proxy_runtime",
        "proxy_runtime_used": True,
        "diagnostic_only": True,
        "execution_readiness_tier": "observe_diagnostic_only",
        "trade_prediction_contract_status": "ready",
        "trade_prediction_contract_observation_gap": False,
    }
    candidate = _runtime_ready_candidate(
        "deferred-proxy-no-formal",
        strategy_type="quality_factor",
        incubation_budget={"track": "deferred_budget_queue"},
    )

    result = _resolve_with_real_resolver(gate, candidate, track="deferred_budget_queue")

    assert result["submission_lane"] == "deferred_submission"
    assert result["submission_action_type"] == "research_only"
    assert result["formal_track_requested"] is False
    assert result["formal_track_auto_corrected"] is False
    assert result["formal_track_eligible"] is False
    assert result["proxy_runtime_used"] is True
    assert result["diagnostic_only"] is True
    assert result["admission_decision"] == "observe_only"


def test_formal_budget_request_is_not_silently_washed_into_wide_observe() -> None:
    gate = {
        "passed": False,
        "validation_grade": "B",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": False,
        "reason_codes": ["weak_bootstrap_ci_lower"],
        "admission_block_reasons": ["weak_bootstrap_ci_lower"],
    }
    candidate = _runtime_ready_candidate(
        "formal-budget-wide-intake",
        observe_first=True,
        strategy_type="quality_factor",
    )
    candidate["incubation_budget"]["track"] = "formal_incubation"

    result = _resolve_with_real_resolver(gate, candidate, track="formal_incubation")

    assert result["planned_submission_lane"] == "formal_incubation"
    assert result["planned_final_status"] == "incubating"
    assert result["submission_lane"] == "rejected"
    assert result["final_status"] == "rejected"
    assert result["formal_track_requested"] is True
    assert result["formal_track_eligible"] is False
    assert result["wide_intake_admitted"] is True
    assert "quality_gate_pass_required_for_formal_track" in result["formal_track_blockers"]
    assert "strict_incubation_pass_required_for_formal_track" in result["formal_track_blockers"]
    assert result["admission_decision"] == "reject"


def test_explicit_invalid_trade_prediction_contract_still_blocks_observe_first() -> None:
    gate = {
        "passed": False,
        "validation_grade": "C",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": False,
        "reason_codes": ["insufficient_statistical_evidence"],
    }
    candidate = {
        "id": "explicit-invalid-trade-prediction",
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
                "contract_source": "explicit",
                "direction": "moon",
            },
            "trade_prediction_contract_status": "rejected",
            "trade_prediction_contract_reject_reasons": ["invalid:direction"],
            "trade_prediction_contract_missing_fields": [],
        },
    }

    result = _resolve_with_real_resolver(gate, candidate)

    assert result["submission_lane"] == "rejected"
    assert result["final_status"] == "rejected"
    assert result["runtime_bootstrap_eligible"] is False
    assert result["wide_intake_admitted"] is False
    assert result["runtime_bootstrap_reason"] == "trade_prediction_contract_not_ready"
    assert "trade_prediction_contract_not_ready" in result["pre_observe_hard_reject_reasons"]
    assert result["trade_prediction_contract_observation_gap"] is False


def test_semantic_hard_fail_still_blocks_observe_first() -> None:
    gate = {
        "passed": False,
        "validation_grade": "C",
        "research_candidate_ready": True,
        "live_candidate_ready": False,
        "strict_incubation_ready": False,
        "reason_codes": ["temporal_coherence_audit_failed"],
        "hard_fail_reasons": ["temporal_coherence_audit_failed"],
    }
    candidate = _runtime_ready_candidate("semantic-hard-fail", observe_first=True)

    result = _resolve_with_real_resolver(gate, candidate)

    assert result["submission_lane"] == "rejected"
    assert result["final_status"] == "rejected"
    assert result["runtime_bootstrap_eligible"] is False
    assert result["wide_intake_admitted"] is False
    assert result["runtime_bootstrap_reason"] == "temporal_coherence_audit_failed"
    assert "temporal_coherence_audit_failed" in result["pre_observe_hard_reject_reasons"]
