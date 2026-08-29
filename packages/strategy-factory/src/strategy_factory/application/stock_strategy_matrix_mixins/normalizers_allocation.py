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


class _MatrixAllocationMixin:
    @classmethod
    def _normalize_stock_family_allocation(cls, snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
        factor_research = dict(snapshot.get("factor_research") or {})
        allocation = dict(factor_research.get("stock_family_allocation") or {})
        normalized: dict[str, dict[str, Any]] = {}
        for code, item in allocation.items():
            normalized_code = str(code or "").strip()
            payload = dict(item or {})
            priority = max(0.0, min(cls._safe_float(payload.get("priority")), 1.0))
            families = [
                str(family or "").strip().lower()
                for family in list(payload.get("families") or [])
                if str(family or "").strip()
            ]
            normalized_family_plans: list[dict[str, Any]] = []
            for index, raw_plan in enumerate(list(payload.get("family_plans") or []), 1):
                plan_payload = dict(raw_plan or {})
                family = str(plan_payload.get("family") or "").strip().lower()
                if not family:
                    continue
                family_rank = max(1, cls._safe_int(plan_payload.get("family_rank")) or index)
                normalized_family_plans.append(
                    {
                        "family": family,
                        "family_rank": family_rank,
                        "budget": max(
                            0.0,
                            min(
                                cls._safe_float(plan_payload.get("budget") or plan_payload.get("budget_weight")),
                                1.0,
                            ),
                        ),
                        "budget_weight": max(
                            0.0,
                            min(
                                cls._safe_float(plan_payload.get("budget_weight") or plan_payload.get("budget")),
                                1.0,
                            ),
                        ),
                        "failure_penalty": max(
                            0.0,
                            min(
                                cls._safe_float(plan_payload.get("failure_penalty")),
                                1.0,
                            ),
                        ),
                        "validation_profile": cls._normalize_validation_profile(
                            family,
                            dict(plan_payload.get("validation_profile") or {}),
                        ),
                    }
                )
            normalized_family_plans.sort(
                key=lambda plan: (
                    int(plan.get("family_rank") or 0),
                    str(plan.get("family") or ""),
                )
            )
            if normalized_family_plans:
                families = [
                    str(plan.get("family") or "").strip().lower()
                    for plan in normalized_family_plans
                    if str(plan.get("family") or "").strip()
                ]
            if not normalized_code or not families:
                continue
            default_family_plans = cls._default_family_plans(families, priority=priority or 0.5)
            default_plan_lookup = {
                str(plan.get("family") or "").strip().lower(): dict(plan or {})
                for plan in default_family_plans
                if str(plan.get("family") or "").strip()
            }
            if not normalized_family_plans:
                normalized_family_plans = default_family_plans
            else:
                resolved_plans: list[dict[str, Any]] = []
                seen_families: set[str] = set()
                for plan in normalized_family_plans:
                    family = str(plan.get("family") or "").strip().lower()
                    if not family or family in seen_families:
                        continue
                    seen_families.add(family)
                    fallback = dict(default_plan_lookup.get(family) or {})
                    budget_weight = cls._safe_float(plan.get("budget_weight") or plan.get("budget"))
                    if budget_weight <= 0.0:
                        budget_weight = cls._safe_float(fallback.get("budget_weight") or fallback.get("budget"))
                    failure_penalty = cls._safe_float(plan.get("failure_penalty"))
                    if failure_penalty <= 0.0:
                        failure_penalty = cls._safe_float(fallback.get("failure_penalty"))
                    resolved_plans.append(
                        {
                            "family": family,
                            "family_rank": max(1, cls._safe_int(plan.get("family_rank")) or len(resolved_plans) + 1),
                            "budget": max(0.0, min(budget_weight, 1.0)),
                            "budget_weight": max(0.0, min(budget_weight, 1.0)),
                            "failure_penalty": max(0.0, min(failure_penalty, 1.0)),
                            "validation_profile": cls._normalize_validation_profile(
                                family,
                                dict(plan.get("validation_profile") or fallback.get("validation_profile") or {}),
                            ),
                        }
                    )
                normalized_family_plans = resolved_plans
            normalized_family_plans = normalized_family_plans[: max(1, STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)]
            families = [
                str(plan.get("family") or "").strip().lower()
                for plan in normalized_family_plans
                if str(plan.get("family") or "").strip()
            ]
            normalized[normalized_code] = {
                "families": list(dict.fromkeys(families))[: max(1, STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK)],
                "family_plans": normalized_family_plans,
                "priority": priority,
                "source_mode": str(payload.get("source_mode") or "").strip() or None,
            }
            industry = str(payload.get("industry") or "").strip()
            if industry:
                normalized[normalized_code]["industry"] = industry
            if normalized_family_plans:
                normalized[normalized_code]["top_family"] = normalized_family_plans[0]["family"]
                normalized[normalized_code]["top_validation_profile"] = (
                    dict(normalized_family_plans[0].get("validation_profile") or {}).get("profile")
                )
        return normalized

    @classmethod
    def _direction_gate_enabled(cls) -> bool:
        if not bool(_matrix_const.STOCK_DIRECTION_GATE_ENABLED):
            return False
        try:
            return bool(stock_direction_gate_enabled())
        except Exception:
            return bool(_matrix_const.STOCK_DIRECTION_GATE_ENABLED)

    @staticmethod
    def _first_present_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key not in payload:
                continue
            value = payload.get(key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None

    @staticmethod
    def _record_direction_gate(
        row: dict[str, Any],
        *,
        enabled: bool,
        status: str,
        source: str,
        reason: str,
        input_families: list[str],
        output_families: list[str],
        dropped_families: list[str] | None = None,
        direction: str | None = None,
        trend_regime: str | None = None,
        trend_score: float | None = None,
        reversal_score: float | None = None,
        pct_chg: float | None = None,
        fallback_family: str | None = None,
    ) -> None:
        payload = {
            "enabled": bool(enabled),
            "status": str(status or "unknown").strip().lower() or "unknown",
            "source": str(source or "").strip().lower() or "unknown",
            "reason": str(reason or "").strip() or None,
            "direction": str(direction or "").strip().lower() or None,
            "trend_regime": str(trend_regime or "").strip().lower() or None,
            "trend_score": None if trend_score is None else round(float(trend_score), 4),
            "reversal_score": None if reversal_score is None else round(float(reversal_score), 4),
            "pct_chg": None if pct_chg is None else round(float(pct_chg), 4),
            "input_families": list(input_families or []),
            "output_families": list(output_families or []),
            "dropped_families": list(dropped_families or []),
            "fallback_family": str(fallback_family or "").strip().lower() or None,
        }
        row["_stock_direction_gate"] = {
            key: value
            for key, value in payload.items()
            if value not in (None, "", [])
        }

    @classmethod
    def _direction_gate_state(
        cls,
        *,
        profile_summary: dict[str, Any] | None,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        summary = dict(profile_summary or {})
        dims = dict(summary.get("factor_dimension_scores") or {})
        regime = dict(summary.get("regime") or {})
        trend_regime = str(regime.get("trend_regime") or "").strip().lower()
        trend_raw = dims.get("trend")
        reversal_raw = dims.get("reversal")
        pct_raw = cls._first_present_value(
            row,
            ("pct_chg", "change_pct", "daily_return", "return_1d"),
        )
        has_signal = any(
            (
                trend_raw is not None,
                reversal_raw is not None,
                pct_raw is not None,
                bool(trend_regime and trend_regime != "unknown"),
            )
        )
        trend_score = cls._safe_float(trend_raw) if trend_raw is not None else 0.0
        reversal_score = cls._safe_float(reversal_raw) if reversal_raw is not None else 0.0
        pct_chg = cls._safe_float(pct_raw) if pct_raw is not None else 0.0
        explicit_downtrend = trend_regime in {"trend_down", "downtrend", "down", "weak", "bearish"}
        explicit_uptrend = trend_regime in {"trend_up", "uptrend", "up", "strong", "bullish"}
        downtrend = explicit_downtrend or (
            trend_score < 0.15 and (reversal_score >= 0.30 or pct_chg < 0)
        )
        uptrend = not downtrend and (explicit_uptrend or trend_score >= 0.35)
        if downtrend:
            direction = "downtrend"
            reason = "downtrend_excludes_trend_families"
        elif uptrend:
            direction = "uptrend"
            reason = "uptrend_excludes_reversal_families"
        elif has_signal:
            direction = "neutral"
            reason = "no_direction_conflict"
        else:
            direction = "unknown"
            reason = "missing_direction_signal"
        return {
            "has_signal": has_signal,
            "direction": direction,
            "reason": reason,
            "trend_regime": trend_regime,
            "trend_score": trend_score if trend_raw is not None else None,
            "reversal_score": reversal_score if reversal_raw is not None else None,
            "pct_chg": pct_chg if pct_raw is not None else None,
        }

    @classmethod
    def _apply_direction_gate(
        cls,
        families: list[str],
        *,
        profile_summary: dict[str, Any] | None,
        row: dict[str, Any],
        source: str = "legacy",
    ) -> list[str]:
        normalized_families = [
            str(family or "").strip().lower()
            for family in list(families or [])
            if str(family or "").strip()
        ]
        if not normalized_families:
            return families
        enabled = cls._direction_gate_enabled()
        state = cls._direction_gate_state(profile_summary=profile_summary, row=row)
        if not enabled:
            cls._record_direction_gate(
                row,
                enabled=False,
                status="disabled",
                source=source,
                reason="toggle_disabled",
                input_families=normalized_families,
                output_families=normalized_families,
                direction=state.get("direction"),
                trend_regime=state.get("trend_regime"),
                trend_score=state.get("trend_score"),
                reversal_score=state.get("reversal_score"),
                pct_chg=state.get("pct_chg"),
            )
            return normalized_families
        if not bool(state.get("has_signal")):
            cls._record_direction_gate(
                row,
                enabled=True,
                status="skipped",
                source=source,
                reason=str(state.get("reason") or "missing_direction_signal"),
                input_families=normalized_families,
                output_families=normalized_families,
                direction=state.get("direction"),
            )
            return normalized_families

        direction = str(state.get("direction") or "").strip().lower()
        drop: set[str] = set()
        if direction == "downtrend":
            drop |= cls._TREND_DIRECTION_FAMILIES
        elif direction == "uptrend":
            drop |= cls._REVERSAL_DIRECTION_FAMILIES

        dropped = [family for family in normalized_families if family in drop]
        if not dropped:
            existing_status = dict(row.get("_stock_direction_gate") or {})
            if str(existing_status.get("status") or "").strip().lower() == "applied":
                return normalized_families
            cls._record_direction_gate(
                row,
                enabled=True,
                status="passed",
                source=source,
                reason=str(state.get("reason") or "no_direction_conflict"),
                input_families=normalized_families,
                output_families=normalized_families,
                direction=direction,
                trend_regime=state.get("trend_regime"),
                trend_score=state.get("trend_score"),
                reversal_score=state.get("reversal_score"),
                pct_chg=state.get("pct_chg"),
            )
            return normalized_families

        filtered = [family for family in normalized_families if family not in drop]
        fallback_family = None
        if not filtered:
            fallback_family = cls._DIRECTION_GATE_FALLBACK_FAMILY
            filtered = [fallback_family]
        cls._record_direction_gate(
            row,
            enabled=True,
            status="applied",
            source=source,
            reason=str(state.get("reason") or "direction_conflict"),
            input_families=normalized_families,
            output_families=filtered,
            dropped_families=dropped,
            direction=direction,
            trend_regime=state.get("trend_regime"),
            trend_score=state.get("trend_score"),
            reversal_score=state.get("reversal_score"),
            pct_chg=state.get("pct_chg"),
            fallback_family=fallback_family,
        )
        return filtered

    @classmethod
    def _normalize_sector_labels(
        cls,
        values: Any,
        *,
        limit: int | None = None,
    ) -> list[str]:
        return normalize_sector_labels(values, limit=limit)

    @classmethod
    def _sector_match_strength(
        cls,
        industry: Any,
        sector_labels: Any,
    ) -> float:
        return sector_match_strength(industry, sector_labels)

    @classmethod
    def _sector_family_biases(
        cls,
        industry: Any,
        *,
        mode: str = "intrinsic",
    ) -> list[str]:
        return sector_family_biases(industry, mode=mode)

    @classmethod
    def _build_sector_label_coverage(
        cls,
        rows: list[dict[str, Any]],
        *,
        sector_labels: set[str],
    ) -> dict[str, dict[str, float]]:
        normalized_labels = [
            str(item or "").strip()
            for item in list(sector_labels or set())
            if str(item or "").strip()
        ]
        if not normalized_labels:
            return {}
        universe_size = max(1, len(list(rows or [])))
        coverage: dict[str, dict[str, float]] = {}
        for label in normalized_labels:
            matched_count = 0
            matched_profile_keys: set[str] = set()
            for row in list(rows or []):
                industry = str(dict(row or {}).get("industry") or dict(row or {}).get("sector") or "").strip()
                if cls._sector_match_strength(industry, [label]) <= 0.0:
                    continue
                matched_count += 1
                for profile in sector_profiles_for_label(industry):
                    profile_key = str(profile.get("key") or "").strip()
                    if profile_key:
                        matched_profile_keys.add(profile_key)
            coverage_ratio = matched_count / float(universe_size)
            profile_breadth = max(1, len(matched_profile_keys))
            breadth_penalty = 1.0 / math.sqrt(float(profile_breadth))
            coverage_penalty = max(0.45, 1.0 - min(coverage_ratio, 0.35) * 1.4)
            coverage[label] = {
                "matched_count": float(matched_count),
                "coverage_ratio": round(coverage_ratio, 4),
                "matched_profile_count": float(profile_breadth),
                "breadth_penalty": round(breadth_penalty, 4),
                "coverage_penalty": round(coverage_penalty, 4),
                "effective_penalty": round(min(1.0, breadth_penalty * coverage_penalty), 4),
            }
        return coverage

    @classmethod
    def _direction_gate_telemetry_for_rows(
        cls,
        rows: list[dict[str, Any]],
        *,
        selected_tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        gate_rows = [row for row in list(rows or []) if str((row or {}).get("code") or "").strip()]
        status_counts: dict[str, int] = {}
        reason_counts: dict[str, int] = {}
        dropped_family_counts: dict[str, int] = {}
        enabled_seen: list[bool] = []
        evaluated_count = 0
        applied_count = 0
        fallback_count = 0
        for row in gate_rows:
            status = dict(row.get("_stock_direction_gate") or {})
            if "enabled" in status:
                enabled_seen.append(bool(status.get("enabled")))
            state = str(status.get("status") or "not_evaluated").strip().lower() or "not_evaluated"
            status_counts[state] = status_counts.get(state, 0) + 1
            if state not in {"disabled", "not_evaluated"}:
                evaluated_count += 1
            if state == "applied":
                applied_count += 1
            if status.get("fallback_family"):
                fallback_count += 1
            reason = str(status.get("reason") or "").strip().lower()
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            for family in list(status.get("dropped_families") or []):
                token = str(family or "").strip().lower()
                if token:
                    dropped_family_counts[token] = dropped_family_counts.get(token, 0) + 1

        selected_applied_count = 0
        selected_fallback_count = 0
        selected_task_count = 0
        for task in list(selected_tasks or []):
            selected_task_count += 1
            status = dict(task.get("stock_direction_gate") or {})
            if str(status.get("status") or "").strip().lower() == "applied":
                selected_applied_count += 1
            if status.get("fallback_family"):
                selected_fallback_count += 1

        return {
            "direction_gate_enabled": bool(cls._direction_gate_enabled() or any(enabled_seen)),
            "direction_gate_candidate_stock_count": len(gate_rows),
            "direction_gate_evaluated_count": evaluated_count,
            "direction_gate_applied_count": applied_count,
            "direction_gate_fallback_count": fallback_count,
            "direction_gate_status_counts": status_counts,
            "direction_gate_reason_counts": reason_counts,
            "direction_gate_dropped_family_counts": dropped_family_counts,
            "selected_direction_gate_applied_count": selected_applied_count,
            "selected_direction_gate_fallback_count": selected_fallback_count,
            "selected_direction_gate_task_count": selected_task_count,
        }
