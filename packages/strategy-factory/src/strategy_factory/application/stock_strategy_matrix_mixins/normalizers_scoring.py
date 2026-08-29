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


class _MatrixScoringMixin:
    @classmethod
    def _sector_regime_component_score(
        cls,
        industry: Any,
        *,
        sector_labels: set[str],
        label_coverage: dict[str, dict[str, float]] | None,
        base_points: float,
    ) -> float:
        normalized_labels = [
            str(item or "").strip()
            for item in list(sector_labels or set())
            if str(item or "").strip()
        ]
        best_score = 0.0
        for label in normalized_labels:
            match_strength = cls._sector_match_strength(industry, [label])
            if match_strength <= 0.0:
                continue
            penalty_payload = dict((label_coverage or {}).get(label) or {})
            breadth_penalty = cls._safe_float(penalty_payload.get("breadth_penalty") or 1.0)
            coverage_penalty = cls._safe_float(penalty_payload.get("coverage_penalty") or 1.0)
            effective_penalty = max(0.35, min(1.0, breadth_penalty * coverage_penalty))
            best_score = max(best_score, base_points * match_strength * effective_penalty)
        return round(best_score, 4)

    @staticmethod
    def _percentile_from_sorted(
        sorted_values: list[float],
        value: float,
        *,
        higher_is_better: bool,
    ) -> float:
        values = [float(item) for item in list(sorted_values or [])]
        if not values:
            return 0.5
        if len(values) == 1:
            return 1.0
        target = float(value)
        left = max(0, min(bisect_left(values, target), len(values) - 1))
        right = max(0, min(bisect_right(values, target) - 1, len(values) - 1))
        average_position = (left + right) / 2.0
        if higher_is_better:
            return max(0.0, min(average_position / (len(values) - 1), 1.0))
        return max(0.0, min(1.0 - (average_position / (len(values) - 1)), 1.0))

    @staticmethod
    def _factor_signal_enabled(active_factors: list[str], token: str) -> bool:
        normalized_token = str(token or "").strip().lower()
        if not normalized_token:
            return False
        for factor_name in list(active_factors or []):
            normalized_factor = str(factor_name or "").strip().lower()
            if not normalized_factor:
                continue
            if normalized_factor == normalized_token:
                return True
            if normalized_token in normalized_factor:
                return True
        return False

    @classmethod
    def _build_priority_scoring_context(
        cls,
        rows: list[dict[str, Any]],
        *,
        snapshot: dict[str, Any],
        hot_sectors: set[str],
        cold_sectors: set[str],
        active_factors: list[str],
        stock_family_allocation: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        allocation = {
            str(code or "").strip(): dict(item or {})
            for code, item in dict(stock_family_allocation or {}).items()
            if str(code or "").strip()
        }
        normalized_hot_sectors = set(cls._normalize_sector_labels(hot_sectors, limit=20))
        normalized_cold_sectors = set(cls._normalize_sector_labels(cold_sectors, limit=20))
        normalized_active_factors = [
            str(item or "").strip().lower()
            for item in list(active_factors or [])
            if str(item or "").strip()
        ]
        preferred_families: list[str] = []
        for factor_name in normalized_active_factors:
            for family in preferred_strategy_types_for_factor(factor_name, default=[]):
                normalized_family = str(family or "").strip().lower()
                if normalized_family and normalized_family not in preferred_families:
                    preferred_families.append(normalized_family)

        size_logs: list[float] = []
        valuation_pe_global: list[float] = []
        valuation_pb_global: list[float] = []
        valuation_pe_by_industry: dict[str, list[float]] = {}
        valuation_pb_by_industry: dict[str, list[float]] = {}
        allocation_priorities: list[float] = []
        allocation_source_modes: list[str] = []
        for row in list(rows or []):
            payload = dict(row or {})
            market_cap = cls._safe_float(payload.get("market_cap"))
            if market_cap > 0:
                size_logs.append(math.log(max(market_cap, 1.0)))
            industry_key = cls._industry_key(payload.get("industry") or payload.get("sector"))
            pe_ratio = cls._safe_float(payload.get("pe_ratio"))
            pb_ratio = cls._safe_float(payload.get("pb_ratio"))
            if pe_ratio > 0:
                valuation_pe_global.append(pe_ratio)
                valuation_pe_by_industry.setdefault(industry_key, []).append(pe_ratio)
            if pb_ratio > 0:
                valuation_pb_global.append(pb_ratio)
                valuation_pb_by_industry.setdefault(industry_key, []).append(pb_ratio)
            code = str(payload.get("code") or "").strip()
            allocation_item = dict(allocation.get(code) or {})
            allocation_priority = max(0.0, min(cls._safe_float(allocation_item.get("priority")), 1.0))
            if allocation_priority > 0.0:
                allocation_priorities.append(allocation_priority)
            source_mode = str(allocation_item.get("source_mode") or "").strip()
            if source_mode:
                allocation_source_modes.append(source_mode)

        for bucket in valuation_pe_by_industry.values():
            bucket.sort()
        for bucket in valuation_pb_by_industry.values():
            bucket.sort()
        size_logs.sort()
        valuation_pe_global.sort()
        valuation_pb_global.sort()
        allocation_priorities.sort()

        source_mode = None
        if allocation_source_modes:
            distinct_source_modes = list(dict.fromkeys(allocation_source_modes))
            source_mode = distinct_source_modes[0] if len(distinct_source_modes) == 1 else "mixed"

        hot_sector_coverage = cls._build_sector_label_coverage(
            rows,
            sector_labels=normalized_hot_sectors,
        )
        cold_sector_coverage = cls._build_sector_label_coverage(
            rows,
            sector_labels=normalized_cold_sectors,
        )

        return {
            "score_contract_version": "strategy_factory.full_market_topn.v2",
            "preferred_families": preferred_families,
            "normalized_active_factors": normalized_active_factors,
            "hot_sectors": normalized_hot_sectors,
            "cold_sectors": normalized_cold_sectors,
            "hot_sector_coverage": hot_sector_coverage,
            "cold_sector_coverage": cold_sector_coverage,
            "size_logs": size_logs,
            "valuation_pe_global": valuation_pe_global,
            "valuation_pb_global": valuation_pb_global,
            "valuation_pe_by_industry": valuation_pe_by_industry,
            "valuation_pb_by_industry": valuation_pb_by_industry,
            "allocation_priorities": allocation_priorities,
            "allocation_source_mode": source_mode,
            "allocation_avg_priority": round(
                sum(allocation_priorities) / len(allocation_priorities),
                4,
            ) if allocation_priorities else 0.0,
            "active_factors": list(active_factors or []),
            "snapshot_date": str(snapshot.get("date") or snapshot.get("snapshot_date") or "").strip() or None,
        }

    # ------------------------------------------------------------------
    # PR-S19 (策略工厂跑偏修复方案 P2)：消费 row["stock_profile"] 画像
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_profile_summary(row: dict[str, Any]) -> dict[str, Any]:
        """从 row 上的 stock_profile（来自 PR-S17）提取 profile_summary。

        路径：row["stock_profile"]["metadata"]["profile_summary"]，
        兼容老结构 row["stock_profile"]["profile_summary"]。
        若画像缺失，返回 {}。
        """

        profile = dict(row.get("stock_profile") or {})
        if not profile:
            return {}
        metadata = profile.get("metadata")
        if isinstance(metadata, str):
            # SQLite 存储有时返回 JSON 字符串
            try:
                import json as _json

                metadata = _json.loads(metadata or "{}")
            except Exception:
                metadata = {}
        metadata = dict(metadata or {})
        summary = dict(metadata.get("profile_summary") or profile.get("profile_summary") or {})
        return summary

    @classmethod
    def _build_lightweight_profile_summary(cls, row: dict[str, Any]) -> dict[str, Any]:
        """Build a local, no-IO profile so strict Stock-First runs never use legacy family fallback silently."""

        pe_ratio = cls._safe_float(row.get("pe_ratio"))
        pb_ratio = cls._safe_float(row.get("pb_ratio"))
        market_cap = cls._safe_float(row.get("market_cap"))
        turnover_rate = cls._safe_float(
            row.get("turnover_rate")
            or row.get("turnover")
            or row.get("turnover_ratio")
        )
        volume_ratio = cls._safe_float(
            row.get("volume_ratio_5_20")
            or row.get("volume_ratio")
            or row.get("volume_ratio_20")
        )
        pct_chg = cls._safe_float(
            row.get("pct_chg")
            or row.get("change_pct")
            or row.get("daily_return")
            or row.get("return_1d")
        )
        amount = cls._safe_float(row.get("amount") or row.get("turnover_amount"))
        valuation_score = 0.0
        if 0 < pe_ratio <= 30:
            valuation_score = max(valuation_score, (30.0 - pe_ratio) / 30.0)
        if 0 < pb_ratio <= 3:
            valuation_score = max(valuation_score, (3.0 - pb_ratio) / 3.0)
        quality_score = 0.0
        if market_cap > 0:
            try:
                quality_score = max(0.0, min(math.log10(market_cap / 1e8 + 1.0) / 3.0, 1.0))
            except Exception:
                quality_score = 0.0
        if amount >= 5e8:
            quality_score = max(quality_score, 0.45)
        elif amount >= 1e8:
            quality_score = max(quality_score, 0.30)
        trend_score = 0.0
        if volume_ratio >= 1.2:
            trend_score += min((volume_ratio - 1.0) / 2.0, 0.35)
        if pct_chg > 0:
            trend_score += min(pct_chg / 8.0, 0.35)
        if turnover_rate >= 1.0:
            trend_score += min(turnover_rate / 12.0, 0.20)
        trend_score = max(0.0, min(trend_score, 1.0))
        reversal_score = 0.0
        if pct_chg < 0:
            reversal_score += min(abs(pct_chg) / 8.0, 0.35)
        if 0.0 < volume_ratio <= 0.85:
            reversal_score += min((0.85 - volume_ratio) / 0.85, 0.25)
        reversal_score = max(0.0, min(reversal_score, 1.0))
        growth_score = 0.0
        if trend_score >= 0.35 and quality_score >= 0.35:
            growth_score = min(0.65, trend_score * 0.7 + quality_score * 0.3)
        trend_regime = "unknown"
        if trend_score >= 0.35:
            trend_regime = "trend_up"
        elif reversal_score >= 0.30:
            trend_regime = "range"
        vol_regime = "unknown"
        if volume_ratio >= 1.5 or turnover_rate >= 5.0:
            vol_regime = "high_vol"
        elif 0.0 < volume_ratio <= 0.8:
            vol_regime = "low_vol"
        elif volume_ratio > 0.0:
            vol_regime = "normal_vol"
        recommended = ["multi_factor"]
        if trend_score >= 0.35:
            recommended = ["momentum", "ma_cross", "multi_factor"]
        elif reversal_score >= 0.35:
            recommended = ["mean_reversion_short", "rsi", "multi_factor"]
        elif valuation_score >= 0.35 and quality_score >= 0.25:
            recommended = ["value_factor", "quality_factor", "multi_factor"]
        elif quality_score >= 0.55:
            recommended = ["quality_factor", "multi_factor"]
        return {
            "profile_quality": "partial",
            "profile_source": "lightweight_row_fallback",
            "primary_archetype": "lightweight_unknown",
            "secondary_archetypes": [],
            "regime": {
                "trend_regime": trend_regime,
                "vol_regime": vol_regime,
                "sentiment_regime": "unknown",
            },
            "factor_dimension_scores": {
                "trend": round(trend_score, 4),
                "reversal": round(reversal_score, 4),
                "valuation": round(max(0.0, min(valuation_score, 1.0)), 4),
                "quality": round(max(0.0, min(quality_score, 1.0)), 4),
                "growth": round(max(0.0, min(growth_score, 1.0)), 4),
            },
            "feature_coverage": {
                "technical_price_volume": "partial" if trend_score > 0.0 or reversal_score > 0.0 else "missing",
                "valuation_financial": "partial" if valuation_score > 0.0 or quality_score > 0.0 else "missing",
                "alternative_sentiment_capital_flow": "missing",
                "event_news_notice_research_theme": "missing",
            },
            "recommended_families": recommended,
            "candidate_factor_families": recommended,
        }

    @classmethod
    def _ensure_lightweight_profile_summary(cls, row: dict[str, Any]) -> bool:
        if cls._extract_profile_summary(row):
            return False
        profile = dict(row.get("stock_profile") or {})
        metadata = profile.get("metadata")
        if isinstance(metadata, str):
            try:
                import json as _json

                metadata = _json.loads(metadata or "{}")
            except Exception:
                metadata = {}
        metadata = dict(metadata or {})
        metadata["profile_summary"] = cls._build_lightweight_profile_summary(row)
        profile["metadata"] = metadata
        profile.setdefault("stock_code", row.get("code"))
        profile.setdefault("source", "lightweight_row_fallback")
        row["stock_profile"] = profile
        row["_stock_first_router_lightweight_profile_generated"] = True
        return True

    @classmethod
    def _row_priority_score(
        cls,
        row: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        hot_sectors: set[str],
        cold_sectors: set[str],
        active_factors: list[str],
        allocation_item: dict[str, Any] | None = None,
        scoring_context: dict[str, Any] | None = None,
    ) -> float:
        components = cls._row_priority_components(
            row,
            snapshot=snapshot,
            hot_sectors=hot_sectors,
            cold_sectors=cold_sectors,
            active_factors=active_factors,
            allocation_item=allocation_item,
            scoring_context=scoring_context,
        )
        return round(sum(cls._safe_float(value) for value in components.values()), 4)

    @classmethod
    def _row_priority_components(
        cls,
        row: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        hot_sectors: set[str],
        cold_sectors: set[str],
        active_factors: list[str],
        allocation_item: dict[str, Any] | None = None,
        scoring_context: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        market_cap = cls._safe_float(row.get("market_cap"))
        industry = str(row.get("industry") or row.get("sector") or "").strip()
        resolved_context = dict(scoring_context or {})
        normalized_active_factors = [
            str(item or "").strip().lower()
            for item in list(
                resolved_context.get("normalized_active_factors")
                or active_factors
                or []
            )
            if str(item or "").strip()
        ]
        preferred_families = [
            str(item or "").strip().lower()
            for item in list(resolved_context.get("preferred_families") or [])
            if str(item or "").strip()
        ]
        components: dict[str, float] = {
            "size_score": 0.0,
            "valuation_score": 0.0,
            "sector_regime_score": 0.0,
            "factor_alignment_score": 0.0,
            "allocation_score": 0.0,
            # PR-S19：画像驱动维度
            "stock_profile_score": 0.0,
            "profile_quality_score": 0.0,
        }
        size_logs = list(resolved_context.get("size_logs") or [])
        if market_cap > 0:
            if size_logs:
                size_pct = cls._percentile_from_sorted(
                    size_logs,
                    math.log(max(market_cap, 1.0)),
                    higher_is_better=True,
                )
            else:
                normalized_log = math.log10(market_cap / 1e8 + 1.0)
                size_pct = max(0.0, min(normalized_log / 2.5, 1.0))
            components["size_score"] = round(size_pct * 12.0, 4)

        hot_sector_set = set(resolved_context.get("hot_sectors") or hot_sectors or set())
        cold_sector_set = set(resolved_context.get("cold_sectors") or cold_sectors or set())
        hot_sector_coverage = dict(resolved_context.get("hot_sector_coverage") or {})
        cold_sector_coverage = dict(resolved_context.get("cold_sector_coverage") or {})
        hot_score = cls._sector_regime_component_score(
            industry,
            sector_labels=hot_sector_set,
            label_coverage=hot_sector_coverage,
            base_points=6.0,
        )
        cold_score = cls._sector_regime_component_score(
            industry,
            sector_labels=cold_sector_set,
            label_coverage=cold_sector_coverage,
            base_points=4.0,
        )
        if hot_score > 0.0:
            components["sector_regime_score"] += hot_score
        if cold_score > 0.0:
            components["sector_regime_score"] -= cold_score

        pe_ratio = cls._safe_float(row.get("pe_ratio"))
        pb_ratio = cls._safe_float(row.get("pb_ratio"))
        if cls._factor_signal_enabled(normalized_active_factors, "value") or cls._factor_signal_enabled(
            normalized_active_factors,
            "reversal",
        ):
            industry_key = cls._industry_key(industry)
            pe_values = list(
                dict(resolved_context).get("valuation_pe_by_industry", {}).get(industry_key)
                or []
            )
            pb_values = list(
                dict(resolved_context).get("valuation_pb_by_industry", {}).get(industry_key)
                or []
            )
            if len(pe_values) < 20:
                pe_values = list(resolved_context.get("valuation_pe_global") or [])
            if len(pb_values) < 20:
                pb_values = list(resolved_context.get("valuation_pb_global") or [])
            valuation_percentiles: list[float] = []
            if pe_ratio > 0:
                valuation_percentiles.append(
                    cls._percentile_from_sorted(
                        pe_values,
                        pe_ratio,
                        higher_is_better=False,
                    )
                )
            if pb_ratio > 0:
                valuation_percentiles.append(
                    cls._percentile_from_sorted(
                        pb_values,
                        pb_ratio,
                        higher_is_better=False,
                    )
                )
            if valuation_percentiles:
                components["valuation_score"] = round(
                    (sum(valuation_percentiles) / len(valuation_percentiles)) * 10.0,
                    4,
                )

        if not preferred_families:
            for factor_name in normalized_active_factors:
                for family in preferred_strategy_types_for_factor(factor_name, default=[]):
                    normalized_family = str(family or "").strip().lower()
                    if normalized_family and normalized_family not in preferred_families:
                        preferred_families.append(normalized_family)
        projected_families = cls._families_for_row(
            row,
            snapshot=snapshot,
            hot_sectors=hot_sector_set,
            cold_sectors=cold_sector_set,
            active_factors=normalized_active_factors,
            allocation_item=None,
        )
        if preferred_families and projected_families:
            preferred_rank = {
                family: index + 1
                for index, family in enumerate(preferred_families)
                if family
            }
            projected_weight_total = sum(1.0 / (index + 1) for index in range(len(projected_families)))
            weighted_overlap = 0.0
            for projected_index, family in enumerate(projected_families, 1):
                preferred_index = preferred_rank.get(family)
                if not preferred_index:
                    continue
                weighted_overlap += (1.0 / projected_index) * (1.0 / preferred_index)
            overlap_ratio = weighted_overlap / max(projected_weight_total, 1e-9)
            components["factor_alignment_score"] = round(max(0.0, min(overlap_ratio, 1.0)) * 8.0, 4)

        allocation_priority = max(0.0, min(cls._safe_float((allocation_item or {}).get("priority")), 1.0))
        allocation_priorities = list(resolved_context.get("allocation_priorities") or [])
        if allocation_priority > 0.0 and allocation_priorities:
            allocation_pct = cls._percentile_from_sorted(
                allocation_priorities,
                allocation_priority,
                higher_is_better=True,
            )
        else:
            allocation_pct = 0.5
        components["allocation_score"] = round((allocation_pct - 0.5) * 8.0, 4)

        # PR-S19：画像分量 - 让 stock_profile 维度真正参与排序
        # 使用 dimension_scores 的关键维度按权重叠加，与现有几个 component 保持同量级。
        profile_summary = cls._extract_profile_summary(row)
        profile_score = 0.0
        profile_quality_score = 0.0
        if profile_summary:
            scores = cls._profile_dimension_scores(profile_summary)
            quality = str(profile_summary.get("profile_quality") or "").lower()
            quality_factor = {"good": 1.0, "partial": 0.7, "low_confidence": 0.4, "failed": 0.0}.get(
                quality, 0.5
            )
            # 与维度对应的权重，覆盖率缺失的维度自动 0
            weighted = (
                scores.get("quality", 0.0) * 1.6
                + scores.get("valuation", 0.0) * 1.4
                + scores.get("trend", 0.0) * 1.2
                + scores.get("growth", 0.0) * 1.0
                + scores.get("volume", 0.0) * 0.6
                + scores.get("reversal", 0.0) * 0.4
                - scores.get("risk", 0.0) * 0.4
            )
            profile_score = round(weighted * quality_factor, 4)
            profile_quality_score = round(quality_factor * 2.0, 4)
        components["stock_profile_score"] = profile_score
        components["profile_quality_score"] = profile_quality_score

        return {key: round(cls._safe_float(value), 4) for key, value in components.items()}
