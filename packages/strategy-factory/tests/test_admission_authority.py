from __future__ import annotations

from typing import Any

import pytest

from _admission_helpers import (
    _formal_runtime,
    _ready_trade_prediction_fields,
    _resolve,
    _runtime_ready_candidate,
)

def test_admission_authority_accepts_formal_incubation_candidate() -> None:
    result = _resolve(
        {
            "passed": True,
            "strict_incubation_ready": True,
            "incubation_pass_mode": "strict",
        }
    )

    assert result["admission_decision"] == "accept"
    assert result["submission_lane"] == "formal_incubation"
    assert result["final_status"] == "incubating"


def test_admission_authority_marks_provisional_pass() -> None:
    result = _resolve(
        {
            "passed": True,
            "provisional_pass": True,
            "research_candidate_ready": True,
            "incubation_candidate_ready": True,
        },
        track="deferred_budget_queue",
    )

    assert result["admission_decision"] == "provisional"
    assert result["submission_lane"] == "deferred_submission"
    assert result["submission_action_type"] == "research_only"
    assert result["final_status"] == "submitted"


def test_admission_authority_rejects_failed_gate() -> None:
    result = _resolve(
        {
            "passed": False,
            "gate_a_decision": "reject",
            "admission_block_reasons": ["missing_research_protocol"],
        }
    )

    assert result["admission_decision"] == "reject"
    assert result["submission_lane"] == "rejected"
    assert result["final_status"] == "rejected"


def test_admission_authority_defers_revision_gate() -> None:
    result = _resolve(
        {
            "passed": False,
            "gate_a_decision": "revise",
            "admission_block_reasons": ["contract_revision_required"],
        }
    )

    assert result["admission_decision"] == "revise"
    assert result["submission_lane"] == "deferred_submission"
    assert result["final_status"] == "draft"


def test_admission_authority_read_only_keeps_legacy_lane_but_marks_decision() -> None:
    result = _resolve(
        {
            "passed": True,
            "strict_incubation_ready": True,
            "incubation_pass_mode": "strict",
        },
        read_only=True,
    )

    assert result["admission_decision"] == "read_only"
    assert result["submission_lane"] == "formal_incubation"
    assert result["final_status"] == "incubating"


# === DEV-V1 P0: D 级 + Gate passed 候选 toggle 化测试 ===
# 这两个用例不再 mock runtime resolver,而是用真实 StrategySubmitter._runtime_bootstrap_context,
# 验证 STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED toggle 的实际效果。

import pytest
