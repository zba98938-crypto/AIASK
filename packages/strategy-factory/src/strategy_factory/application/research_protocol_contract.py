"""Research protocol v2 helpers for compatible strategy-factory rollout."""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from dataclasses import asdict
from typing import Any, Mapping

from ..api.contracts import ResearchValidationContract

RESEARCH_PROTOCOL_CONTRACT_VERSION = "strategy_factory.research_protocol.v2"
CANDIDATE_CONTRACT_V2 = "strategy_factory.candidate_contract.v2"
PREDICTION_TRACE_CONTRACT_VERSION = "strategy_factory.prediction_trace.v2"
SPEC_COMPLETENESS_REQUIRED_FIELDS = (
    "walk_forward_config",
    "baseline_reference",
    "cash_sleeve_policy",
    "cost_sensitivity_grid",
    "capacity_execution",
    "multiple_testing",
    "admission_thresholds",
    "family_holding_bucket",
)


def _string(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _normalized_decision(value: Any, default: str = "revise") -> str:
    token = _string(value).lower()
    if token in {"pass", "revise", "reject", "pending"}:
        return token
    return default


def normalize_field_provenance_token(value: Any) -> str:
    token = _string(value).lower()
    if token in {"user_asserted", "derived", "validated", "missing"}:
        return token
    if token in {
        "user",
        "metadata",
        "source_candidate",
        "params",
        "source_candidate_params",
        "explicit",
        "provided",
        "asserted",
    }:
        return "user_asserted"
    if token in {
        "validated",
        "verified",
        "measured",
        "audited",
        "checked",
    }:
        return "validated"
    if token in {
        "default",
        "fallback",
        "legacy",
        "legacy_fill",
        "legacy_default",
        "missing",
        "absent",
        "none",
    }:
        return "missing"
    if token:
        return "derived"
    return "missing"


def build_field_provenance_summary(field_provenance: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized: dict[str, str] = {}
    counts = {"user_asserted": 0, "derived": 0, "validated": 0, "missing": 0}
    for field_name, raw_value in dict(field_provenance or {}).items():
        key = _string(field_name)
        if not key:
            continue
        token = normalize_field_provenance_token(raw_value)
        normalized[key] = token
        counts[token] = counts.get(token, 0) + 1
    return {
        "counts": counts,
        "fields": normalized,
        "missing_required_fields": [
            field_name
            for field_name in SPEC_COMPLETENESS_REQUIRED_FIELDS
            if normalized.get(field_name, "missing") == "missing"
        ],
    }


def _normalize_hard_failures(hard_failures: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in list(hard_failures or []):
        payload = dict(item or {})
        field_name = _string(payload.get("field"))
        reason_code = _string(payload.get("reason_code") or payload.get("issue") or field_name)
        if not reason_code:
            continue
        dedupe_key = (field_name, reason_code)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        issue = _string(payload.get("issue")) or "hard_failure"
        detail = _string(payload.get("detail"))
        normalized_payload = {
            "reason_code": reason_code,
            "issue": issue,
            "severity": "reject",
            "decision": "reject",
        }
        if field_name:
            normalized_payload["field"] = field_name
        if detail:
            normalized_payload["detail"] = detail
        normalized.append(normalized_payload)
    return normalized


def build_completion_issues(
    field_provenance: Mapping[str, Any] | None,
    hard_failures: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    summary = build_field_provenance_summary(field_provenance)
    issues: list[dict[str, Any]] = []
    for field_name in list(summary.get("missing_required_fields") or []):
        issues.append(
            {
                "field": field_name,
                "severity": "revise",
                "decision": "revise",
                "issue": "missing_required_field",
                "reason_code": "research_protocol_required_field_missing",
            }
        )
    issues.extend(_normalize_hard_failures(hard_failures))
    return issues


def resolve_spec_completeness(
    field_provenance: Mapping[str, Any] | None,
    completion_issues: list[dict[str, Any]] | None = None,
    hard_failures: list[dict[str, Any]] | None = None,
) -> str:
    issues = list(completion_issues or build_completion_issues(field_provenance, hard_failures))
    return "incomplete" if issues else "complete"


def normalize_prediction_trace_id(
    prediction_trace_id: Any = None,
    trace_id: Any = None,
    *,
    fallback: Any = None,
) -> str:
    for value in (prediction_trace_id, trace_id, fallback):
        token = _string(value)
        if token:
            return token
    return ""


def build_research_validation_contract(
    *,
    walk_forward_config: Mapping[str, Any] | None = None,
    baseline_reference: Mapping[str, Any] | None = None,
    cash_sleeve_policy: Mapping[str, Any] | None = None,
    cost_sensitivity_grid: Mapping[str, Any] | None = None,
    capacity_execution: Mapping[str, Any] | None = None,
    multiple_testing: Mapping[str, Any] | None = None,
    admission_thresholds: Mapping[str, Any] | None = None,
    family_holding_bucket: Mapping[str, Any] | None = None,
    field_provenance: Mapping[str, Any] | None = None,
    recommended_defaults: Mapping[str, Any] | None = None,
    hard_failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary = build_field_provenance_summary(field_provenance)
    normalized_hard_failures = _normalize_hard_failures(hard_failures)
    completion_issues = build_completion_issues(field_provenance, normalized_hard_failures)
    section_payloads = {
        "walk_forward_config": dict(walk_forward_config or {}),
        "baseline_reference": dict(baseline_reference or {}),
        "cash_sleeve_policy": dict(cash_sleeve_policy or {}),
        "cost_sensitivity_grid": dict(cost_sensitivity_grid or {}),
        "capacity_execution": dict(capacity_execution or {}),
        "multiple_testing": dict(multiple_testing or {}),
        "admission_thresholds": dict(admission_thresholds or {}),
        "family_holding_bucket": dict(family_holding_bucket or {}),
    }
    effective_contract = {
        field_name: dict(value or {})
        for field_name, value in section_payloads.items()
        if dict(value or {})
    }
    defaults_payload = {
        _string(field_name): dict(value or {})
        for field_name, value in dict(recommended_defaults or {}).items()
        if _string(field_name) and dict(value or {})
    }
    contract = ResearchValidationContract(
        contract_version=RESEARCH_PROTOCOL_CONTRACT_VERSION,
        walk_forward_config=dict(section_payloads.get("walk_forward_config") or {}),
        baseline_reference=dict(section_payloads.get("baseline_reference") or {}),
        cash_sleeve_policy=dict(section_payloads.get("cash_sleeve_policy") or {}),
        cost_sensitivity_grid=dict(section_payloads.get("cost_sensitivity_grid") or {}),
        capacity_execution=dict(section_payloads.get("capacity_execution") or {}),
        multiple_testing=dict(section_payloads.get("multiple_testing") or {}),
        admission_thresholds=dict(section_payloads.get("admission_thresholds") or {}),
        family_holding_bucket=dict(section_payloads.get("family_holding_bucket") or {}),
        effective_contract=effective_contract,
        recommended_defaults=defaults_payload,
        field_provenance=dict(summary.get("fields") or {}),
        field_provenance_summary=summary,
        spec_completeness=resolve_spec_completeness(
            field_provenance,
            completion_issues,
            normalized_hard_failures,
        ),
        completion_issues=completion_issues,
        hard_failures=normalized_hard_failures,
    )
    return asdict(contract)


def adapt_research_validation_contract_for_submission(
    contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(contract or {})
    effective_contract = dict(payload.get("effective_contract") or {})
    walk_forward = dict(effective_contract.get("walk_forward_config") or payload.get("walk_forward_config") or {})
    multiple_testing = dict(effective_contract.get("multiple_testing") or payload.get("multiple_testing") or {})
    thresholds = dict(
        effective_contract.get("admission_thresholds") or payload.get("admission_thresholds") or {}
    )
    family_holding_bucket = dict(
        effective_contract.get("family_holding_bucket") or payload.get("family_holding_bucket") or {}
    )
    baseline_reference = dict(
        effective_contract.get("baseline_reference") or payload.get("baseline_reference") or {}
    )
    cash_sleeve_policy = dict(
        effective_contract.get("cash_sleeve_policy") or payload.get("cash_sleeve_policy") or {}
    )
    cost_sensitivity_grid = dict(
        effective_contract.get("cost_sensitivity_grid") or payload.get("cost_sensitivity_grid") or {}
    )
    capacity_execution = dict(
        effective_contract.get("capacity_execution") or payload.get("capacity_execution") or {}
    )
    validation_profile_payload = dict(thresholds.get("validation_profile") or {})
    return {
        "validation_profile": {
            "profile": _string(validation_profile_payload.get("profile"))
            or _string(thresholds.get("profile")),
            "validation_focus": _string(validation_profile_payload.get("validation_focus"))
            or _string(thresholds.get("validation_focus")),
            "primary_validation_layer": _string(validation_profile_payload.get("primary_validation_layer"))
            or _string(thresholds.get("primary_validation_layer")),
        },
        "walk_forward": walk_forward,
        "multiple_testing": multiple_testing,
        "admission_thresholds": thresholds,
        "family_holding_bucket": family_holding_bucket,
        "baseline_reference": baseline_reference,
        "cash_sleeve_policy": cash_sleeve_policy,
        "cost_sensitivity_grid": cost_sensitivity_grid,
        "capacity_execution": capacity_execution,
        "effective_contract": effective_contract,
        "recommended_defaults": dict(payload.get("recommended_defaults") or {}),
        "research_protocol_version": _string(payload.get("contract_version")) or RESEARCH_PROTOCOL_CONTRACT_VERSION,
        "spec_completeness": _string(payload.get("spec_completeness")) or "complete",
        "completion_issues": list(payload.get("completion_issues") or []),
        "hard_failures": list(payload.get("hard_failures") or []),
        "field_provenance_summary": dict(payload.get("field_provenance_summary") or {}),
    }


def evaluate_research_validation_contract_admission(
    contract: Mapping[str, Any] | None,
    *,
    observed: Mapping[str, Any] | None = None,
    spec_completeness_mode: str = "revise",
) -> dict[str, Any]:
    payload = dict(contract or {})
    if not payload:
        return {
            "available": False,
            "review_decision": "pending",
            "blocking_reasons": [],
            "warnings": [],
            "business_admission_decision": {
                "decision": "pending",
                "status": "pending",
                "reasons": [],
            },
            "benchmark_comparison": {},
            "cost_sensitivity_summary": {},
            "cash_sleeve_audit": {},
            "family_holding_bucket": {},
            "admission_thresholds": {},
            "artifact_ids": [],
            "retrieval_context_ids": [],
            "prediction_trace_id": None,
        }

    observed_payload = dict(observed or {})
    submission_view = adapt_research_validation_contract_for_submission(payload)
    field_provenance_summary = dict(
        submission_view.get("field_provenance_summary")
        or payload.get("field_provenance_summary")
        or {}
    )
    missing_required_fields = [
        _string(item)
        for item in list(field_provenance_summary.get("missing_required_fields") or [])
        if _string(item)
    ]
    missing_gate_fields = [
        field_name
        for field_name in (
            "baseline_reference",
            "cash_sleeve_policy",
            "cost_sensitivity_grid",
            "admission_thresholds",
            "family_holding_bucket",
        )
        if field_name in missing_required_fields
    ]
    hard_failures = [
        dict(item or {})
        for item in list(submission_view.get("hard_failures") or payload.get("hard_failures") or [])
        if isinstance(item, MappingABC)
    ]
    completion_issues = [
        dict(item or {})
        for item in list(submission_view.get("completion_issues") or payload.get("completion_issues") or [])
        if isinstance(item, MappingABC)
    ]
    admission_thresholds = dict(submission_view.get("admission_thresholds") or {})
    business_gate = dict(admission_thresholds.get("business_admission_gate") or {})
    baseline_reference = dict(submission_view.get("baseline_reference") or {})
    cash_sleeve_policy = dict(submission_view.get("cash_sleeve_policy") or {})
    cost_sensitivity_grid = dict(submission_view.get("cost_sensitivity_grid") or {})
    family_holding_bucket = dict(submission_view.get("family_holding_bucket") or {})
    normalized_mode = _normalized_decision(spec_completeness_mode, default="revise")
    blocking_reasons: list[str] = []
    warnings: list[str] = []

    if hard_failures:
        for item in hard_failures:
            code = _string(item.get("reason_code") or item.get("issue") or item.get("field"))
            if code and code not in blocking_reasons:
                blocking_reasons.append(code)

    if missing_gate_fields:
        issue_code = (
            "research_protocol_required_fields_rejected"
            if normalized_mode == "reject" or hard_failures
            else "research_protocol_required_fields_need_revision"
        )
        if issue_code not in blocking_reasons:
            blocking_reasons.append(issue_code)
        for field_name in missing_gate_fields:
            code = f"research_protocol_missing:{field_name}"
            if code not in blocking_reasons:
                blocking_reasons.append(code)

    benchmark_return_multiple_min = _safe_float(
        business_gate.get("benchmark_return_multiple_min")
        or business_gate.get("benchmark_return_multiple_vs_benchmark_min")
        or business_gate.get("return_multiple_vs_benchmark_min")
    )
    if benchmark_return_multiple_min is None:
        benchmark_return_multiple_min = 2.0
    drawdown_mode = _string(
        business_gate.get("benchmark_drawdown_mode")
        or business_gate.get("max_drawdown_relative_to_benchmark")
        or "lte"
    ).lower() or "lte"
    target_oos_return = _safe_float(
        observed_payload.get("oos_cagr")
        if observed_payload.get("oos_cagr") is not None
        else observed_payload.get("target_layer_oos_return")
    )
    if target_oos_return is None:
        target_oos_return = _safe_float(observed_payload.get("total_return"))
    benchmark_oos_return = _safe_float(
        observed_payload.get("benchmark_oos_cagr")
        if observed_payload.get("benchmark_oos_cagr") is not None
        else observed_payload.get("benchmark_target_layer_oos_return")
    )
    if benchmark_oos_return is None:
        benchmark_oos_return = _safe_float(
            baseline_reference.get("benchmark_oos_cagr")
            or baseline_reference.get("benchmark_return")
        )
    target_oos_mdd = _safe_float(
        observed_payload.get("oos_max_drawdown")
        if observed_payload.get("oos_max_drawdown") is not None
        else observed_payload.get("max_drawdown")
    )
    benchmark_oos_mdd = _safe_float(
        observed_payload.get("benchmark_oos_max_drawdown")
        if observed_payload.get("benchmark_oos_max_drawdown") is not None
        else observed_payload.get("benchmark_max_drawdown")
    )
    if benchmark_oos_mdd is None:
        benchmark_oos_mdd = _safe_float(baseline_reference.get("benchmark_oos_max_drawdown"))
    benchmark_gate_required = bool(
        business_gate
        or _safe_float(baseline_reference.get("benchmark_oos_cagr")) is not None
        or _safe_float(baseline_reference.get("benchmark_return")) is not None
        or _safe_float(baseline_reference.get("benchmark_oos_max_drawdown")) is not None
    )
    benchmark_comparison_reasons: list[str] = []
    benchmark_passed = None
    if (
        target_oos_return is not None
        and benchmark_oos_return is not None
        and target_oos_mdd is not None
        and benchmark_oos_mdd is not None
    ):
        benchmark_passed = (
            target_oos_return >= benchmark_oos_return * benchmark_return_multiple_min
            and (
                abs(target_oos_mdd) <= abs(benchmark_oos_mdd)
                if drawdown_mode == "lte"
                else abs(target_oos_mdd) < abs(benchmark_oos_mdd)
            )
        )
        if not benchmark_passed:
            benchmark_comparison_reasons.extend(
                [
                    "business_gate_benchmark_return_multiple_failed",
                    "business_gate_benchmark_max_drawdown_failed",
                ]
            )
    elif benchmark_gate_required:
        benchmark_comparison_reasons.append("business_gate_missing_benchmark_comparison_inputs")
        if "business_gate_missing_benchmark_comparison_inputs" not in warnings:
            warnings.append("business_gate_missing_benchmark_comparison_inputs")

    contract_family = _string(
        family_holding_bucket.get("family")
        or admission_thresholds.get("family")
    ) or None
    contract_holding_bucket = _string(
        family_holding_bucket.get("holding_bucket")
        or admission_thresholds.get("holding_bucket")
    ) or None
    enforce_family_alignment = family_holding_bucket.get("enforce_family_alignment")
    if enforce_family_alignment is None:
        enforce_family_alignment = bool(contract_family)
    enforce_holding_bucket_alignment = family_holding_bucket.get("enforce_holding_bucket_alignment")
    if enforce_holding_bucket_alignment is None:
        enforce_holding_bucket_alignment = bool(contract_holding_bucket)
    observed_family = _string(observed_payload.get("family")) or contract_family
    observed_holding_bucket = _string(observed_payload.get("holding_bucket")) or contract_holding_bucket
    family_alignment_reasons: list[str] = []
    if (
        enforce_family_alignment
        and contract_family
        and observed_family
        and contract_family.lower() != observed_family.lower()
    ):
        family_alignment_reasons.append("research_protocol_family_mismatch")
    if (
        enforce_holding_bucket_alignment
        and
        contract_holding_bucket
        and observed_holding_bucket
        and contract_holding_bucket.lower() != observed_holding_bucket.lower()
    ):
        family_alignment_reasons.append("research_protocol_holding_bucket_mismatch")

    scenario_bps_candidates: list[float] = []
    for value in list(business_gate.get("cost_sensitivity_required_bps") or []):
        parsed = _safe_float(value)
        if parsed is not None and parsed not in scenario_bps_candidates:
            scenario_bps_candidates.append(parsed)
    for key in (
        "control_scenario_slippage_bps",
        "base_slippage_bps",
        "main_scenario_slippage_bps",
        "stress_slippage_bps",
    ):
        parsed = _safe_float(cost_sensitivity_grid.get(key))
        if parsed is not None and parsed not in scenario_bps_candidates:
            scenario_bps_candidates.append(parsed)
    for value in list(cost_sensitivity_grid.get("slippage_bps_grid") or []):
        parsed = _safe_float(value)
        if parsed is not None and parsed not in scenario_bps_candidates:
            scenario_bps_candidates.append(parsed)
    cost_scenarios = dict(
        observed_payload.get("cost_sensitivity_results")
        or observed_payload.get("cost_scenarios")
        or {}
    )
    observed_scenario_rows: list[dict[str, Any]] = []
    observed_bps: list[float] = []
    if cost_scenarios:
        for raw_key, raw_value in cost_scenarios.items():
            parsed_bps = _safe_float(raw_key)
            if parsed_bps is None:
                parsed_bps = _safe_float(dict(raw_value or {}).get("slippage_bps"))
            if parsed_bps is None:
                continue
            if parsed_bps not in observed_bps:
                observed_bps.append(parsed_bps)
            observed_scenario_rows.append(
                {
                    "slippage_bps": round(parsed_bps, 4),
                    "post_cost_sharpe": _safe_float(dict(raw_value or {}).get("post_cost_sharpe")),
                    "total_return": _safe_float(dict(raw_value or {}).get("total_return")),
                    "max_drawdown": _safe_float(dict(raw_value or {}).get("max_drawdown")),
                    "available": True,
                }
            )
    observed_effective_bps = _safe_float(
        observed_payload.get("effective_total_bps")
        or observed_payload.get("observed_total_cost_bps")
    )
    if observed_effective_bps is not None and observed_effective_bps not in observed_bps:
        observed_bps.append(observed_effective_bps)
        observed_scenario_rows.append(
            {
                "slippage_bps": round(observed_effective_bps, 4),
                "post_cost_sharpe": _safe_float(observed_payload.get("post_cost_sharpe")),
                "total_return": _safe_float(observed_payload.get("total_return")),
                "max_drawdown": _safe_float(observed_payload.get("max_drawdown")),
                "available": True,
            }
        )
    missing_observed_bps = [
        round(item, 4)
        for item in scenario_bps_candidates
        if item not in observed_bps
    ]
    cost_sensitivity_summary = {
        "required_bps": [round(item, 4) for item in scenario_bps_candidates],
        "observed_bps": [round(item, 4) for item in observed_bps],
        "missing_observed_bps": missing_observed_bps,
        "scenarios": observed_scenario_rows,
        "available": bool(cost_sensitivity_grid),
        "review_decision": "pass" if not missing_observed_bps else "revise",
    }

    observed_cash_sleeve = dict(observed_payload.get("cash_sleeve") or {})
    expected_cash_enabled = bool(cash_sleeve_policy.get("enabled"))
    expected_schedule_clock = _string(cash_sleeve_policy.get("schedule_clock")) or None
    observed_cash_enabled = observed_cash_sleeve.get("enabled")
    observed_schedule_clock = _string(observed_cash_sleeve.get("schedule_clock")) or None
    cash_sleeve_reasons: list[str] = []
    if expected_cash_enabled and observed_cash_enabled is False:
        cash_sleeve_reasons.append("cash_sleeve_policy_disabled_in_runtime")
    if expected_schedule_clock and observed_schedule_clock and expected_schedule_clock != observed_schedule_clock:
        cash_sleeve_reasons.append("cash_sleeve_schedule_clock_mismatch")
    cash_sleeve_audit = {
        "required": bool(cash_sleeve_policy),
        "enabled": expected_cash_enabled,
        "schedule_clock": expected_schedule_clock,
        "observed_enabled": observed_cash_enabled,
        "observed_schedule_clock": observed_schedule_clock,
        "available": bool(cash_sleeve_policy),
        "reasons": cash_sleeve_reasons,
        "passed": len(cash_sleeve_reasons) == 0,
    }

    decision = "pass"
    if hard_failures:
        decision = "reject"
    elif missing_gate_fields:
        decision = "reject" if normalized_mode == "reject" else "revise"
    if benchmark_passed is False:
        decision = "reject"
        for code in benchmark_comparison_reasons:
            if code not in blocking_reasons:
                blocking_reasons.append(code)
    elif benchmark_comparison_reasons and decision == "pass":
        decision = "revise"
    if family_alignment_reasons:
        if decision == "pass":
            decision = "revise"
        for code in family_alignment_reasons:
            if code not in blocking_reasons:
                blocking_reasons.append(code)
    if decision in {"revise", "reject"} and benchmark_comparison_reasons:
        for code in benchmark_comparison_reasons:
            if code not in blocking_reasons:
                blocking_reasons.append(code)

    business_decision_payload = {
        "decision": decision,
        "status": (
            "passed"
            if decision == "pass"
            else "rejected"
            if decision == "reject"
            else "revision_required"
        ),
        "family": observed_family,
        "holding_bucket": observed_holding_bucket,
        "reasons": list(dict.fromkeys(blocking_reasons)),
        "spec_completeness": submission_view.get("spec_completeness") or payload.get("spec_completeness"),
        "spec_completeness_mode": normalized_mode,
    }
    benchmark_comparison = {
        "available": benchmark_passed is not None,
        "passed": benchmark_passed,
        "return_multiple_min": round(float(benchmark_return_multiple_min), 4),
        "drawdown_mode": drawdown_mode,
        "oos_cagr": target_oos_return,
        "benchmark_oos_cagr": benchmark_oos_return,
        "oos_max_drawdown": abs(target_oos_mdd) if target_oos_mdd is not None else None,
        "benchmark_oos_max_drawdown": abs(benchmark_oos_mdd) if benchmark_oos_mdd is not None else None,
        "reasons": benchmark_comparison_reasons,
    }
    return {
        "available": True,
        "review_decision": decision,
        "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
        "warnings": list(dict.fromkeys(warnings)),
        "business_admission_decision": business_decision_payload,
        "benchmark_comparison": benchmark_comparison,
        "cost_sensitivity_summary": cost_sensitivity_summary,
        "cash_sleeve_audit": cash_sleeve_audit,
        "family_holding_bucket": family_holding_bucket,
        "admission_thresholds": admission_thresholds,
        "completion_issues": completion_issues,
        "hard_failures": hard_failures,
        "artifact_ids": [
            _string(item)
            for item in list(observed_payload.get("artifact_ids") or [])
            if _string(item)
        ],
        "retrieval_context_ids": [
            _string(item)
            for item in list(observed_payload.get("retrieval_context_ids") or [])
            if _string(item)
        ],
        "prediction_trace_id": _string(
            observed_payload.get("prediction_trace_id")
            or observed_payload.get("trace_id")
        ) or None,
    }


__all__ = [
    "CANDIDATE_CONTRACT_V2",
    "PREDICTION_TRACE_CONTRACT_VERSION",
    "RESEARCH_PROTOCOL_CONTRACT_VERSION",
    "SPEC_COMPLETENESS_REQUIRED_FIELDS",
    "adapt_research_validation_contract_for_submission",
    "build_completion_issues",
    "build_field_provenance_summary",
    "build_research_validation_contract",
    "evaluate_research_validation_contract_admission",
    "normalize_field_provenance_token",
    "normalize_prediction_trace_id",
    "resolve_spec_completeness",
]
