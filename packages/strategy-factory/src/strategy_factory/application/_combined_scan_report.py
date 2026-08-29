"""Helpers for assembling autonomy pre-run scan summaries."""

from __future__ import annotations

from typing import Any

from ..domain.constants import (
    AUTONOMY_MAX_RESEARCH_TASKS,
    STOCK_STRATEGY_MATRIX_BATCH_SIZE,
    STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY,
    STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
)


def build_combined_scan_report(
    *,
    scan_summary: dict[str, Any],
    tasks: list[dict[str, Any]],
    task_source_counts: dict[str, Any],
    event_task_count: int,
    bulk_tasks: list[dict[str, Any]],
    bulk_report: dict[str, Any],
    bulk_cursor: dict[str, Any],
    task_budget_meta: dict[str, Any],
    external_provider_control: dict[str, Any],
    generator_mode_controls: dict[str, Any],
    opportunity_scan: dict[str, Any],
) -> dict[str, Any]:
    bulk_summary = dict((bulk_report or {}).get("summary") or {})
    task_type_counts: dict[str, int] = {}
    for task in list(tasks or []):
        opportunity_type = str((task or {}).get("opportunity_type") or "unknown")
        task_type_counts[opportunity_type] = task_type_counts.get(opportunity_type, 0) + 1

    summary = {
        **dict(scan_summary or {}),
        "task_count": len(tasks),
        "task_types": task_type_counts,
        "task_sources": dict(task_source_counts or {}),
        "event_task_count": int(event_task_count or 0),
        "bulk_stock_task_count": len(bulk_tasks or []),
        "bulk_stock_matrix_enabled": bool(bulk_summary.get("enabled")),
        "bulk_stock_matrix_configured_enabled": bool(bulk_summary.get("configured_enabled")),
        "bulk_stock_matrix_stock_count": int(bulk_summary.get("stock_count") or 0),
        "bulk_stock_matrix_eligible_stock_count": int(bulk_summary.get("eligible_stock_count") or 0),
        "bulk_stock_matrix_loaded_stock_count": int(bulk_summary.get("loaded_stock_count") or 0),
        "bulk_stock_matrix_pages_loaded": int(bulk_summary.get("pages_loaded") or 0),
        "bulk_stock_matrix_analysis_complete": bool(bulk_summary.get("analysis_complete")),
        "bulk_stock_matrix_analysis_stock_coverage_ratio": bulk_summary.get("analysis_stock_coverage_ratio"),
        "bulk_stock_matrix_family_counts": dict(bulk_summary.get("family_counts") or {}),
        "bulk_stock_matrix_planned_family_counts": dict(bulk_summary.get("planned_family_counts") or {}),
        "bulk_stock_matrix_universe_limit": int(bulk_summary.get("universe_limit") or STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT),
        "bulk_stock_matrix_batch_size": int(bulk_summary.get("batch_size") or STOCK_STRATEGY_MATRIX_BATCH_SIZE),
        "bulk_stock_matrix_batch_count": int(bulk_summary.get("batch_count") or 0),
        "bulk_stock_matrix_selected_batch_count": int(bulk_summary.get("selected_batch_count") or 0),
        "bulk_stock_matrix_bulk_concurrency": int(
            bulk_summary.get("bulk_concurrency") or STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY
        ),
        "bulk_stock_matrix_run_window": bulk_summary.get("run_window"),
        "bulk_stock_matrix_run_window_active": bool(bulk_summary.get("run_window_active")),
        "bulk_stock_matrix_run_window_current_period": bulk_summary.get("run_window_current_period"),
        "bulk_stock_matrix_skip_reason": bulk_summary.get("skip_reason"),
        "bulk_stock_matrix_load_error": bulk_summary.get("load_error"),
        "bulk_stock_matrix_load_error_type": bulk_summary.get("load_error_type"),
        "bulk_stock_matrix_last_error": bulk_summary.get("last_error"),
        "bulk_stock_matrix_last_error_type": bulk_summary.get("last_error_type"),
        "bulk_stock_matrix_last_error_offset": bulk_summary.get("last_error_offset"),
        "bulk_stock_matrix_profile_loaded_count": int(bulk_summary.get("profile_loaded_count") or 0),
        "bulk_stock_matrix_profile_missing_count": int(bulk_summary.get("profile_missing_count") or 0),
        "bulk_stock_matrix_profile_load_error": bulk_summary.get("profile_load_error"),
        "bulk_stock_matrix_profile_load_error_type": bulk_summary.get("profile_load_error_type"),
        "bulk_stock_matrix_requested_universe_offset": int(bulk_summary.get("requested_universe_offset") or 0),
        "bulk_stock_matrix_effective_universe_offset": int(bulk_summary.get("effective_universe_offset") or 0),
        "bulk_stock_matrix_universe_offset_fallback": bool(bulk_summary.get("universe_offset_fallback")),
        "bulk_stock_matrix_next_universe_offset": int(bulk_summary.get("next_universe_offset") or 0),
        "bulk_stock_matrix_cursor_wrapped": bool(bulk_summary.get("cursor_wrapped")),
        "bulk_stock_matrix_cursor_mode": bulk_summary.get("cursor_mode") or "task_offset",
        "bulk_stock_matrix_requested_task_offset": int(bulk_summary.get("requested_task_offset") or 0),
        "bulk_stock_matrix_effective_task_offset": int(bulk_summary.get("effective_task_offset") or 0),
        "bulk_stock_matrix_task_offset_fallback": bool(bulk_summary.get("task_offset_fallback")),
        "bulk_stock_matrix_next_task_offset": int(bulk_summary.get("next_task_offset") or 0),
        "bulk_stock_matrix_task_cursor_wrapped": bool(bulk_summary.get("task_cursor_wrapped")),
        "bulk_stock_matrix_cursor_source": bulk_summary.get("cursor_source") or bulk_cursor.get("source"),
        "bulk_stock_matrix_cursor_resume_from_run_id": (
            bulk_summary.get("cursor_resume_from_run_id") or bulk_cursor.get("resume_from_run_id")
        ),
        "bulk_stock_matrix_effective_task_budget": int(bulk_summary.get("effective_task_budget") or 0),
        "bulk_stock_matrix_max_candidates_per_run": int(bulk_summary.get("max_candidates_per_run") or 0),
        "bulk_stock_matrix_estimated_candidate_count": int(bulk_summary.get("estimated_candidate_count") or 0),
        "bulk_stock_matrix_planned_task_count": int(bulk_summary.get("planned_task_count") or 0),
        "bulk_stock_matrix_planned_candidate_count": int(bulk_summary.get("planned_candidate_count") or 0),
        "bulk_stock_matrix_tasks_per_shard": int(bulk_summary.get("tasks_per_shard") or 0),
        "bulk_stock_matrix_shard_count": int(bulk_summary.get("shard_count") or 0),
        "bulk_stock_matrix_selected_shard_count": int(bulk_summary.get("selected_shard_count") or 0),
        "bulk_stock_matrix_selected_shard_ids": list(bulk_summary.get("selected_shard_ids") or []),
        "bulk_stock_matrix_stock_coverage_ratio": bulk_summary.get("stock_coverage_ratio"),
        "bulk_stock_matrix_allocation_mode": bulk_summary.get("allocation_mode"),
        "bulk_stock_matrix_allocation_pass_counts": dict(bulk_summary.get("allocation_pass_counts") or {}),
        "bulk_stock_matrix_planned_allocation_pass_counts": dict(
            bulk_summary.get("planned_allocation_pass_counts") or {}
        ),
        "bulk_stock_matrix_overflow_task_count": int(bulk_summary.get("overflow_task_count") or 0),
        "router_enabled": bool(bulk_summary.get("router_enabled")),
        "router_strict": bool(bulk_summary.get("router_strict")),
        "router_telemetry_enabled": bool(bulk_summary.get("router_telemetry_enabled")),
        "router_candidate_stock_count": int(bulk_summary.get("router_candidate_stock_count") or 0),
        "router_applied_count": int(bulk_summary.get("router_applied_count") or 0),
        "router_status_counts": dict(bulk_summary.get("router_status_counts") or {}),
        "router_fallback_reason_counts": dict(
            bulk_summary.get("router_fallback_reason_counts") or {}
        ),
        "router_family_counts": dict(bulk_summary.get("router_family_counts") or {}),
        "router_holding_bucket_counts": dict(
            bulk_summary.get("router_holding_bucket_counts") or {}
        ),
        "profile_summary_present_count": int(bulk_summary.get("profile_summary_present_count") or 0),
        "profile_summary_missing_count": int(bulk_summary.get("profile_summary_missing_count") or 0),
        "profile_summary_generated_count": int(bulk_summary.get("profile_summary_generated_count") or 0),
        "selected_router_applied_count": int(bulk_summary.get("selected_router_applied_count") or 0),
        "selected_profile_summary_missing_count": int(
            bulk_summary.get("selected_profile_summary_missing_count") or 0
        ),
        "direction_gate_enabled": bool(bulk_summary.get("direction_gate_enabled")),
        "direction_gate_candidate_stock_count": int(
            bulk_summary.get("direction_gate_candidate_stock_count") or 0
        ),
        "direction_gate_evaluated_count": int(bulk_summary.get("direction_gate_evaluated_count") or 0),
        "direction_gate_applied_count": int(bulk_summary.get("direction_gate_applied_count") or 0),
        "direction_gate_fallback_count": int(bulk_summary.get("direction_gate_fallback_count") or 0),
        "direction_gate_status_counts": dict(bulk_summary.get("direction_gate_status_counts") or {}),
        "direction_gate_reason_counts": dict(bulk_summary.get("direction_gate_reason_counts") or {}),
        "direction_gate_dropped_family_counts": dict(
            bulk_summary.get("direction_gate_dropped_family_counts") or {}
        ),
        "selected_direction_gate_applied_count": int(
            bulk_summary.get("selected_direction_gate_applied_count") or 0
        ),
        "selected_direction_gate_fallback_count": int(
            bulk_summary.get("selected_direction_gate_fallback_count") or 0
        ),
        "selected_direction_gate_task_count": int(
            bulk_summary.get("selected_direction_gate_task_count") or 0
        ),
        "max_research_tasks": int(task_budget_meta.get("max_research_tasks") or AUTONOMY_MAX_RESEARCH_TASKS),
        "max_bulk_research_tasks": int(task_budget_meta.get("max_bulk_research_tasks") or 0),
        "combined_research_task_budget": int(
            task_budget_meta.get("combined_research_task_budget")
            or task_budget_meta.get("max_research_tasks")
            or AUTONOMY_MAX_RESEARCH_TASKS
        ),
        "scan_research_task_budget": int(
            task_budget_meta.get("scan_research_task_budget") or AUTONOMY_MAX_RESEARCH_TASKS
        ),
        "reserved_bulk_task_budget": int(task_budget_meta.get("reserved_bulk_task_budget") or 0),
        "selected_scan_task_count": int(task_budget_meta.get("selected_scan_task_count") or 0),
        "selected_bulk_task_count": int(task_budget_meta.get("selected_bulk_task_count") or 0),
        "planned_bulk_task_count": int(task_budget_meta.get("planned_bulk_task_count") or 0),
        "clipped_bulk_task_count": int(task_budget_meta.get("clipped_bulk_task_count") or 0),
        "planned_feedback_control_mode_counts": dict(
            task_budget_meta.get("planned_feedback_control_mode_counts") or {}
        ),
        "planned_feedback_legacy_control_mode_counts": dict(
            task_budget_meta.get("planned_feedback_legacy_control_mode_counts")
            or task_budget_meta.get("planned_feedback_control_mode_counts")
            or {}
        ),
        "planned_feedback_skill_control_mode_counts": dict(
            task_budget_meta.get("planned_feedback_skill_control_mode_counts") or {}
        ),
        "planned_feedback_target_pool_control_mode_counts": dict(
            task_budget_meta.get("planned_feedback_target_pool_control_mode_counts") or {}
        ),
        "planned_feedback_holding_bucket_control_mode_counts": dict(
            task_budget_meta.get("planned_feedback_holding_bucket_control_mode_counts") or {}
        ),
        "planned_feedback_generator_mode_control_mode_counts": dict(
            task_budget_meta.get("planned_feedback_generator_mode_control_mode_counts") or {}
        ),
        "planned_feedback_skill_target_pool_control_mode_counts": dict(
            task_budget_meta.get("planned_feedback_skill_target_pool_control_mode_counts") or {}
        ),
        "planned_feedback_skill_holding_bucket_control_mode_counts": dict(
            task_budget_meta.get("planned_feedback_skill_holding_bucket_control_mode_counts")
            or {}
        ),
        "planned_feedback_skill_generator_mode_control_mode_counts": dict(
            task_budget_meta.get("planned_feedback_skill_generator_mode_control_mode_counts")
            or {}
        ),
        "planned_feedback_cooldown_task_count": int(
            task_budget_meta.get("planned_feedback_cooldown_task_count") or 0
        ),
        "planned_feedback_limited_task_count": int(
            task_budget_meta.get("planned_feedback_limited_task_count") or 0
        ),
        "planned_feedback_relaxed_task_count": int(
            task_budget_meta.get("planned_feedback_relaxed_task_count") or 0
        ),
        "blocked_feedback_task_count": int(task_budget_meta.get("blocked_feedback_task_count") or 0),
        "suppressed_families": list(task_budget_meta.get("suppressed_families") or []),
        "suppressed_target_pools": list(task_budget_meta.get("suppressed_target_pools") or []),
        "suppressed_holding_buckets": list(task_budget_meta.get("suppressed_holding_buckets") or []),
        "suppressed_generator_modes": list(task_budget_meta.get("suppressed_generator_modes") or []),
        "selected_feedback_control_mode_counts": dict(
            task_budget_meta.get("selected_feedback_control_mode_counts") or {}
        ),
        "selected_feedback_legacy_control_mode_counts": dict(
            task_budget_meta.get("selected_feedback_legacy_control_mode_counts")
            or task_budget_meta.get("selected_feedback_control_mode_counts")
            or {}
        ),
        "selected_feedback_skill_control_mode_counts": dict(
            task_budget_meta.get("selected_feedback_skill_control_mode_counts") or {}
        ),
        "selected_feedback_target_pool_control_mode_counts": dict(
            task_budget_meta.get("selected_feedback_target_pool_control_mode_counts") or {}
        ),
        "selected_feedback_holding_bucket_control_mode_counts": dict(
            task_budget_meta.get("selected_feedback_holding_bucket_control_mode_counts") or {}
        ),
        "selected_feedback_generator_mode_control_mode_counts": dict(
            task_budget_meta.get("selected_feedback_generator_mode_control_mode_counts") or {}
        ),
        "selected_feedback_skill_target_pool_control_mode_counts": dict(
            task_budget_meta.get("selected_feedback_skill_target_pool_control_mode_counts") or {}
        ),
        "selected_feedback_skill_holding_bucket_control_mode_counts": dict(
            task_budget_meta.get("selected_feedback_skill_holding_bucket_control_mode_counts")
            or {}
        ),
        "selected_feedback_skill_generator_mode_control_mode_counts": dict(
            task_budget_meta.get("selected_feedback_skill_generator_mode_control_mode_counts")
            or {}
        ),
        "selected_feedback_limited_task_count": int(
            task_budget_meta.get("selected_feedback_limited_task_count") or 0
        ),
        "selected_feedback_relaxed_task_count": int(
            task_budget_meta.get("selected_feedback_relaxed_task_count") or 0
        ),
        "external_llm_provider_control_mode": external_provider_control.get("control_mode"),
        "external_llm_provider_control_reasons": list(
            external_provider_control.get("control_reasons") or []
        ),
        "generator_mode_controls": dict(generator_mode_controls or {}),
    }
    return {
        "summary": summary,
        "tasks": list(tasks or []),
        "router_artifact": dict((bulk_report or {}).get("router_artifact") or {}),
        "direction_gate_artifact": dict((bulk_report or {}).get("direction_gate_artifact") or {}),
        "opportunity_scan": dict(opportunity_scan or {}),
        "bulk_stock_matrix": dict(bulk_report or {}),
    }
