"""Factor research artifact summary assembly helpers."""

from __future__ import annotations

from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return float(default)


def build_factor_research_summary(
    *,
    active_factors: list[dict[str, Any]],
    active_candidate_pool: dict[str, Any],
    governed_source_candidate_count: int,
    governed_active_registry_candidate_count: int,
    governed_blocked_candidate_count: int,
    governed_blocked_ratio: float,
    governed_pending_candidate_count: int,
    governed_pending_ratio: float,
    governed_ineligible_candidate_count: int,
    governed_ineligible_ratio: float,
    governed_latest_candidate_at: str | None,
    governed_freshness_days: int | None,
    ranked_factors: list[dict[str, Any]],
    top_factor_names: list[str],
    top_candidate_names: list[str],
    governed_family_summary: list[dict[str, Any]],
    governed_regime_summary: list[dict[str, Any]],
    preferred_strategy_types: list[str],
    family_preference_order: list[str],
    family_preference_source_mode: str | None,
    governed_top_candidates: list[dict[str, Any]],
    governed_pool_missing_after_scheduler_success: bool,
    governed_candidate_pool_mode: str | None,
    governed_candidate_pool_provisional: bool,
    governed_candidate_pool_strict_count: int,
    governed_candidate_pool_provisional_count: int,
    governed_candidate_pool_provisional_spillover_count: int,
    governed_candidate_pool_provisional_spillover_enabled: bool,
    governed_candidate_pool_provisional_spillover_policy: dict[str, Any],
    governed_candidate_pool_provisional_spillover_policy_status: str | None,
    governed_candidate_pool_provisional_pending_count: int,
    governed_candidate_pool_strict_shortfall_count: int,
    scheduler_status: dict[str, Any],
    scheduler_recent_success: bool,
    scheduler_llm_validation_status: str | None,
    scheduler_llm_provider: dict[str, Any],
    scheduler_llm_provider_health_status: str | None,
    lightweight_mock_fallback: bool,
    governed_exclusion_reason_counts: dict[str, Any],
    governed_blocking_reason_counts: dict[str, Any],
    governed_pending_reason_counts: dict[str, Any],
    governed_ineligible_reason_counts: dict[str, Any],
    governed_registry_summary: dict[str, Any],
    top_candidate_lineage: list[dict[str, Any]],
    model_registry_lineage: dict[str, Any],
    model_lineage_summary: dict[str, Any],
    stock_family_allocation_summary: dict[str, Any],
    lifecycle_feedback_input: dict[str, Any],
    budget_feedback_summary: dict[str, Any],
    paper_observation_backlog: dict[str, Any],
    incubation_factory_health: dict[str, Any],
    search_route_action_counts: dict[str, int],
    degraded: bool,
    freshness_days: int | None,
    latest_factor_date: str | None,
    history_stale: bool,
    stale: bool,
    quality_flags: list[str],
    decay_factors: list[str],
    stability_tags: dict[str, str],
) -> dict[str, Any]:
    return {
        "active_factor_count": len(active_factors),
        "active_candidate_count": int(active_candidate_pool.get("count") or 0),
        "governed_source_candidate_count": governed_source_candidate_count,
        "governed_active_registry_candidate_count": governed_active_registry_candidate_count,
        "governed_blocked_candidate_count": governed_blocked_candidate_count,
        "governed_blocked_ratio": governed_blocked_ratio,
        "governed_pending_candidate_count": governed_pending_candidate_count,
        "governed_pending_ratio": governed_pending_ratio,
        "governed_ineligible_candidate_count": governed_ineligible_candidate_count,
        "governed_ineligible_ratio": governed_ineligible_ratio,
        "governed_latest_candidate_at": governed_latest_candidate_at,
        "governed_freshness_days": governed_freshness_days,
        "ranked_factor_count": len(ranked_factors),
        "top_factor_names": top_factor_names,
        "top_candidate_names": top_candidate_names,
        "active_family_names": [
            str(item.get("family") or "")
            for item in governed_family_summary
            if str(item.get("family") or "")
        ],
        "active_regime_names": [
            str(item.get("regime") or "")
            for item in governed_regime_summary
            if str(item.get("regime") or "")
        ],
        "preferred_strategy_types": preferred_strategy_types,
        "family_preference_order": family_preference_order,
        "family_preference_source_mode": family_preference_source_mode,
        "factor_source_mode": (
            "active_factor_pool_fallback"
            if governed_top_candidates and governed_candidate_pool_mode == "active_factor_pool_fallback"
            else (
                "factor_mining_active_pool"
                if governed_top_candidates and governed_candidate_pool_mode == "factor_mining_active_pool"
                else (
                    "governed_candidate_pool"
                    if governed_top_candidates
                    else (
                        "governed_pool_missing_after_scheduler_success"
                        if governed_pool_missing_after_scheduler_success
                        else "seed_fallback"
                    )
                )
            )
        ),
        "governed_candidate_pool_mode": governed_candidate_pool_mode,
        "governed_candidate_pool_provisional": governed_candidate_pool_provisional,
        "governed_candidate_pool_strict_count": governed_candidate_pool_strict_count,
        "governed_candidate_pool_provisional_count": governed_candidate_pool_provisional_count,
        "governed_candidate_pool_provisional_spillover_count": (
            governed_candidate_pool_provisional_spillover_count
        ),
        "governed_candidate_pool_provisional_spillover_enabled": (
            governed_candidate_pool_provisional_spillover_enabled
        ),
        "governed_candidate_pool_provisional_spillover_policy": (
            governed_candidate_pool_provisional_spillover_policy
        ),
        "governed_candidate_pool_provisional_spillover_policy_status": (
            governed_candidate_pool_provisional_spillover_policy_status
        ),
        "governed_candidate_pool_provisional_pending_count": (
            governed_candidate_pool_provisional_pending_count
        ),
        "governed_candidate_pool_strict_shortfall_count": (
            governed_candidate_pool_strict_shortfall_count
        ),
        "scheduler_last_run": scheduler_status.get("last_run"),
        "scheduler_freshness_sec": scheduler_status.get("freshness_sec"),
        "scheduler_recent_success": scheduler_recent_success,
        "scheduler_llm_validation_status": scheduler_llm_validation_status,
        "factor_llm_provider_enabled": bool(scheduler_llm_provider.get("enabled")),
        "factor_llm_provider_ready": bool(scheduler_llm_provider.get("ready")),
        "factor_llm_provider_health_status": scheduler_llm_provider_health_status,
        "factor_llm_provider_rebuild_count": int(
            scheduler_llm_provider.get("rebuild_count") or 0
        ),
        "factor_llm_provider_last_error_type": scheduler_llm_provider.get("last_error_type"),
        "governed_pool_missing_after_scheduler_success": (
            governed_pool_missing_after_scheduler_success
        ),
        "lightweight_mock_fallback": lightweight_mock_fallback,
        "governed_exclusion_reason_counts": governed_exclusion_reason_counts,
        "governed_blocking_reason_counts": governed_blocking_reason_counts,
        "governed_pending_reason_counts": governed_pending_reason_counts,
        "governed_ineligible_reason_counts": governed_ineligible_reason_counts,
        "governed_registry_stage_counts": dict(
            governed_registry_summary.get("registry_stage_counts") or {}
        ),
        "top_candidate_lineage": top_candidate_lineage,
        "model_registry_lineage_available": bool(model_registry_lineage.get("available")),
        "model_registry_lineage_summary": model_lineage_summary,
        "paper_observation_backlog_count": int(
            paper_observation_backlog.get("paper_observation_backlog_count") or 0
        ),
        "paper_observation_active_count": int(
            paper_observation_backlog.get("paper_observation_active_count") or 0
        ),
        "paper_observation_backlog_status": (
            paper_observation_backlog.get("paper_observation_backlog_status") or "empty"
        ),
        "paper_observation_last_recognized_at": paper_observation_backlog.get(
            "paper_observation_last_recognized_at"
        ),
        "paper_observation_latest_bound_at": paper_observation_backlog.get(
            "paper_observation_latest_bound_at"
        ),
        "incubation_factory_health": incubation_factory_health,
        "paper_observation_intake_stale": bool(
            int(paper_observation_backlog.get("paper_observation_backlog_count") or 0) > 0
            and incubation_factory_health
            and not bool(incubation_factory_health.get("healthy", True))
        ),
        "governed_risk_counts": {
            "lookahead": dict(governed_registry_summary.get("lookahead_risk_counts") or {}),
            "multiple_testing": dict(
                governed_registry_summary.get("multiple_testing_risk_counts") or {}
            ),
            "overall": dict(governed_registry_summary.get("overall_risk_counts") or {}),
        },
        "stock_family_allocation_count": int(stock_family_allocation_summary.get("count") or 0),
        "stock_family_allocation_family_counts": dict(
            stock_family_allocation_summary.get("family_counts") or {}
        ),
        "stock_family_allocation_entropy": stock_family_allocation_summary.get(
            "allocation_entropy"
        ),
        "stock_family_allocation_avg_priority": stock_family_allocation_summary.get(
            "avg_priority"
        ),
        "stock_family_allocation_source_mode": stock_family_allocation_summary.get(
            "source_mode"
        ),
        "lifecycle_feedback_input_contract_version": lifecycle_feedback_input.get(
            "contract_version"
        ),
        "lifecycle_feedback_input_available": bool(lifecycle_feedback_input.get("available")),
        "budget_feedback_available": bool(lifecycle_feedback_input.get("available")),
        "budget_feedback_family_count": int(budget_feedback_summary.get("family_count") or 0),
        "budget_feedback_strategy_count": int(
            budget_feedback_summary.get("strategy_count") or 0
        ),
        "budget_feedback_target_pool_scope_count": int(
            budget_feedback_summary.get("target_pool_scope_count") or 0
        ),
        "budget_feedback_holding_bucket_scope_count": int(
            budget_feedback_summary.get("holding_bucket_scope_count") or 0
        ),
        "budget_feedback_generator_mode_scope_count": int(
            budget_feedback_summary.get("generator_mode_scope_count") or 0
        ),
        "budget_feedback_runtime_alert_count": int(
            budget_feedback_summary.get("runtime_alert_count") or 0
        ),
        "budget_feedback_runtime_risk_event_count": int(
            budget_feedback_summary.get("runtime_risk_event_count") or 0
        ),
        "budget_feedback_promotion_review_count": int(
            budget_feedback_summary.get("promotion_review_count") or 0
        ),
        "budget_feedback_promotion_review_status_counts": dict(
            budget_feedback_summary.get("promotion_review_status_counts") or {}
        ),
        "budget_feedback_paper_hit_ratio": _safe_float(
            budget_feedback_summary.get("paper_hit_ratio"),
            0.5,
        ),
        "budget_feedback_paper_skill_lcb": _safe_float(
            budget_feedback_summary.get("paper_skill_lcb")
        ),
        "budget_feedback_paper_recent_skill_lcb": _safe_float(
            budget_feedback_summary.get("paper_recent_skill_lcb")
        ),
        "budget_feedback_paper_stability_gap": _safe_float(
            budget_feedback_summary.get("paper_stability_gap")
        ),
        "budget_feedback_paper_coverage_ratio": _safe_float(
            budget_feedback_summary.get("paper_coverage_ratio"),
            1.0,
        ),
        "budget_feedback_execution_conversion_efficiency": (
            _safe_float(budget_feedback_summary.get("execution_conversion_efficiency"))
            if budget_feedback_summary.get("execution_conversion_efficiency") is not None
            else None
        ),
        "budget_feedback_execution_conversion_efficiency_observed_count": int(
            budget_feedback_summary.get("execution_conversion_efficiency_observed_count") or 0
        ),
        "budget_feedback_legacy_control_mode_counts": dict(
            budget_feedback_summary.get("legacy_control_mode_counts") or {}
        ),
        "budget_feedback_skill_control_mode_counts": dict(
            budget_feedback_summary.get("skill_control_mode_counts") or {}
        ),
        "budget_feedback_action_counts": dict(
            budget_feedback_summary.get("budget_action_counts") or {}
        ),
        "budget_feedback_dual_axis_action_family_count": int(
            budget_feedback_summary.get("dual_axis_action_family_count") or 0
        ),
        "budget_feedback_execution_optimization_queue_count": int(
            budget_feedback_summary.get("execution_optimization_queue_count") or 0
        ),
        "budget_feedback_small_budget_observe_count": int(
            budget_feedback_summary.get("small_budget_observe_count") or 0
        ),
        "budget_feedback_prioritize_scale_count": int(
            budget_feedback_summary.get("prioritize_scale_count") or 0
        ),
        "budget_feedback_cool_or_freeze_count": int(
            budget_feedback_summary.get("cool_or_freeze_count") or 0
        ),
        "budget_feedback_retain_family_reduce_budget_count": int(
            budget_feedback_summary.get("retain_family_reduce_budget_count") or 0
        ),
        "budget_feedback_signal_count_total": int(
            budget_feedback_summary.get("signal_count_total") or 0
        ),
        "budget_feedback_zero_signal_strategy_count": int(
            budget_feedback_summary.get("zero_signal_strategy_count") or 0
        ),
        "budget_feedback_zero_signal_ratio": _safe_float(
            budget_feedback_summary.get("zero_signal_ratio")
        ),
        "budget_feedback_low_signal_strategy_count": int(
            budget_feedback_summary.get("low_signal_strategy_count") or 0
        ),
        "budget_feedback_low_signal_ratio": _safe_float(
            budget_feedback_summary.get("low_signal_ratio")
        ),
        "budget_feedback_observed_forward_window_count": int(
            budget_feedback_summary.get("observed_forward_window_count") or 0
        ),
        "budget_feedback_missing_forward_window_count": int(
            budget_feedback_summary.get("missing_forward_window_count") or 0
        ),
        "budget_feedback_expected_forward_window_count": int(
            budget_feedback_summary.get("expected_forward_window_count") or 0
        ),
        "budget_feedback_forward_window_coverage_ratio": _safe_float(
            budget_feedback_summary.get("forward_window_coverage_ratio"),
            1.0,
        ),
        "budget_feedback_promotion_ready_count": int(
            budget_feedback_summary.get("promotion_ready_count") or 0
        ),
        "budget_feedback_promotion_ready_ratio": _safe_float(
            budget_feedback_summary.get("promotion_ready_ratio"),
            1.0,
        ),
        "budget_feedback_promotion_review_coverage_ratio": _safe_float(
            budget_feedback_summary.get("promotion_review_coverage_ratio"),
            1.0,
        ),
        "budget_feedback_evidence_debt_strategy_count": int(
            budget_feedback_summary.get("evidence_debt_strategy_count") or 0
        ),
        "budget_feedback_evidence_debt_ratio": _safe_float(
            budget_feedback_summary.get("evidence_debt_ratio")
        ),
        "search_route_action_counts": search_route_action_counts,
        "degraded": degraded,
        "freshness_days": freshness_days,
        "latest_factor_date": latest_factor_date,
        "history_stale": bool(history_stale),
        "stale": stale,
        "quality_flags": quality_flags,
        "decay_factor_names": decay_factors,
        "stability_tags": stability_tags,
    }


__all__ = ["build_factor_research_summary"]
