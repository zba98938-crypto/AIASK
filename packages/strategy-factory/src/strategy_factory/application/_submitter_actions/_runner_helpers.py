"""策略工厂候选提交。"""


from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time as _time
from collections import Counter
from typing import TYPE_CHECKING, Any, List, Optional
from uuid import uuid4

from aiask_quant_core.strategy_explanation import build_strategy_explanation, render_strategy_description

from ..candidate_contract import (
    apply_resolved_candidate_envelope,
    build_candidate_contract_hash,
    build_candidate_identity_signature,
    build_execution_contract_hash,
    build_portfolio_candidate_contract,
    build_resolved_candidate_envelope,
    build_tested_object_hash,
    candidate_contract_value,
)
from ..incubation_budgeter import IncubationBudgeter
from ..compact_contracts import compact_event_window_metrics, compact_json, compact_scalar_metrics
from ..quality_gates import _VALID_STRATEGY_TYPES, build_completed_gate_3_report
from ..quality_reporting import build_quality_report, normalize_quality_gate_result
from ..semantic_contract import (
    audit_candidate_semantic_contract,
    build_candidate_evidence_records,
    ensure_candidate_semantic_contract,
    normalize_semantic_contract_fields,
    synthesize_confidence_contract,
)
from ..submission_gate import run_submission_quality_gate as _local_run_submission_quality_gate
from ..trade_prediction_promotion_gate import (
    evaluate_trade_prediction_promotion_gate,
)
from ..utils import (
    _auto_name as _local_auto_name,
    _extract_event_context as _local_extract_event_context,
    _update_strategy_status as _local_update_strategy_status,
    get_strategy_factory_package as _local_get_strategy_factory_package,
)
from ..research_protocol_contract import (
    adapt_research_validation_contract_for_submission,
    normalize_prediction_trace_id,
)
from ..services.submission_coordinator import SubmissionExecutionOptions
from ..services.admission_authority import SubmissionAdmissionAuthority
from ..services.readiness_service import resolve_market_temperature_context
from .._runtime_toggles import (
    diagnostic_observation_batch_limit as _diagnostic_observation_batch_limit,
    diagnostic_observation_dedupe_enabled as _diagnostic_observation_dedupe_enabled,
    diagnostic_observation_enabled as _diagnostic_observation_enabled,
    diagnostic_observation_final_status as _diagnostic_observation_final_status,
    diagnostic_observation_health_guard_enabled as _diagnostic_observation_health_guard_enabled,
    diagnostic_observation_health_max_age_hours as _diagnostic_observation_health_max_age_hours,
    diagnostic_observation_min_trade_count as _diagnostic_observation_min_trade_count,
    diagnostic_observation_min_win_rate as _diagnostic_observation_min_win_rate,
    diagnostic_observation_ttl_days as _diagnostic_observation_ttl_days,
    observe_first_enabled as _observe_first_enabled,
    observe_d_grade_enabled as _observe_d_grade_enabled,
    strategy_factory_gate3_record_only_enabled as _gate3_record_only_enabled,
    strategy_factory_min_validation_grade as _strategy_factory_min_validation_grade,
    validation_grade_at_least as _validation_grade_at_least,
    wide_intake_observe_enabled as _wide_intake_observe_enabled,
)
from ...domain.constants import (
    FACTORY_SUBMISSION_MIN_BACKTEST_TRADES,
    FACTORY_SUBMISSION_MIN_EVENT_TARGET_COVERAGE,
    FACTORY_SUBMISSION_REJECT_GENERIC_AI_NAMES,
    FACTORY_SUBMISSION_REQUIRE_STRICT_PASS_FOR_REFRESH,
    FACTORY_SUBMISSION_REQUIRE_TASK_PREFERENCE_MATCH,
    STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE,
    STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED,
    SUBMIT_CONCURRENCY,
)
from ...domain.targets import _build_task_signature, _normalize_research_task_contract, _normalize_target_codes
from ...infrastructure.mcp_services import (
    build_strategy_vector_profile,
    get_strategy_promotion_pipeline_service,
    get_strategy_runtime_control_service,
)

if TYPE_CHECKING:
    from ...api.contracts import IncubationGateway, RiskGateway, ValidationGateway

logger = logging.getLogger(__name__)

def _compact_unique(values: Any, *, limit: int = 12) -> list[str]:
    items: list[str] = []
    for value in list(values or []):
        token = str(value or "").strip()
        if token and token not in items:
            items.append(token)
        if len(items) >= max(1, int(limit or 12)):
            break
    return items


def _quality_summary_validation_grade(quality_summary: dict[str, Any], gate: dict[str, Any] | None = None) -> str:
    payload = dict(quality_summary or {})
    gate_payload = dict(gate or {})
    return str(
        payload.get("effective_validation_grade")
        or payload.get("validation_grade")
        or payload.get("raw_validation_grade")
        or gate_payload.get("effective_validation_grade")
        or gate_payload.get("validation_grade")
        or gate_payload.get("raw_validation_grade")
        or ""
    ).strip().upper()


def _build_gate3_quality_record_contract(
    *,
    gate: dict[str, Any] | None,
    quality_summary: dict[str, Any] | None,
    read_only: bool,
    gate3_record_only: bool,
) -> dict[str, Any]:
    min_grade = _strategy_factory_min_validation_grade()
    grade = _quality_summary_validation_grade(dict(quality_summary or {}), gate)
    grade_meets_minimum = bool(grade and _validation_grade_at_least(grade, min_grade))
    gate_passed = bool(dict(gate or {}).get("passed"))
    recorded = bool(not read_only and gate3_record_only)
    qualified = bool(recorded and gate_passed and grade_meets_minimum)
    return {
        "gate3_record_only_min_grade": min_grade,
        "gate3_record_grade": grade or None,
        "gate3_record_gate_passed": gate_passed,
        "gate3_record_grade_meets_minimum": grade_meets_minimum,
        "gate3_record_quality_qualified": qualified,
        "gate3_quality_recorded": qualified,
        "production_quality_recorded": qualified,
        "gate3_record_diagnostic_only": bool(recorded and not qualified),
    }


def _candidate_trace_ids(candidate: dict[str, Any]) -> list[str]:
    trace_id = normalize_prediction_trace_id(
        candidate.get("prediction_trace_id"),
        candidate.get("trace_id"),
        fallback=dict(candidate.get("params") or {}).get("prediction_trace_id"),
    )
    return [trace_id] if trace_id else []


def _candidate_artifact_ids(candidate: dict[str, Any]) -> list[str]:
    provenance = dict(candidate.get("candidate_provenance") or {})
    params = dict(candidate.get("params") or {})
    return _compact_unique(
        [
            provenance.get("source_candidate_artifact_id"),
            provenance.get("source_generation_artifact_id"),
            provenance.get("source_validation_artifact_id"),
            provenance.get("memory_record_id"),
            candidate.get("hypothesis_artifact_id"),
            candidate.get("experiment_id"),
            candidate.get("multiple_testing_registry_record_id"),
            params.get("source_candidate_artifact_id"),
            params.get("source_generation_artifact_id"),
            params.get("source_validation_artifact_id"),
            params.get("candidate_memory_record_id"),
            params.get("multiple_testing_registry_record_id"),
            params.get("task_run_id"),
        ]
    )


def _candidate_retrieval_context_ids(candidate: dict[str, Any]) -> list[str]:
    provenance = dict(candidate.get("candidate_provenance") or {})
    research_task = dict(candidate.get("research_task") or {})
    params = dict(candidate.get("params") or {})
    return _compact_unique(
        [
            research_task.get("task_id"),
            research_task.get("event_id"),
            candidate.get("task_run_id"),
            candidate.get("multiple_testing_registry_record_id"),
            provenance.get("memory_record_id"),
            params.get("task_run_id"),
            params.get("multiple_testing_registry_record_id"),
            params.get("candidate_memory_record_id"),
            params.get("vector_profile_id"),
        ]
    )


def _candidate_family_outcome_summary(
    candidate: dict[str, Any],
    *,
    final_status: str | None = None,
) -> dict[str, Any]:
    provenance = dict(candidate.get("candidate_provenance") or {})
    family = (
        provenance.get("candidate_family")
        or candidate.get("candidate_family")
        or dict(candidate.get("params") or {}).get("candidate_family")
        or candidate.get("strategy_type")
    )
    return {
        "candidate_family": str(family or "").strip().lower() or None,
        "strategy_type": str(candidate.get("strategy_type") or "").strip().lower() or None,
        "spec_completeness": str(candidate.get("spec_completeness") or "").strip().lower() or None,
        "final_status": str(final_status or "").strip().lower() or None,
    }


def _candidate_hard_failures(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    hard_failures: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in list(candidate.get("hard_failures") or []):
        payload = dict(raw or {})
        field_name = str(payload.get("field") or "").strip()
        reason_code = str(payload.get("reason_code") or payload.get("issue") or field_name).strip()
        if not reason_code:
            continue
        dedupe_key = (field_name, reason_code)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized = {
            "reason_code": reason_code,
            "issue": str(payload.get("issue") or "hard_failure").strip() or "hard_failure",
            "severity": "reject",
            "decision": "reject",
        }
        if field_name:
            normalized["field"] = field_name
        detail = str(payload.get("detail") or "").strip()
        if detail:
            normalized["detail"] = detail
        hard_failures.append(normalized)
    for raw in list(candidate.get("completion_issues") or []):
        payload = dict(raw or {})
        decision = str(payload.get("decision") or payload.get("severity") or "").strip().lower()
        if decision != "reject":
            continue
        field_name = str(payload.get("field") or "").strip()
        reason_code = str(payload.get("reason_code") or payload.get("issue") or field_name).strip()
        if not reason_code:
            continue
        dedupe_key = (field_name, reason_code)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized = {
            "reason_code": reason_code,
            "issue": str(payload.get("issue") or "hard_failure").strip() or "hard_failure",
            "severity": "reject",
            "decision": "reject",
        }
        if field_name:
            normalized["field"] = field_name
        hard_failures.append(normalized)
    return hard_failures


def _gate_a_revision_actions(candidate: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    for issue in list(candidate.get("completion_issues") or []):
        payload = dict(issue or {})
        field_name = str(payload.get("field") or "").strip()
        if field_name:
            action = f"provide_required_research_field:{field_name}"
            if action not in actions:
                actions.append(action)
    for field_name in list(candidate.get("semantic_contract_missing_fields") or []):
        token = str(field_name or "").strip()
        if token:
            action = f"repair_semantic_contract_field:{token}"
            if action not in actions:
                actions.append(action)
    if bool(candidate.get("execution_semantic_gap")):
        action = "repair_execution_semantic_gap"
        if action not in actions:
            actions.append(action)
    return actions


def _gate_a_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    completion_issues = [dict(item or {}) for item in list(candidate.get("completion_issues") or []) if isinstance(item, dict)]
    hard_failures = _candidate_hard_failures(candidate)
    hard_failure_codes = _compact_unique(
        [dict(item or {}).get("reason_code") for item in hard_failures]
    )
    blocking_reasons = [
        str(item.get("reason_code") or item.get("issue") or item.get("field") or "").strip()
        for item in completion_issues
        if str(item.get("reason_code") or item.get("issue") or item.get("field") or "").strip()
    ]
    if bool(candidate.get("execution_semantic_gap")) and "execution_semantic_gap" not in blocking_reasons:
        blocking_reasons.append("execution_semantic_gap")
    for field_name in list(candidate.get("semantic_contract_missing_fields") or []):
        token = str(field_name or "").strip()
        if token:
            code = f"semantic_contract_missing:{token}"
            if code not in blocking_reasons:
                blocking_reasons.append(code)
    for code in hard_failure_codes:
        if code not in blocking_reasons:
            blocking_reasons.append(code)
    evidence_gap_codes = [
        str(item.get("reason_code") or item.get("issue") or item.get("field") or "").strip()
        for item in completion_issues
        if str(item.get("decision") or item.get("severity") or "").strip().lower() != "reject"
        and str(item.get("reason_code") or item.get("issue") or item.get("field") or "").strip()
    ]
    decision = "reject" if hard_failures else "revise" if blocking_reasons else "pass"
    return {
        "contract_version": "strategy_factory.gate_artifact.v2",
        "gate_name": "gate_a",
        "stage": "gate_a",
        "decision": decision,
        "status": "blocked" if blocking_reasons else "passed",
        "hard_failures": hard_failures,
        "evidence_gap_codes": _compact_unique(evidence_gap_codes),
        "artifact_ids": _candidate_artifact_ids(candidate),
        "retrieval_context_ids": _candidate_retrieval_context_ids(candidate),
        "trace_ids": _candidate_trace_ids(candidate),
        "family_outcome_summary": _candidate_family_outcome_summary(candidate),
        "blocking_reasons": blocking_reasons,
        "warnings": ["spec_completeness_incomplete"] if str(candidate.get("spec_completeness") or "").strip() == "incomplete" else [],
        "revision_actions": _gate_a_revision_actions(candidate),
        "evidence_refs": _candidate_trace_ids(candidate),
        "legacy_gate_mapping": ["gate_0", "pre_gate", "gate_1"],
        "spec_completeness": candidate.get("spec_completeness"),
    }


def _gate_b_payload(candidate: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    blocking_reasons = [str(item).strip() for item in list(gate.get("reason_codes") or []) if str(item).strip()]
    warnings = [str(item).strip() for item in list(gate.get("warning_codes") or []) if str(item).strip()]
    review_decision = str(
        dict(gate.get("business_admission_decision") or {}).get("decision")
        or gate.get("gate_b_review_decision")
        or ("pass" if gate.get("passed") else "reject" if blocking_reasons else "pending")
    ).strip().lower() or "pending"
    decision = "block" if blocking_reasons else "pass" if gate.get("passed") else "pending"
    return {
        "contract_version": "strategy_factory.gate_artifact.v2",
        "gate_name": "gate_b",
        "stage": "gate_b",
        "decision": decision,
        "review_decision": review_decision,
        "status": "blocked" if blocking_reasons else "passed" if gate.get("passed") else "pending",
        "hard_failures": [
            {
                "reason_code": code,
                "issue": "submission_gate_blocker",
                "severity": "reject",
                "decision": "reject",
            }
            for code in blocking_reasons
        ],
        "evidence_gap_codes": warnings,
        "artifact_ids": _candidate_artifact_ids(candidate),
        "retrieval_context_ids": _candidate_retrieval_context_ids(candidate),
        "trace_ids": _candidate_trace_ids(candidate),
        "family_outcome_summary": _candidate_family_outcome_summary(candidate),
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "evidence_refs": _candidate_trace_ids(candidate),
        "legacy_gate_mapping": ["gate_2", "gate_3"],
        "business_admission_decision": dict(gate.get("business_admission_decision") or {}),
        "benchmark_comparison": dict(gate.get("benchmark_comparison") or {}),
        "cost_sensitivity_summary": dict(gate.get("cost_sensitivity_summary") or {}),
        "cash_sleeve_audit": dict(gate.get("cash_sleeve_audit") or {}),
        "family_holding_bucket": dict(gate.get("family_holding_bucket") or {}),
    }


def _gate_c_payload(candidate: dict[str, Any], gate: dict[str, Any], final_status: str) -> dict[str, Any]:
    blockers = [str(item).strip() for item in list(candidate.get("gate_blockers") or []) if str(item).strip()]
    execution_audit_status = str(
        candidate.get("execution_audit_gate_status")
        or gate.get("execution_audit_gate_status")
        or ""
    ).strip()
    evidence_gap_codes = list(candidate.get("evidence_gap_codes") or [])
    if execution_audit_status in {
        "missing",
        "bootstrap_pending",
        "insufficient_samples",
        "bootstrap_ready",
        "insufficient_evidence",
    }:
        evidence_gap_codes.append(f"execution_audit_gate:{execution_audit_status}")
    decision = "block" if blockers else "observe" if final_status else "pending"
    return {
        "contract_version": "strategy_factory.gate_artifact.v2",
        "gate_name": "gate_c",
        "stage": "gate_c",
        "decision": decision,
        "status": "blocked" if blockers else "observe" if final_status else "pending",
        "hard_failures": [
            {
                "reason_code": code,
                "issue": "promotion_gate_blocker",
                "severity": "reject",
                "decision": "reject",
            }
            for code in blockers
        ],
        "evidence_gap_codes": _compact_unique(evidence_gap_codes),
        "artifact_ids": _compact_unique(
            [
                candidate.get("paper_account_id"),
                candidate.get("incubation_account_id"),
                candidate.get("promotion_review_id"),
                candidate.get("vector_profile_id"),
            ]
        ),
        "retrieval_context_ids": _candidate_retrieval_context_ids(candidate),
        "trace_ids": _candidate_trace_ids(candidate),
        "family_outcome_summary": _candidate_family_outcome_summary(
            candidate,
            final_status=final_status,
        ),
        "blocking_reasons": blockers,
        "warnings": [],
        "evidence_refs": _candidate_trace_ids(candidate),
        "legacy_gate_mapping": [
            "signal_quality",
            "execution_quality",
            "execution_audit_gate_status",
            "hard_gate_result",
            "promotion_ready",
        ],
        "signal_quality": candidate.get("signal_quality"),
        "execution_quality": candidate.get("execution_quality"),
        "execution_audit_gate_status": execution_audit_status or None,
        "hard_gate_result": candidate.get("hard_gate_result") or gate.get("hard_gate_result"),
        "promotion_ready": bool(candidate.get("promotion_ready")),
    }


def _enrich_quality_report_v2(
    quality_report: dict[str, Any],
    *,
    candidate: dict[str, Any],
    gate: dict[str, Any],
    final_status: str,
) -> dict[str, Any]:
    report = dict(quality_report or {})
    summary = dict(report.get("summary") or {})
    prediction_trace_id = normalize_prediction_trace_id(
        candidate.get("prediction_trace_id"),
        candidate.get("trace_id"),
        fallback=summary.get("trace_id"),
    )
    gate_a = _gate_a_payload(candidate)
    gate_b = _gate_b_payload(candidate, gate)
    gate_c = _gate_c_payload(candidate, gate, final_status)
    for key, value in (
        ("prediction_trace_id", prediction_trace_id),
        ("trace_id", prediction_trace_id),
        ("research_protocol_version", candidate.get("research_protocol_version")),
        ("candidate_contract_version", candidate.get("candidate_contract_version")),
        ("spec_completeness", candidate.get("spec_completeness")),
        ("field_provenance_summary", dict(candidate.get("field_provenance_summary") or {})),
        ("completion_issues", list(candidate.get("completion_issues") or [])),
        ("hard_failures", list(candidate.get("hard_failures") or [])),
        ("gate_a", gate_a),
        ("gate_b", gate_b),
        ("gate_c", gate_c),
    ):
        if value in (None, "", [], {}):
            continue
        summary[key] = value
        report[key] = value
    report["summary"] = summary
    return report

def _compat_setting(name: str, default):
    return default


def _normalized_spec_completeness_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {"warn", "revise", "reject"}:
        return "warn"
    return mode


def _spec_completeness_mode() -> str:
    return _normalized_spec_completeness_mode(
        _compat_setting("STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE", STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE)
    )


def _build_gate_a_spec_override(candidate: dict[str, Any]) -> Optional[dict[str, Any]]:
    if str(candidate.get("spec_completeness") or "complete").strip().lower() != "incomplete":
        return None
    mode = _spec_completeness_mode()
    hard_failure_codes = _compact_unique(
        [dict(item or {}).get("reason_code") for item in _candidate_hard_failures(candidate)]
    )
    if mode == "warn" and not hard_failure_codes:
        return None
    gate_a = _gate_a_payload(candidate)
    blocking_reasons = [str(item).strip() for item in list(gate_a.get("blocking_reasons") or []) if str(item).strip()]
    revision_actions = [str(item).strip() for item in list(gate_a.get("revision_actions") or []) if str(item).strip()]
    decision = "reject" if (mode == "reject" or hard_failure_codes) else "revise"
    trigger = (
        "research_protocol_hard_failure"
        if hard_failure_codes
        else "research_protocol_required_fields_rejected"
        if decision == "reject"
        else "research_protocol_required_fields_need_revision"
    )
    reasons = list(dict.fromkeys([trigger, *hard_failure_codes, *blocking_reasons]))
    warning_codes = ["spec_completeness_incomplete"]
    return normalize_quality_gate_result(
        {
            "passed": False,
            "passed_strict": False,
            "provisional_pass": False,
            "reasons": reasons,
            "reason_codes": reasons,
            "warning_codes": warning_codes,
            "warnings": warning_codes,
            "admission_stage": "gate_a",
            "incubation_pass_mode": decision,
            "research_candidate_ready": decision == "revise",
            "incubation_candidate_ready": False,
            "live_candidate_ready": False,
            "strict_incubation_ready": False,
            "strict_incubation_blocked": True,
            "admission_block_reasons": reasons,
            "gate_protocol": f"strategy_factory.research_protocol.v2:gate_a_{decision}",
            "gate_a_decision": decision,
            "spec_completeness_mode": mode,
            "spec_completeness": "incomplete",
            "completion_issues": list(candidate.get("completion_issues") or []),
            "hard_failures": list(candidate.get("hard_failures") or []),
            "revision_actions": revision_actions,
        }
    )
