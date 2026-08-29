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


class _MatrixNormalizersCoreMixin:
    """Plan per-stock strategy-family tasks for bulk autonomy generation."""

    _MIN_HISTORY_BARS = 250
    _HISTORY_COUNT_CHUNK_SIZE = 400
    _HIGH_CONFLICT_FAMILY_SHARE_CAP = 0.2
    _HIGH_CONFLICT_FAMILY_ABS_CAP = 4
    def __init__(self) -> None:
        self.last_report: dict[str, Any] = {
            "summary": {
                "enabled": bool(_matrix_const.STOCK_STRATEGY_MATRIX_ENABLED),
                "task_count": 0,
                "stock_count": 0,
                "eligible_stock_count": 0,
                "loaded_stock_count": 0,
                "pages_loaded": 0,
                "analysis_complete": False,
                "analysis_stock_coverage_ratio": 0.0,
                "family_counts": {},
                "planned_family_counts": {},
                "universe_limit": _matrix_const.STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
                "requested_universe_offset": 0,
                "effective_universe_offset": 0,
                "universe_offset_fallback": False,
                "next_universe_offset": 0,
                "cursor_wrapped": False,
                "cursor_mode": "task_offset",
                "requested_task_offset": 0,
                "effective_task_offset": 0,
                "task_offset_fallback": False,
                "next_task_offset": 0,
                "task_cursor_wrapped": False,
                "max_tasks_per_run": _matrix_const.STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN,
                "max_candidates_per_run": _matrix_const.STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN,
                "generation_limit_per_task": STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK,
                "effective_task_budget": 0,
                "estimated_candidate_count": 0,
                "planned_task_count": 0,
                "planned_candidate_count": 0,
                "batch_size": STOCK_STRATEGY_MATRIX_BATCH_SIZE,
                "batch_count": 0,
                "selected_batch_count": 0,
                "batch_task_counts": {},
                "tasks_per_shard": STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD,
                "shard_count": 0,
                "selected_shard_count": 0,
                "selected_shard_ids": [],
                "stock_coverage_ratio": 0.0,
                "allocation_mode": "stock_round_robin_by_family_rank",
                "allocation_pass_counts": {},
                "planned_allocation_pass_counts": {},
                "overflow_task_count": 0,
                "stock_family_allocation_count": 0,
                "stock_family_allocation_applied_count": 0,
                "stock_family_allocation_coverage_ratio": 0.0,
                "min_history_bars": self._MIN_HISTORY_BARS,
                "history_prefilter_applied": False,
                "insufficient_history_filtered_count": 0,
            },
            "tasks": [],
        }

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    @classmethod
    def _runtime_flags_for_snapshot(cls, snapshot: dict[str, Any] | None) -> dict[str, bool]:
        payload = dict(snapshot or {})
        execution_mode = str(
            payload.get("factory_execution_mode")
            or payload.get("execution_mode")
            or ""
        ).strip()
        if not execution_mode:
            return {}
        try:
            return dict(resolve_runtime_mode_flags(execution_mode) or {})
        except Exception:
            return {}

    @classmethod
    def _effective_stock_matrix_enabled(cls, snapshot: dict[str, Any] | None) -> bool:
        flags = cls._runtime_flags_for_snapshot(snapshot)
        return bool(_matrix_const.STOCK_STRATEGY_MATRIX_ENABLED or flags.get("stock_first_observe_mode"))

    @classmethod
    def _effective_router_enabled(cls, snapshot: dict[str, Any] | None) -> bool:
        flags = cls._runtime_flags_for_snapshot(snapshot)
        return bool(_matrix_const.STOCK_FIRST_ROUTER_ENABLED or flags.get("router_enabled"))

    @classmethod
    def _effective_router_strict(cls, snapshot: dict[str, Any] | None) -> bool:
        flags = cls._runtime_flags_for_snapshot(snapshot)
        return bool(_matrix_const.STOCK_FIRST_ROUTER_STRICT or flags.get("router_strict"))

    @staticmethod
    def _normalize_factor_names(snapshot: dict[str, Any]) -> list[str]:
        factor_research = dict(snapshot.get("factor_research") or {})
        summary = dict(factor_research.get("summary") or {})
        names = [
            str(item).strip()
            for item in list(
                factor_research.get("active_factors")
                or summary.get("active_factors")
                or summary.get("top_factor_names")
                or []
            )
            if str(item).strip()
        ]
        return list(dict.fromkeys(names))[:6]

    @classmethod
    def _default_validation_profile_for_family(
        cls,
        family: str,
        *,
        validation_focus: str = "candidate_target_only",
    ) -> dict[str, Any]:
        normalized_family = str(family or "").strip().lower()
        normalized_focus = str(validation_focus or "candidate_target_only").strip().lower() or "candidate_target_only"
        if normalized_family == "macro_timing":
            profile = "macro_regime_validation"
        elif normalized_focus == "event_target_only" or normalized_family in {"north_capital_track", "margin_divergence"}:
            profile = "event_trade_validation"
            normalized_focus = "event_target_only"
        elif normalized_family == "quality_factor" and normalized_focus == "candidate_target_only":
            profile = "trade_rule_validation"
        elif normalized_family in {"value_factor", "quality_factor", "growth_factor", "multi_factor", "sentiment", "sentiment_factor"}:
            profile = "factor_rank_validation"
        else:
            profile = "trade_rule_validation"
        return {
            "profile": profile,
            "validation_focus": normalized_focus,
            "primary_validation_layer": "target" if normalized_focus in {"candidate_target_only", "event_target_only"} else "combined",
        }

    @classmethod
    def _normalize_validation_profile(
        cls,
        family: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile = dict(payload or {})
        default_profile = cls._default_validation_profile_for_family(
            family,
            validation_focus=str(profile.get("validation_focus") or "candidate_target_only"),
        )
        normalized_profile = str(profile.get("profile") or default_profile.get("profile") or "").strip().lower()
        validation_focus = str(
            profile.get("validation_focus")
            or default_profile.get("validation_focus")
            or "candidate_target_only"
        ).strip().lower() or "candidate_target_only"
        if str(family or "").strip().lower() == "quality_factor" and validation_focus == "candidate_target_only":
            normalized_profile = "trade_rule_validation"
        if not normalized_profile:
            normalized_profile = str(
                cls._default_validation_profile_for_family(
                    family,
                    validation_focus=validation_focus,
                ).get("profile")
                or "trade_rule_validation"
            )
        primary_layer = str(
            profile.get("primary_validation_layer")
            or default_profile.get("primary_validation_layer")
            or ("target" if validation_focus in {"candidate_target_only", "event_target_only"} else "combined")
        ).strip().lower() or "target"
        return {
            "profile": normalized_profile,
            "validation_focus": validation_focus,
            "primary_validation_layer": primary_layer,
        }

    @staticmethod
    def _default_failure_penalty_for_family(family: str, *, family_rank: int) -> float:
        normalized_family = str(family or "").strip().lower()
        if normalized_family == "mean_reversion_short":
            base_penalty = 0.28
        elif normalized_family in {"momentum", "growth_factor", "volatility_breakout", "gap_fill"}:
            base_penalty = 0.22
        elif normalized_family in {"quality_factor", "value_factor"}:
            base_penalty = 0.08
        else:
            base_penalty = 0.14
        return round(min(base_penalty + max(family_rank - 1, 0) * 0.03, 0.45), 4)

    @classmethod
    def _family_task_cap(cls, family: str, *, effective_task_budget: int) -> int | None:
        normalized_family = str(family or "").strip().lower()
        if normalized_family != "mean_reversion_short":
            return None
        dynamic_cap = int(math.ceil(max(1, int(effective_task_budget or 1)) * cls._HIGH_CONFLICT_FAMILY_SHARE_CAP))
        return max(1, min(cls._HIGH_CONFLICT_FAMILY_ABS_CAP, dynamic_cap))

    @classmethod
    def _apply_family_pressure_caps(
        cls,
        tasks: list[dict[str, Any]],
        *,
        effective_task_budget: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        if not tasks:
            return [], {}

        kept: list[dict[str, Any]] = []
        family_counts: dict[str, int] = {}
        family_caps: dict[str, int] = {}
        for raw_task in tasks:
            task = dict(raw_task or {})
            family = str(task.get("candidate_family") or "").strip().lower()
            cap = cls._family_task_cap(family, effective_task_budget=effective_task_budget)
            if cap is not None:
                family_caps[family] = cap
                if int(family_counts.get(family) or 0) >= cap:
                    continue
            kept.append(task)
            if family:
                family_counts[family] = family_counts.get(family, 0) + 1
        return kept, family_caps

    @classmethod
    def _default_family_plans(
        cls,
        families: list[str],
        *,
        priority: float,
    ) -> list[dict[str, Any]]:
        selected = [
            str(item or "").strip().lower()
            for item in list(families or [])
            if str(item or "").strip()
        ][: max(1, STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)]
        if not selected:
            return []
        raw_weights: list[float] = []
        for family_rank, _family in enumerate(selected, 1):
            rank_multiplier = max(0.4, 1.0 - (family_rank - 1) * 0.2)
            raw_weights.append(max(0.05, max(priority, 0.35) * rank_multiplier))
        total = sum(raw_weights) or float(len(selected))
        plans: list[dict[str, Any]] = []
        allocated = 0.0
        for family_rank, family in enumerate(selected, 1):
            if family_rank == len(selected):
                budget_weight = round(max(0.0, 1.0 - allocated), 4)
            else:
                budget_weight = round(raw_weights[family_rank - 1] / total, 4)
                allocated += budget_weight
            plans.append(
                {
                    "family": family,
                    "family_rank": family_rank,
                    "budget": budget_weight,
                    "budget_weight": budget_weight,
                    "failure_penalty": cls._default_failure_penalty_for_family(family, family_rank=family_rank),
                    "validation_profile": cls._normalize_validation_profile(family),
                }
            )
        return plans

    @classmethod
    def _research_family_preference_order(cls, snapshot: dict[str, Any]) -> list[str]:
        factor_research = dict(snapshot.get("factor_research") or {})
        summary = dict(factor_research.get("summary") or {})
        ordered: list[str] = []
        for source in (
            factor_research.get("family_preference_order"),
            summary.get("family_preference_order"),
        ):
            for item in list(source or []):
                family = str(item or "").strip().lower()
                if family and family not in ordered:
                    ordered.append(family)
        return ordered

    @classmethod
    def _family_preference_source(cls, snapshot: dict[str, Any]) -> str:
        factor_research = dict(snapshot.get("factor_research") or {})
        summary = dict(factor_research.get("summary") or {})
        if cls._research_family_preference_order(snapshot):
            return (
                str(
                    factor_research.get("family_preference_source_mode")
                    or summary.get("family_preference_source_mode")
                    or "factor_research"
                ).strip()
                or "factor_research"
            )
        return "fear_greed_base_order"

    @classmethod
    def _base_family_order(cls, snapshot: dict[str, Any]) -> list[str]:
        research_order = cls._research_family_preference_order(snapshot)
        if research_order:
            return research_order
        fg = cls._safe_float(snapshot.get("fear_greed_index") or 50.0)
        if fg >= 60:
            return ["momentum", "growth_factor", "ma_cross", "quality_factor"]
        if fg <= 40:
            return ["rsi", "value_factor", "quality_factor", "ma_cross"]
        return ["ma_cross", "quality_factor", "multi_factor", "value_factor"]

    @staticmethod
    def _industry_key(value: Any) -> str:
        token = str(value or "").strip()
        return token or "__unknown__"

    _DIRECTION_GATE_FALLBACK_FAMILY = "multi_factor"
    _TREND_DIRECTION_FAMILIES = frozenset(
        {
            "momentum",
            "ma_cross",
            "growth_factor",
            "volatility_breakout",
            "event_structure_breakout",
            "breakout",
            "sector_breakout",
            "rotation_balanced",
        }
    )
    _REVERSAL_DIRECTION_FAMILIES = frozenset({"mean_reversion_short", "gap_fill"})

    @staticmethod
    def _set_router_status(
        row: dict[str, Any],
        *,
        status: str,
        enabled: bool | None = None,
        strict: bool | None = None,
        reason: str | None = None,
        families: list[str] | None = None,
        holding_bucket: str | None = None,
        confidence: float | None = None,
        exclusions: list[str] | None = None,
        error_type: str | None = None,
    ) -> None:
        payload = {
            "enabled": bool(_matrix_const.STOCK_FIRST_ROUTER_ENABLED if enabled is None else enabled),
            "strict": bool(_matrix_const.STOCK_FIRST_ROUTER_STRICT if strict is None else strict),
            "status": str(status or "unknown").strip().lower() or "unknown",
            "reason": str(reason or "").strip() or None,
            "families": list(families or []),
            "holding_bucket": str(holding_bucket or "").strip() or None,
            "confidence": confidence,
            "exclusions": list(exclusions or []),
            "error_type": str(error_type or "").strip() or None,
            "lightweight_profile_generated": bool(row.get("_stock_first_router_lightweight_profile_generated")),
        }
        row["_stock_first_router"] = {key: value for key, value in payload.items() if value not in (None, "", [])}

    @classmethod
    def _router_telemetry_for_rows(
        cls,
        rows: list[dict[str, Any]],
        *,
        selected_tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        router_rows = [row for row in list(rows or []) if str((row or {}).get("code") or "").strip()]
        status_counts: dict[str, int] = {}
        fallback_reason_counts: dict[str, int] = {}
        family_counts: dict[str, int] = {}
        bucket_counts: dict[str, int] = {}
        applied_count = 0
        present_count = 0
        generated_count = 0
        enabled_seen: list[bool] = []
        strict_seen: list[bool] = []
        for row in router_rows:
            if cls._extract_profile_summary(row):
                present_count += 1
            if bool(row.get("_stock_first_router_lightweight_profile_generated")):
                generated_count += 1
            status = dict(row.get("_stock_first_router") or {})
            if "enabled" in status:
                enabled_seen.append(bool(status.get("enabled")))
            if "strict" in status:
                strict_seen.append(bool(status.get("strict")))
            state = str(status.get("status") or "not_evaluated").strip().lower() or "not_evaluated"
            status_counts[state] = status_counts.get(state, 0) + 1
            if state == "applied":
                applied_count += 1
                for family in list(status.get("families") or []):
                    token = str(family or "").strip().lower()
                    if token:
                        family_counts[token] = family_counts.get(token, 0) + 1
                bucket = str(status.get("holding_bucket") or "").strip().lower()
                if bucket:
                    bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
            elif state in {"fallback", "blocked"}:
                reason = str(status.get("reason") or "unknown").strip().lower() or "unknown"
                fallback_reason_counts[reason] = fallback_reason_counts.get(reason, 0) + 1

        selected_router_applied_count = 0
        selected_profile_missing_count = 0
        selected_task_count = 0
        for task in list(selected_tasks or []):
            selected_task_count += 1
            router = dict(task.get("stock_first_router") or {})
            if str(router.get("status") or "").strip().lower() == "applied":
                selected_router_applied_count += 1
            if not task.get("stock_profile_summary"):
                selected_profile_missing_count += 1

        missing_count = max(0, len(router_rows) - present_count)
        return {
            "router_enabled": bool(_matrix_const.STOCK_FIRST_ROUTER_ENABLED or any(enabled_seen)),
            "router_strict": bool(_matrix_const.STOCK_FIRST_ROUTER_STRICT or any(strict_seen)),
            "router_telemetry_enabled": bool(STOCK_FIRST_ROUTER_TELEMETRY_ENABLED),
            "router_candidate_stock_count": len(router_rows),
            "router_applied_count": applied_count,
            "router_status_counts": status_counts,
            "router_fallback_reason_counts": fallback_reason_counts,
            "router_family_counts": family_counts,
            "router_holding_bucket_counts": bucket_counts,
            "profile_summary_present_count": present_count,
            "profile_summary_missing_count": missing_count,
            "profile_summary_generated_count": generated_count,
            "selected_task_count": selected_task_count,
            "selected_router_applied_count": selected_router_applied_count,
            "selected_profile_summary_missing_count": selected_profile_missing_count,
        }

    @staticmethod
    def _profile_dimension_scores(profile_summary: dict[str, Any]) -> dict[str, float]:
        scores = dict(profile_summary.get("factor_dimension_scores") or {})
        out: dict[str, float] = {}
        for key, value in scores.items():
            try:
                out[str(key).strip().lower()] = max(0.0, min(float(value), 1.0))
            except (TypeError, ValueError):
                continue
        return out
