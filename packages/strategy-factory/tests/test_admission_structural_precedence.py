"""Structural rejection must precede advisory quality and intake routing."""
from __future__ import annotations

import pytest

from strategy_factory.application.services.admission_authority import SubmissionAdmissionAuthority


def _resolve(gate, *, track="observe_incubation", hard_reasons=None,
             refresh=False, existing_status="draft", read_only=False):
    def bootstrap(_gate, *, candidate):
        return {
            "pre_observe_hard_reject_reasons": list(hard_reasons or []),
            "runtime_bootstrap_eligible": not bool(hard_reasons),
            "wide_intake_admitted": not bool(hard_reasons),
            "runtime_bootstrap_reason": "structural_failure" if hard_reasons else "wide_intake_observe",
            "semantic_runtime_match": not bool(hard_reasons),
            "execution_semantic_gap": bool(hard_reasons),
            "execution_readiness_tier": "missing_executable_contract" if hard_reasons else "formal_runtime_ready",
            "trade_prediction_contract_status": "rejected" if hard_reasons else "ready",
        }

    return SubmissionAdmissionAuthority.resolve(
        gate,
        candidate={},
        refresh_existing=refresh,
        existing_status=existing_status,
        incubation_budget_track=track,
        runtime_bootstrap_resolver=bootstrap,
        read_only=read_only,
    )


@pytest.mark.parametrize("passed", [False, True])
@pytest.mark.parametrize("gate_a", ["reject", "revise", "pass"])
@pytest.mark.parametrize("track", ["observe_incubation", "deferred_budget_queue", "formal_incubation"])
def test_structural_rejection_precedes_all_new_intake_routes(passed, gate_a, track):
    result = _resolve(
        {
            "passed": passed,
            "gate_a_decision": gate_a,
            "provisional_pass": True,
            "strict_incubation_ready": True,
            "live_candidate_ready": True,
            "admission_block_reasons": ["weak_bootstrap_ci_lower"],
        },
        track=track,
        hard_reasons=["trade_prediction_contract_not_ready"],
    )

    assert result["submission_lane"] == "rejected"
    assert result["final_status"] == "rejected"
    assert result["admission_decision"] == "reject"
    assert result["submission_action_trigger"] == "pre_observe_hard_reject"
    assert result["submission_action_completed"] is True
    assert result["submission_action_gaps"] == [
        "weak_bootstrap_ci_lower", "trade_prediction_contract_not_ready",
    ]
    assert result["submission_action"]["admission_decision"] == "reject"


@pytest.mark.parametrize("gate_a", ["reject", "pass"])
def test_weak_quality_without_structural_failure_still_observes(gate_a):
    result = _resolve({
        "passed": False,
        "gate_a_decision": gate_a,
        "admission_block_reasons": ["weak_bootstrap_ci_lower"],
    })

    assert result["submission_lane"] == "observe_incubation"
    assert result["final_status"] == "submitted"
    assert result["admission_decision"] == "observe_only"
    assert "weak_bootstrap_ci_lower" in result["submission_action_gaps"]


@pytest.mark.parametrize("status", ["incubating", "rejected"])
@pytest.mark.parametrize("provisional", [False, True])
def test_refresh_retains_existing_status_and_legacy_decision(status, provisional):
    result = _resolve(
        {"passed": True, "provisional_pass": provisional},
        hard_reasons=["semantic_hard_fail"],
        refresh=True,
        existing_status=status,
    )

    assert result["submission_lane"] == "refresh_existing"
    assert result["final_status"] == status
    assert result["submission_action_trigger"] == "existing_strategy_refresh"
    assert result["submission_action_gaps"] == []
    expected = "provisional" if provisional else "reject" if status == "rejected" else "accept"
    assert result["admission_decision"] == expected


def test_read_only_reports_rejected_plan_without_admission_side_effects():
    result = _resolve(
        {"passed": True, "provisional_pass": True},
        hard_reasons=["semantic_hard_fail"],
        read_only=True,
    )
    assert result["submission_lane"] == "rejected"
    assert result["final_status"] == "rejected"
    assert result["admission_decision"] == "read_only"


@pytest.mark.parametrize("lane,status", [("rejected", "submitted"), ("deferred_submission", "rejected")])
def test_explicit_rejected_lane_or_status_outranks_provisional(lane, status):
    decision = SubmissionAdmissionAuthority._decision(
        gate={"passed": False, "provisional_pass": True, "gate_a_decision": "revise"},
        action_type="research_only",
        submission_lane=lane,
        final_status=status,
        read_only=False,
    )
    assert decision == "reject"
