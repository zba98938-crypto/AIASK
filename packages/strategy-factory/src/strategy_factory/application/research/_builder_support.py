"""Shared support mixin for factor research builder helper logic."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, List, Optional
from unittest.mock import AsyncMock, Mock

from ...domain.constants import (
    FACTORY_BACKLOG_RELAX_ENABLED,
    FACTORY_BACKLOG_RELAX_WEIGHT_MULTIPLIER,
    preferred_strategy_types_for_factor,
)
from .._budget_feedback import (
    is_relaxable_feedback_backlog_control,
    resolve_feedback_metrics,
    resolve_relaxed_research_control_mode,
)


class FactorResearchBuilderSupportMixin:
    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return float(default)

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    @classmethod
    def _normalize_trend(cls, value: Any) -> str:
        trend = str(value or "flat").strip().lower()
        return trend if trend in {"rising", "falling", "flat"} else "flat"

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        raw = str(value or "").strip()
        if not raw:
            return None
        for parser in (
            date.fromisoformat,
            lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")).date(),
        ):
            try:
                return parser(raw)
            except Exception:
                continue
        return None

    @classmethod
    def _days_since(cls, value: Optional[date], *, reference_date: Optional[date] = None) -> Optional[int]:
        if value is None:
            return None
        try:
            baseline = reference_date or date.today()
            return max((baseline - value).days, 0)
        except Exception:
            return None

    @classmethod
    def _preferred_types_for_factor(cls, factor_name: str) -> List[str]:
        return preferred_strategy_types_for_factor(factor_name)

    @classmethod
    def _default_family_preference_order(cls, snapshot: dict[str, Any]) -> List[str]:
        fear_greed = cls._safe_float(snapshot.get("fear_greed_index") or 50.0)
        if fear_greed >= 60:
            return ["momentum", "growth_factor", "ma_cross", "quality_factor"]
        if fear_greed <= 40:
            return ["rsi", "value_factor", "quality_factor", "ma_cross"]
        return ["ma_cross", "quality_factor", "multi_factor", "value_factor"]

    @classmethod
    def _build_family_preference_order(
        cls,
        snapshot: dict[str, Any],
        *,
        preferred_strategy_types: List[str],
        allocation_family_counts: Optional[dict[str, Any]] = None,
    ) -> List[str]:
        ordered: list[str] = []
        preferred_rank = {
            str(item or "").strip().lower(): index
            for index, item in enumerate(list(preferred_strategy_types or []))
            if str(item or "").strip()
        }

        def add(items: List[str]) -> None:
            for item in list(items or []):
                family = str(item or "").strip().lower()
                if family and family not in ordered:
                    ordered.append(family)

        if isinstance(allocation_family_counts, dict) and allocation_family_counts:
            ranked_allocation_families = [
                str(family or "").strip().lower()
                for family, _count in sorted(
                    allocation_family_counts.items(),
                    key=lambda item: (
                        -cls._safe_int(item[1]),
                        preferred_rank.get(
                            str(item[0] or "").strip().lower(),
                            len(preferred_rank) + 100,
                        ),
                        str(item[0] or "").strip().lower(),
                    ),
                )
                if str(family or "").strip()
            ]
            add(ranked_allocation_families)
        add(preferred_strategy_types)
        add(cls._default_family_preference_order(snapshot))
        return ordered

    @staticmethod
    def _family_preference_source_mode(
        *,
        family_preference_order: List[str],
        preferred_strategy_types: List[str],
        allocation_family_counts: Optional[dict[str, Any]] = None,
        feedback_routed: bool = False,
    ) -> str:
        if feedback_routed:
            return "feedback_router"
        if isinstance(allocation_family_counts, dict) and bool(allocation_family_counts):
            return "stock_family_allocation"
        if family_preference_order and preferred_strategy_types:
            return "preferred_strategy_types"
        return "fear_greed_base_order"

    @staticmethod
    def _merge_ranked_families(*family_lists: List[str]) -> List[str]:
        merged: list[str] = []
        for items in family_lists:
            for item in list(items or []):
                family = str(item or "").strip().lower()
                if family and family not in merged:
                    merged.append(family)
        return merged

    @staticmethod
    def _normalize_codes(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        if not text:
            return []
        return [item.strip() for item in text.split(",") if item.strip()]

    @staticmethod
    def _has_concrete_async_method(target: Any, method_name: str) -> bool:
        method = getattr(target, method_name, None)
        if method is None or not callable(method):
            return False
        if isinstance(method, AsyncMock):
            return True
        if isinstance(method, Mock):
            return False
        return True

    @classmethod
    def _should_use_lightweight_mock_fallback(cls, db: Any, snapshot: dict[str, Any]) -> bool:
        raw_db = db
        candidate_raw = getattr(db, "raw", None)
        if candidate_raw is not None and not (isinstance(db, Mock) and "raw" not in getattr(db, "__dict__", {})):
            raw_db = candidate_raw
        if not isinstance(raw_db, Mock):
            return False
        if cls._has_concrete_async_method(raw_db, "get_factor_ic_history"):
            return False
        if cls._has_concrete_async_method(raw_db, "list_stock_universe"):
            return False
        if dict(snapshot.get("factor_ic") or {}):
            return False
        if dict(snapshot.get("factor_ic_trend") or {}):
            return False
        if cls._normalize_codes(snapshot.get("candidate_codes")):
            return False
        return True

    @classmethod
    def _history_summary(cls, rows: List[dict[str, Any]]) -> dict[str, Any]:
        history = [dict(item or {}) for item in list(rows or []) if isinstance(item, dict)]
        ic_values = [
            cls._safe_float(item.get("ic_value"))
            for item in history
            if item.get("ic_value") is not None
        ]
        latest_date = None
        if history:
            latest_date = cls._parse_date(history[0].get("ic_date"))
        recent_mean_5 = round(sum(ic_values[:5]) / max(len(ic_values[:5]), 1), 6) if ic_values else 0.0
        baseline_slice = ic_values[5:10] if len(ic_values) >= 10 else ic_values[5:]
        baseline_mean = round(sum(baseline_slice) / len(baseline_slice), 6) if baseline_slice else recent_mean_5
        delta = round(recent_mean_5 - baseline_mean, 6)
        latest_value = cls._safe_float(ic_values[0]) if ic_values else 0.0
        stability_tag = "insufficient_history"
        if len(ic_values) >= 10 and recent_mean_5 * baseline_mean < 0:
            stability_tag = "regime_flip"
        elif len(ic_values) >= 8 and abs(delta) <= 0.005:
            stability_tag = "stable"
        elif delta > 0.005:
            stability_tag = "improving"
        elif delta < -0.005:
            stability_tag = "weakening"
        elif ic_values:
            stability_tag = "short_history"
        decay_flag = bool(delta < -0.01 or (baseline_mean > 0.0 and recent_mean_5 <= 0.0))
        return {
            "history_count": len(ic_values),
            "latest_ic_date": latest_date.isoformat() if latest_date else None,
            "latest_ic_value": round(latest_value, 6),
            "recent_mean_5": recent_mean_5,
            "baseline_mean_5": baseline_mean,
            "delta_vs_baseline": delta,
            "stability_tag": stability_tag,
            "decay_flag": decay_flag,
        }

    @staticmethod
    def _family_risk_level(family: str) -> str:
        normalized = str(family or "").strip().lower()
        if normalized in {"momentum", "growth_factor", "volatility_breakout", "event_structure_breakout", "gap_fill"}:
            return "high"
        if normalized in {"quality_factor", "value_factor"}:
            return "low"
        return "medium"

    @classmethod
    def _family_validation_profile(cls, family: str) -> dict[str, Any]:
        normalized = str(family or "").strip().lower()
        validation_focus = "candidate_target_only"
        if normalized == "macro_timing":
            profile = "macro_regime_validation"
        elif normalized in {"north_capital_track", "margin_divergence", "event_structure_breakout"}:
            profile = "event_trade_validation"
            validation_focus = "event_target_only"
        elif normalized == "quality_factor":
            profile = "trade_rule_validation"
        elif normalized in {"value_factor", "quality_factor", "growth_factor", "multi_factor", "sentiment", "sentiment_factor"}:
            profile = "factor_rank_validation"
        else:
            profile = "trade_rule_validation"
        return {
            "profile": profile,
            "validation_focus": validation_focus,
            "primary_validation_layer": "target" if validation_focus in {"candidate_target_only", "event_target_only"} else "combined",
        }

    @classmethod
    def _family_failure_penalty(cls, family: str, *, family_rank: int) -> float:
        risk_level = cls._family_risk_level(family)
        base_penalty = {
            "high": 0.22,
            "medium": 0.14,
            "low": 0.08,
        }.get(risk_level, 0.14)
        return round(min(base_penalty + max(family_rank - 1, 0) * 0.03, 0.45), 4)

    @classmethod
    def _family_budget_weights(
        cls,
        selected: List[str],
        *,
        priority: float,
        budget_feedback_root: Any = None,
    ) -> List[float]:
        raw_weights: List[float] = []
        for family_rank, family in enumerate(selected, 1):
            rank_multiplier = max(0.45, 1.0 - (family_rank - 1) * 0.2)
            feedback_metrics = resolve_feedback_metrics(
                budget_feedback_root,
                family=family,
            )
            budget_multiplier = cls._safe_float(
                feedback_metrics.get("budget_multiplier"),
                1.0,
            )
            if (
                FACTORY_BACKLOG_RELAX_ENABLED
                and budget_multiplier <= 0.0
                and str(feedback_metrics.get("control_mode") or "").strip().lower() in {"suppress", "freeze"}
                and is_relaxable_feedback_backlog_control(feedback_metrics)
            ):
                budget_multiplier = FACTORY_BACKLOG_RELAX_WEIGHT_MULTIPLIER
            risk_level = cls._family_risk_level(family)
            risk_multiplier = {
                "high": 0.9,
                "medium": 1.0,
                "low": 1.05,
            }.get(risk_level, 1.0)
            if budget_multiplier <= 0.0:
                raw_weights.append(0.0)
                continue
            raw_weights.append(
                max(0.0, float(priority or 0.0) * rank_multiplier * risk_multiplier * budget_multiplier)
            )
        total = sum(raw_weights)
        if total <= 0.0:
            return [0.0 for _ in selected]
        normalized_weights: List[float] = []
        accumulated = 0.0
        for index, raw_weight in enumerate(raw_weights, 1):
            if index == len(raw_weights):
                weight = round(max(0.0, 1.0 - accumulated), 4)
            else:
                weight = round(raw_weight / total, 4)
                accumulated += weight
            normalized_weights.append(weight)
        return normalized_weights

    @classmethod
    def _build_family_plans(
        cls,
        families: List[str],
        *,
        priority: float,
        budget_feedback_root: Any = None,
    ) -> List[dict[str, Any]]:
        selected = [
            str(item or "").strip().lower()
            for item in list(families or [])
            if str(item or "").strip()
        ]
        if not selected:
            return []
        budget_weights = cls._family_budget_weights(
            selected,
            priority=priority,
            budget_feedback_root=budget_feedback_root,
        )
        plans: List[dict[str, Any]] = []
        for family_rank, family in enumerate(selected, 1):
            budget_weight = budget_weights[family_rank - 1] if family_rank - 1 < len(budget_weights) else 0.0
            feedback_metrics = resolve_feedback_metrics(
                budget_feedback_root,
                family=family,
            )
            feedback_control_mode = str(feedback_metrics.get("control_mode") or "normal")
            feedback_control_relaxed = False
            if (
                FACTORY_BACKLOG_RELAX_ENABLED
                and budget_weight > 0.0
                and str(feedback_control_mode).strip().lower() in {"suppress", "freeze"}
                and is_relaxable_feedback_backlog_control(feedback_metrics)
            ):
                feedback_control_relaxed = True
                feedback_control_mode = resolve_relaxed_research_control_mode(
                    {"task_source": "bulk_stock_matrix"}
                )
            feedback_penalty_adjustment = cls._safe_float(
                feedback_metrics.get("failure_penalty_adjustment")
            )
            plans.append(
                {
                    "family": family,
                    "family_rank": family_rank,
                    "budget": budget_weight,
                    "budget_weight": budget_weight,
                    "failure_penalty": round(
                        min(
                            max(
                                cls._family_failure_penalty(family, family_rank=family_rank)
                                + feedback_penalty_adjustment,
                                0.0,
                            ),
                            0.9,
                        ),
                        4,
                    ),
                    "validation_profile": cls._family_validation_profile(family),
                    "feedback_metrics": feedback_metrics,
                    "feedback_budget_multiplier": cls._safe_float(
                        feedback_metrics.get("budget_multiplier")
                    ),
                    "feedback_priority_adjustment": cls._safe_float(
                        feedback_metrics.get("priority_adjustment")
                    ),
                    "feedback_failure_penalty_adjustment": feedback_penalty_adjustment,
                    "feedback_control_mode": feedback_control_mode,
                    "feedback_control_original_mode": str(feedback_metrics.get("control_mode") or "normal"),
                    "feedback_control_relaxed_mode": feedback_control_mode if feedback_control_relaxed else None,
                    "feedback_control_reasons": list(feedback_metrics.get("control_reasons") or []),
                    "feedback_cooldown_active": bool(feedback_metrics.get("cooldown_active")),
                    "feedback_suppressed": bool(feedback_metrics.get("suppressed")),
                    "feedback_family_freeze_active": bool(feedback_metrics.get("family_freeze_active")),
                    "feedback_control_relaxed": bool(feedback_control_relaxed),
                }
            )
        plans.sort(
            key=lambda item: (
                int(item.get("family_rank") or 0),
                str(item.get("family") or ""),
            )
        )
        return plans
