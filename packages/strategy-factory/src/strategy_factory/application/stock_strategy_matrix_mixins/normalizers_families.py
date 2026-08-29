"""Bulk stock-strategy matrix planning for P0 factory expansion."""


from __future__ import annotations

from ...domain import constants as _matrix_const

import logging
import math
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List

from ...domain.constants import (
    STOCK_FIRST_ROUTER_TELEMETRY_ENABLED,
    STOCK_STRATEGY_MATRIX_BATCH_SIZE,
    STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK,
    STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK,
    STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD,
    STRATEGY_FACTORY_VECTOR_REUSE_MIN_SAMPLES,
    STRATEGY_FACTORY_VECTOR_REUSE_MIN_SIMILARITY,
    STRATEGY_FACTORY_VECTOR_REUSE_TOPN,
    preferred_strategy_types_for_factor,
)
from .._opportunity_utils import _MarketOpportunityScannerUtilityMixin
from .._stock_universe_loader import filter_stock_universe_rows_by_codes, load_stock_universe_rows
from ..factory_market_views import build_full_market_topn_payload
from ..factory_execution import resolve_runtime_mode_flags
from .._matrix_vector_reuse import VectorReuseService
from .._runtime_toggles import stock_direction_gate_enabled
from ..research_plane_contract import build_task_artifact
from ..stock_strategy_router import StockRegimeProfile, route_strategies
from ..sector_taxonomy import (
    normalize_sector_labels,
    sector_profiles_for_label,
    sector_family_biases,
    sector_match_strength,
)

logger = logging.getLogger(__name__)


class _MatrixFamiliesMixin:
    @classmethod
    def _intrinsic_families_for_row(
        cls,
        row: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        hot_sectors: set[str],
        cold_sectors: set[str],
        active_factors: list[str],
    ) -> list[str]:
        families: list[str] = []
        industry = str(row.get("industry") or row.get("sector") or "").strip()
        pe_ratio = cls._safe_float(row.get("pe_ratio"))
        pb_ratio = cls._safe_float(row.get("pb_ratio"))

        def add(*items: str) -> None:
            for item in items:
                lowered = str(item or "").strip().lower()
                if lowered and lowered not in families:
                    families.append(lowered)

        # PR-S19：优先消费 stock_profile 推荐 family，让画像真正影响 family 选择
        profile_summary = cls._extract_profile_summary(row)
        profile_quality = str(profile_summary.get("profile_quality") or "").strip().lower()
        coverage = dict(profile_summary.get("feature_coverage") or {})
        router_enabled = cls._effective_router_enabled(snapshot)
        router_strict = cls._effective_router_strict(snapshot)

        # SR-1 (P1-2)：toggle ON 且画像可用时，由 StockStrategyRouter 依 regime+周期+排除项决定 family。
        # 严守同步边界：只读已挂在 row 上的 profile_summary，不做任何异步/网络调用。
        if router_enabled and not profile_summary:
            cls._set_router_status(
                row,
                status="blocked" if router_strict else "fallback",
                enabled=router_enabled,
                strict=router_strict,
                reason="missing_profile_summary",
            )
            if router_strict:
                return []
        if router_enabled and profile_summary and profile_quality in {"failed", ""}:
            reason = "profile_failed" if profile_quality == "failed" else "profile_quality_missing"
            cls._set_router_status(
                row,
                status="blocked" if router_strict else "fallback",
                enabled=router_enabled,
                strict=router_strict,
                reason=reason,
            )
            if router_strict:
                return []
        if router_enabled and profile_summary and profile_quality not in {"failed", ""}:
            try:
                regime = dict(profile_summary.get("regime") or {})
                extras = {
                    "rsi": ((profile_summary.get("factor_dimension_scores") or {}) or {}).get("rsi"),
                    "volume_ratio": cls._safe_float(row.get("volume_ratio_5_20")) or 1.0,
                    "event_catalyst": str(coverage.get("event_news_notice_research_theme") or "").lower()
                    in {"ok", "partial"},
                    "liquidity_low": (cls._safe_float(row.get("amount")) or 0.0) > 0
                    and (cls._safe_float(row.get("amount")) or 0.0) < 1e7,
                }
                profile = StockRegimeProfile.from_profile_summary(
                    str(row.get("code") or ""), profile_summary, regime, extras
                )
                routed = route_strategies(profile, max_families=STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)
                if routed.families:
                    families = cls._apply_direction_gate(
                        routed.families[: max(1, STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)],
                        profile_summary=profile_summary,
                        row=row,
                        source="router",
                    )[: max(1, STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)]
                    direction_gate = dict(row.get("_stock_direction_gate") or {})
                    exclusions = list(routed.exclusions or [])
                    for family in list(direction_gate.get("dropped_families") or []):
                        if family not in exclusions:
                            exclusions.append(family)
                    cls._set_router_status(
                        row,
                        status="applied",
                        enabled=router_enabled,
                        strict=router_strict,
                        families=families,
                        holding_bucket=routed.holding_period_bucket,
                        confidence=float(routed.confidence or 0.0),
                        exclusions=exclusions,
                    )
                    return families
                cls._set_router_status(
                    row,
                    status="blocked" if router_strict else "fallback",
                    enabled=router_enabled,
                    strict=router_strict,
                    reason="empty_routed_families",
                )
                if router_strict:
                    return []
            except Exception as exc:
                cls._set_router_status(
                    row,
                    status="blocked" if router_strict else "fallback",
                    enabled=router_enabled,
                    strict=router_strict,
                    reason="router_exception",
                    error_type=type(exc).__name__,
                )
                if router_strict:
                    return []

        if profile_summary and profile_quality not in {"failed", ""}:
            recommended = [
                str(item or "").strip().lower()
                for item in list(profile_summary.get("recommended_families") or [])
                if str(item or "").strip()
            ]
            candidate_families = [
                str(item or "").strip().lower()
                for item in list(profile_summary.get("candidate_factor_families") or [])
                if str(item or "").strip()
            ]
            # alternative_sentiment 与 event 维度 coverage 缺失时，剔除强情绪/事件 family
            alt_cov = str(coverage.get("alternative_sentiment_capital_flow") or "").lower()
            event_cov = str(coverage.get("event_news_notice_research_theme") or "").lower()

            def _allowed(fam: str) -> bool:
                if fam == "event_structure_breakout":
                    return event_cov in {"ok", "partial"}
                if fam == "sentiment" or fam == "sentiment_factor":
                    return alt_cov in {"ok", "partial"}
                return True

            for fam in recommended:
                if _allowed(fam):
                    add(fam)
            for fam in candidate_families:
                if _allowed(fam):
                    add(fam)

        if router_enabled:
            current = dict(row.get("_stock_first_router") or {})
            if str(current.get("status") or "").strip().lower() != "applied":
                cls._set_router_status(
                    row,
                    status="fallback",
                    enabled=router_enabled,
                    strict=router_strict,
                    reason=str(current.get("reason") or "legacy_family_fallback"),
                )

        # 旧逻辑（hot/cold sector + 估值 + base family 序列）作为 fallback / 补充
        hot_match = cls._sector_match_strength(industry, hot_sectors)
        cold_match = cls._sector_match_strength(industry, cold_sectors)
        add(*cls._sector_family_biases(industry, mode="intrinsic"))
        if hot_match > 0.0:
            add(*cls._sector_family_biases(industry, mode="hot") or ["momentum", "growth_factor"])
        if cold_match > 0.0:
            add(*cls._sector_family_biases(industry, mode="cold") or ["rsi", "value_factor", "quality_factor"])
        value_candidate = 0 < pe_ratio <= 18 or 0 < pb_ratio <= 1.8
        reversal_enabled = cls._factor_signal_enabled(active_factors, "reversal")
        if value_candidate:
            add("value_factor")
            if reversal_enabled:
                add("mean_reversion_short")
            add("quality_factor")
        add(*cls._base_family_order(snapshot))
        for factor_name in active_factors:
            add(*preferred_strategy_types_for_factor(factor_name, default=[]))

        gated = cls._apply_direction_gate(
            families,
            profile_summary=profile_summary,
            row=row,
            source="legacy",
        )
        return gated[: max(1, STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)]

    @classmethod
    def _families_for_row(
        cls,
        row: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        hot_sectors: set[str],
        cold_sectors: set[str],
        active_factors: list[str],
        allocation_item: dict[str, Any] | None = None,
    ) -> list[str]:
        families: list[str] = []

        def add(*items: str) -> None:
            for item in items:
                lowered = str(item or "").strip().lower()
                if lowered and lowered not in families:
                    families.append(lowered)

        intrinsic_families = cls._intrinsic_families_for_row(
            row,
            snapshot=snapshot,
            hot_sectors=hot_sectors,
            cold_sectors=cold_sectors,
            active_factors=active_factors,
        )

        router_enabled = cls._effective_router_enabled(snapshot)
        router_strict = cls._effective_router_strict(snapshot)
        if allocation_item and not (router_enabled and router_strict):
            allocation_families = [
                str(plan.get("family") or "").strip().lower()
                for plan in list(allocation_item.get("family_plans") or [])
                if str(plan.get("family") or "").strip()
            ]
            if not allocation_families:
                allocation_families = [
                    str(item or "").strip().lower()
                    for item in list(allocation_item.get("families") or [])
                    if str(item or "").strip()
                ]
            if allocation_families:
                source_mode = str(allocation_item.get("source_mode") or "").strip().lower()
                if source_mode.startswith("stock_universe_projection") and intrinsic_families:
                    sector_anchor = next(
                        (
                            family
                            for family in cls._sector_family_biases(
                                row.get("industry") or row.get("sector"),
                                mode="intrinsic",
                            )
                            if str(family or "").strip()
                        ),
                        intrinsic_families[0],
                    )
                    # Keep one industry-driven anchor so projection-covered leaders do not all collapse
                    # into the same allocation trio.
                    add(sector_anchor)
                    add(*allocation_families)
                    add(*intrinsic_families[1:])
                else:
                    add(*allocation_families)
                    add(*intrinsic_families)
                gated_families = cls._apply_direction_gate(
                    families,
                    profile_summary=cls._extract_profile_summary(row),
                    row=row,
                    source="allocation_overlay",
                )
                return gated_families[: max(1, STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)]
        add(*intrinsic_families)

        gated_families = cls._apply_direction_gate(
            families,
            profile_summary=cls._extract_profile_summary(row),
            row=row,
            source="final",
        )
        return gated_families[: max(1, STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)]

    @classmethod
    def _family_plans_for_row(
        cls,
        row: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        hot_sectors: set[str],
        cold_sectors: set[str],
        active_factors: list[str],
        allocation_item: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        allocation_plans = [
            dict(plan or {})
            for plan in list((allocation_item or {}).get("family_plans") or [])
            if isinstance(plan, dict)
        ]
        router_enabled = cls._effective_router_enabled(snapshot)
        router_strict = cls._effective_router_strict(snapshot)
        if allocation_plans and not (router_enabled and router_strict):
            normalized_plans: list[dict[str, Any]] = []
            for index, plan in enumerate(allocation_plans, 1):
                family = str(plan.get("family") or "").strip().lower()
                if not family:
                    continue
                family_rank = max(1, cls._safe_int(plan.get("family_rank")) or index)
                normalized_plans.append(
                    {
                        "family": family,
                        "family_rank": family_rank,
                        "budget": max(
                            0.0,
                            min(
                                cls._safe_float(plan.get("budget") or plan.get("budget_weight")),
                                1.0,
                            ),
                        ),
                        "budget_weight": max(
                            0.0,
                            min(
                                cls._safe_float(plan.get("budget_weight") or plan.get("budget")),
                                1.0,
                            ),
                        ),
                        "failure_penalty": max(
                            0.0,
                            min(
                                cls._safe_float(plan.get("failure_penalty")),
                                1.0,
                            ),
                        ),
                        "validation_profile": cls._normalize_validation_profile(
                            family,
                            dict(plan.get("validation_profile") or {}),
                        ),
                    }
                )
            normalized_plans.sort(
                key=lambda plan: (
                    int(plan.get("family_rank") or 0),
                    str(plan.get("family") or ""),
                )
            )
            if normalized_plans:
                normalized_plans = normalized_plans[: max(1, STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)]
                gated_families = cls._apply_direction_gate(
                    [
                        str(plan.get("family") or "").strip().lower()
                        for plan in normalized_plans
                        if str(plan.get("family") or "").strip()
                    ],
                    profile_summary=cls._extract_profile_summary(row),
                    row=row,
                    source="allocation_plan",
                )
                plan_lookup = {
                    str(plan.get("family") or "").strip().lower(): dict(plan or {})
                    for plan in normalized_plans
                    if str(plan.get("family") or "").strip()
                }
                gated_plans: list[dict[str, Any]] = []
                for family in gated_families[: max(1, STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)]:
                    plan = dict(plan_lookup.get(family) or {})
                    if not plan:
                        fallback_plans = cls._default_family_plans([family], priority=0.5)
                        plan = dict((fallback_plans or [{}])[0] or {})
                    if plan:
                        gated_plans.append(plan)
                if gated_plans:
                    return gated_plans

        families = cls._families_for_row(
            row,
            snapshot=snapshot,
            hot_sectors=hot_sectors,
            cold_sectors=cold_sectors,
            active_factors=active_factors,
            allocation_item=allocation_item,
        )
        return cls._default_family_plans(
            families,
            priority=max(0.0, min(cls._safe_float((allocation_item or {}).get("priority")), 1.0)),
        )

    @staticmethod
    def _holding_bucket_for_family(family: str) -> str:
        if family in {"rsi"}:
            return "short"
        if family in {"momentum"}:
            return "medium"
        if family in {"value_factor"}:
            return "long"
        return "medium"

    @staticmethod
    def _holding_window_for_family(family: str) -> dict[str, Any]:
        normalized_family = str(family or "").strip().lower()
        if normalized_family == "quality_factor":
            return {"min_days": 24, "max_days": 72}
        if normalized_family == "ma_cross":
            return {"min_days": 14, "max_days": 48}
        if normalized_family == "momentum":
            return {"min_days": 14, "max_days": 42}
        if normalized_family in {"value_factor", "growth_factor", "multi_factor"}:
            return {"min_days": 18, "max_days": 60}
        if normalized_family in {"rsi", "gap_fill", "mean_reversion_short"}:
            return {"min_days": 3, "max_days": 12}
        return {"min_days": 5, "max_days": 20}

    @staticmethod
    def _alpha_source_for_family(family: str) -> str:
        if family in {"momentum", "ma_cross", "rsi"}:
            return "technical"
        if family in {"value_factor", "quality_factor", "growth_factor"}:
            return "fundamental"
        if family == "macro_timing":
            return "macro"
        return "multi_factor"

    @staticmethod
    def _risk_level_for_family(family: str) -> str:
        if family in {"momentum", "growth_factor"}:
            return "high"
        if family in {"quality_factor"}:
            return "low"
        return "medium"

    # ------------------------------------------------------------------
    # PR-S19：画像驱动的 holding_window / risk_level / 参数 band
    # ------------------------------------------------------------------

    @classmethod
    def _holding_window_with_profile(
        cls,
        family: str,
        profile_summary: dict[str, Any] | None,
    ) -> dict[str, Any]:
        base = dict(cls._holding_window_for_family(family))
        if not profile_summary:
            return base
        scores = cls._profile_dimension_scores(profile_summary)
        risk = scores.get("risk", 0.0)
        trend = scores.get("trend", 0.0)
        # 高波动 / 高换手：缩短持仓上限；低波动 / 趋势稳健：放宽上限
        min_days = max(2, int(base.get("min_days") or 5))
        max_days = max(min_days + 1, int(base.get("max_days") or 20))
        if risk >= 0.6:
            max_days = max(min_days + 1, int(max_days * 0.7))
        elif risk <= 0.25 and trend >= 0.4:
            max_days = int(max_days * 1.25)
        return {"min_days": min_days, "max_days": max_days}

    @classmethod
    def _risk_level_with_profile(
        cls,
        family: str,
        profile_summary: dict[str, Any] | None,
    ) -> str:
        base = cls._risk_level_for_family(family)
        if not profile_summary:
            return base
        scores = cls._profile_dimension_scores(profile_summary)
        risk = scores.get("risk", 0.0)
        if risk >= 0.7:
            return "high"
        if risk <= 0.2 and scores.get("quality", 0.0) >= 0.5:
            return "low"
        return base

    @classmethod
    def _alpha_source_with_profile(
        cls,
        family: str,
        profile_summary: dict[str, Any] | None,
    ) -> str:
        base = cls._alpha_source_for_family(family)
        if not profile_summary:
            return base
        coverage = dict(profile_summary.get("feature_coverage") or {})
        # 如果事件 coverage missing，但 family 想用 event：降级到 multi_factor
        if base == "event" or family == "event_structure_breakout":
            event_cov = str(coverage.get("event_news_notice_research_theme") or "").lower()
            if event_cov not in {"ok", "partial"}:
                return "multi_factor"
        return base

    @classmethod
    def _param_band_for_profile(
        cls,
        family: str,
        profile_summary: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """根据画像维度分数给出 family 的参数微调 band。

        返回结构示例：
            {
              "lookback_days": {"min": 14, "max": 36, "preferred": 24},
              "risk_budget": {"min": 0.02, "max": 0.05, "preferred": 0.035},
              "rsi_overbought": {...},
              "_profile_quality": "partial",
            }

        下游 `_expand_bulk_rule_specs` / `param_search_space` 消费时
        以 `preferred` 为种子点，`min/max` 为采样区间。
        """

        normalized_family = str(family or "").strip().lower()
        if not profile_summary:
            return {}

        scores = cls._profile_dimension_scores(profile_summary)
        quality = str(profile_summary.get("profile_quality") or "").lower()
        quality_factor = {"good": 1.0, "partial": 0.75, "low_confidence": 0.5, "failed": 0.3}.get(
            quality, 0.6
        )

        risk = scores.get("risk", 0.0)
        trend = scores.get("trend", 0.0)
        reversal = scores.get("reversal", 0.0)
        volume = scores.get("volume", 0.0)

        # 通用 lookback：高波动/高反转→更短 lookback；低波动/强趋势→更长 lookback
        base_min, base_max = 14, 36
        if normalized_family in {"rsi", "mean_reversion_short", "gap_fill"}:
            base_min, base_max = 4, 14
        elif normalized_family in {"value_factor", "quality_factor", "growth_factor", "multi_factor"}:
            base_min, base_max = 20, 60
        elif normalized_family in {"momentum", "ma_cross"}:
            base_min, base_max = 12, 36

        skew = (trend - reversal) * 0.3 - risk * 0.25
        center_shift = round((base_max - base_min) * skew, 1)
        lo = max(2, int(base_min + center_shift * 0.4))
        hi = max(lo + 2, int(base_max + center_shift * 0.6))
        preferred = (lo + hi) // 2

        # risk_budget：高波动/低质量 → 收紧
        rb_min, rb_max = 0.015, 0.06
        rb_preferred = 0.035
        if risk >= 0.6:
            rb_preferred = 0.020
            rb_max = 0.040
        elif risk <= 0.25:
            rb_preferred = 0.045
            rb_max = 0.070
        rb_min = round(rb_min * quality_factor, 4)
        rb_preferred = round(rb_preferred * quality_factor, 4)

        band: dict[str, Any] = {
            "lookback_days": {"min": lo, "max": hi, "preferred": preferred},
            "risk_budget": {
                "min": max(0.005, rb_min),
                "max": rb_max,
                "preferred": rb_preferred,
            },
            "_profile_quality": quality or "unknown",
            "_dimension_scores": dict(scores),
        }

        # family 特异参数 band
        if normalized_family in {"rsi", "mean_reversion_short"}:
            # 反转家族：reversal 高时拉宽 oversold/overbought
            ext = 5 + int(reversal * 10)
            band["rsi_overbought"] = {
                "min": 65 + ext // 2,
                "max": 80 + ext,
                "preferred": 70 + ext // 2,
            }
            band["rsi_oversold"] = {
                "min": 20 - ext // 2,
                "max": 35 - ext // 2,
                "preferred": 30 - ext // 2,
            }
        if normalized_family in {"momentum", "ma_cross"}:
            # 动量/均线：volume 强时偏短均线
            fast = max(5, int(10 - volume * 3))
            slow = max(fast + 3, int(30 - trend * 5))
            band["fast_window"] = {"min": fast - 1, "max": fast + 3, "preferred": fast}
            band["slow_window"] = {"min": slow - 3, "max": slow + 5, "preferred": slow}

        return band

    @staticmethod
    def _merge_param_search_space(
        family_default: dict[str, Any] | None,
        profile_band: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """合并 family 默认参数空间与 profile 提供的 band。

        合并规则：profile_band 中 ``_`` 开头的元数据键（如 _profile_quality）
        会原样保留；其他参数键如果同时出现在两边，profile_band 的 min/max/preferred 覆盖 default。
        """

        merged: dict[str, Any] = {}
        for source in (family_default or {}, profile_band or {}):
            if not isinstance(source, Mapping):
                continue
            for key, value in source.items():
                if key.startswith("_"):
                    merged[key] = value
                    continue
                if isinstance(value, Mapping):
                    existing = dict(merged.get(key) or {})
                    existing.update(dict(value))
                    merged[key] = existing
                else:
                    merged[key] = value
        return merged

    @staticmethod
    def _effective_generation_limit() -> int:
        candidate_budget = max(1, int(_matrix_const.STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN))
        return max(1, min(int(STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK), candidate_budget))

    @classmethod
    def _effective_task_budget(cls) -> int:
        generation_limit = cls._effective_generation_limit()
        candidate_budget = max(1, int(_matrix_const.STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN))
        candidate_limited_budget = max(1, candidate_budget // generation_limit)
        return max(1, min(int(_matrix_const.STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN), candidate_limited_budget))
