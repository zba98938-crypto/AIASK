"""Feedback/search-route orchestration helpers for factor research."""

from __future__ import annotations

from typing import Any, List, Optional

from ...domain.constants import (
    STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK,
    STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
)
from .._budget_feedback import (
    extract_feedback_root,
    extract_generator_mode,
    extract_holding_bucket,
    extract_target_pool_id,
    normalize_feedback_input_contract,
    normalize_text,
    resolve_feedback_metrics,
)
from .._stock_universe_loader import load_stock_universe_rows
from ..runtime import _call_optional_async
from ..sector_taxonomy import normalize_sector_labels


def rewrite_family_preference_order_by_feedback(
    builder_cls,
    family_preference_order: List[str],
    *,
    family_plans: List[dict[str, Any]],
) -> List[str]:
    original_order = [
        normalize_text(item)
        for item in list(family_preference_order or [])
        if normalize_text(item)
    ]
    if not family_plans:
        return original_order
    original_rank = {family: index for index, family in enumerate(original_order)}
    action_rank = {
        "family_explore": 4,
        "family_cooldown": 3,
        "family_freeze": 2,
        "family_retire": 1,
    }
    planned_rank = {
        normalize_text(plan.get("family")): plan
        for plan in family_plans
        if normalize_text(plan.get("family"))
    }
    ranked = sorted(
        planned_rank,
        key=lambda family: (
            -action_rank.get(
                builder_cls._resolve_search_route_action(
                    planned_rank[family],
                    dict(planned_rank[family].get("feedback_metrics") or {}),
                ),
                0,
            ),
            -builder_cls._feedback_ab_quality_score(
                dict(planned_rank[family].get("feedback_metrics") or {})
            ),
            -builder_cls._safe_float(
                dict(planned_rank[family].get("feedback_metrics") or {}).get(
                    "raw_validation_b_rate"
                )
            ),
            -builder_cls._safe_float(
                dict(planned_rank[family].get("feedback_metrics") or {}).get(
                    "raw_validation_a_rate"
                )
            ),
            -builder_cls._safe_float(
                dict(planned_rank[family].get("feedback_metrics") or {}).get(
                    "strict_incubation_ready_rate"
                )
            ),
            -builder_cls._safe_float(
                dict(planned_rank[family].get("feedback_metrics") or {}).get(
                    "raw_validation_total_score_mean"
                )
            ),
            -int(
                bool(
                    dict(planned_rank[family].get("feedback_metrics") or {}).get(
                        "family_feedback_available"
                    )
                )
            ),
            -builder_cls._safe_float(planned_rank[family].get("budget_weight")),
            -builder_cls._safe_float(
                dict(planned_rank[family].get("feedback_metrics") or {}).get(
                    "promotion_ready_ratio"
                ),
                1.0,
            ),
            original_rank.get(family, len(original_rank) + 100),
            family,
        ),
    )
    for family in original_order:
        if family not in ranked:
            ranked.append(family)
    return ranked


def build_search_route_feedback_snapshot(
    builder_cls,
    *,
    family_preference_order: List[str],
    budget_feedback_root: Any = None,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    List[dict[str, Any]],
    List[dict[str, Any]],
]:
    selected: List[str] = []
    for item in list(family_preference_order or []):
        token = normalize_text(item)
        if token and token not in selected:
            selected.append(token)
    for item in list(dict(budget_feedback_root or {}).keys()):
        token = normalize_text(item)
        if token and token not in selected:
            selected.append(token)
    if not selected:
        return {}, {}, [], []
    family_plans = builder_cls._build_family_plans(
        selected,
        priority=1.0,
        budget_feedback_root=budget_feedback_root,
    )
    family_reward_table: dict[str, dict[str, Any]] = {}
    family_debt_table: dict[str, dict[str, Any]] = {}
    search_route_actions: List[dict[str, Any]] = []
    for plan in family_plans:
        family = normalize_text(plan.get("family")) or "unknown"
        feedback_metrics = dict(plan.get("feedback_metrics") or {})
        family_bucket = dict((dict(budget_feedback_root or {}).get(family) or {}))
        family_action = builder_cls._resolve_search_route_action(plan, feedback_metrics)
        family_quality_score = builder_cls._feedback_ab_quality_score(feedback_metrics)
        target_pool_routes: dict[str, Any] = {}
        holding_bucket_routes: dict[str, Any] = {}
        generator_mode_routes: dict[str, Any] = {}
        family_reward_table[family] = {
            "budget_weight": round(builder_cls._safe_float(plan.get("budget_weight")), 4),
            "feedback_budget_multiplier": round(
                builder_cls._safe_float(plan.get("feedback_budget_multiplier"), 1.0),
                4,
            ),
            "feedback_priority_adjustment": round(
                builder_cls._safe_float(plan.get("feedback_priority_adjustment")),
                4,
            ),
            "promotion_ready_ratio": round(
                builder_cls._safe_float(feedback_metrics.get("promotion_ready_ratio"), 1.0),
                4,
            ),
            "forward_window_coverage_ratio": round(
                builder_cls._safe_float(feedback_metrics.get("forward_window_coverage_ratio"), 1.0),
                4,
            ),
            "paper_hit_ratio": round(
                builder_cls._safe_float(feedback_metrics.get("paper_hit_ratio"), 0.5),
                4,
            ),
            "paper_skill_lcb": round(
                builder_cls._safe_float(feedback_metrics.get("paper_skill_lcb")),
                4,
            ),
            "paper_recent_skill_lcb": round(
                builder_cls._safe_float(feedback_metrics.get("paper_recent_skill_lcb")),
                4,
            ),
            "paper_stability_gap": round(
                builder_cls._safe_float(feedback_metrics.get("paper_stability_gap")),
                4,
            ),
            "paper_coverage_ratio": round(
                builder_cls._safe_float(feedback_metrics.get("paper_coverage_ratio"), 1.0),
                4,
            ),
            "legacy_control_mode": feedback_metrics.get("legacy_control_mode")
            or plan.get("feedback_control_mode"),
            "skill_control_mode": feedback_metrics.get("skill_control_mode"),
            "legacy_budget_multiplier": round(
                builder_cls._safe_float(
                    feedback_metrics.get("legacy_budget_multiplier"),
                    plan.get("feedback_budget_multiplier"),
                ),
                4,
            ),
            "skill_budget_multiplier": round(
                builder_cls._safe_float(feedback_metrics.get("skill_budget_multiplier"), 1.0),
                4,
            ),
            "legacy_priority_adjustment": round(
                builder_cls._safe_float(
                    feedback_metrics.get("legacy_priority_adjustment"),
                    plan.get("feedback_priority_adjustment"),
                ),
                4,
            ),
            "skill_priority_adjustment": round(
                builder_cls._safe_float(feedback_metrics.get("skill_priority_adjustment")),
                4,
            ),
            "raw_validation_a_rate": round(
                builder_cls._safe_float(feedback_metrics.get("raw_validation_a_rate")),
                4,
            ),
            "raw_validation_b_rate": round(
                builder_cls._safe_float(feedback_metrics.get("raw_validation_b_rate")),
                4,
            ),
            "raw_validation_total_score_mean": round(
                builder_cls._safe_float(feedback_metrics.get("raw_validation_total_score_mean")),
                4,
            ),
            "strict_incubation_ready_rate": round(
                builder_cls._safe_float(feedback_metrics.get("strict_incubation_ready_rate")),
                4,
            ),
            "family_quality_score": family_quality_score,
            "family_route_action": family_action,
        }
        family_debt_table[family] = {
            "zero_signal_ratio": round(
                builder_cls._safe_float(feedback_metrics.get("zero_signal_ratio")),
                4,
            ),
            "low_signal_ratio": round(
                builder_cls._safe_float(feedback_metrics.get("low_signal_ratio")),
                4,
            ),
            "evidence_debt_ratio": round(
                builder_cls._safe_float(feedback_metrics.get("evidence_debt_ratio")),
                4,
            ),
            "raw_validation_d_rate": round(
                builder_cls._safe_float(feedback_metrics.get("raw_validation_d_rate")),
                4,
            ),
            "paper_skill_lcb": round(
                builder_cls._safe_float(feedback_metrics.get("paper_skill_lcb")),
                4,
            ),
            "paper_recent_skill_lcb": round(
                builder_cls._safe_float(feedback_metrics.get("paper_recent_skill_lcb")),
                4,
            ),
            "paper_stability_gap": round(
                builder_cls._safe_float(feedback_metrics.get("paper_stability_gap")),
                4,
            ),
            "paper_coverage_ratio": round(
                builder_cls._safe_float(feedback_metrics.get("paper_coverage_ratio"), 1.0),
                4,
            ),
            "control_mode": plan.get("feedback_control_mode"),
            "legacy_control_mode": feedback_metrics.get("legacy_control_mode")
            or plan.get("feedback_control_mode"),
            "skill_control_mode": feedback_metrics.get("skill_control_mode"),
            "control_reasons": list(plan.get("feedback_control_reasons") or []),
            "legacy_control_reasons": list(feedback_metrics.get("legacy_control_reasons") or []),
            "skill_control_reasons": list(feedback_metrics.get("skill_control_reasons") or []),
            "family_freeze_active": bool(plan.get("feedback_family_freeze_active")),
            "family_quality_score": family_quality_score,
            "family_route_action": family_action,
        }
        search_route_actions.append(
            {
                "family": family,
                "scope": "family",
                "action": family_action,
                "control_mode": plan.get("feedback_control_mode"),
                "legacy_control_mode": feedback_metrics.get("legacy_control_mode")
                or plan.get("feedback_control_mode"),
                "skill_control_mode": feedback_metrics.get("skill_control_mode"),
                "budget_weight": round(builder_cls._safe_float(plan.get("budget_weight")), 4),
                "budget_multiplier": round(
                    builder_cls._safe_float(plan.get("feedback_budget_multiplier"), 1.0),
                    4,
                ),
                "legacy_budget_multiplier": round(
                    builder_cls._safe_float(
                        feedback_metrics.get("legacy_budget_multiplier"),
                        plan.get("feedback_budget_multiplier"),
                    ),
                    4,
                ),
                "skill_budget_multiplier": round(
                    builder_cls._safe_float(feedback_metrics.get("skill_budget_multiplier"), 1.0),
                    4,
                ),
                "family_quality_score": family_quality_score,
                "paper_skill_lcb": round(
                    builder_cls._safe_float(feedback_metrics.get("paper_skill_lcb")),
                    4,
                ),
                "paper_recent_skill_lcb": round(
                    builder_cls._safe_float(feedback_metrics.get("paper_recent_skill_lcb")),
                    4,
                ),
                "paper_stability_gap": round(
                    builder_cls._safe_float(feedback_metrics.get("paper_stability_gap")),
                    4,
                ),
                "paper_coverage_ratio": round(
                    builder_cls._safe_float(feedback_metrics.get("paper_coverage_ratio"), 1.0),
                    4,
                ),
                "raw_validation_a_rate": round(
                    builder_cls._safe_float(feedback_metrics.get("raw_validation_a_rate")),
                    4,
                ),
                "raw_validation_b_rate": round(
                    builder_cls._safe_float(feedback_metrics.get("raw_validation_b_rate")),
                    4,
                ),
                "strict_incubation_ready_rate": round(
                    builder_cls._safe_float(feedback_metrics.get("strict_incubation_ready_rate")),
                    4,
                ),
                "priority_adjustment": round(
                    builder_cls._safe_float(plan.get("feedback_priority_adjustment")),
                    4,
                ),
                "reasons": list(plan.get("feedback_control_reasons") or []),
            }
        )
        for target_pool_id, _target_pool_bucket in dict(
            family_bucket.get("target_pool_feedback") or {}
        ).items():
            pool_id = str(target_pool_id or "").strip()
            if not pool_id:
                continue
            scope_metrics = resolve_feedback_metrics(
                budget_feedback_root,
                family=family,
                target_pool_id=pool_id,
            )
            action, payload = builder_cls._scope_route_action(
                scope_name="target_pool",
                scope_metrics=scope_metrics,
            )
            if not action:
                continue
            target_pool_routes[pool_id] = {
                "action": action,
                **payload,
            }
            search_route_actions.append(
                {
                    "family": family,
                    "scope": "target_pool",
                    "scope_key": pool_id,
                    "action": action,
                    **payload,
                }
            )
        for holding_bucket, _holding_bucket_bucket in dict(
            family_bucket.get("holding_bucket_feedback") or {}
        ).items():
            bucket_name = normalize_text(holding_bucket)
            if not bucket_name:
                continue
            scope_metrics = resolve_feedback_metrics(
                budget_feedback_root,
                family=family,
                holding_bucket=bucket_name,
            )
            action, payload = builder_cls._scope_route_action(
                scope_name="holding_bucket",
                scope_metrics=scope_metrics,
            )
            if not action:
                continue
            holding_bucket_routes[bucket_name] = {
                "action": action,
                **payload,
            }
            search_route_actions.append(
                {
                    "family": family,
                    "scope": "holding_bucket",
                    "scope_key": bucket_name,
                    "action": action,
                    **payload,
                }
            )
        generator_scope = dict(family_bucket.get("generator_mode_feedback") or {})
        for generator_mode, _generator_bucket in generator_scope.items():
            mode_name = normalize_text(generator_mode)
            if not mode_name:
                continue
            scope_metrics = resolve_feedback_metrics(
                budget_feedback_root,
                family=family,
                generator_mode=mode_name,
            )
            action, payload = builder_cls._scope_route_action(
                scope_name="generator_mode",
                scope_metrics=scope_metrics,
                preferred_shift_target=builder_cls._preferred_generator_shift_target(
                    family_bucket,
                    current_mode=mode_name,
                ),
            )
            if not action:
                continue
            generator_mode_routes[mode_name] = {
                "action": action,
                **payload,
            }
            search_route_actions.append(
                {
                    "family": family,
                    "scope": "generator_mode",
                    "scope_key": mode_name,
                    "action": action,
                    **payload,
                }
            )
        if target_pool_routes:
            family_reward_table[family]["target_pool_routes"] = target_pool_routes
            family_debt_table[family]["target_pool_routes"] = target_pool_routes
        if holding_bucket_routes:
            family_reward_table[family]["holding_bucket_routes"] = holding_bucket_routes
            family_debt_table[family]["holding_bucket_routes"] = holding_bucket_routes
        if generator_mode_routes:
            family_reward_table[family]["generator_mode_routes"] = generator_mode_routes
            family_debt_table[family]["generator_mode_routes"] = generator_mode_routes
    return family_reward_table, family_debt_table, search_route_actions, family_plans


async def load_budget_feedback(
    builder_cls,
    db,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """优化 2：并发加载所有策略的反馈数据（原为逐策略串行）。"""
    import asyncio

    seed_feedback_root = extract_feedback_root(snapshot.get("family_gate_feedback") or {})
    strategy_rows = await builder_cls._list_feedback_source_strategies(db)
    aggregate_root: dict[str, dict[str, Any]] = {}
    runtime_alert_total = 0
    runtime_risk_total = 0
    promotion_review_total = 0
    promotion_review_status_counts: dict[str, int] = {}
    promotion_review_recommendation_counts: dict[str, int] = {}

    # 并发加载所有策略的 5 种反馈数据
    sem = asyncio.Semaphore(10)  # 限制并发度避免 DB 过载

    async def _load_strategy_feedback(strategy: dict) -> Optional[dict[str, Any]]:
        strategy_id = str(strategy.get("id") or "").strip()
        if not strategy_id:
            return None
        async with sem:
            # 5 个 IO 操作并发执行
            metric_task = _call_optional_async(
                db, "list_strategy_incubation_metrics", strategy_id, limit=1, default=[],
            )
            risk_task = _call_optional_async(
                db, "list_strategy_runtime_risk_events", strategy_id=strategy_id, status="open", limit=20, default=[],
            )
            alert_task = _call_optional_async(
                db, "list_strategy_runtime_alerts", strategy_id=strategy_id, status="open_or_ack", limit=20, default=[],
            )
            evidence_task = builder_cls._load_feedback_evidence_overview(db, strategy)
            review_task = _call_optional_async(
                db, "get_latest_strategy_promotion_review", strategy_id, default=None,
            )
            latest_metric_rows, risk_events, runtime_alerts, evidence_overview, latest_promotion_review = (
                await asyncio.gather(metric_task, risk_task, alert_task, evidence_task, review_task)
            )
            return {
                "strategy": strategy,
                "strategy_id": strategy_id,
                "latest_metric_rows": latest_metric_rows,
                "risk_events": risk_events,
                "runtime_alerts": runtime_alerts,
                "evidence_overview": evidence_overview,
                "latest_promotion_review": latest_promotion_review,
            }

    # 并发加载所有策略
    raw_results = await asyncio.gather(
        *[_load_strategy_feedback(s) for s in strategy_rows],
        return_exceptions=True,
    )

    # 聚合结果（保持原有聚合逻辑）
    for item in raw_results:
        if isinstance(item, BaseException) or item is None:
            continue
        strategy = item["strategy"]
        strategy_id = item["strategy_id"]
        family = builder_cls._feedback_family_key(strategy)
        latest_metric_rows = item["latest_metric_rows"]
        risk_events = item["risk_events"]
        runtime_alerts = item["runtime_alerts"]
        evidence_overview = item["evidence_overview"]
        latest_promotion_review = item["latest_promotion_review"]

        latest_metric = dict((list(latest_metric_rows or []) or [None])[0] or {})
        open_runtime_alerts = [
            dict(item_a or {})
            for item_a in list(runtime_alerts or [])
            if normalize_text((item_a or {}).get("status") or "open") not in {"resolved", "closed"}
        ]
        open_risk_events = [
            dict(item_r or {})
            for item_r in list(risk_events or [])
            if normalize_text((item_r or {}).get("status") or "open") not in {"resolved", "closed"}
        ]
        runtime_alert_total += len(open_runtime_alerts)
        runtime_risk_total += len(open_risk_events)
        review_payload = dict(latest_promotion_review or {})
        if review_payload:
            promotion_review_total += 1
            review_status = normalize_text(review_payload.get("status"))
            if review_status:
                promotion_review_status_counts[review_status] = int(
                    promotion_review_status_counts.get(review_status) or 0
                ) + 1
            review_recommendation = normalize_text(review_payload.get("recommendation"))
            if review_recommendation:
                promotion_review_recommendation_counts[review_recommendation] = int(
                    promotion_review_recommendation_counts.get(review_recommendation) or 0
                ) + 1
        paper_hit_ratio = builder_cls._safe_float(latest_metric.get("hit_rate_5d"))
        if latest_metric.get("hit_rate_5d") is None:
            paper_hit_ratio = 0.5
        metric_metadata = dict(latest_metric.get("metadata") or {})
        signal_quality = dict(
            metric_metadata.get("signal_quality")
            or evidence_overview.get("signal_quality")
            or {}
        )
        paper_skill_lcb = evidence_overview.get("skill_lcb")
        if paper_skill_lcb is None:
            paper_skill_lcb = (
                signal_quality.get("primary_skill_lcb")
                if signal_quality.get("primary_skill_lcb") is not None
                else signal_quality.get("primary_signal_skill_lcb")
            )
        paper_recent_skill_lcb = evidence_overview.get("recent_skill_lcb")
        if paper_recent_skill_lcb is None:
            paper_recent_skill_lcb = signal_quality.get("recent_primary_skill_lcb")
        if paper_recent_skill_lcb is None:
            paper_recent_skill_lcb = paper_skill_lcb
        paper_stability_gap = evidence_overview.get("stability_gap")
        if paper_stability_gap is None:
            paper_stability_gap = signal_quality.get("stability_gap")
        paper_coverage_ratio = evidence_overview.get("coverage_ratio")
        if paper_coverage_ratio is None:
            paper_coverage_ratio = signal_quality.get("coverage_ratio")
        if paper_coverage_ratio is None:
            observed_days = [
                int(day)
                for day in list(evidence_overview.get("observed_forward_days") or [])
                if int(day) in builder_cls.EVIDENCE_FORWARD_WINDOWS
            ]
            paper_coverage_ratio = (
                len(observed_days) / len(builder_cls.EVIDENCE_FORWARD_WINDOWS)
                if builder_cls.EVIDENCE_FORWARD_WINDOWS
                else 0.0
            )
        feedback_metrics = {
            "paper_hit_ratio": round(min(max(paper_hit_ratio, 0.0), 1.0), 4),
            "paper_skill_lcb": round(
                max(min(builder_cls._safe_float(paper_skill_lcb), 1.0), -1.0),
                4,
            ),
            "paper_recent_skill_lcb": round(
                max(min(builder_cls._safe_float(paper_recent_skill_lcb), 1.0), -1.0),
                4,
            ),
            "paper_stability_gap": round(
                max(builder_cls._safe_float(paper_stability_gap), 0.0),
                4,
            ),
            "paper_coverage_ratio": round(
                min(max(builder_cls._safe_float(paper_coverage_ratio, 1.0), 0.0), 1.0),
                4,
            ),
            "runtime_alert_pressure": builder_cls._feedback_runtime_alert_pressure(
                latest_metric,
                open_risk_events,
                open_runtime_alerts,
            ),
            "realized_turnover": round(
                min(max(builder_cls._safe_float(latest_metric.get("turnover_rate")), 0.0), 2.0),
                4,
            ),
            "capacity_crowding": builder_cls._feedback_capacity_crowding(
                latest_metric,
                open_risk_events,
                open_runtime_alerts,
            ),
        }
        family_bucket = aggregate_root.setdefault(family, {})
        builder_cls._accumulate_feedback_bucket(
            family_bucket,
            strategy_id=strategy_id,
            metrics=feedback_metrics,
            runtime_alert_count=len(open_runtime_alerts),
            runtime_risk_event_count=len(open_risk_events),
            evidence_overview=evidence_overview,
            promotion_review=review_payload,
        )
        target_pool_id = extract_target_pool_id(strategy)
        if target_pool_id:
            target_scope = dict(family_bucket.get("target_pool_feedback") or {})
            scoped_bucket = dict(target_scope.get(target_pool_id) or {})
            builder_cls._accumulate_feedback_bucket(
                scoped_bucket,
                strategy_id=strategy_id,
                metrics=feedback_metrics,
                runtime_alert_count=len(open_runtime_alerts),
                runtime_risk_event_count=len(open_risk_events),
                evidence_overview=evidence_overview,
                promotion_review=review_payload,
            )
            target_scope[target_pool_id] = scoped_bucket
            family_bucket["target_pool_feedback"] = target_scope
        generator_mode = extract_generator_mode(strategy)
        if generator_mode:
            generator_scope = dict(family_bucket.get("generator_mode_feedback") or {})
            scoped_bucket = dict(generator_scope.get(generator_mode) or {})
            builder_cls._accumulate_feedback_bucket(
                scoped_bucket,
                strategy_id=strategy_id,
                metrics=feedback_metrics,
                runtime_alert_count=len(open_runtime_alerts),
                runtime_risk_event_count=len(open_risk_events),
                evidence_overview=evidence_overview,
                promotion_review=review_payload,
            )
            generator_scope[generator_mode] = scoped_bucket
            family_bucket["generator_mode_feedback"] = generator_scope
        holding_bucket = extract_holding_bucket(strategy)
        if holding_bucket:
            holding_scope = dict(family_bucket.get("holding_bucket_feedback") or {})
            scoped_bucket = dict(holding_scope.get(holding_bucket) or {})
            builder_cls._accumulate_feedback_bucket(
                scoped_bucket,
                strategy_id=strategy_id,
                metrics=feedback_metrics,
                runtime_alert_count=len(open_runtime_alerts),
                runtime_risk_event_count=len(open_risk_events),
                evidence_overview=evidence_overview,
                promotion_review=review_payload,
            )
            holding_scope[holding_bucket] = scoped_bucket
            family_bucket["holding_bucket_feedback"] = holding_scope

    finalized_root = {
        family: builder_cls._finalize_feedback_bucket(bucket)
        for family, bucket in aggregate_root.items()
    }
    merged_root: dict[str, Any] = {}
    for family in set(seed_feedback_root) | set(finalized_root):
        merged_root[family] = builder_cls._merge_feedback_bucket(
            seed_feedback_root.get(family),
            finalized_root.get(family),
        )
    target_pool_scope_count = sum(
        len(dict((bucket or {}).get("target_pool_feedback") or {}))
        for bucket in merged_root.values()
        if isinstance(bucket, dict)
    )
    generator_mode_scope_count = sum(
        len(dict((bucket or {}).get("generator_mode_feedback") or {}))
        for bucket in merged_root.values()
        if isinstance(bucket, dict)
    )
    holding_bucket_scope_count = sum(
        len(dict((bucket or {}).get("holding_bucket_feedback") or {}))
        for bucket in merged_root.values()
        if isinstance(bucket, dict)
    )
    return normalize_feedback_input_contract(
        {
            "available": bool(merged_root),
            "reason": None if merged_root else "feedback_unavailable",
            "feedback": merged_root,
            "summary": {
                "family_count": len(merged_root),
                "seeded_family_count": len(seed_feedback_root),
                "strategy_count": len(strategy_rows),
                "runtime_alert_count": runtime_alert_total,
                "runtime_risk_event_count": runtime_risk_total,
                "promotion_review_count": promotion_review_total,
                "promotion_review_status_counts": promotion_review_status_counts,
                "promotion_review_recommendation_counts": (
                    promotion_review_recommendation_counts
                ),
                "target_pool_scope_count": target_pool_scope_count,
                "holding_bucket_scope_count": holding_bucket_scope_count,
                "generator_mode_scope_count": generator_mode_scope_count,
            },
        }
    )


async def load_stock_family_allocation(
    builder_cls,
    db,
    snapshot: dict[str, Any],
    *,
    active_factors: List[str],
    family_preference_order: List[str],
    governed_top_candidates: List[dict[str, Any]],
    budget_feedback_root: Any = None,
) -> dict[str, Any]:
    candidate_hints = builder_cls._build_candidate_hint_map(governed_top_candidates)
    allocation: dict[str, dict[str, Any]] = {}
    family_counts: dict[str, int] = {}
    priorities: list[float] = []
    limit = max(1, int(STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT))
    families_per_stock = max(1, int(STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK))

    def _track(code: str, families: List[str], priority: float, *, source_mode: str, row: Optional[dict[str, Any]] = None) -> None:
        normalized_code = str(code or "").strip()
        normalized_families = [str(item or "").strip().lower() for item in list(families or []) if str(item or "").strip()]
        if not normalized_code or not normalized_families:
            return
        selected_families = list(dict.fromkeys(normalized_families))[:families_per_stock]
        if not selected_families:
            return
        bounded_priority = round(max(0.01, min(float(priority or 0.0), 0.99)), 4)
        family_plans = builder_cls._build_family_plans(
            selected_families,
            priority=bounded_priority,
            budget_feedback_root=budget_feedback_root,
        )
        active_family_plans = [
            plan
            for plan in family_plans
            if str(plan.get("feedback_control_mode") or "normal").strip().lower() in {"normal", "cooldown"}
            and builder_cls._safe_float(plan.get("budget_weight")) > 0.0
        ]
        if not active_family_plans:
            return
        selected_family_plans = active_family_plans
        selected_families = [
            str(plan.get("family") or "").strip().lower()
            for plan in selected_family_plans[:families_per_stock]
            if str(plan.get("family") or "").strip()
        ]
        family_plans = [
            dict(plan or {})
            for plan in selected_family_plans
            if str(plan.get("family") or "").strip().lower() in set(selected_families)
        ]
        allocation[normalized_code] = {
            "families": selected_families,
            "family_plans": family_plans,
            "priority": bounded_priority,
            "source_mode": source_mode,
        }
        if family_plans:
            allocation[normalized_code]["top_family"] = family_plans[0]["family"]
            allocation[normalized_code]["top_validation_profile"] = (
                dict(family_plans[0].get("validation_profile") or {}).get("profile")
            )
            allocation[normalized_code]["top_feedback_budget_multiplier"] = builder_cls._safe_float(
                family_plans[0].get("feedback_budget_multiplier")
            )
        if isinstance(row, dict):
            industry = str(row.get("industry") or row.get("sector") or "").strip()
            if industry:
                allocation[normalized_code]["industry"] = industry
        priorities.append(bounded_priority)
        for family in selected_families:
            family_counts[family] = int(family_counts.get(family, 0)) + 1

    rows: list[dict[str, Any]] = []
    universe_page_size = max(100, min(limit, 1000))
    try:
        rows, _ = await load_stock_universe_rows(
            db,
            limit=limit,
            page_size=universe_page_size,
            start_offset=0,
        )
    except Exception:
        rows = []

    if rows:
        from .matrix import StockStrategyMatrixPlanner

        matrix_snapshot = dict(snapshot or {})
        if family_preference_order:
            factor_research_snapshot = dict(matrix_snapshot.get("factor_research") or {})
            factor_research_snapshot["family_preference_order"] = list(family_preference_order)
            matrix_snapshot["factor_research"] = factor_research_snapshot

        hot_sectors = set(normalize_sector_labels(snapshot.get("hot_sectors") or [], limit=12))
        cold_sectors = set(normalize_sector_labels(snapshot.get("cold_sectors") or [], limit=12))
        for row in rows:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            # PR-U3: 排除不适合做策略目标的股票
            if code.startswith("920") or code.startswith("8") or code.startswith("200") or code.startswith("900"):
                continue
            hint = dict(candidate_hints.get(code) or {})
            hinted_families = [
                str(family or "").strip().lower()
                for family in list(hint.get("families") or [])
                if str(family or "").strip()
            ]
            projected_families = StockStrategyMatrixPlanner._families_for_row(
                row,
                snapshot=matrix_snapshot,
                hot_sectors=hot_sectors,
                cold_sectors=cold_sectors,
                active_factors=active_factors,
            )
            families = builder_cls._merge_ranked_families(hinted_families, projected_families)
            if not families:
                continue
            base_score = StockStrategyMatrixPlanner._row_priority_score(
                row,
                snapshot=matrix_snapshot,
                hot_sectors=hot_sectors,
                cold_sectors=cold_sectors,
                active_factors=active_factors,
            )
            # v2 row priority now lives on a roughly 0-40 cross-sectional scale.
            base_priority = max(0.05, min(base_score / 40.0, 0.92))
            hint_bonus = min(max(list(hint.get("scores") or [0.0]) or [0.0]), 1.0) * 0.18
            _track(
                code,
                families,
                base_priority + hint_bonus,
                source_mode="stock_universe_projection" if not hint else "stock_universe_projection_with_candidate_hints",
                row=row,
            )
    elif candidate_hints:
        for code, hint in candidate_hints.items():
            hint_score = max(list(hint.get("scores") or [0.0]) or [0.0])
            _track(
                code,
                list(hint.get("families") or []),
                0.55 + hint_score * 0.35,
                source_mode="governed_candidate_hint_only",
            )

    family_counts = dict(sorted(family_counts.items(), key=lambda item: (-int(item[1]), item[0])))
    summary = {
        "count": len(allocation),
        "family_counts": family_counts,
        "allocation_entropy": builder_cls._family_allocation_entropy(family_counts),
        "avg_priority": round(sum(priorities) / len(priorities), 4) if priorities else 0.0,
        "max_priority": round(max(priorities), 4) if priorities else 0.0,
        "min_priority": round(min(priorities), 4) if priorities else 0.0,
        "candidate_hint_count": len(candidate_hints),
        "universe_limit": limit,
        "source_mode": (
            "stock_universe_projection"
            if rows
            else ("governed_candidate_hint_only" if candidate_hints else "unavailable")
        ),
        "feedback_enabled": bool(budget_feedback_root),
        "feedback_family_count": len(dict(budget_feedback_root or {})),
    }
    return {
        "available": bool(allocation),
        "reason": None if allocation else ("stock_universe_unavailable" if not rows and not candidate_hints else "empty_stock_allocation"),
        "allocation": allocation,
        "summary": summary,
    }
