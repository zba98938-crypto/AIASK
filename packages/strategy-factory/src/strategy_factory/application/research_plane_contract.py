"""Research plane artifact contracts for strategy factory runs.

P1 goal: let factor research, task planning, and autonomy execution expose a
stable research-plane output that can be observed independently from the
submission / incubation pipeline.
"""

from __future__ import annotations

from typing import Any

from .research.candidate_origin import (
    EXTERNAL_AUTONOMY_CANDIDATE_ORIGIN,
    GOVERNED_CANDIDATE_ACTIVATION_ORIGIN,
    LOCAL_RULE_CANDIDATE_ORIGIN,
    OPEN_RESEARCH_TASK_ORIGIN,
    classify_research_candidate_origin,
    classify_research_task_origin,
    count_candidate_origins,
    count_task_origins,
)
from .services.readiness_service import resolve_governed_pool_state

RESEARCH_PLANE_CONTRACT_VERSION = "strategy_factory.research_plane.v1"
RESEARCH_ARTIFACT_CONTRACT_VERSION = "strategy_factory.research_artifact.v1"
TASK_ARTIFACT_CONTRACT_VERSION = "strategy_factory.task_artifact.v1"
CANDIDATE_ARTIFACT_CONTRACT_VERSION = "strategy_factory.candidate_artifact.v1"
RESEARCH_EVIDENCE_ARTIFACT_CONTRACT_VERSION = "strategy_factory.research_evidence_artifact.v1"


def _string(value: Any) -> str:
    return str(value or "").strip()


def _normalized_text(value: Any) -> str:
    return _string(value).lower()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _router_telemetry_from_task_scan(
    task_scan_summary: dict[str, Any],
    router_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = dict(router_artifact or task_scan_summary or {})
    return {
        "router_artifact_contract_version": source.get("contract_version"),
        "router_enabled": bool(source.get("router_enabled")),
        "router_strict": bool(source.get("router_strict")),
        "router_telemetry_enabled": bool(source.get("router_telemetry_enabled")),
        "router_candidate_stock_count": _safe_int(source.get("router_candidate_stock_count")),
        "router_applied_count": _safe_int(source.get("router_applied_count")),
        "router_status_counts": dict(source.get("router_status_counts") or {}),
        "router_fallback_reason_counts": dict(source.get("router_fallback_reason_counts") or {}),
        "router_family_counts": dict(source.get("router_family_counts") or {}),
        "router_holding_bucket_counts": dict(source.get("router_holding_bucket_counts") or {}),
        "profile_summary_present_count": _safe_int(source.get("profile_summary_present_count")),
        "profile_summary_missing_count": _safe_int(source.get("profile_summary_missing_count")),
        "profile_summary_generated_count": _safe_int(source.get("profile_summary_generated_count")),
        "selected_task_count": _safe_int(source.get("selected_task_count")),
        "selected_router_applied_count": _safe_int(source.get("selected_router_applied_count")),
        "selected_profile_summary_missing_count": _safe_int(
            source.get("selected_profile_summary_missing_count")
        ),
    }


def _direction_gate_telemetry_from_task_scan(
    task_scan_summary: dict[str, Any],
    direction_gate_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = dict(direction_gate_artifact or task_scan_summary or {})
    return {
        "direction_gate_artifact_contract_version": source.get("contract_version"),
        "direction_gate_enabled": bool(source.get("direction_gate_enabled")),
        "direction_gate_candidate_stock_count": _safe_int(source.get("direction_gate_candidate_stock_count")),
        "direction_gate_evaluated_count": _safe_int(source.get("direction_gate_evaluated_count")),
        "direction_gate_applied_count": _safe_int(source.get("direction_gate_applied_count")),
        "direction_gate_fallback_count": _safe_int(source.get("direction_gate_fallback_count")),
        "direction_gate_status_counts": dict(source.get("direction_gate_status_counts") or {}),
        "direction_gate_reason_counts": dict(source.get("direction_gate_reason_counts") or {}),
        "direction_gate_dropped_family_counts": dict(source.get("direction_gate_dropped_family_counts") or {}),
        "selected_direction_gate_applied_count": _safe_int(source.get("selected_direction_gate_applied_count")),
        "selected_direction_gate_fallback_count": _safe_int(source.get("selected_direction_gate_fallback_count")),
        "selected_direction_gate_task_count": _safe_int(source.get("selected_direction_gate_task_count")),
    }


def _compact_list(values: Any, *, limit: int = 8) -> list[str]:
    items: list[str] = []
    for value in list(values or []):
        token = _string(value)
        if token and token not in items:
            items.append(token)
        if len(items) >= limit:
            break
    return items


def _count_by(items: list[dict[str, Any]], resolver) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in list(items or []):
        key = _normalized_text(resolver(dict(item or {})))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def _prepare_candidate_for_artifact(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = dict(candidate or {})
    if not payload:
        return {}
    try:
        from .candidate_contract import apply_resolved_candidate_envelope

        return apply_resolved_candidate_envelope(payload)
    except Exception:
        return payload


def _candidate_contract_snapshot(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = _prepare_candidate_for_artifact(candidate)
    params = dict(payload.get("params") or {})
    return dict(payload.get("candidate_contract_snapshot") or params.get("candidate_contract_snapshot") or {})


def _candidate_has_evidence(candidate: dict[str, Any]) -> bool:
    payload = _prepare_candidate_for_artifact(candidate)
    params = dict(payload.get("params") or {})
    provenance = dict(params.get("candidate_provenance") or {})
    evidence_status = dict(
        payload.get("candidate_evidence_status")
        or provenance.get("candidate_evidence_status")
        or {}
    )
    return bool(
        evidence_status
        or payload.get("evidence_bundle")
        or payload.get("evidence_chain")
        or params.get("evidence_chain")
        or payload.get("prediction_contract")
        or params.get("prediction_contract")
    )


def _resolve_contract_artifact(
    payload: dict[str, Any] | None,
    *,
    expected_version: str,
) -> dict[str, Any]:
    artifact = dict(payload or {})
    if str(artifact.get("contract_version") or "").strip() != expected_version:
        return {}
    return artifact


def _candidate_family(candidate: dict[str, Any]) -> str:
    payload = dict(candidate or {})
    research_task = dict(payload.get("research_task") or {})
    params = dict(payload.get("params") or {})
    provenance = dict(params.get("candidate_provenance") or {})
    return (
        _normalized_text(
            payload.get("candidate_family")
            or research_task.get("candidate_family")
            or provenance.get("candidate_family")
            or payload.get("strategy_type")
        )
        or "unknown"
    )


def _candidate_task_source(candidate: dict[str, Any]) -> str:
    payload = dict(candidate or {})
    research_task = dict(payload.get("research_task") or {})
    return _normalized_text(research_task.get("task_source") or payload.get("task_source")) or "unknown"


def _candidate_generator_mode(candidate: dict[str, Any]) -> str:
    payload = dict(candidate or {})
    params = dict(payload.get("params") or {})
    provenance = dict(params.get("candidate_provenance") or {})
    return (
        _normalized_text(
            payload.get("generator_mode")
            or payload.get("generator_type")
            or provenance.get("generator_mode")
            or provenance.get("generator_type")
        )
        or "unknown"
    )


def _compact_task_brief(task: dict[str, Any]) -> dict[str, Any]:
    payload = dict(task or {})
    return {
        "task_id": _string(payload.get("task_id") or payload.get("task_key")) or None,
        "task_source": _string(payload.get("task_source")) or None,
        "task_origin": classify_research_task_origin(payload),
        "opportunity_type": _string(payload.get("opportunity_type")) or None,
        "candidate_family": _string(payload.get("candidate_family")) or None,
        "factor_name": _string(payload.get("factor_name")) or None,
        "source_candidate_artifact_id": _string(payload.get("source_candidate_artifact_id")) or None,
        "target_symbols": _compact_list(payload.get("target_symbols"), limit=12),
        "generation_limit": _safe_int(payload.get("generation_limit")),
        "event_id": _string(payload.get("event_id")) or None,
        "theme_code": _string(payload.get("theme_code")) or None,
    }


def _compact_task_result_brief(task_result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(task_result or {})
    brief = _compact_task_brief(payload.get("task") or {})
    brief.update(
        {
            "task_run_id": payload.get("task_run_id"),
            "status": _string(payload.get("status")) or None,
            "generated_count": _safe_int(payload.get("generated_count")),
            "reviewed_count": _safe_int(payload.get("reviewed_count")),
            "evidence_count": _safe_int(payload.get("evidence_count")),
            "external_llm_status": _string(payload.get("external_llm_status")) or None,
        }
    )
    return brief


def _compact_candidate_brief(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = _prepare_candidate_for_artifact(candidate)
    params = dict(payload.get("params") or {})
    provenance = dict(params.get("candidate_provenance") or {})
    contract_snapshot = _candidate_contract_snapshot(payload)
    targeting = dict(contract_snapshot.get("targeting") or {})
    return {
        "name": _string(payload.get("name")) or None,
        "strategy_type": _string(payload.get("strategy_type")) or None,
        "candidate_family": _candidate_family(payload),
        "task_source": _candidate_task_source(payload),
        "generator_mode": _candidate_generator_mode(payload),
        "research_candidate_origin": classify_research_candidate_origin(payload),
        "target_pool_id": _string(
            payload.get("target_pool_id")
            or targeting.get("target_pool_id")
        )
        or None,
        "target_symbols": _compact_list(
            payload.get("target_symbols")
            or targeting.get("target_symbols"),
            limit=12,
        ),
        "source_candidate_artifact_id": _string(
            payload.get("source_candidate_artifact_id")
            or dict(payload.get("research_task") or {}).get("source_candidate_artifact_id")
            or provenance.get("source_candidate_artifact_id")
        )
        or None,
        "experiment_id": _string(payload.get("experiment_id")) or None,
        "candidate_contract_ready": bool(contract_snapshot),
        "evidence_ready": _candidate_has_evidence(payload),
    }


def _compact_experiment_brief(experiment: dict[str, Any]) -> dict[str, Any]:
    payload = dict(experiment or {})
    return {
        "experiment_id": _string(payload.get("experiment_id") or payload.get("id")) or None,
        "task_id": _string(payload.get("task_id")) or None,
        "strategy_id": _string(payload.get("strategy_id")) or None,
        "status": _string(payload.get("status")) or None,
        "generator_mode": _string(payload.get("generator_mode")) or None,
    }


def build_research_artifact(
    *,
    factor_research: dict[str, Any] | None = None,
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact = dict(factor_research or {})
    summary = dict(artifact.get("summary") or {})
    readiness_payload = dict(readiness or {})
    governed_pool_state = resolve_governed_pool_state(summary)
    lifecycle_feedback_input = dict(artifact.get("lifecycle_feedback_input") or {})
    top_candidate_lineage = [
        {
            "artifact_id": _string(item.get("artifact_id")) or None,
            "name": _string(item.get("name")) or None,
            "family": _string(item.get("family")) or None,
            "registry_stage": _string(item.get("registry_stage")) or None,
            "latest_validation_at": item.get("latest_validation_at"),
        }
        for item in list(artifact.get("top_candidate_lineage") or [])[:3]
        if isinstance(item, dict)
    ]
    search_route_actions = [
        {
            "family": _string(item.get("family")) or None,
            "action": _string(item.get("action")) or None,
            "control_mode": _string(item.get("control_mode")) or None,
            "budget_weight": round(_safe_float(item.get("budget_weight")), 4),
            "budget_multiplier": round(_safe_float(item.get("budget_multiplier"), 1.0), 4),
            "priority_adjustment": round(_safe_float(item.get("priority_adjustment")), 4),
            "reasons": _compact_list(item.get("reasons"), limit=6),
        }
        for item in list(
            artifact.get("search_route_actions") or summary.get("search_route_actions") or []
        )[:12]
        if isinstance(item, dict)
    ]
    search_route_action_counts = _count_by(search_route_actions, lambda item: item.get("action"))
    return {
        "contract_version": RESEARCH_ARTIFACT_CONTRACT_VERSION,
        "available": bool(artifact),
        "factor_source_mode": summary.get("factor_source_mode"),
        "degraded": bool(artifact.get("degraded")),
        "stale": bool(summary.get("stale")),
        "active_factor_count": _safe_int(summary.get("active_factor_count")),
        "active_candidate_count": _safe_int(summary.get("active_candidate_count")),
        "active_family_names": _compact_list(summary.get("active_family_names"), limit=12),
        "active_regime_names": _compact_list(summary.get("active_regime_names"), limit=12),
        "top_factor_names": _compact_list(summary.get("top_factor_names"), limit=8),
        "top_candidate_names": _compact_list(summary.get("top_candidate_names"), limit=8),
        "family_preference_order": _compact_list(summary.get("family_preference_order"), limit=12),
        "family_preference_source_mode": summary.get("family_preference_source_mode"),
        "governed_candidate_pool_active": bool(governed_pool_state.get("active")),
        "governed_candidate_pool_mode": summary.get("governed_candidate_pool_mode"),
        "governed_candidate_pool_provisional": bool(summary.get("governed_candidate_pool_provisional")),
        "governed_candidate_pool_provisional_spillover_policy_status": summary.get(
            "governed_candidate_pool_provisional_spillover_policy_status"
        ),
        "governed_candidate_pool_provisional_pending_count": _safe_int(
            summary.get("governed_candidate_pool_provisional_pending_count")
        ),
        "governed_candidate_pool_strict_shortfall_count": _safe_int(
            summary.get("governed_candidate_pool_strict_shortfall_count")
        ),
        "stock_family_allocation_count": _safe_int(summary.get("stock_family_allocation_count")),
        "stock_family_allocation_source_mode": summary.get("stock_family_allocation_source_mode"),
        "factor_llm_provider_health_status": summary.get("factor_llm_provider_health_status"),
        "factor_llm_provider_ready": bool(summary.get("factor_llm_provider_ready")),
        "lifecycle_feedback_input_contract_version": lifecycle_feedback_input.get("contract_version"),
        "lifecycle_feedback_input_available": bool(lifecycle_feedback_input.get("available")),
        "lifecycle_feedback_family_count": _safe_int(summary.get("budget_feedback_family_count")),
        "lifecycle_feedback_strategy_count": _safe_int(summary.get("budget_feedback_strategy_count")),
        "lifecycle_feedback_target_pool_scope_count": _safe_int(
            summary.get("budget_feedback_target_pool_scope_count")
        ),
        "lifecycle_feedback_holding_bucket_scope_count": _safe_int(
            summary.get("budget_feedback_holding_bucket_scope_count")
        ),
        "lifecycle_feedback_generator_mode_scope_count": _safe_int(
            summary.get("budget_feedback_generator_mode_scope_count")
        ),
        "lifecycle_feedback_runtime_alert_count": _safe_int(
            summary.get("budget_feedback_runtime_alert_count")
        ),
        "lifecycle_feedback_runtime_risk_event_count": _safe_int(
            summary.get("budget_feedback_runtime_risk_event_count")
        ),
        "lifecycle_feedback_promotion_review_count": _safe_int(
            summary.get("budget_feedback_promotion_review_count")
        ),
        "lifecycle_feedback_promotion_review_status_counts": dict(
            summary.get("budget_feedback_promotion_review_status_counts") or {}
        ),
        "lifecycle_feedback_signal_count_total": _safe_int(
            summary.get("budget_feedback_signal_count_total")
        ),
        "lifecycle_feedback_zero_signal_strategy_count": _safe_int(
            summary.get("budget_feedback_zero_signal_strategy_count")
        ),
        "lifecycle_feedback_zero_signal_ratio": _safe_float(
            summary.get("budget_feedback_zero_signal_ratio")
        ),
        "lifecycle_feedback_low_signal_strategy_count": _safe_int(
            summary.get("budget_feedback_low_signal_strategy_count")
        ),
        "lifecycle_feedback_low_signal_ratio": _safe_float(
            summary.get("budget_feedback_low_signal_ratio")
        ),
        "lifecycle_feedback_observed_forward_window_count": _safe_int(
            summary.get("budget_feedback_observed_forward_window_count")
        ),
        "lifecycle_feedback_missing_forward_window_count": _safe_int(
            summary.get("budget_feedback_missing_forward_window_count")
        ),
        "lifecycle_feedback_expected_forward_window_count": _safe_int(
            summary.get("budget_feedback_expected_forward_window_count")
        ),
        "lifecycle_feedback_forward_window_coverage_ratio": _safe_float(
            summary.get("budget_feedback_forward_window_coverage_ratio"),
            1.0,
        ),
        "lifecycle_feedback_promotion_ready_count": _safe_int(
            summary.get("budget_feedback_promotion_ready_count")
        ),
        "lifecycle_feedback_promotion_ready_ratio": _safe_float(
            summary.get("budget_feedback_promotion_ready_ratio"),
            1.0,
        ),
        "lifecycle_feedback_promotion_review_coverage_ratio": _safe_float(
            summary.get("budget_feedback_promotion_review_coverage_ratio"),
            1.0,
        ),
        "lifecycle_feedback_evidence_debt_strategy_count": _safe_int(
            summary.get("budget_feedback_evidence_debt_strategy_count")
        ),
        "lifecycle_feedback_evidence_debt_ratio": _safe_float(
            summary.get("budget_feedback_evidence_debt_ratio")
        ),
        "family_reward_table": dict(
            artifact.get("family_reward_table") or summary.get("family_reward_table") or {}
        ),
        "family_debt_table": dict(
            artifact.get("family_debt_table") or summary.get("family_debt_table") or {}
        ),
        "search_route_actions": search_route_actions,
        "search_route_action_counts": (
            dict(summary.get("search_route_action_counts") or {}) or search_route_action_counts
        ),
        "source_chain": list(artifact.get("source_chain") or []),
        "readiness_reference": {
            "decision": readiness_payload.get("decision"),
            "readiness_score": readiness_payload.get("readiness_score"),
            "can_proceed": bool(readiness_payload.get("can_proceed", True)),
            "blocking_reason_codes": list(readiness_payload.get("blocking_reason_codes") or []),
        },
        "top_candidate_lineage_preview": top_candidate_lineage,
    }


def build_task_artifact(autonomy_stage: dict[str, Any] | None = None) -> dict[str, Any]:
    stage = dict(autonomy_stage or {})
    task_scan = dict(stage.get("task_scan") or {})
    task_scan_summary = dict(task_scan.get("summary") or {})
    router_artifact = dict(task_scan.get("router_artifact") or {})
    direction_gate_artifact = dict(task_scan.get("direction_gate_artifact") or {})
    planned_tasks_raw = [
        dict(task or {})
        for task in list(task_scan.get("tasks") or [])
        if isinstance(task, dict)
    ]
    task_origin_counts = count_task_origins(planned_tasks_raw)
    task_source_counts = dict(stage.get("task_source_counts") or task_scan_summary.get("task_sources") or {})
    event_task_count = _safe_int(
        stage.get("event_task_count"),
        _safe_int(task_scan_summary.get("event_task_count")),
    )
    bulk_stock_task_count = _safe_int(
        stage.get("bulk_stock_task_count"),
        _safe_int(task_scan_summary.get("bulk_stock_task_count")),
    )
    router_telemetry = _router_telemetry_from_task_scan(task_scan_summary, router_artifact)
    direction_gate_telemetry = _direction_gate_telemetry_from_task_scan(
        task_scan_summary,
        direction_gate_artifact,
    )
    snapshot_task_count = _safe_int(
        stage.get("snapshot_task_count"),
        _safe_int(task_source_counts.get("snapshot")),
    )
    planned_tasks = [
        _compact_task_brief(task)
        for task in planned_tasks_raw[:12]
    ]
    task_results = [
        _compact_task_result_brief(task_result)
        for task_result in list(stage.get("task_results") or [])[:12]
        if isinstance(task_result, dict)
    ]
    return {
        "contract_version": TASK_ARTIFACT_CONTRACT_VERSION,
        "available": bool(autonomy_stage),
        "planned_task_count": len(list(task_scan.get("tasks") or [])),
        "executed_task_count": len(list(stage.get("task_results") or [])),
        "completed_task_count": _safe_int(stage.get("completed_task_count")),
        "failed_task_count": _safe_int(stage.get("failed_task_count")),
        "generated_candidate_count": _safe_int(stage.get("generated_count")),
        "task_source_counts": task_source_counts,
        "task_origin_counts": task_origin_counts,
        "governed_candidate_activation_task_count": _safe_int(
            task_origin_counts.get(GOVERNED_CANDIDATE_ACTIVATION_ORIGIN)
        ),
        "open_research_task_count": _safe_int(task_origin_counts.get(OPEN_RESEARCH_TASK_ORIGIN)),
        "event_task_count": event_task_count,
        "snapshot_task_count": snapshot_task_count,
        "bulk_stock_task_count": bulk_stock_task_count,
        "bulk_stock_matrix_enabled": bool(task_scan_summary.get("bulk_stock_matrix_enabled")),
        "bulk_stock_matrix_stock_count": _safe_int(task_scan_summary.get("bulk_stock_matrix_stock_count")),
        "bulk_stock_matrix_eligible_stock_count": _safe_int(
            task_scan_summary.get("bulk_stock_matrix_eligible_stock_count")
        ),
        "router_telemetry": router_telemetry,
        "router_artifact": router_artifact,
        **router_telemetry,
        "direction_gate_telemetry": direction_gate_telemetry,
        "direction_gate_artifact": direction_gate_artifact,
        **direction_gate_telemetry,
        "feedback_control_mode_counts": dict(stage.get("selected_feedback_control_mode_counts") or {}),
        "feedback_target_pool_control_mode_counts": dict(
            stage.get("selected_feedback_target_pool_control_mode_counts") or {}
        ),
        "feedback_holding_bucket_control_mode_counts": dict(
            stage.get("selected_feedback_holding_bucket_control_mode_counts") or {}
        ),
        "feedback_generator_mode_control_mode_counts": dict(
            stage.get("selected_feedback_generator_mode_control_mode_counts") or {}
        ),
        "planned_task_briefs": planned_tasks,
        "task_result_briefs": task_results,
    }


def build_candidate_artifact(candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = [
        _prepare_candidate_for_artifact(dict(item or {}))
        for item in list(candidates or [])
        if isinstance(item, dict)
    ]
    briefs = [_compact_candidate_brief(item) for item in items[:12]]
    candidate_origin_counts = count_candidate_origins(items)
    targeted_candidate_count = sum(1 for item in items if list(item.get("target_symbols") or []))
    experiment_linked_count = sum(1 for item in items if _string(item.get("experiment_id")))
    contract_ready_count = sum(1 for item in items if bool(_candidate_contract_snapshot(item)))
    evidence_ready_count = sum(1 for item in items if _candidate_has_evidence(item))
    return {
        "contract_version": CANDIDATE_ARTIFACT_CONTRACT_VERSION,
        "available": bool(items),
        "candidate_count": len(items),
        "targeted_candidate_count": targeted_candidate_count,
        "experiment_linked_count": experiment_linked_count,
        "candidate_contract_ready_count": contract_ready_count,
        "candidate_evidence_ready_count": evidence_ready_count,
        "candidate_origin_counts": candidate_origin_counts,
        "local_rule_candidate_count": _safe_int(candidate_origin_counts.get(LOCAL_RULE_CANDIDATE_ORIGIN)),
        "external_autonomy_candidate_count": _safe_int(
            candidate_origin_counts.get(EXTERNAL_AUTONOMY_CANDIDATE_ORIGIN)
        ),
        "governed_candidate_activation_count": _safe_int(
            candidate_origin_counts.get(GOVERNED_CANDIDATE_ACTIVATION_ORIGIN)
        ),
        "generator_type_counts": _count_by(items, _candidate_generator_mode),
        "task_source_counts": _count_by(items, _candidate_task_source),
        "family_counts": _count_by(items, _candidate_family),
        "candidate_briefs": briefs,
    }


def build_research_evidence_artifact(
    autonomy_stage: dict[str, Any] | None = None,
    *,
    experiments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stage = dict(autonomy_stage or {})
    experiment_rows = [dict(item or {}) for item in list(experiments or []) if isinstance(item, dict)]
    task_results = [dict(item or {}) for item in list(stage.get("task_results") or []) if isinstance(item, dict)]
    task_status_counts = _count_by(task_results, lambda item: item.get("status"))
    task_origin_counts = count_task_origins([dict(item.get("task") or {}) for item in task_results])
    return {
        "contract_version": RESEARCH_EVIDENCE_ARTIFACT_CONTRACT_VERSION,
        "available": bool(stage or experiment_rows),
        "task_evidence_count": _safe_int(stage.get("event_evidence_count")),
        "task_run_count": len(list(stage.get("task_run_ids") or [])),
        "task_run_ids": list(stage.get("task_run_ids") or [])[:12],
        "task_result_status_counts": task_status_counts,
        "task_origin_counts": task_origin_counts,
        "governed_candidate_activation_task_count": _safe_int(
            task_origin_counts.get(GOVERNED_CANDIDATE_ACTIVATION_ORIGIN)
        ),
        "experiment_count": _safe_int(stage.get("experiment_count"), len(experiment_rows)),
        "experiment_briefs": [_compact_experiment_brief(item) for item in experiment_rows[:12]],
        "external_llm_status": stage.get("external_llm_status"),
        "external_llm_status_counts": dict(stage.get("external_llm_status_counts") or {}),
        "external_llm_attempt_count": _safe_int(stage.get("external_llm_attempt_count")),
        "external_llm_network_request_count": _safe_int(stage.get("external_llm_network_request_count")),
        "external_llm_real_request_count": _safe_int(stage.get("external_llm_real_request_count")),
        "external_llm_selected_count": _safe_int(stage.get("external_llm_selected_count")),
        "external_llm_compatibility_skip_count": _safe_int(
            stage.get("external_llm_compatibility_skip_count")
        ),
        "external_llm_cooldown_skip_count": _safe_int(stage.get("external_llm_cooldown_skip_count")),
        "external_llm_compatibility_failure_count": _safe_int(
            stage.get("external_llm_compatibility_failure_count")
        ),
        "external_llm_effective_response_count": _safe_int(
            stage.get("external_llm_effective_response_count")
        ),
        "external_llm_empty_200_response_count": _safe_int(
            stage.get("external_llm_empty_200_response_count")
        ),
        "external_llm_effective_response_ratio": round(
            _safe_float(stage.get("external_llm_effective_response_ratio")),
            4,
        ),
        "external_llm_provider_health_status": stage.get("external_llm_provider_health_status"),
        "persistence_failure_count": _safe_int(stage.get("persistence_failure_count")),
        "last_error_type": stage.get("external_llm_last_error_type"),
        "last_error": stage.get("external_llm_last_error"),
    }


def build_research_plane_artifact(
    *,
    factor_research: dict[str, Any] | None = None,
    readiness: dict[str, Any] | None = None,
    autonomy_stage: dict[str, Any] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    experiments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    autonomy_payload = dict(autonomy_stage or {})
    factor_payload = dict(factor_research or {})
    prebuilt_research_artifact = _resolve_contract_artifact(
        factor_payload.get("research_artifact"),
        expected_version=RESEARCH_ARTIFACT_CONTRACT_VERSION,
    )
    derived_research_artifact = build_research_artifact(
        factor_research=factor_research,
        readiness=readiness,
    )
    research_artifact = derived_research_artifact
    if bool(prebuilt_research_artifact.get("available")):
        research_artifact = {
            **derived_research_artifact,
            **prebuilt_research_artifact,
        }
        research_artifact["available"] = bool(
            prebuilt_research_artifact.get("available")
            or derived_research_artifact.get("available")
        )
        research_artifact["source_chain"] = _compact_list(
            [
                *list(prebuilt_research_artifact.get("source_chain") or []),
                *list(derived_research_artifact.get("source_chain") or []),
            ],
            limit=12,
        )
    prebuilt_task_artifact = _resolve_contract_artifact(
        autonomy_payload.get("task_artifact"),
        expected_version=TASK_ARTIFACT_CONTRACT_VERSION,
    )
    task_artifact = (
        prebuilt_task_artifact
        if bool(prebuilt_task_artifact.get("available"))
        else build_task_artifact(autonomy_stage)
    )
    prebuilt_candidate_artifact = _resolve_contract_artifact(
        autonomy_payload.get("candidate_artifact"),
        expected_version=CANDIDATE_ARTIFACT_CONTRACT_VERSION,
    )
    candidate_artifact = (
        prebuilt_candidate_artifact
        if bool(prebuilt_candidate_artifact.get("available"))
        else build_candidate_artifact(candidates)
    )
    prebuilt_evidence_artifact = _resolve_contract_artifact(
        autonomy_payload.get("evidence_artifact"),
        expected_version=RESEARCH_EVIDENCE_ARTIFACT_CONTRACT_VERSION,
    )
    evidence_artifact = (
        prebuilt_evidence_artifact
        if bool(prebuilt_evidence_artifact.get("available"))
        else build_research_evidence_artifact(
            autonomy_stage,
            experiments=experiments,
        )
    )
    available = any(
        bool(item.get("available"))
        for item in (
            research_artifact,
            task_artifact,
            candidate_artifact,
            evidence_artifact,
        )
    )
    source_chain = list(research_artifact.get("source_chain") or [])
    if task_artifact.get("available"):
        source_chain.append("autonomy.task_artifact")
    if candidate_artifact.get("available"):
        source_chain.append("research.candidate_artifact")
    if evidence_artifact.get("available"):
        source_chain.append("research.evidence_artifact")
    return {
        "contract_version": RESEARCH_PLANE_CONTRACT_VERSION,
        "available": available,
        "plane": "research",
        "research_artifact": research_artifact,
        "task_artifact": task_artifact,
        "candidate_artifact": candidate_artifact,
        "evidence_artifact": evidence_artifact,
        "source_chain": source_chain,
    }


__all__ = [
    "CANDIDATE_ARTIFACT_CONTRACT_VERSION",
    "RESEARCH_ARTIFACT_CONTRACT_VERSION",
    "RESEARCH_EVIDENCE_ARTIFACT_CONTRACT_VERSION",
    "RESEARCH_PLANE_CONTRACT_VERSION",
    "TASK_ARTIFACT_CONTRACT_VERSION",
    "build_candidate_artifact",
    "build_research_artifact",
    "build_research_evidence_artifact",
    "build_research_plane_artifact",
    "build_task_artifact",
]
