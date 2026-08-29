"""策略工厂轻量因子研究 artifact 构建。"""

from __future__ import annotations

from datetime import date
from typing import Any, List, Optional, Tuple

from ...domain.constants import (
    FACTORY_RESEARCH_FACTORS,
    STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
)
from ...infrastructure.mcp_services import (
    get_factor_scheduler_singleton,
    get_quant_manager_callable,
    get_strategy_lifecycle_shared_runtime,
)
from ._builder_support import FactorResearchBuilderSupportMixin
from ._feedback_metrics import (
    accumulate_feedback_bucket,
    fallback_feedback_evidence_overview,
    family_allocation_entropy,
    feedback_ab_quality_score,
    feedback_capacity_crowding,
    feedback_family_key,
    feedback_runtime_alert_pressure,
    finalize_feedback_bucket,
    list_feedback_source_strategies,
    load_feedback_evidence_overview,
    merge_feedback_bucket,
    preferred_generator_shift_target,
    resolve_promotion_review_outcome,
    resolve_search_route_action,
    scope_route_action,
)
from ._feedback_routes import (
    build_search_route_feedback_snapshot,
    load_budget_feedback,
    load_stock_family_allocation,
    rewrite_family_preference_order_by_feedback,
)
from ._research_artifact_payload import build_factor_research_artifact_payload
from ._research_build_steps import (
    apply_active_factor_pool_fallback,
    build_ranked_factor_context,
    load_research_runtime_context,
    resolve_factor_names,
)
from ._research_context_sources import (
    build_candidate_hint_map,
    extract_candidate_codes,
    load_factor_history_meta,
    load_governed_candidate_pool,
    load_model_registry_lineage,
)


class FactorResearchBuilder(FactorResearchBuilderSupportMixin):
    """基于 collect 阶段已有因子摘要构建统一 artifact。"""

    HISTORY_LIMIT = 20
    STALE_AFTER_DAYS = 2
    EVIDENCE_FORWARD_WINDOWS = (1, 5, 10, 20)

    # 优化 7：TTL 缓存基础设施
    _cache: dict[str, tuple[float, Any]] = {}
    CACHE_TTL_SEC: dict[str, float] = {
        "governed_pool": 300.0,        # 5 分钟
        "model_lineage": 600.0,        # 10 分钟
        "stock_allocation": 1800.0,    # 30 分钟
        "factor_history": 120.0,       # 2 分钟
    }

    @classmethod
    def _cache_get(cls, key: str) -> Optional[Any]:
        """从 TTL 缓存获取值，过期返回 None。"""
        import time as _time
        if key in cls._cache:
            expire, value = cls._cache[key]
            if _time.time() < expire:
                return value
            del cls._cache[key]
        return None

    @classmethod
    def _cache_set(cls, key: str, value: Any, *, ttl_category: str = "factor_history") -> None:
        """设置 TTL 缓存值。"""
        import time as _time
        ttl = cls.CACHE_TTL_SEC.get(ttl_category, 120.0)
        cls._cache[key] = (_time.time() + ttl, value)

    @classmethod
    def _cache_clear(cls) -> None:
        """清空所有缓存。"""
        cls._cache.clear()

    @classmethod
    async def _load_factory_pool_factors(cls) -> list[dict[str, Any]]:
        """从因子挖掘工厂的活跃池加载因子（带缓存）。"""
        cache_key = "factory_pool_factors"
        cached = cls._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            from ...infrastructure.mcp_services import get_factor_pool_gateway
            gateway = get_factor_pool_gateway()
            factors = await gateway.get_active_factors(limit=50)
            if not factors:
                pool_rows = await gateway.get_active_factors(
                    limit=100,
                    include_quarantine=True,
                )
                factors = [
                    dict(item or {})
                    for item in list(pool_rows or [])
                    if cls._is_eligible_active_pool_factor(item)
                ][:50]
            cls._cache_set(cache_key, factors, ttl_category="governed_pool")
            return factors
        except Exception:
            # 工厂不可用时静默降级
            return []

    @classmethod
    def _qc_shelf_decision_blocks(cls, validation_summary: dict[str, Any]) -> bool:
        shelf_decision = dict(validation_summary.get("qc_shelf_decision") or {})
        decision = str(shelf_decision.get("decision") or "").strip().lower()
        if decision not in {"retire", "quarantine", "reject"}:
            return False
        if bool(validation_summary.get("qc_autoshelf_applied")):
            return True
        labels = validation_summary.get("qc_labels")
        if not isinstance(labels, dict):
            return True
        availability_keys = {
            "oos_available",
            "layered_available",
            "robustness_available",
            "multiple_testing_available",
        }
        if any(key in labels for key in availability_keys):
            return any(bool(labels.get(key)) for key in availability_keys)
        numeric_keys = (
            "rank_ic_ir",
            "bootstrap_ci_lower",
            "monotonicity",
            "long_short_return",
            "window_stability",
            "param_sensitivity",
            "dsr",
            "pbo",
        )
        all_zero = True
        for key in numeric_keys:
            try:
                if abs(float(labels.get(key) or 0.0)) > 1e-12:
                    all_zero = False
                    break
            except (TypeError, ValueError):
                pass
        unknown_oos = (
            not bool(labels.get("oos_pass"))
            and str(labels.get("oos_grade") or "").strip().lower() in {"", "unknown"}
        )
        return not (all_zero and unknown_oos)

    @classmethod
    def _is_eligible_active_pool_factor(cls, item: dict[str, Any]) -> bool:
        payload = dict(item or {})
        if str(payload.get("status") or "").strip().lower() != "active":
            return False
        if not str(payload.get("expression_dsl") or "").strip():
            return False
        validation_summary = dict(payload.get("validation_summary") or {})
        quality_status = str(validation_summary.get("quality_status") or "").strip().lower()
        if cls._qc_shelf_decision_blocks(validation_summary):
            return False
        shelf_decision = dict(validation_summary.get("qc_shelf_decision") or {})
        shelf_action = str(shelf_decision.get("decision") or "").strip().lower()
        if quality_status != "promoted" and shelf_action in {"retire", "quarantine", "reject"}:
            return False
        current_ic = payload.get("current_ic")
        if current_ic is not None:
            try:
                if float(current_ic) < 0.0:
                    return False
            except (TypeError, ValueError):
                pass
        if quality_status == "quarantine":
            return False
        if quality_status == "promoted":
            return True
        grade = str(
            payload.get("admission_grade")
            or validation_summary.get("grade")
            or dict(validation_summary.get("rating") or {}).get("grade")
            or ""
        ).strip().upper()
        return grade in {"A", "B"}

    @classmethod
    def _active_pool_factor_sort_key(cls, item: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
        payload = dict(item or {})
        validation_summary = dict(payload.get("validation_summary") or {})
        rating = dict(validation_summary.get("rating") or {})
        metrics = dict(validation_summary.get("metrics") or {})
        grade = str(
            payload.get("admission_grade")
            or validation_summary.get("grade")
            or rating.get("grade")
            or ""
        ).strip().upper()
        grade_score = {"SS": 4.0, "S": 3.5, "A": 3.0, "B": 2.0}.get(grade, 0.0)
        return (
            cls._safe_float(rating.get("total_score")),
            cls._safe_float(metrics.get("rank_ic_ir")),
            cls._safe_float(metrics.get("rank_ic_mean")),
            cls._safe_float(payload.get("current_ic")),
            cls._safe_float(payload.get("fitness")),
            grade_score,
        )

    @classmethod
    def _rank_active_pool_factors(cls, factors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            [dict(item or {}) for item in list(factors or []) if isinstance(item, dict)],
            key=cls._active_pool_factor_sort_key,
            reverse=True,
        )

    @classmethod
    async def _load_factor_history_meta(
        cls,
        db,
        factor_names: List[str],
    ) -> Tuple[dict[str, dict[str, Any]], Optional[date]]:
        return await load_factor_history_meta(cls, db, factor_names)

    @classmethod
    async def _load_governed_candidate_pool(
        cls,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        return await load_governed_candidate_pool(
            cls,
            snapshot,
            quant_manager_provider=get_quant_manager_callable,
        )

    @classmethod
    async def _load_model_registry_lineage(
        cls,
        candidates: List[dict[str, Any]],
    ) -> dict[str, Any]:
        return await load_model_registry_lineage(
            cls,
            candidates,
            quant_manager_provider=get_quant_manager_callable,
        )

    @classmethod
    def _extract_candidate_codes(cls, item: dict[str, Any]) -> List[str]:
        return extract_candidate_codes(cls, item)

    @classmethod
    def _build_candidate_hint_map(
        cls,
        candidates: List[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        return build_candidate_hint_map(cls, candidates)

    @classmethod
    def _feedback_ab_quality_score(cls, feedback_metrics: dict[str, Any]) -> float:
        return feedback_ab_quality_score(cls, feedback_metrics)

    @classmethod
    def _resolve_search_route_action(
        cls,
        plan: dict[str, Any],
        feedback_metrics: dict[str, Any],
    ) -> str:
        return resolve_search_route_action(cls, plan, feedback_metrics)

    @classmethod
    def _scope_route_action(
        cls,
        *,
        scope_name: str,
        scope_metrics: dict[str, Any],
        preferred_shift_target: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        return scope_route_action(
            cls,
            scope_name=scope_name,
            scope_metrics=scope_metrics,
            preferred_shift_target=preferred_shift_target,
        )

    @classmethod
    def _preferred_generator_shift_target(
        cls,
        family_bucket: dict[str, Any],
        *,
        current_mode: str | None,
    ) -> str | None:
        return preferred_generator_shift_target(
            cls,
            family_bucket,
            current_mode=current_mode,
        )

    @classmethod
    def _rewrite_family_preference_order_by_feedback(
        cls,
        family_preference_order: List[str],
        *,
        family_plans: List[dict[str, Any]],
    ) -> List[str]:
        return rewrite_family_preference_order_by_feedback(
            cls,
            family_preference_order,
            family_plans=family_plans,
        )

    @classmethod
    def _build_search_route_feedback_snapshot(
        cls,
        *,
        family_preference_order: List[str],
        budget_feedback_root: Any = None,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        List[dict[str, Any]],
        List[dict[str, Any]],
    ]:
        return build_search_route_feedback_snapshot(
            cls,
            family_preference_order=family_preference_order,
            budget_feedback_root=budget_feedback_root,
        )

    @staticmethod
    def _feedback_family_key(payload: dict[str, Any]) -> str:
        return feedback_family_key(payload)

    @staticmethod
    def _feedback_runtime_alert_pressure(
        latest_metric: dict[str, Any],
        risk_events: list[dict[str, Any]],
        runtime_alerts: list[dict[str, Any]],
    ) -> float:
        return feedback_runtime_alert_pressure(
            FactorResearchBuilder,
            latest_metric,
            risk_events,
            runtime_alerts,
        )

    @classmethod
    def _feedback_capacity_crowding(
        cls,
        latest_metric: dict[str, Any],
        risk_events: list[dict[str, Any]],
        runtime_alerts: list[dict[str, Any]],
    ) -> float:
        return feedback_capacity_crowding(cls, latest_metric, risk_events, runtime_alerts)

    @classmethod
    async def _list_feedback_source_strategies(
        cls,
        db,
        *,
        limit: int = 180,
    ) -> List[dict[str, Any]]:
        return await list_feedback_source_strategies(cls, db, limit=limit)

    @staticmethod
    def _resolve_promotion_review_outcome(
        status_counts: dict[str, Any] | None,
        recommendation_counts: dict[str, Any] | None,
    ) -> tuple[str | None, str | None]:
        return resolve_promotion_review_outcome(status_counts, recommendation_counts)

    @classmethod
    def _fallback_feedback_evidence_overview(
        cls,
        signal_stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return fallback_feedback_evidence_overview(cls, signal_stats)

    @classmethod
    async def _load_feedback_evidence_overview(
        cls,
        db,
        strategy: dict[str, Any],
    ) -> dict[str, Any]:
        return await load_feedback_evidence_overview(
            cls,
            db,
            strategy,
            lifecycle_runtime_provider=get_strategy_lifecycle_shared_runtime,
        )

    @classmethod
    def _accumulate_feedback_bucket(
        cls,
        accumulator: dict[str, Any],
        *,
        strategy_id: str,
        metrics: dict[str, Any],
        runtime_alert_count: int,
        runtime_risk_event_count: int,
        evidence_overview: dict[str, Any] | None = None,
        promotion_review: dict[str, Any] | None = None,
    ) -> None:
        accumulate_feedback_bucket(
            cls,
            accumulator,
            strategy_id=strategy_id,
            metrics=metrics,
            runtime_alert_count=runtime_alert_count,
            runtime_risk_event_count=runtime_risk_event_count,
            evidence_overview=evidence_overview,
            promotion_review=promotion_review,
        )

    @classmethod
    def _finalize_feedback_bucket(cls, accumulator: dict[str, Any]) -> dict[str, Any]:
        return finalize_feedback_bucket(cls, accumulator)

    @classmethod
    def _merge_feedback_bucket(
        cls,
        base: Any,
        fresh: Any,
    ) -> dict[str, Any]:
        return merge_feedback_bucket(cls, base, fresh)

    @classmethod
    async def _load_budget_feedback(
        cls,
        db,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        return await load_budget_feedback(cls, db, snapshot)

    @staticmethod
    def _family_allocation_entropy(family_counts: dict[str, int]) -> float:
        return family_allocation_entropy(family_counts)

    @classmethod
    async def _load_stock_family_allocation(
        cls,
        db,
        snapshot: dict[str, Any],
        *,
        active_factors: List[str],
        family_preference_order: List[str],
        governed_top_candidates: List[dict[str, Any]],
        budget_feedback_root: Any = None,
    ) -> dict[str, Any]:
        return await load_stock_family_allocation(
            cls,
            db,
            snapshot,
            active_factors=active_factors,
            family_preference_order=family_preference_order,
            governed_top_candidates=governed_top_candidates,
            budget_feedback_root=budget_feedback_root,
        )

    @classmethod
    async def build(cls, db, snapshot: dict[str, Any]) -> dict[str, Any]:
        factor_ic = dict(snapshot.get("factor_ic") or {})
        factor_trend = dict(snapshot.get("factor_ic_trend") or {})
        lightweight_mock_fallback = cls._should_use_lightweight_mock_fallback(db, snapshot)
        snapshot_date = cls._parse_date(snapshot.get("date"))
        names = list(dict.fromkeys([*factor_ic.keys(), *factor_trend.keys(), *FACTORY_RESEARCH_FACTORS]))
        if lightweight_mock_fallback:
            history_meta, latest_factor_date = {}, None
        else:
            history_meta, latest_factor_date = await cls._load_factor_history_meta(db, names)
        names = resolve_factor_names(
            factor_ic=factor_ic,
            factor_trend=factor_trend,
            history_meta=history_meta,
        )
        runtime_context = await load_research_runtime_context(
            cls,
            db,
            snapshot,
            lightweight_mock_fallback=lightweight_mock_fallback,
            snapshot_date=snapshot_date,
            scheduler_provider=get_factor_scheduler_singleton,
        )
        governed_top_candidates = runtime_context["governed_top_candidates"]
        budget_feedback_root = runtime_context["budget_feedback_root"]

        # ── Factor Mining Factory Pool 集成 ──────────────────────────
        factory_pool_factors = await cls._load_factory_pool_factors()
        if factory_pool_factors:
            eligible_factory_pool_factors = cls._rank_active_pool_factors(
                [
                    dict(item or {})
                    for item in list(factory_pool_factors or [])
                    if cls._is_eligible_active_pool_factor(item)
                ]
            )
            # 将工厂池中的活跃因子合并到 governed_top_candidates
            for pool_factor in factory_pool_factors:
                factor_name = pool_factor.get("name", "")
                if factor_name and factor_name not in names:
                    names.append(factor_name)
            runtime_context["factory_pool_factors_raw_count"] = len(factory_pool_factors)
            runtime_context["factory_pool_factors_filtered_count"] = max(
                len(factory_pool_factors) - len(eligible_factory_pool_factors),
                0,
            )
            runtime_context["factory_pool_factors"] = eligible_factory_pool_factors
            runtime_context["factory_pool_size"] = len(factory_pool_factors)
            runtime_context["factory_pool_eligible_size"] = len(eligible_factory_pool_factors)
            governed_strict_count = int(
                runtime_context.get("governed_candidate_pool_strict_count")
                or dict(runtime_context.get("active_candidate_pool") or {}).get("strict_count")
                or 0
            )
            if not governed_top_candidates or governed_strict_count <= 0:
                governed_top_candidates = apply_active_factor_pool_fallback(
                    cls,
                    runtime_context,
                    eligible_factory_pool_factors,
                    snapshot_date=snapshot_date,
                )
        # ── End Factory Pool 集成 ────────────────────────────────────

        factor_context = build_ranked_factor_context(
            cls,
            factor_ic=factor_ic,
            factor_trend=factor_trend,
            names=names,
            history_meta=history_meta,
            governed_top_candidates=governed_top_candidates,
            snapshot=snapshot,
        )
        active_factors = factor_context["active_factors"]
        preferred_strategy_types = factor_context["preferred_strategy_types"]

        factor_ic_source = dict((snapshot.get("sources") or {}).get("factor_ic") or {})
        family_preference_order_seed = cls._build_family_preference_order(
            snapshot,
            preferred_strategy_types=preferred_strategy_types,
        )
        if lightweight_mock_fallback:
            stock_family_allocation_payload = {
                "available": False,
                "reason": "lightweight_mock_fallback",
                "allocation": {},
                "summary": {
                    "count": 0,
                    "family_counts": {},
                    "allocation_entropy": 0.0,
                    "avg_priority": 0.0,
                    "max_priority": 0.0,
                    "min_priority": 0.0,
                    "candidate_hint_count": 0,
                    "universe_limit": max(1, int(STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT)),
                    "source_mode": "lightweight_mock_fallback",
                },
            }
        else:
            # PR-B: Cold start optimization for stock_family_allocation
            incubating_count = int(snapshot.get("incubating_count") or 0)
            if incubating_count == 0 and not governed_top_candidates:
                stock_family_allocation_payload = {
                    "available": False,
                    "reason": "cold_start_no_incubating",
                    "allocation": {},
                    "summary": {
                        "count": 0,
                        "family_counts": {},
                        "allocation_entropy": 0.0,
                        "avg_priority": 0.0,
                        "max_priority": 0.0,
                        "min_priority": 0.0,
                        "candidate_hint_count": 0,
                        "universe_limit": max(1, int(STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT)),
                        "source_mode": "cold_start_skip",
                    },
                }
            else:
                stock_family_allocation_payload = await cls._load_stock_family_allocation(
                    db,
                    snapshot,
                    active_factors=active_factors,
                    family_preference_order=family_preference_order_seed,
                    governed_top_candidates=governed_top_candidates,
                    budget_feedback_root=budget_feedback_root,
                )
        stock_family_allocation = dict(stock_family_allocation_payload.get("allocation") or {})
        stock_family_allocation_summary = dict(stock_family_allocation_payload.get("summary") or {})
        return build_factor_research_artifact_payload(
            cls,
            snapshot=snapshot,
            snapshot_date=snapshot_date,
            latest_factor_date=latest_factor_date,
            history_meta=history_meta,
            factor_ic_source=factor_ic_source,
            factor_context=factor_context,
            runtime_context=runtime_context,
            stock_family_allocation=stock_family_allocation,
            stock_family_allocation_summary=stock_family_allocation_summary,
            lightweight_mock_fallback=lightweight_mock_fallback,
        )


__all__ = ["FactorResearchBuilder"]
