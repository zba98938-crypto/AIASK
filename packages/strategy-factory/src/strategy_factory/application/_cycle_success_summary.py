"""Success-path run summary builder for factory cycles.

P2 refactor: extract the large success summary assembly from ``cycle_runner`` so
the runner focuses on orchestration rather than payload shaping.
"""

from __future__ import annotations

from typing import Any

from .services.readiness_service import (
    resolve_governed_pool_state,
    resolve_market_temperature_context,
)

_VALIDATION_GRADE_ORDER: dict[str, int] = {
    "D": 0,
    "C": 1,
    "B": 2,
    "A": 3,
    "S": 4,
    "SS": 5,
    "SSS": 6,
}

from ._cycle_success_aggregators import (
    _add_count,
    _aggregate_diagnostic_validation_grade_counts,
    _aggregate_metric_source_audit,
    _aggregate_quality_validation_grade_counts,
    _aggregate_statistical_metric_missing_counts,
    _aggregate_submission_action_type_counts,
    _aggregate_submission_lane_counts,
    _aggregate_validation_grade_counts,
    _build_autonomy_task_briefs,
    _build_llm_status_counts,
    _is_quality_production_record,
    _resolve_event_task_count,
    _safe_int,
    _set_count_at_least,
    _strategy_validation_grade,
    _top_validation_grade,
    _walk_dict_values,
)


def build_success_run_summary(
    *,
    trace_id: str,
    snapshot: dict[str, Any],
    candidates: list[dict[str, Any]],
    passed: list[dict[str, Any]],
    unique: list[dict[str, Any]],
    eliminated: list[dict[str, Any]],
    spawn_report: dict[str, Any],
    submit_result: dict[str, Any],
    quality_gate_report: dict[str, Any],
    backtest_report: dict[str, Any],
    autonomy_summary: dict[str, Any],
    task_scan_summary: dict[str, Any],
    task_source_counts: dict[str, Any],
    bulk_stock_matrix_family_counts: dict[str, Any],
    bulk_stock_matrix_allocation_pass_counts: dict[str, Any],
    factor_research_summary: dict[str, Any],
    factor_refresh_summary: dict[str, Any],
    readiness_summary: dict[str, Any],
    warmup_summary: dict[str, Any],
    backtest_audit_summary: dict[str, Any],
    submission_audit_summary: dict[str, Any],
    vector_summary: dict[str, Any],
    elapsed: float,
) -> dict[str, Any]:
    snapshot_task_count = int(
        autonomy_summary.get("snapshot_task_count")
        or task_source_counts.get("snapshot", 0)
    )
    event_task_count = _resolve_event_task_count(
        autonomy_summary,
        task_scan_summary,
        task_source_counts,
    )
    autonomy_task_briefs = _build_autonomy_task_briefs(autonomy_summary)
    gate_0_summary = dict(quality_gate_report.get("gate_0") or {})
    pre_gate_summary = dict(quality_gate_report.get("pre_gate") or {})
    gate_1_summary = dict(quality_gate_report.get("gate_1") or {})
    gate_2_summary = dict(quality_gate_report.get("gate_2") or {})
    backtest_summary = dict(backtest_report.get("summary") or {})
    gate_3_failure_topn = list(submit_result.get("gate_3_failure_reason_topn") or [])
    validation_grade_counts = _aggregate_validation_grade_counts(submit_result)
    qualified_validation_grade_counts = _aggregate_quality_validation_grade_counts(submit_result)
    diagnostic_validation_grade_counts = _aggregate_diagnostic_validation_grade_counts(submit_result)
    latest_qualified_validation_grade = _top_validation_grade(qualified_validation_grade_counts)
    latest_diagnostic_validation_grade = _top_validation_grade(diagnostic_validation_grade_counts)
    statistical_metric_missing_counts = _aggregate_statistical_metric_missing_counts(submit_result)
    metric_source_audit = _aggregate_metric_source_audit(submit_result)
    submission_lane_counts = _aggregate_submission_lane_counts(submit_result)
    submission_action_type_counts = _aggregate_submission_action_type_counts(submit_result)
    formal_incubation_count = max(
        _safe_int(submit_result.get("formal_incubation_count")),
        _safe_int(submission_lane_counts.get("formal_incubation")),
    )
    observe_incubation_count = max(
        _safe_int(submit_result.get("observe_incubation_count")),
        _safe_int(submission_lane_counts.get("observe_incubation")),
    )
    diagnostic_observation_count = max(
        _safe_int(submit_result.get("diagnostic_observation_count")),
        _safe_int(submission_lane_counts.get("diagnostic_observation")),
    )
    live_ready_review_count = max(
        _safe_int(submit_result.get("live_ready_review_count")),
        _safe_int(submission_lane_counts.get("live_ready_review")),
    )
    deferred_submission_count = max(
        _safe_int(submit_result.get("deferred_submission_count")),
        _safe_int(submission_lane_counts.get("deferred_submission")),
    )
    research_only_count = max(
        _safe_int(submit_result.get("research_only_count")),
        _safe_int(submission_action_type_counts.get("research_only")),
    )
    observe_admitted_count = max(
        _safe_int(submit_result.get("observe_admitted_count")),
        observe_incubation_count + diagnostic_observation_count,
    )
    llm_status_counts = _build_llm_status_counts(autonomy_summary)
    market_temperature_context = dict(readiness_summary.get("market_temperature_context") or {})
    if not market_temperature_context:
        market_temperature_context = resolve_market_temperature_context(snapshot)
    if not bool(market_temperature_context.get("available")):
        market_temperature_context = {}

    return {
        "trace_id": trace_id,
        "runtime_enabled": bool(readiness_summary.get("runtime_enabled", True)),
        "event_runtime_mode": readiness_summary.get("event_runtime_mode"),
        "factory_readiness_contract_version": readiness_summary.get("readiness_contract_version"),
        "factory_readiness_authority_version": readiness_summary.get("authority_contract_version"),
        "factory_readiness_decision": readiness_summary.get("decision"),
        "factory_readiness_hard_gate": readiness_summary.get("hard_gate"),
        "factory_readiness_blocking_stage": readiness_summary.get("blocking_stage"),
        "factory_readiness_effective_blocking_reason_codes": list(
            readiness_summary.get("effective_blocking_reason_codes") or []
        ),
        "factory_readiness_blocking_reason_codes": list(
            readiness_summary.get("effective_blocking_reason_codes") or []
        ),
        "factory_readiness_raw_blocking_reason_codes": list(
            readiness_summary.get("blocking_reason_codes") or []
        ),
        "factory_readiness_blocking_reason_codes_source": readiness_summary.get(
            "blocking_reason_codes_source"
        ),
        "factory_readiness_critical_blocking_reason_codes": list(
            readiness_summary.get("critical_blocking_reason_codes") or []
        ),
        "factory_readiness_score": readiness_summary.get("readiness_score"),
        "factory_readiness_can_proceed": readiness_summary.get("can_proceed"),
        "factory_readiness_blocker_count": readiness_summary.get("blocker_count", 0),
        "factory_readiness_warning_count": readiness_summary.get("warning_count", 0),
        "market_temperature_context_available": bool(market_temperature_context),
        "market_temperature_context": market_temperature_context,
        "market_temperature": market_temperature_context.get("temperature"),
        "market_temperature_state": market_temperature_context.get("state"),
        "market_temperature_as_of": market_temperature_context.get("as_of"),
        "market_temperature_quality_status": market_temperature_context.get("quality_status"),
        "market_temperature_readiness_status": market_temperature_context.get("readiness_status"),
        "market_temperature_staleness_days": market_temperature_context.get("staleness_days"),
        "fear_greed": snapshot.get("fear_greed_index"),
        "listed_count": snapshot.get("listed_count", 0),
        "snapshot_degraded": bool(snapshot.get("degraded")),
        "snapshot_completion_ratio": (snapshot.get("completeness") or {}).get("completion_ratio", 1.0),
        "snapshot_failure_reason_count": len(snapshot.get("failure_reasons") or []),
        "warmup_status": warmup_summary.get("status"),
        "warmup_task_type": warmup_summary.get("task_type"),
        "warmup_matched": int(warmup_summary.get("matched") or 0),
        "warmup_executed": int(warmup_summary.get("executed") or 0),
        "warmup_failed": int(warmup_summary.get("failed") or 0),
        "candidates_spawned": len(candidates),
        "autonomy_generated": autonomy_summary.get("generated_count", 0),
        "autonomy_task_count": autonomy_summary.get("task_count", 0),
        "autonomy_completed_task_count": autonomy_summary.get("completed_task_count", 0),
        "autonomy_failed_task_count": autonomy_summary.get("failed_task_count", 0),
        "llm_status_counts": llm_status_counts,
        "pipeline_fallback_counts": dict(autonomy_summary.get("pipeline_fallback_counts") or {}),
        "task_timeout_skip_count": int(autonomy_summary.get("task_timeout_skip_count") or 0),
        "task_timeout_policy": autonomy_summary.get("task_timeout_policy"),
        "event_task_count": event_task_count,
        "single_anchor_event_count": int(
            autonomy_summary.get("single_anchor_event_count")
            or task_scan_summary.get("single_anchor_event_count")
            or 0
        ),
        "multi_source_confirmed_event_count": int(
            autonomy_summary.get("multi_source_confirmed_event_count")
            or task_scan_summary.get("multi_source_confirmed_event_count")
            or 0
        ),
        "conflict_event_count": int(
            autonomy_summary.get("conflict_event_count")
            or task_scan_summary.get("conflict_event_count")
            or 0
        ),
        "news_only_rejected_count": int(
            autonomy_summary.get("news_only_rejected_count")
            or task_scan_summary.get("news_only_rejected_count")
            or 0
        ),
        "snapshot_task_count": snapshot_task_count,
        "bulk_stock_task_count": autonomy_summary.get("bulk_stock_task_count", 0),
        "bulk_stock_matrix_enabled": bool(task_scan_summary.get("bulk_stock_matrix_enabled")),
        "bulk_stock_matrix_configured_enabled": bool(
            task_scan_summary.get("bulk_stock_matrix_configured_enabled")
        ),
        "bulk_stock_matrix_stock_count": int(task_scan_summary.get("bulk_stock_matrix_stock_count") or 0),
        "bulk_stock_matrix_eligible_stock_count": int(
            task_scan_summary.get("bulk_stock_matrix_eligible_stock_count") or 0
        ),
        "bulk_stock_matrix_loaded_stock_count": int(
            task_scan_summary.get("bulk_stock_matrix_loaded_stock_count") or 0
        ),
        "bulk_stock_matrix_pages_loaded": int(task_scan_summary.get("bulk_stock_matrix_pages_loaded") or 0),
        "bulk_stock_matrix_analysis_complete": bool(
            task_scan_summary.get("bulk_stock_matrix_analysis_complete")
        ),
        "bulk_stock_matrix_analysis_stock_coverage_ratio": task_scan_summary.get(
            "bulk_stock_matrix_analysis_stock_coverage_ratio"
        ),
        "bulk_stock_matrix_family_counts": bulk_stock_matrix_family_counts,
        "bulk_stock_matrix_planned_family_counts": dict(
            task_scan_summary.get("bulk_stock_matrix_planned_family_counts") or {}
        ),
        "bulk_stock_matrix_universe_limit": int(task_scan_summary.get("bulk_stock_matrix_universe_limit") or 0),
        "bulk_stock_matrix_batch_count": int(task_scan_summary.get("bulk_stock_matrix_batch_count") or 0),
        "bulk_stock_matrix_selected_batch_count": int(
            task_scan_summary.get("bulk_stock_matrix_selected_batch_count") or 0
        ),
        "bulk_stock_matrix_run_window": task_scan_summary.get("bulk_stock_matrix_run_window"),
        "bulk_stock_matrix_run_window_active": bool(
            task_scan_summary.get("bulk_stock_matrix_run_window_active")
        ),
        "bulk_stock_matrix_run_window_current_period": task_scan_summary.get(
            "bulk_stock_matrix_run_window_current_period"
        ),
        "bulk_stock_matrix_skip_reason": task_scan_summary.get("bulk_stock_matrix_skip_reason"),
        "bulk_stock_matrix_load_error": task_scan_summary.get("bulk_stock_matrix_load_error"),
        "bulk_stock_matrix_load_error_type": task_scan_summary.get("bulk_stock_matrix_load_error_type"),
        "bulk_stock_matrix_last_error": task_scan_summary.get("bulk_stock_matrix_last_error"),
        "bulk_stock_matrix_last_error_type": task_scan_summary.get("bulk_stock_matrix_last_error_type"),
        "bulk_stock_matrix_last_error_offset": task_scan_summary.get("bulk_stock_matrix_last_error_offset"),
        "bulk_stock_matrix_requested_universe_offset": int(
            task_scan_summary.get("bulk_stock_matrix_requested_universe_offset") or 0
        ),
        "bulk_stock_matrix_effective_universe_offset": int(
            task_scan_summary.get("bulk_stock_matrix_effective_universe_offset") or 0
        ),
        "bulk_stock_matrix_universe_offset_fallback": bool(
            task_scan_summary.get("bulk_stock_matrix_universe_offset_fallback")
        ),
        "bulk_stock_matrix_next_universe_offset": int(
            task_scan_summary.get("bulk_stock_matrix_next_universe_offset") or 0
        ),
        "bulk_stock_matrix_cursor_wrapped": bool(task_scan_summary.get("bulk_stock_matrix_cursor_wrapped")),
        "bulk_stock_matrix_cursor_mode": task_scan_summary.get("bulk_stock_matrix_cursor_mode"),
        "bulk_stock_matrix_requested_task_offset": int(
            task_scan_summary.get("bulk_stock_matrix_requested_task_offset") or 0
        ),
        "bulk_stock_matrix_effective_task_offset": int(
            task_scan_summary.get("bulk_stock_matrix_effective_task_offset") or 0
        ),
        "bulk_stock_matrix_task_offset_fallback": bool(
            task_scan_summary.get("bulk_stock_matrix_task_offset_fallback")
        ),
        "bulk_stock_matrix_next_task_offset": int(
            task_scan_summary.get("bulk_stock_matrix_next_task_offset") or 0
        ),
        "bulk_stock_matrix_task_cursor_wrapped": bool(
            task_scan_summary.get("bulk_stock_matrix_task_cursor_wrapped")
        ),
        "bulk_stock_matrix_cursor_source": task_scan_summary.get("bulk_stock_matrix_cursor_source"),
        "bulk_stock_matrix_cursor_resume_from_run_id": task_scan_summary.get(
            "bulk_stock_matrix_cursor_resume_from_run_id"
        ),
        "bulk_stock_matrix_effective_task_budget": int(
            task_scan_summary.get("bulk_stock_matrix_effective_task_budget") or 0
        ),
        "bulk_stock_matrix_max_candidates_per_run": int(
            task_scan_summary.get("bulk_stock_matrix_max_candidates_per_run") or 0
        ),
        "bulk_stock_matrix_estimated_candidate_count": int(
            task_scan_summary.get("bulk_stock_matrix_estimated_candidate_count") or 0
        ),
        "bulk_stock_matrix_planned_task_count": int(
            task_scan_summary.get("bulk_stock_matrix_planned_task_count") or 0
        ),
        "bulk_stock_matrix_planned_candidate_count": int(
            task_scan_summary.get("bulk_stock_matrix_planned_candidate_count") or 0
        ),
        "bulk_stock_matrix_tasks_per_shard": int(task_scan_summary.get("bulk_stock_matrix_tasks_per_shard") or 0),
        "bulk_stock_matrix_shard_count": int(task_scan_summary.get("bulk_stock_matrix_shard_count") or 0),
        "bulk_stock_matrix_selected_shard_count": int(
            task_scan_summary.get("bulk_stock_matrix_selected_shard_count") or 0
        ),
        "bulk_stock_matrix_selected_shard_ids": list(
            task_scan_summary.get("bulk_stock_matrix_selected_shard_ids") or []
        ),
        "bulk_stock_matrix_stock_coverage_ratio": task_scan_summary.get("bulk_stock_matrix_stock_coverage_ratio"),
        "bulk_stock_matrix_allocation_mode": task_scan_summary.get("bulk_stock_matrix_allocation_mode"),
        "bulk_stock_matrix_allocation_pass_counts": bulk_stock_matrix_allocation_pass_counts,
        "bulk_stock_matrix_planned_allocation_pass_counts": dict(
            task_scan_summary.get("bulk_stock_matrix_planned_allocation_pass_counts") or {}
        ),
        "bulk_stock_matrix_overflow_task_count": int(
            task_scan_summary.get("bulk_stock_matrix_overflow_task_count") or 0
        ),
        "router_enabled": bool(task_scan_summary.get("router_enabled")),
        "router_strict": bool(task_scan_summary.get("router_strict")),
        "router_telemetry_enabled": bool(task_scan_summary.get("router_telemetry_enabled")),
        "router_candidate_stock_count": int(task_scan_summary.get("router_candidate_stock_count") or 0),
        "router_applied_count": int(task_scan_summary.get("router_applied_count") or 0),
        "router_status_counts": dict(task_scan_summary.get("router_status_counts") or {}),
        "router_fallback_reason_counts": dict(
            task_scan_summary.get("router_fallback_reason_counts") or {}
        ),
        "router_family_counts": dict(task_scan_summary.get("router_family_counts") or {}),
        "router_holding_bucket_counts": dict(
            task_scan_summary.get("router_holding_bucket_counts") or {}
        ),
        "profile_summary_present_count": int(task_scan_summary.get("profile_summary_present_count") or 0),
        "profile_summary_missing_count": int(task_scan_summary.get("profile_summary_missing_count") or 0),
        "profile_summary_generated_count": int(task_scan_summary.get("profile_summary_generated_count") or 0),
        "selected_router_applied_count": int(task_scan_summary.get("selected_router_applied_count") or 0),
        "selected_profile_summary_missing_count": int(
            task_scan_summary.get("selected_profile_summary_missing_count") or 0
        ),
        "direction_gate_enabled": bool(task_scan_summary.get("direction_gate_enabled")),
        "direction_gate_candidate_stock_count": int(
            task_scan_summary.get("direction_gate_candidate_stock_count") or 0
        ),
        "direction_gate_evaluated_count": int(task_scan_summary.get("direction_gate_evaluated_count") or 0),
        "direction_gate_applied_count": int(task_scan_summary.get("direction_gate_applied_count") or 0),
        "direction_gate_fallback_count": int(task_scan_summary.get("direction_gate_fallback_count") or 0),
        "direction_gate_status_counts": dict(task_scan_summary.get("direction_gate_status_counts") or {}),
        "direction_gate_reason_counts": dict(task_scan_summary.get("direction_gate_reason_counts") or {}),
        "direction_gate_dropped_family_counts": dict(
            task_scan_summary.get("direction_gate_dropped_family_counts") or {}
        ),
        "selected_direction_gate_applied_count": int(
            task_scan_summary.get("selected_direction_gate_applied_count") or 0
        ),
        "selected_direction_gate_fallback_count": int(
            task_scan_summary.get("selected_direction_gate_fallback_count") or 0
        ),
        "selected_direction_gate_task_count": int(
            task_scan_summary.get("selected_direction_gate_task_count") or 0
        ),
        "task_source_counts": task_source_counts,
        "scanner_task_types": task_scan_summary.get("task_types") or {},
        "event_snapshot_mixed": bool(
            event_task_count > 0 and snapshot_task_count > 0
        ),
        "max_research_tasks": int(autonomy_summary.get("max_research_tasks") or 0),
        "max_bulk_research_tasks": int(autonomy_summary.get("max_bulk_research_tasks") or 0),
        "combined_research_task_budget": int(autonomy_summary.get("combined_research_task_budget") or 0),
        "scan_research_task_budget": int(autonomy_summary.get("scan_research_task_budget") or 0),
        "reserved_bulk_task_budget": int(autonomy_summary.get("reserved_bulk_task_budget") or 0),
        "selected_scan_task_count": int(autonomy_summary.get("selected_scan_task_count") or 0),
        "selected_bulk_task_count": int(autonomy_summary.get("selected_bulk_task_count") or 0),
        "planned_bulk_task_count": int(autonomy_summary.get("planned_bulk_task_count") or 0),
        "clipped_bulk_task_count": int(autonomy_summary.get("clipped_bulk_task_count") or 0),
        "blocked_feedback_task_count": int(autonomy_summary.get("blocked_feedback_task_count") or 0),
        "planned_feedback_cooldown_task_count": int(
            autonomy_summary.get("planned_feedback_cooldown_task_count") or 0
        ),
        "planned_feedback_limited_task_count": int(
            autonomy_summary.get("planned_feedback_limited_task_count") or 0
        ),
        "planned_feedback_relaxed_task_count": int(
            autonomy_summary.get("planned_feedback_relaxed_task_count") or 0
        ),
        "planned_feedback_control_mode_counts": dict(
            autonomy_summary.get("planned_feedback_control_mode_counts") or {}
        ),
        "planned_feedback_legacy_control_mode_counts": dict(
            autonomy_summary.get("planned_feedback_legacy_control_mode_counts")
            or autonomy_summary.get("planned_feedback_control_mode_counts")
            or {}
        ),
        "planned_feedback_skill_control_mode_counts": dict(
            autonomy_summary.get("planned_feedback_skill_control_mode_counts") or {}
        ),
        "planned_feedback_target_pool_control_mode_counts": dict(
            autonomy_summary.get("planned_feedback_target_pool_control_mode_counts") or {}
        ),
        "planned_feedback_holding_bucket_control_mode_counts": dict(
            autonomy_summary.get("planned_feedback_holding_bucket_control_mode_counts") or {}
        ),
        "planned_feedback_generator_mode_control_mode_counts": dict(
            autonomy_summary.get("planned_feedback_generator_mode_control_mode_counts") or {}
        ),
        "planned_feedback_skill_target_pool_control_mode_counts": dict(
            autonomy_summary.get("planned_feedback_skill_target_pool_control_mode_counts") or {}
        ),
        "planned_feedback_skill_holding_bucket_control_mode_counts": dict(
            autonomy_summary.get("planned_feedback_skill_holding_bucket_control_mode_counts")
            or {}
        ),
        "planned_feedback_skill_generator_mode_control_mode_counts": dict(
            autonomy_summary.get("planned_feedback_skill_generator_mode_control_mode_counts")
            or {}
        ),
        "selected_feedback_control_mode_counts": dict(
            autonomy_summary.get("selected_feedback_control_mode_counts") or {}
        ),
        "selected_feedback_legacy_control_mode_counts": dict(
            autonomy_summary.get("selected_feedback_legacy_control_mode_counts")
            or autonomy_summary.get("selected_feedback_control_mode_counts")
            or {}
        ),
        "selected_feedback_skill_control_mode_counts": dict(
            autonomy_summary.get("selected_feedback_skill_control_mode_counts") or {}
        ),
        "selected_feedback_target_pool_control_mode_counts": dict(
            autonomy_summary.get("selected_feedback_target_pool_control_mode_counts") or {}
        ),
        "selected_feedback_holding_bucket_control_mode_counts": dict(
            autonomy_summary.get("selected_feedback_holding_bucket_control_mode_counts") or {}
        ),
        "selected_feedback_generator_mode_control_mode_counts": dict(
            autonomy_summary.get("selected_feedback_generator_mode_control_mode_counts") or {}
        ),
        "selected_feedback_skill_target_pool_control_mode_counts": dict(
            autonomy_summary.get("selected_feedback_skill_target_pool_control_mode_counts") or {}
        ),
        "selected_feedback_skill_holding_bucket_control_mode_counts": dict(
            autonomy_summary.get("selected_feedback_skill_holding_bucket_control_mode_counts")
            or {}
        ),
        "selected_feedback_skill_generator_mode_control_mode_counts": dict(
            autonomy_summary.get("selected_feedback_skill_generator_mode_control_mode_counts")
            or {}
        ),
        "selected_feedback_limited_task_count": int(
            autonomy_summary.get("selected_feedback_limited_task_count") or 0
        ),
        "selected_feedback_relaxed_task_count": int(
            autonomy_summary.get("selected_feedback_relaxed_task_count") or 0
        ),
        "suppressed_families": list(autonomy_summary.get("suppressed_families") or []),
        "suppressed_target_pools": list(autonomy_summary.get("suppressed_target_pools") or []),
        "suppressed_generator_modes": list(autonomy_summary.get("suppressed_generator_modes") or []),
        "factor_research_used": bool(snapshot.get("factor_research")),
        "active_factor_count": int(factor_research_summary.get("active_factor_count") or 0),
        "active_candidate_count": int(factor_research_summary.get("active_candidate_count") or 0),
        "governed_source_candidate_count": int(
            factor_research_summary.get("governed_source_candidate_count") or 0
        ),
        "governed_blocked_candidate_count": int(
            factor_research_summary.get("governed_blocked_candidate_count") or 0
        ),
        "governed_blocked_ratio": factor_research_summary.get("governed_blocked_ratio"),
        "governed_pending_candidate_count": int(
            factor_research_summary.get("governed_pending_candidate_count") or 0
        ),
        "governed_pending_ratio": factor_research_summary.get("governed_pending_ratio"),
        "governed_ineligible_candidate_count": int(
            factor_research_summary.get("governed_ineligible_candidate_count") or 0
        ),
        "governed_ineligible_ratio": factor_research_summary.get("governed_ineligible_ratio"),
        "governed_latest_candidate_at": factor_research_summary.get("governed_latest_candidate_at"),
        "governed_freshness_days": factor_research_summary.get("governed_freshness_days"),
        "governed_exclusion_reason_counts": dict(
            factor_research_summary.get("governed_exclusion_reason_counts") or {}
        ),
        "governed_blocking_reason_counts": dict(
            factor_research_summary.get("governed_blocking_reason_counts") or {}
        ),
        "governed_pending_reason_counts": dict(
            factor_research_summary.get("governed_pending_reason_counts") or {}
        ),
        "governed_ineligible_reason_counts": dict(
            factor_research_summary.get("governed_ineligible_reason_counts") or {}
        ),
        "governed_risk_counts": dict(factor_research_summary.get("governed_risk_counts") or {}),
        "active_family_count": len(list(factor_research_summary.get("active_family_names") or [])),
        "active_regime_count": len(list(factor_research_summary.get("active_regime_names") or [])),
        "top_factor_names": list(factor_research_summary.get("top_factor_names") or []),
        "top_candidate_names": list(factor_research_summary.get("top_candidate_names") or []),
        "active_family_names": list(factor_research_summary.get("active_family_names") or []),
        "active_regime_names": list(factor_research_summary.get("active_regime_names") or []),
        "family_preference_order": list(factor_research_summary.get("family_preference_order") or []),
        "family_preference_source_mode": factor_research_summary.get("family_preference_source_mode"),
        "factor_source_mode": factor_research_summary.get("factor_source_mode"),
        "governed_candidate_pool_mode": factor_research_summary.get("governed_candidate_pool_mode"),
        "governed_candidate_pool_provisional": bool(
            factor_research_summary.get("governed_candidate_pool_provisional")
        ),
        "governed_candidate_pool_strict_count": int(
            factor_research_summary.get("governed_candidate_pool_strict_count") or 0
        ),
        "governed_candidate_pool_provisional_count": int(
            factor_research_summary.get("governed_candidate_pool_provisional_count") or 0
        ),
        "governed_candidate_pool_provisional_spillover_count": int(
            factor_research_summary.get("governed_candidate_pool_provisional_spillover_count") or 0
        ),
        "governed_candidate_pool_provisional_spillover_enabled": bool(
            factor_research_summary.get("governed_candidate_pool_provisional_spillover_enabled")
        ),
        "governed_candidate_pool_provisional_spillover_policy": dict(
            factor_research_summary.get("governed_candidate_pool_provisional_spillover_policy") or {}
        ),
        "governed_candidate_pool_provisional_spillover_policy_status": (
            factor_research_summary.get("governed_candidate_pool_provisional_spillover_policy_status")
        ),
        "governed_candidate_pool_provisional_pending_count": int(
            factor_research_summary.get("governed_candidate_pool_provisional_pending_count") or 0
        ),
        "governed_candidate_pool_strict_shortfall_count": int(
            factor_research_summary.get("governed_candidate_pool_strict_shortfall_count") or 0
        ),
        "stock_family_allocation_count": int(
            factor_research_summary.get("stock_family_allocation_count") or 0
        ),
        "stock_family_allocation_family_counts": dict(
            factor_research_summary.get("stock_family_allocation_family_counts") or {}
        ),
        "stock_family_allocation_entropy": factor_research_summary.get("stock_family_allocation_entropy"),
        "stock_family_allocation_avg_priority": factor_research_summary.get(
            "stock_family_allocation_avg_priority"
        ),
        "stock_family_allocation_source_mode": factor_research_summary.get(
            "stock_family_allocation_source_mode"
        ),
        "governed_candidate_pool_active": bool(
            resolve_governed_pool_state(factor_research_summary).get("active")
        ),
        "governed_candidate_pool_runtime_state": readiness_summary.get(
            "governed_candidate_pool_runtime_state"
        ),
        "factor_research_degraded": bool((snapshot.get("factor_research") or {}).get("degraded")),
        "factor_research_stale": bool(factor_research_summary.get("stale")),
        "factor_research_freshness_days": factor_research_summary.get("freshness_days"),
        "scheduler_recent_success": bool(factor_research_summary.get("scheduler_recent_success")),
        "scheduler_llm_validation_status": factor_research_summary.get("scheduler_llm_validation_status"),
        "factor_scheduler_recent_success": bool(factor_research_summary.get("scheduler_recent_success")),
        "factor_scheduler_llm_validation_status": factor_research_summary.get(
            "scheduler_llm_validation_status"
        ),
        "factor_llm_provider_enabled": bool(factor_research_summary.get("factor_llm_provider_enabled")),
        "factor_llm_provider_ready": bool(factor_research_summary.get("factor_llm_provider_ready")),
        "factor_llm_provider_health_status": factor_research_summary.get(
            "factor_llm_provider_health_status"
        ),
        "factor_llm_provider_rebuild_count": int(
            factor_research_summary.get("factor_llm_provider_rebuild_count") or 0
        ),
        "factor_llm_provider_last_error_type": factor_research_summary.get(
            "factor_llm_provider_last_error_type"
        ),
        "lifecycle_feedback_input_contract_version": factor_research_summary.get(
            "lifecycle_feedback_input_contract_version"
        ),
        "lifecycle_feedback_input_available": bool(
            factor_research_summary.get("lifecycle_feedback_input_available")
        ),
        "budget_feedback_available": bool(factor_research_summary.get("budget_feedback_available")),
        "budget_feedback_family_count": int(
            factor_research_summary.get("budget_feedback_family_count") or 0
        ),
        "budget_feedback_strategy_count": int(
            factor_research_summary.get("budget_feedback_strategy_count") or 0
        ),
        "budget_feedback_target_pool_scope_count": int(
            factor_research_summary.get("budget_feedback_target_pool_scope_count") or 0
        ),
        "budget_feedback_holding_bucket_scope_count": int(
            factor_research_summary.get("budget_feedback_holding_bucket_scope_count") or 0
        ),
        "budget_feedback_generator_mode_scope_count": int(
            factor_research_summary.get("budget_feedback_generator_mode_scope_count") or 0
        ),
        "budget_feedback_runtime_alert_count": int(
            factor_research_summary.get("budget_feedback_runtime_alert_count") or 0
        ),
        "budget_feedback_runtime_risk_event_count": int(
            factor_research_summary.get("budget_feedback_runtime_risk_event_count") or 0
        ),
        "budget_feedback_promotion_review_count": int(
            factor_research_summary.get("budget_feedback_promotion_review_count") or 0
        ),
        "budget_feedback_promotion_review_status_counts": dict(
            factor_research_summary.get("budget_feedback_promotion_review_status_counts") or {}
        ),
        "budget_feedback_execution_conversion_efficiency": (
            float(factor_research_summary.get("budget_feedback_execution_conversion_efficiency"))
            if factor_research_summary.get("budget_feedback_execution_conversion_efficiency")
            is not None
            else None
        ),
        "budget_feedback_execution_conversion_efficiency_observed_count": int(
            factor_research_summary.get(
                "budget_feedback_execution_conversion_efficiency_observed_count"
            )
            or 0
        ),
        "budget_feedback_action_counts": dict(
            factor_research_summary.get("budget_feedback_action_counts") or {}
        ),
        "budget_feedback_dual_axis_action_family_count": int(
            factor_research_summary.get("budget_feedback_dual_axis_action_family_count") or 0
        ),
        "budget_feedback_execution_optimization_queue_count": int(
            factor_research_summary.get("budget_feedback_execution_optimization_queue_count")
            or 0
        ),
        "budget_feedback_small_budget_observe_count": int(
            factor_research_summary.get("budget_feedback_small_budget_observe_count") or 0
        ),
        "budget_feedback_prioritize_scale_count": int(
            factor_research_summary.get("budget_feedback_prioritize_scale_count") or 0
        ),
        "budget_feedback_cool_or_freeze_count": int(
            factor_research_summary.get("budget_feedback_cool_or_freeze_count") or 0
        ),
        "budget_feedback_retain_family_reduce_budget_count": int(
            factor_research_summary.get("budget_feedback_retain_family_reduce_budget_count")
            or 0
        ),
        "budget_feedback_signal_count_total": int(
            factor_research_summary.get("budget_feedback_signal_count_total") or 0
        ),
        "budget_feedback_zero_signal_strategy_count": int(
            factor_research_summary.get("budget_feedback_zero_signal_strategy_count") or 0
        ),
        "budget_feedback_zero_signal_ratio": float(
            factor_research_summary.get("budget_feedback_zero_signal_ratio") or 0.0
        ),
        "budget_feedback_low_signal_strategy_count": int(
            factor_research_summary.get("budget_feedback_low_signal_strategy_count") or 0
        ),
        "budget_feedback_low_signal_ratio": float(
            factor_research_summary.get("budget_feedback_low_signal_ratio") or 0.0
        ),
        "budget_feedback_observed_forward_window_count": int(
            factor_research_summary.get("budget_feedback_observed_forward_window_count") or 0
        ),
        "budget_feedback_missing_forward_window_count": int(
            factor_research_summary.get("budget_feedback_missing_forward_window_count") or 0
        ),
        "budget_feedback_expected_forward_window_count": int(
            factor_research_summary.get("budget_feedback_expected_forward_window_count") or 0
        ),
        "budget_feedback_forward_window_coverage_ratio": float(
            factor_research_summary.get("budget_feedback_forward_window_coverage_ratio") or 1.0
        ),
        "budget_feedback_promotion_ready_count": int(
            factor_research_summary.get("budget_feedback_promotion_ready_count") or 0
        ),
        "budget_feedback_promotion_ready_ratio": float(
            factor_research_summary.get("budget_feedback_promotion_ready_ratio") or 1.0
        ),
        "budget_feedback_promotion_review_coverage_ratio": float(
            factor_research_summary.get("budget_feedback_promotion_review_coverage_ratio") or 1.0
        ),
        "budget_feedback_evidence_debt_strategy_count": int(
            factor_research_summary.get("budget_feedback_evidence_debt_strategy_count") or 0
        ),
        "budget_feedback_evidence_debt_ratio": float(
            factor_research_summary.get("budget_feedback_evidence_debt_ratio") or 0.0
        ),
        "factor_research_refresh_attempted": bool(factor_refresh_summary.get("refresh_attempted")),
        "factor_research_refresh_status": factor_refresh_summary.get("refresh_status"),
        "factor_research_refresh_trigger": factor_refresh_summary.get("refresh_trigger"),
        "shared_generation_context_preloaded": bool(
            autonomy_summary.get("shared_generation_context_preloaded")
        ),
        "autonomy_task_briefs": autonomy_task_briefs,
        "event_evidence_count": autonomy_summary.get("event_evidence_count", 0),
        "autonomy_lifecycle_state_counts": autonomy_summary.get("lifecycle_state_counts") or {},
        "autonomy_phase_status_counts": autonomy_summary.get("phase_status_counts") or {},
        "autonomy_failed_phase_counts": autonomy_summary.get("failed_phase_counts") or {},
        "quota_fill_candidates": (spawn_report.get("summary") or {}).get("quota_fill_count", 0),
        "effective_quota_fill_candidates": (
            (spawn_report.get("summary") or {}).get("effective_quota_fill_count", 0)
        ),
        "historical_guided_quota_fill_candidates": (
            (spawn_report.get("summary") or {}).get("historical_guided_quota_fill_count", 0)
        ),
        "signal_aligned_quota_fill_candidates": (
            (spawn_report.get("summary") or {}).get("signal_aligned_quota_fill_count", 0)
        ),
        "no_signal_quota_fill_candidates": (
            (spawn_report.get("summary") or {}).get("no_signal_quota_fill_count", 0)
        ),
        "quota_fill_mode_counts": dict(
            (spawn_report.get("summary") or {}).get("quota_fill_mode_counts") or {}
        ),
        "quota_fill_quality_counts": dict(
            (spawn_report.get("summary") or {}).get("quota_fill_quality_counts") or {}
        ),
        "parameter_source_counts": dict(
            (spawn_report.get("summary") or {}).get("parameter_source_counts") or {}
        ),
        "historical_distribution_candidates": (
            (spawn_report.get("summary") or {}).get("historical_distribution_count", 0)
        ),
        "signal_trigger_candidates": (
            (spawn_report.get("summary") or {}).get("signal_trigger_count", len(candidates))
        ),
        "gate_0_passed": gate_0_summary.get("passed_count"),
        "gate_0_failed": gate_0_summary.get("failed_count"),
        "pre_gate_passed": pre_gate_summary.get("passed_count"),
        "pre_gate_failed": pre_gate_summary.get("failed_count"),
        "gate_1_passed": gate_1_summary.get("passed_count"),
        "gate_1_failed": gate_1_summary.get("failed_count"),
        "gate_2_input": gate_2_summary.get("input_count", backtest_summary.get("input_count", len(candidates))),
        "gate_2_passed": gate_2_summary.get("passed_count", len(passed)),
        "candidates_passed_backtest": gate_2_summary.get("passed_count", len(passed)),
        "candidates_failed_backtest": backtest_summary.get(
            "failed_count",
            max(len(candidates) - len(passed), 0),
        ),
        "backtest_failed_reason_counts": backtest_summary.get("failed_reason_counts") or {},
        "candidates_after_dedup": len(unique),
        "created": submit_result.get("created", 0),
        "created_strategy_pool": submit_result.get(
            "created_strategy_pool",
            submit_result.get("created", 0),
        ),
        "created_audit_only": submit_result.get("created_audit_only", 0),
        "created_total": submit_result.get(
            "created_total",
            int(submit_result.get("created", 0)) + int(submit_result.get("created_audit_only", 0)),
        ),
        "gate_3_input": submit_result.get("gate_3_input", len(unique)),
        "submitted": submit_result.get("submitted", 0),
        "passed_quality_gate": submit_result.get("passed_quality_gate", 0),
        "gate_3_passed": submit_result.get(
            "gate_3_passed",
            submit_result.get("passed_quality_gate", 0),
        ),
        "gate_3_failed": submit_result.get(
            "gate_3_failed",
            max(
                int(submit_result.get("gate_3_input", len(unique)))
                - int(submit_result.get("passed_quality_gate", 0)),
                0,
            ),
        ),
        "gate_3_provisional_passed": submit_result.get("gate_3_provisional_passed", 0),
        "gate_3_recorded": submit_result.get("gate_3_recorded", 0),
        "gate3_quality_recorded": submit_result.get(
            "gate3_quality_recorded",
            submit_result.get("gate3_record_quality_qualified_count", 0),
        ),
        "gate3_record_quality_qualified_count": submit_result.get(
            "gate3_record_quality_qualified_count",
            submit_result.get("gate3_quality_recorded", 0),
        ),
        "gate3_record_diagnostic_only_count": submit_result.get(
            "gate3_record_diagnostic_only_count",
            0,
        ),
        "observe_admitted_count": observe_admitted_count,
        "formal_incubation_count": formal_incubation_count,
        "observe_incubation_count": observe_incubation_count,
        "diagnostic_observation_count": diagnostic_observation_count,
        "live_ready_review_count": live_ready_review_count,
        "deferred_submission_count": deferred_submission_count,
        "research_only_count": research_only_count,
        "submission_lane_counts": submission_lane_counts,
        "submission_action_type_counts": submission_action_type_counts,
        "gate_3_failure_reason_topn": gate_3_failure_topn,
        "gate_3_failure_topn": gate_3_failure_topn,
        "validation_grade_counts": validation_grade_counts,
        "qualified_validation_grade_counts": qualified_validation_grade_counts,
        "diagnostic_validation_grade_counts": diagnostic_validation_grade_counts,
        "latest_qualified_validation_grade": latest_qualified_validation_grade,
        "latest_diagnostic_validation_grade": latest_diagnostic_validation_grade,
        "latest_validation_grade": latest_qualified_validation_grade,
        "statistical_metric_missing_counts": statistical_metric_missing_counts,
        "metric_source_audit": metric_source_audit,
        "log_compaction_summary": {
            "strategy_payload_logging": "summary_only",
            "large_runtime_fields_inline": False,
            "gate_3_failure_topn_count": len(gate_3_failure_topn),
        },
        "incubation_budget_family_counts": dict(
            (submit_result.get("incubation_budget_summary") or {}).get("family_counts") or {}
        ),
        "incubation_budget_formal_runtime_ready_candidate_count": _safe_int(
            (submit_result.get("incubation_budget_summary") or {}).get(
                "formal_runtime_ready_candidate_count"
            )
        ),
        "incubation_budget_formal_runtime_ready_selected_count": _safe_int(
            (submit_result.get("incubation_budget_summary") or {}).get(
                "formal_runtime_ready_selected_count"
            )
        ),
        **submission_audit_summary,
        **backtest_audit_summary,
        **vector_summary,
        "eliminated": len(eliminated),
        "external_llm_status": autonomy_summary.get("external_llm_status"),
        "external_llm_attempt_count": autonomy_summary.get("external_llm_attempt_count", 0),
        "external_llm_stage_attempt_count": autonomy_summary.get(
            "external_llm_stage_attempt_count",
            autonomy_summary.get("external_llm_attempt_count", 0),
        ),
        "external_llm_network_request_count": autonomy_summary.get(
            "external_llm_network_request_count",
            0,
        ),
        "external_llm_real_request_count": autonomy_summary.get("external_llm_real_request_count", 0),
        "external_llm_compatibility_skip_count": autonomy_summary.get(
            "external_llm_compatibility_skip_count",
            0,
        ),
        "external_llm_cooldown_skip_count": autonomy_summary.get("external_llm_cooldown_skip_count", 0),
        "external_llm_compatibility_failure_count": autonomy_summary.get(
            "external_llm_compatibility_failure_count",
            0,
        ),
        "external_llm_compatibility_failure_ratio": autonomy_summary.get(
            "external_llm_compatibility_failure_ratio",
            0.0,
        ),
        "external_llm_effective_response_count": autonomy_summary.get(
            "external_llm_effective_response_count",
            0,
        ),
        "external_llm_effective_response_ratio": autonomy_summary.get(
            "external_llm_effective_response_ratio",
            0.0,
        ),
        "external_llm_empty_200_response_count": autonomy_summary.get(
            "external_llm_empty_200_response_count",
            0,
        ),
        "external_llm_request_status_counts": dict(
            autonomy_summary.get("external_llm_request_status_counts") or {}
        ),
        "external_llm_selected_count": autonomy_summary.get("external_llm_selected_count", 0),
        "external_llm_last_error_type": autonomy_summary.get("external_llm_last_error_type"),
        "external_llm_last_error": autonomy_summary.get("external_llm_last_error"),
        "external_llm_provider_health_status": autonomy_summary.get(
            "external_llm_provider_health_status"
        ),
        "external_llm_provider_scheduler_should_disable": bool(
            autonomy_summary.get("external_llm_provider_scheduler_should_disable")
        ),
        "external_llm_provider_scheduler_skip_reason": autonomy_summary.get(
            "external_llm_provider_scheduler_skip_reason"
        ),
        "external_llm_provider_control_mode": autonomy_summary.get("external_llm_provider_control_mode"),
        "external_llm_provider_control_reasons": list(
            autonomy_summary.get("external_llm_provider_control_reasons") or []
        ),
        "generator_mode_controls": dict(autonomy_summary.get("generator_mode_controls") or {}),
        "external_llm_elapsed_seconds": autonomy_summary.get("external_llm_elapsed_seconds"),
        "elapsed_seconds": round(elapsed, 1),
    }
