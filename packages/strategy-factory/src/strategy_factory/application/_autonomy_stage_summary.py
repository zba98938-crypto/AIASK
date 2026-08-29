"""Helpers for assembling autonomy stage summaries."""

from __future__ import annotations

from typing import Any

from ..domain.constants import (
    AUTONOMY_MAX_RESEARCH_TASKS,
    RESEARCH_TASK_CONCURRENCY,
    STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY,
    STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
)

_BENIGN_SKIPPED_EXTERNAL_STATUSES = {
    "skipped",
    "skipped_target_context_blocked",
}


def _normalize_external_status_counts(external_status_counts: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for raw_status, raw_count in dict(external_status_counts or {}).items():
        token = str(raw_status or "").strip().lower()
        if not token:
            token = "unknown"
        elif token in _BENIGN_SKIPPED_EXTERNAL_STATUSES:
            token = "skipped"
        counts[token] = counts.get(token, 0) + int(raw_count or 0)
    return counts


def _aggregate_pipeline_fallback_counts(task_results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in list(task_results or []):
        llm_generation = dict((item or {}).get("llm_generation") or {})
        fallback_counts = dict(llm_generation.get("pipeline_staged_fallback_counts") or {})
        if not fallback_counts:
            reason = str(llm_generation.get("pipeline_staged_fallback_reason") or "").strip()
            if reason:
                fallback_counts = {reason: 1}
        for reason, count in fallback_counts.items():
            token = str(reason or "unknown").strip() or "unknown"
            counts[token] = counts.get(token, 0) + int(count or 0)
    return counts


def _count_task_timeout_skips(task_results: list[dict[str, Any]]) -> int:
    total = 0
    for item in list(task_results or []):
        llm_generation = dict((item or {}).get("llm_generation") or {})
        if bool(llm_generation.get("task_timeout_skip")):
            total += 1
    return total


def resolve_autonomy_overall_status(
    task_results: list[dict[str, Any]],
    external_status_counts: dict[str, Any],
    *,
    generated_candidate_count: int = 0,
    experiment_count: int = 0,
) -> tuple[str, int, int]:
    completed_task_count = len([item for item in task_results if item.get("status") == "completed"])
    failed_task_count = len([item for item in task_results if item.get("status") == "failed"])
    normalized_external_status_counts = _normalize_external_status_counts(external_status_counts)
    positive_provider = sum(
        int(normalized_external_status_counts.get(key, 0) or 0)
        for key in ("succeeded", "fallback_only")
    )
    failed_provider = int(normalized_external_status_counts.get("failed", 0) or 0)
    skipped_provider = int(normalized_external_status_counts.get("skipped", 0) or 0)
    has_local_output = int(generated_candidate_count or 0) > 0 or int(experiment_count or 0) > 0
    if not task_results:
        overall_status = "skipped"
    elif positive_provider > 0 and failed_provider == 0 and failed_task_count == 0:
        overall_status = "succeeded"
    elif failed_provider > 0 and failed_task_count == 0 and completed_task_count == len(task_results) and has_local_output:
        overall_status = "succeeded"
    elif failed_provider > 0 and positive_provider == 0 and skipped_provider == 0:
        overall_status = "failed"
    elif failed_provider > 0 or failed_task_count > 0:
        overall_status = "partial" if completed_task_count > 0 else "failed"
    elif skipped_provider == len(task_results) and failed_task_count == 0:
        overall_status = "succeeded"
    else:
        overall_status = "partial" if completed_task_count else "failed"
    return overall_status, completed_task_count, failed_task_count


def build_autonomy_stage_summary(
    *,
    task_results: list[dict[str, Any]],
    task_source_counts: dict[str, Any],
    event_task_count: int,
    bulk_report: dict[str, Any],
    bulk_cursor: dict[str, Any],
    generated_candidates: list[dict[str, Any]],
    all_experiments: list[dict[str, Any]],
    external_status_counts: dict[str, Any],
    total_attempt_count: int,
    total_network_request_count: int,
    total_real_request_count: int,
    total_compatibility_skip_count: int,
    total_cooldown_skip_count: int,
    total_compatibility_failure_count: int,
    total_effective_response_count: int,
    total_empty_200_response_count: int,
    total_request_status_counts: dict[str, Any],
    total_selected_count: int,
    total_evidence_count: int,
    last_error_type: str | None,
    last_error: str | None,
    elapsed_seconds: float,
    external_provider_health: dict[str, Any],
    effective_research_concurrency: int,
    has_bulk_tasks: bool,
    effective_bulk_research_concurrency: int,
    bulk_tasks_use_external_llm: bool,
    research_task_timeout_sec: float,
    task_budget_meta: dict[str, Any],
    selected_feedback_summary: dict[str, Any],
    external_provider_control: dict[str, Any],
    generator_mode_controls: dict[str, Any],
    shared_generation_context_preloaded: bool,
    persistence_failures: list[dict[str, Any]],
    lifecycle_metrics: dict[str, Any],
    combined_scan_report: dict[str, Any],
) -> dict[str, Any]:
    bulk_summary = dict((bulk_report or {}).get("summary") or {})
    overall_status, completed_task_count, failed_task_count = resolve_autonomy_overall_status(
        task_results,
        external_status_counts,
        generated_candidate_count=len(generated_candidates or []),
        experiment_count=len(all_experiments or []),
    )
    pipeline_fallback_counts = _aggregate_pipeline_fallback_counts(task_results)
    task_timeout_skip_count = _count_task_timeout_skips(task_results)
    return {
        "task_count": len(task_results),
        "task_source_counts": dict(task_source_counts or {}),
        "event_task_count": int(event_task_count or 0),
        "snapshot_task_count": int((task_source_counts or {}).get("snapshot", 0)),
        "bulk_stock_task_count": int((task_source_counts or {}).get("bulk_stock_matrix", 0)),
        "bulk_stock_matrix_eligible_stock_count": int(bulk_summary.get("eligible_stock_count") or 0),
        "bulk_stock_matrix_loaded_stock_count": int(bulk_summary.get("loaded_stock_count") or 0),
        "bulk_stock_matrix_pages_loaded": int(bulk_summary.get("pages_loaded") or 0),
        "bulk_stock_matrix_skip_reason": bulk_summary.get("skip_reason"),
        "bulk_stock_matrix_load_error": bulk_summary.get("load_error"),
        "bulk_stock_matrix_load_error_type": bulk_summary.get("load_error_type"),
        "bulk_stock_matrix_last_error": bulk_summary.get("last_error"),
        "bulk_stock_matrix_last_error_type": bulk_summary.get("last_error_type"),
        "bulk_stock_matrix_last_error_offset": bulk_summary.get("last_error_offset"),
        "bulk_stock_matrix_analysis_complete": bool(bulk_summary.get("analysis_complete")),
        "bulk_stock_matrix_analysis_stock_coverage_ratio": bulk_summary.get("analysis_stock_coverage_ratio"),
        "bulk_stock_matrix_universe_limit": int(
            bulk_summary.get("universe_limit") or STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT
        ),
        "bulk_stock_matrix_batch_count": int(bulk_summary.get("batch_count") or 0),
        "bulk_stock_matrix_selected_batch_count": int(bulk_summary.get("selected_batch_count") or 0),
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
        "bulk_stock_matrix_estimated_candidate_count": int(
            bulk_summary.get("estimated_candidate_count") or 0
        ),
        "bulk_stock_matrix_planned_task_count": int(bulk_summary.get("planned_task_count") or 0),
        "bulk_stock_matrix_planned_candidate_count": int(
            bulk_summary.get("planned_candidate_count") or 0
        ),
        "bulk_stock_matrix_shard_count": int(bulk_summary.get("shard_count") or 0),
        "bulk_stock_matrix_selected_shard_count": int(bulk_summary.get("selected_shard_count") or 0),
        "bulk_stock_matrix_selected_shard_ids": list(bulk_summary.get("selected_shard_ids") or []),
        # PR-S17/PR-S19/PR-S20：画像加载、画像质量分布、向量复用观测
        "bulk_stock_matrix_profile_loaded_count": int(bulk_summary.get("profile_loaded_count") or 0),
        "bulk_stock_matrix_profile_missing_count": int(bulk_summary.get("profile_missing_count") or 0),
        "bulk_stock_matrix_profile_load_error": bulk_summary.get("profile_load_error"),
        "bulk_stock_matrix_profile_load_error_type": bulk_summary.get("profile_load_error_type"),
        "bulk_stock_matrix_profile_quality_distribution": dict(
            bulk_summary.get("profile_quality_distribution") or {}
        ),
        "bulk_stock_matrix_profile_archetype_distribution": dict(
            bulk_summary.get("profile_archetype_distribution") or {}
        ),
        "bulk_stock_matrix_similar_profile_lookup_count": int(
            bulk_summary.get("similar_profile_lookup_count") or 0
        ),
        "bulk_stock_matrix_similar_profile_hit_count": int(
            bulk_summary.get("similar_profile_hit_count") or 0
        ),
        "bulk_stock_matrix_verified_strategy_index_count": int(
            bulk_summary.get("verified_strategy_index_count") or 0
        ),
        "bulk_stock_matrix_vector_reuse_eligible_count": int(
            bulk_summary.get("vector_reuse_eligible_count") or 0
        ),
        "bulk_stock_matrix_vector_reuse_count": int(
            bulk_summary.get("vector_reuse_count") or 0
        ),
        "bulk_stock_matrix_vector_reuse_avg_similarity": float(
            bulk_summary.get("vector_reuse_avg_similarity") or 0.0
        ),
        "bulk_stock_matrix_vector_reuse_enabled": bool(
            bulk_summary.get("vector_reuse_enabled")
        ),
        "event_evidence_count": int(total_evidence_count or 0),
        "completed_task_count": int(completed_task_count),
        "failed_task_count": int(failed_task_count),
        "task_scan": dict(combined_scan_report or {}),
        "task_results": list(task_results or []),
        "generated_count": len(generated_candidates or []),
        "experiment_count": len(all_experiments or []),
        "task_run_ids": [item.get("task_run_id") for item in task_results if item.get("task_run_id") is not None],
        "external_llm_status": overall_status,
        "llm_status_counts": dict(external_status_counts or {}),
        "external_llm_status_counts": dict(external_status_counts or {}),
        "pipeline_fallback_counts": pipeline_fallback_counts,
        "task_timeout_skip_count": task_timeout_skip_count,
        "task_timeout_policy": "skip_without_local_fallback" if task_timeout_skip_count else None,
        "external_llm_attempt_count": int(total_attempt_count or 0),
        "external_llm_stage_attempt_count": int(total_attempt_count or 0),
        "external_llm_network_request_count": int(total_network_request_count or 0),
        "external_llm_real_request_count": int(total_real_request_count or 0),
        "external_llm_compatibility_skip_count": int(total_compatibility_skip_count or 0),
        "external_llm_cooldown_skip_count": int(total_cooldown_skip_count or 0),
        "external_llm_compatibility_failure_count": int(total_compatibility_failure_count or 0),
        "external_llm_compatibility_failure_ratio": round(
            total_compatibility_failure_count / total_real_request_count,
            4,
        )
        if total_real_request_count
        else 0.0,
        "external_llm_effective_response_count": int(total_effective_response_count or 0),
        "external_llm_effective_response_ratio": round(
            total_effective_response_count / total_real_request_count,
            4,
        )
        if total_real_request_count
        else 0.0,
        "external_llm_empty_200_response_count": int(total_empty_200_response_count or 0),
        "external_llm_request_status_counts": dict(total_request_status_counts or {}),
        "external_llm_selected_count": int(total_selected_count or 0),
        "external_llm_last_error_type": last_error_type,
        "external_llm_last_error": last_error,
        "external_llm_elapsed_seconds": round(float(elapsed_seconds or 0.0), 4),
        "external_llm_provider_health_status": external_provider_health.get("health_status"),
        "external_llm_provider_scheduler_should_disable": bool(
            external_provider_health.get("scheduler_should_disable")
        ),
        "external_llm_provider_scheduler_skip_reason": external_provider_health.get("scheduler_skip_reason"),
        "external_llm_provider_cooldown_active": bool(
            external_provider_health.get("compatibility_cooldown_active")
        ),
        "research_task_concurrency": int(effective_research_concurrency or 0),
        "configured_research_task_concurrency": RESEARCH_TASK_CONCURRENCY,
        "bulk_task_concurrency": int(effective_bulk_research_concurrency or 0) if has_bulk_tasks else 0,
        "configured_bulk_task_concurrency": int(STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY) if has_bulk_tasks else 0,
        "bulk_tasks_use_external_llm": bool(bulk_tasks_use_external_llm) if has_bulk_tasks else False,
        "research_task_timeout_sec": round(float(research_task_timeout_sec or 0.0), 4),
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
            selected_feedback_summary.get("feedback_control_mode_counts") or {}
        ),
        "selected_feedback_legacy_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_legacy_control_mode_counts")
            or selected_feedback_summary.get("feedback_control_mode_counts")
            or {}
        ),
        "selected_feedback_skill_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_skill_control_mode_counts") or {}
        ),
        "selected_feedback_target_pool_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_target_pool_control_mode_counts") or {}
        ),
        "selected_feedback_holding_bucket_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_holding_bucket_control_mode_counts") or {}
        ),
        "selected_feedback_generator_mode_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_generator_mode_control_mode_counts") or {}
        ),
        "selected_feedback_skill_target_pool_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_skill_target_pool_control_mode_counts") or {}
        ),
        "selected_feedback_skill_holding_bucket_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_skill_holding_bucket_control_mode_counts")
            or {}
        ),
        "selected_feedback_skill_generator_mode_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_skill_generator_mode_control_mode_counts")
            or {}
        ),
        "selected_feedback_limited_task_count": int(
            selected_feedback_summary.get("feedback_limited_task_count") or 0
        ),
        "selected_feedback_relaxed_task_count": int(
            selected_feedback_summary.get("feedback_relaxed_task_count") or 0
        ),
        "external_llm_provider_control_mode": external_provider_control.get("control_mode"),
        "external_llm_provider_control_reasons": list(
            external_provider_control.get("control_reasons") or []
        ),
        "external_llm_provider_control_metrics": {
            key: external_provider_control.get(key)
            for key in (
                "stage_attempt_count",
                "real_request_count",
                "compatibility_skip_count",
                "compatibility_skip_ratio",
                "compatibility_failure_count",
                "compatibility_failure_ratio",
                "effective_response_count",
                "effective_response_ratio",
                "empty_200_response_count",
                "empty_200_response_ratio",
            )
        },
        "generator_mode_controls": dict(generator_mode_controls or {}),
        "shared_generation_context_preloaded": bool(shared_generation_context_preloaded),
        "persistence_failures": list(persistence_failures or []),
        "persistence_failure_count": len(list(persistence_failures or [])),
        **dict(lifecycle_metrics or {}),
    }
