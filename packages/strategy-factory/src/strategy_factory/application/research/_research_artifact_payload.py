"""Final factor research artifact assembly extracted from the builder."""

from __future__ import annotations

from datetime import date
from typing import Any

from .._budget_feedback import normalize_text
from ._artifact_summary import build_factor_research_summary
from ._research_build_steps import build_candidate_lineage_views


def build_factor_research_artifact_payload(
    builder_cls,
    *,
    snapshot: dict[str, Any],
    snapshot_date: date | None,
    latest_factor_date: date | None,
    history_meta: dict[str, dict[str, Any]],
    factor_ic_source: dict[str, Any],
    factor_context: dict[str, Any],
    runtime_context: dict[str, Any],
    stock_family_allocation: dict[str, Any],
    stock_family_allocation_summary: dict[str, Any],
    lightweight_mock_fallback: bool,
) -> dict[str, Any]:
    ranked_factors = list(factor_context.get("ranked_factors") or [])
    positive_rising_factors = list(factor_context.get("positive_rising_factors") or [])
    active_factors = list(factor_context.get("active_factors") or [])
    preferred_strategy_types = list(factor_context.get("preferred_strategy_types") or [])
    top_factor_names = list(factor_context.get("top_factor_names") or [])

    governed_pool = dict(runtime_context.get("governed_pool") or {})
    active_candidate_pool = dict(runtime_context.get("active_candidate_pool") or {})
    governed_registry_summary = dict(runtime_context.get("governed_registry_summary") or {})
    governed_candidate_pool_mode = runtime_context.get("governed_candidate_pool_mode")
    governed_candidate_pool_provisional = bool(
        runtime_context.get("governed_candidate_pool_provisional")
    )
    governed_candidate_pool_strict_count = int(
        runtime_context.get("governed_candidate_pool_strict_count") or 0
    )
    governed_candidate_pool_provisional_count = int(
        runtime_context.get("governed_candidate_pool_provisional_count") or 0
    )
    governed_candidate_pool_provisional_spillover_count = int(
        runtime_context.get("governed_candidate_pool_provisional_spillover_count") or 0
    )
    governed_candidate_pool_provisional_spillover_policy = dict(
        runtime_context.get("governed_candidate_pool_provisional_spillover_policy") or {}
    )
    governed_candidate_pool_provisional_pending_count = int(
        runtime_context.get("governed_candidate_pool_provisional_pending_count") or 0
    )
    governed_candidate_pool_strict_shortfall_count = int(
        runtime_context.get("governed_candidate_pool_strict_shortfall_count") or 0
    )
    governed_candidate_pool_provisional_spillover_policy_status = runtime_context.get(
        "governed_candidate_pool_provisional_spillover_policy_status"
    )
    governed_top_candidates = [
        dict(item or {})
        for item in list(runtime_context.get("governed_top_candidates") or [])
        if isinstance(item, dict)
    ]
    governed_excluded_candidates = [
        dict(item or {})
        for item in list(runtime_context.get("governed_excluded_candidates") or [])
        if isinstance(item, dict)
    ]
    governed_family_summary = [
        dict(item or {})
        for item in list(runtime_context.get("governed_family_summary") or [])
        if isinstance(item, dict)
    ]
    governed_regime_summary = [
        dict(item or {})
        for item in list(runtime_context.get("governed_regime_summary") or [])
        if isinstance(item, dict)
    ]
    model_registry_lineage = dict(runtime_context.get("model_registry_lineage") or {})
    model_lineage_summary = dict(runtime_context.get("model_lineage_summary") or {})
    model_lineage_by_validation_id = dict(
        runtime_context.get("model_lineage_by_validation_id") or {}
    )
    lifecycle_feedback_input = dict(runtime_context.get("lifecycle_feedback_input") or {})
    budget_feedback_root = dict(runtime_context.get("budget_feedback_root") or {})
    budget_feedback_summary = dict(runtime_context.get("budget_feedback_summary") or {})
    paper_observation_backlog = dict(runtime_context.get("paper_observation_backlog") or {})
    incubation_factory_health = dict(runtime_context.get("incubation_factory_health") or {})
    governed_source_candidate_count = int(
        runtime_context.get("governed_source_candidate_count") or 0
    )
    governed_active_registry_candidate_count = int(
        runtime_context.get("governed_active_registry_candidate_count") or 0
    )
    governed_blocked_candidate_count = int(
        runtime_context.get("governed_blocked_candidate_count") or 0
    )
    governed_pending_candidate_count = int(
        runtime_context.get("governed_pending_candidate_count") or 0
    )
    governed_ineligible_candidate_count = int(
        runtime_context.get("governed_ineligible_candidate_count") or 0
    )
    governed_exclusion_reason_counts = {
        str(key): int(value or 0)
        for key, value in dict(runtime_context.get("governed_exclusion_reason_counts") or {}).items()
    }
    governed_blocking_reason_counts = {
        str(key): int(value or 0)
        for key, value in dict(runtime_context.get("governed_blocking_reason_counts") or {}).items()
    }
    governed_pending_reason_counts = {
        str(key): int(value or 0)
        for key, value in dict(runtime_context.get("governed_pending_reason_counts") or {}).items()
    }
    governed_ineligible_reason_counts = {
        str(key): int(value or 0)
        for key, value in dict(runtime_context.get("governed_ineligible_reason_counts") or {}).items()
    }
    governed_latest_candidate_at = runtime_context.get("governed_latest_candidate_at")
    governed_freshness_days = runtime_context.get("governed_freshness_days")
    governed_blocked_ratio = builder_cls._safe_float(
        runtime_context.get("governed_blocked_ratio")
    )
    governed_pending_ratio = builder_cls._safe_float(
        runtime_context.get("governed_pending_ratio")
    )
    governed_ineligible_ratio = builder_cls._safe_float(
        runtime_context.get("governed_ineligible_ratio")
    )
    scheduler_status = dict(runtime_context.get("scheduler_status") or {})
    scheduler_quality_flags = list(runtime_context.get("scheduler_quality_flags") or [])
    scheduler_recent_success = bool(runtime_context.get("scheduler_recent_success"))
    scheduler_llm_validation_status = runtime_context.get("scheduler_llm_validation_status")
    scheduler_llm_provider = dict(runtime_context.get("scheduler_llm_provider") or {})
    scheduler_llm_provider_health_status = runtime_context.get(
        "scheduler_llm_provider_health_status"
    )
    factory_pool_factors = [
        dict(item or {})
        for item in list(runtime_context.get("factory_pool_factors") or [])
        if isinstance(item, dict)
    ]
    factory_pool_payload = {
        "available": bool(factory_pool_factors),
        "count": len(factory_pool_factors),
        "factors": factory_pool_factors,
        "raw_count": int(runtime_context.get("factory_pool_factors_raw_count") or len(factory_pool_factors)),
        "filtered_count": int(runtime_context.get("factory_pool_factors_filtered_count") or 0),
    }

    family_preference_order = builder_cls._build_family_preference_order(
        snapshot,
        preferred_strategy_types=preferred_strategy_types,
        allocation_family_counts=dict(stock_family_allocation_summary.get("family_counts") or {}),
    )
    family_preference_source_mode = builder_cls._family_preference_source_mode(
        family_preference_order=family_preference_order,
        preferred_strategy_types=preferred_strategy_types,
        allocation_family_counts=dict(stock_family_allocation_summary.get("family_counts") or {}),
    )
    lineage_context = build_candidate_lineage_views(
        builder_cls,
        governed_top_candidates=governed_top_candidates,
        governed_excluded_candidates=governed_excluded_candidates,
        model_registry_lineage=model_registry_lineage,
        model_lineage_by_validation_id=model_lineage_by_validation_id,
    )
    top_candidate_names = lineage_context["top_candidate_names"]
    top_candidate_lineage = lineage_context["top_candidate_lineage"]
    blocked_candidate_lineage = lineage_context["blocked_candidate_lineage"]

    rationale: list[str] = []
    if active_factors:
        rationale.append(f"活跃因子: {', '.join(active_factors)}")
    if preferred_strategy_types:
        rationale.append(f"优先策略类型: {', '.join(preferred_strategy_types[:4])}")
    if governed_top_candidates:
        if governed_candidate_pool_provisional:
            rationale.append(
                "治理候选池当前以 provisional validated/watch 候选供给，"
                f"Top 候选: {', '.join(top_candidate_names[:3])}"
            )
        else:
            rationale.append(f"治理后候选池已接入，Top 候选: {', '.join(top_candidate_names[:3])}")
    elif governed_blocked_candidate_count:
        rationale.append(f"治理候选池存在 {governed_blocked_candidate_count} 个高风险候选，当前未纳入活跃池。")
    elif governed_pool.get("reason"):
        rationale.append(f"治理后候选池未生效，已回退到种子因子: {governed_pool.get('reason')}")
    if governed_latest_candidate_at:
        rationale.append(f"治理候选池最近验证时间: {governed_latest_candidate_at}")
    if model_lineage_summary:
        rationale.append(
            "候选已接入 model/retrain 血缘: "
            f"champion={int(model_lineage_summary.get('champion_count') or 0)} "
            f"challenger={int(model_lineage_summary.get('challenger_count') or 0)} "
            f"retrain_plan={int(model_lineage_summary.get('retrain_plan_count') or 0)}"
        )
    if stock_family_allocation:
        rationale.append(
            "逐股 family 分配已生成: "
            f"覆盖 {int(stock_family_allocation_summary.get('count') or 0)} 只股票，"
            f"allocation_entropy={stock_family_allocation_summary.get('allocation_entropy')}"
        )
    if budget_feedback_root:
        rationale.append(
            "paper/runtime feedback 已回流 allocation/budget: "
            f"families={int(budget_feedback_summary.get('family_count') or 0)} "
            f"strategies={int(budget_feedback_summary.get('strategy_count') or 0)}"
        )
    if int(budget_feedback_summary.get("promotion_review_count") or 0) > 0:
        rationale.append(
            "生命周期反馈已纳入 promotion review: "
            f"count={int(budget_feedback_summary.get('promotion_review_count') or 0)} "
            f"status={dict(budget_feedback_summary.get('promotion_review_status_counts') or {})}"
        )
    if builder_cls._safe_float(budget_feedback_summary.get("zero_signal_ratio")) >= 0.40:
        rationale.append(
            "incubating 零信号 backlog 偏高: "
            f"{round(builder_cls._safe_float(budget_feedback_summary.get('zero_signal_ratio')) * 100, 1)}%"
        )
    if builder_cls._safe_float(budget_feedback_summary.get("forward_window_coverage_ratio"), 1.0) <= 0.50:
        rationale.append(
            "前向观察窗口覆盖不足: "
            f"{round(builder_cls._safe_float(budget_feedback_summary.get('forward_window_coverage_ratio'), 1.0) * 100, 1)}%"
        )
    if builder_cls._safe_float(budget_feedback_summary.get("evidence_debt_ratio")) >= 0.45:
        rationale.append(
            "生命周期证据债务偏高，下一轮应优先补 signals / forward windows / promotion review。"
        )
    if governed_blocked_ratio >= 0.40:
        rationale.append(f"治理候选池 blocked 比例偏高: {round(governed_blocked_ratio * 100, 1)}%")
    if governed_pending_ratio >= 0.50:
        rationale.append(f"治理候选池待晋级候选占比偏高: {round(governed_pending_ratio * 100, 1)}%")
    if governed_ineligible_candidate_count:
        rationale.append(
            "治理候选池存在应清退候选: "
            f"count={governed_ineligible_candidate_count} "
            f"reasons={governed_ineligible_reason_counts or {'ineligible': governed_ineligible_candidate_count}}"
        )
    if governed_candidate_pool_provisional_spillover_policy_status in {
        "spillover_applied",
        "spillover_capacity_exhausted",
        "spillover_disabled",
        "awaiting_governed_promotion",
    }:
        rationale.append(
            "治理候选池 spillover 策略: "
            f"status={governed_candidate_pool_provisional_spillover_policy_status} "
            f"strict_shortfall={governed_candidate_pool_strict_shortfall_count} "
            f"spillover={governed_candidate_pool_provisional_spillover_count} "
            f"pending={governed_candidate_pool_provisional_pending_count}"
        )
    governed_pool_observable = bool(
        governed_pool.get("available")
        or governed_source_candidate_count > 0
        or int((governed_pool.get("active_pool") or {}).get("count") or 0) > 0
    )
    governed_pool_missing_after_scheduler_success = bool(
        governed_pool_observable
        and scheduler_recent_success
        and not governed_top_candidates
    )
    if governed_pool_missing_after_scheduler_success:
        rationale.append("调度器近期已成功运行，但治理活跃池仍为空，建议核查验证与晋级门槛。")
    if scheduler_llm_provider_health_status in {"degraded", "closed", "misconfigured", "error"}:
        rationale.append(
            "factor llm provider 生命周期异常: "
            f"health={scheduler_llm_provider_health_status} "
            f"error={scheduler_llm_provider.get('last_error_type') or 'unknown'}"
        )

    freshness_days = builder_cls._days_since(latest_factor_date, reference_date=snapshot_date)
    history_stale = bool(
        ("stale" in scheduler_quality_flags)
        or (freshness_days is not None and freshness_days > builder_cls.STALE_AFTER_DAYS)
    )
    governed_pool_fresh = bool(
        governed_top_candidates
        and governed_freshness_days is not None
        and builder_cls._safe_float(governed_freshness_days, default=999.0)
        <= builder_cls.STALE_AFTER_DAYS
    )
    stale = bool(history_stale and not governed_pool_fresh)
    decay_factors = [
        str(item.get("factor_name") or "")
        for item in ranked_factors
        if bool(item.get("decay_flag"))
    ]
    stability_tags = {
        str(item.get("factor_name") or ""): str(item.get("stability_tag") or "insufficient_history")
        for item in ranked_factors
        if str(item.get("factor_name") or "")
    }
    quality_flags: list[str] = []
    if history_stale:
        quality_flags.append("factor_history_stale")
    if stale:
        quality_flags.append("stale")
    if decay_factors:
        quality_flags.append("decay_detected")
    if governed_top_candidates:
        quality_flags.append("governed_candidate_pool_active")
    if governed_candidate_pool_provisional:
        quality_flags.append("governed_candidate_pool_provisional")
    if model_registry_lineage.get("available"):
        quality_flags.append("model_registry_lineage_available")
    if governed_blocked_candidate_count:
        quality_flags.append("governed_candidate_pool_blocked_candidates")
    if governed_blocked_ratio >= 0.75:
        quality_flags.append("governed_candidate_pool_blocked_ratio_high")
    elif governed_blocked_ratio >= 0.40:
        quality_flags.append("governed_candidate_pool_blocked_ratio_elevated")
    if governed_pending_ratio >= 0.75:
        quality_flags.append("governed_candidate_pool_promotion_backlog_high")
    elif governed_pending_ratio >= 0.40:
        quality_flags.append("governed_candidate_pool_promotion_backlog_elevated")
    if governed_freshness_days is None and governed_source_candidate_count > 0:
        quality_flags.append("governed_candidate_pool_freshness_unknown")
    elif (
        governed_freshness_days is not None
        and governed_freshness_days > builder_cls.STALE_AFTER_DAYS
    ):
        quality_flags.append("governed_candidate_pool_stale")
    if scheduler_recent_success and not governed_top_candidates:
        quality_flags.append("scheduler_recent_success_without_governed_pool")
    if governed_pool_missing_after_scheduler_success:
        quality_flags.append("governed_pool_missing_after_scheduler_success")
    factor_ic_status = str(factor_ic_source.get("status") or "")
    if factor_ic_status and factor_ic_status != "success":
        quality_flags.append(f"factor_ic_{factor_ic_status}")
    if scheduler_llm_provider_health_status in {"degraded", "closed", "misconfigured", "error"}:
        quality_flags.append(f"factor_llm_provider_{scheduler_llm_provider_health_status}")
    if budget_feedback_root:
        quality_flags.append("budget_feedback_available")
    if builder_cls._safe_float(budget_feedback_summary.get("zero_signal_ratio")) >= 0.75:
        quality_flags.append("budget_feedback_zero_signal_backlog_high")
    elif builder_cls._safe_float(budget_feedback_summary.get("zero_signal_ratio")) >= 0.40:
        quality_flags.append("budget_feedback_zero_signal_backlog_elevated")
    if builder_cls._safe_float(budget_feedback_summary.get("forward_window_coverage_ratio"), 1.0) <= 0.25:
        quality_flags.append("budget_feedback_forward_window_coverage_low")
    elif builder_cls._safe_float(budget_feedback_summary.get("forward_window_coverage_ratio"), 1.0) <= 0.50:
        quality_flags.append("budget_feedback_forward_window_coverage_elevated")
    if builder_cls._safe_float(budget_feedback_summary.get("evidence_debt_ratio")) >= 0.75:
        quality_flags.append("budget_feedback_evidence_debt_high")
    elif builder_cls._safe_float(budget_feedback_summary.get("evidence_debt_ratio")) >= 0.45:
        quality_flags.append("budget_feedback_evidence_debt_elevated")
    if not ranked_factors:
        quality_flags.append("empty")
    quality_flags.extend([flag for flag in scheduler_quality_flags if flag not in quality_flags])

    # 优化 9：Shannon 熵告警阈值
    import math as _math
    _allocation_entropy = builder_cls._safe_float(
        stock_family_allocation_summary.get("allocation_entropy"), 0.0
    )
    _family_count = max(len(family_preference_order), 1)
    _max_possible_entropy = _math.log2(_family_count) if _family_count > 1 else 1.0
    _normalized_entropy = round(
        _allocation_entropy / max(_max_possible_entropy, 0.001), 4
    ) if _max_possible_entropy > 0 else 0.0
    if _normalized_entropy < 0.4 and stock_family_allocation:
        quality_flags.append("allocation_concentration_critical")
    elif _normalized_entropy < 0.6 and stock_family_allocation:
        quality_flags.append("allocation_concentration_warning")

    if not rationale:
        rationale.append("未识别到显著活跃因子，后续阶段回退到原始快照因子摘要逻辑。")
    if history_stale and not stale:
        rationale.append(
            "因子 IC 历史存在 freshness 风险，但治理候选池仍在新鲜窗口内，本轮按治理候选池继续供给。"
        )
    if stale:
        rationale.append("因子研究数据存在 freshness 风险，后续阶段应降低置信度或触发补算。")
    if decay_factors:
        rationale.append(f"检测到衰减因子: {', '.join(decay_factors[:3])}")

    degraded = (not bool(ranked_factors) and not bool(governed_top_candidates)) or (
        stale and not bool(governed_top_candidates)
    )
    (
        family_reward_table,
        family_debt_table,
        search_route_actions,
        search_route_family_plans,
    ) = builder_cls._build_search_route_feedback_snapshot(
        family_preference_order=family_preference_order,
        budget_feedback_root=budget_feedback_root,
    )
    effective_family_preference_order = builder_cls._rewrite_family_preference_order_by_feedback(
        family_preference_order,
        family_plans=search_route_family_plans,
    )
    feedback_routed = effective_family_preference_order != family_preference_order
    family_preference_order = effective_family_preference_order
    family_preference_source_mode = builder_cls._family_preference_source_mode(
        family_preference_order=family_preference_order,
        preferred_strategy_types=preferred_strategy_types,
        allocation_family_counts=dict(stock_family_allocation_summary.get("family_counts") or {}),
        feedback_routed=feedback_routed,
    )
    search_route_action_counts: dict[str, int] = {}
    for action in search_route_actions:
        action_name = normalize_text(action.get("action")) or "unknown"
        search_route_action_counts[action_name] = search_route_action_counts.get(action_name, 0) + 1

    return {
        "active_factors": active_factors,
        "ranked_factors": ranked_factors,
        "positive_rising_factors": positive_rising_factors,
        "preferred_strategy_types": preferred_strategy_types,
        "factory_pool_payload": factory_pool_payload,
        "factory_pool_factors": factory_pool_factors,
        "governed_candidates": governed_top_candidates,
        "blocked_candidates": governed_excluded_candidates,
        "top_candidate_lineage": top_candidate_lineage,
        "blocked_candidate_lineage": blocked_candidate_lineage,
        "model_registry_lineage": model_registry_lineage,
        "lifecycle_feedback_input": lifecycle_feedback_input,
        "budget_feedback": budget_feedback_root,
        "active_candidate_pool": active_candidate_pool,
        "stock_family_allocation": stock_family_allocation,
        "family_preference_order": family_preference_order,
        "normalized_allocation_entropy": _normalized_entropy,
        "allocation_concentration_level": (
            "critical" if _normalized_entropy < 0.4 and stock_family_allocation
            else "warning" if _normalized_entropy < 0.6 and stock_family_allocation
            else "healthy"
        ),
        "family_reward_table": family_reward_table,
        "family_debt_table": family_debt_table,
        "search_route_actions": search_route_actions,
        "active_family_summary": governed_family_summary,
        "active_regime_summary": governed_regime_summary,
        "research_rationale": rationale,
        "source_chain": [
            "snapshot.factor_ic",
            "snapshot.factor_ic_trend",
            f"db.factor_ic_history(limit={builder_cls.HISTORY_LIMIT})",
            "quant_manager.factor_candidate_registry(active_pool)",
            "quant_manager.model_registry(lineage)",
            "factor_scheduler.status",
            "artifact_v2",
            *(["lightweight_mock_fallback"] if lightweight_mock_fallback else []),
        ],
        "lightweight_mock_fallback": lightweight_mock_fallback,
        "degraded": degraded,
        "latest_factor_date": latest_factor_date.isoformat() if latest_factor_date else None,
        "freshness_days": freshness_days,
        "history_stale": history_stale,
        "governed_pool_fresh": governed_pool_fresh,
        "stale": stale,
        "quality_flags": quality_flags,
        "factor_history": history_meta,
        "scheduler_status": {
            "running": bool(scheduler_status.get("running")),
            "last_run": scheduler_status.get("last_run"),
            "freshness_sec": scheduler_status.get("freshness_sec"),
            "quality_flags": scheduler_quality_flags,
            "llm_validation_status": scheduler_llm_validation_status,
            "recent_success": scheduler_recent_success,
            "llm_provider": scheduler_llm_provider,
        },
        "summary": build_factor_research_summary(
            active_factors=active_factors,
            active_candidate_pool=active_candidate_pool,
            governed_source_candidate_count=governed_source_candidate_count,
            governed_active_registry_candidate_count=governed_active_registry_candidate_count,
            governed_blocked_candidate_count=governed_blocked_candidate_count,
            governed_blocked_ratio=governed_blocked_ratio,
            governed_pending_candidate_count=governed_pending_candidate_count,
            governed_pending_ratio=governed_pending_ratio,
            governed_ineligible_candidate_count=governed_ineligible_candidate_count,
            governed_ineligible_ratio=governed_ineligible_ratio,
            governed_latest_candidate_at=governed_latest_candidate_at,
            governed_freshness_days=governed_freshness_days,
            ranked_factors=ranked_factors,
            top_factor_names=top_factor_names,
            top_candidate_names=top_candidate_names,
            governed_family_summary=governed_family_summary,
            governed_regime_summary=governed_regime_summary,
            preferred_strategy_types=preferred_strategy_types,
            family_preference_order=family_preference_order,
            family_preference_source_mode=family_preference_source_mode,
            governed_top_candidates=governed_top_candidates,
            governed_pool_missing_after_scheduler_success=governed_pool_missing_after_scheduler_success,
            governed_candidate_pool_mode=governed_candidate_pool_mode,
            governed_candidate_pool_provisional=governed_candidate_pool_provisional,
            governed_candidate_pool_strict_count=governed_candidate_pool_strict_count,
            governed_candidate_pool_provisional_count=governed_candidate_pool_provisional_count,
            governed_candidate_pool_provisional_spillover_count=governed_candidate_pool_provisional_spillover_count,
            governed_candidate_pool_provisional_spillover_enabled=bool(
                active_candidate_pool.get("provisional_spillover_enabled")
            ),
            governed_candidate_pool_provisional_spillover_policy=governed_candidate_pool_provisional_spillover_policy,
            governed_candidate_pool_provisional_spillover_policy_status=governed_candidate_pool_provisional_spillover_policy_status,
            governed_candidate_pool_provisional_pending_count=governed_candidate_pool_provisional_pending_count,
            governed_candidate_pool_strict_shortfall_count=governed_candidate_pool_strict_shortfall_count,
            scheduler_status=scheduler_status,
            scheduler_recent_success=scheduler_recent_success,
            scheduler_llm_validation_status=scheduler_llm_validation_status,
            scheduler_llm_provider=scheduler_llm_provider,
            scheduler_llm_provider_health_status=scheduler_llm_provider_health_status,
            lightweight_mock_fallback=lightweight_mock_fallback,
            governed_exclusion_reason_counts=governed_exclusion_reason_counts,
            governed_blocking_reason_counts=governed_blocking_reason_counts,
            governed_pending_reason_counts=governed_pending_reason_counts,
            governed_ineligible_reason_counts=governed_ineligible_reason_counts,
            governed_registry_summary=governed_registry_summary,
            top_candidate_lineage=top_candidate_lineage,
            model_registry_lineage=model_registry_lineage,
            model_lineage_summary=model_lineage_summary,
            stock_family_allocation_summary=stock_family_allocation_summary,
            lifecycle_feedback_input=lifecycle_feedback_input,
            budget_feedback_summary=budget_feedback_summary,
            paper_observation_backlog=paper_observation_backlog,
            incubation_factory_health=incubation_factory_health,
            search_route_action_counts=search_route_action_counts,
            degraded=degraded,
            freshness_days=freshness_days,
            latest_factor_date=latest_factor_date.isoformat() if latest_factor_date else None,
            history_stale=history_stale,
            stale=stale,
            quality_flags=quality_flags,
            decay_factors=decay_factors,
            stability_tags=stability_tags,
        ),
    }


__all__ = ["build_factor_research_artifact_payload"]
