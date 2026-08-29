"""策略工厂候选生成。"""


from __future__ import annotations

import json
import random
from typing import Any, Dict, List, Optional

from .constants import (
    CATEGORY_MINIMUMS,
    SPAWNER_EVENT_SOURCE_BASE_CAP,
    SPAWNER_EVENT_SOURCE_SUPPLEMENTAL_BONUS,
    SPAWNER_EVENT_FILL_BUDGET_MAX,
    SPAWNER_FILL_BUDGET_MAX,
    SPAWNER_TARGET_TOTAL,
    STRATEGY_FACTORY_FACTOR_IC_CLASSIC_FALLING_MAX_IC,
    STRATEGY_FACTORY_FACTOR_IC_CLASSIC_MIN_IC,
    STRATEGY_FACTORY_FACTOR_IC_GENERIC_INTAKE_ENABLED,
    STRATEGY_FACTORY_FACTOR_IC_GENERIC_MIN_ABS_IC,
    STRATEGY_FACTORY_FACTOR_IC_GENERIC_MAX_FACTORS,
    STRATEGY_FACTORY_FACTOR_POOL_OOS_GATE_ENABLED,
    STRATEGY_FACTORY_FACTOR_POOL_OOS_MIN_RANK_IC_IR,
    STRATEGY_FACTORY_FACTOR_POOL_OOS_MIN_SAMPLE_DATES,
    preferred_strategy_types_for_factor,
)
from .market_evidence import apply_evidence_first_candidate, summarize_generation_quality
from .parameter_distribution_registry import ParameterDistributionRegistry
from .spawn_policy_registry import (
    get_event_focus_targets_by_keyword,
    get_event_ready_source_weights,
    get_spawn_policy_version,
)
from .targets import _normalize_target_codes

class StrategySpawner:
    """根据每日数据快照生成候选策略。"""
    _EVENT_FOCUS_TARGETS_BY_KEYWORD = get_event_focus_targets_by_keyword()
    _TREND_CLUSTER_TYPES = frozenset({"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout", "sector_rotation"})
    _rejection_memory: set = set()
    _REJECTION_MEMORY_SIZE = 200
    _LOCAL_GENERATION_CAPS = {
        "mean_reversion_short": 1,
    }
    _DIVERSIFICATION_GROUPS = {
        "quality_defensive": frozenset({"quality_factor", "value_factor", "macro_timing"}),
        "mean_reversion": frozenset({"rsi", "gap_fill", "mean_reversion_short"}),
        "flow_rotation": frozenset({"north_capital_track", "sector_rotation", "margin_divergence"}),
        "event_breakout": frozenset({"event_structure_breakout", "volatility_breakout", "momentum"}),
    }
    _POOL_PROFILE_BY_TYPE = {
        "momentum": "high_vol_growth",
        "volatility_breakout": "high_vol_growth",
        "event_structure_breakout": "high_vol_growth",
        "growth_factor": "high_vol_growth",
        "gap_fill": "high_vol_growth",
        "mean_reversion_short": "high_vol_growth",
        "rsi": "high_vol_growth",
        "quality_factor": "low_vol_defensive",
        "value_factor": "low_vol_defensive",
        "macro_timing": "low_vol_defensive",
        "north_capital_track": "cycle_resource",
        "sector_rotation": "cycle_resource",
        "margin_divergence": "cycle_resource",
        "ma_cross": "cycle_resource",
    }
    _SNAPSHOT_TARGET_SYMBOL_BUDGET_BY_TYPE = {
        "momentum": 3,
        "ma_cross": 3,
        "rsi": 3,
        "volatility_breakout": 3,
        "event_structure_breakout": 3,
        "gap_fill": 2,
        "mean_reversion_short": 2,
        "value_factor": 4,
        "quality_factor": 4,
        "growth_factor": 4,
        "multi_factor": 4,
        "macro_timing": 4,
        "sector_rotation": 4,
        "north_capital_track": 3,
        "margin_divergence": 3,
    }
    _SNAPSHOT_TARGET_FAMILY_ALIASES = {
        "momentum": ("momentum", "ma_cross", "growth_factor"),
        "ma_cross": ("ma_cross", "momentum", "quality_factor"),
        "rsi": ("rsi", "gap_fill", "mean_reversion_short", "value_factor"),
        "volatility_breakout": ("volatility_breakout", "event_structure_breakout", "momentum", "ma_cross"),
        "event_structure_breakout": ("event_structure_breakout", "volatility_breakout", "momentum", "north_capital_track"),
        "gap_fill": ("gap_fill", "rsi", "mean_reversion_short"),
        "mean_reversion_short": ("mean_reversion_short", "rsi", "gap_fill", "value_factor"),
        "value_factor": ("value_factor", "quality_factor", "multi_factor"),
        "quality_factor": ("quality_factor", "value_factor", "multi_factor"),
        "growth_factor": ("growth_factor", "momentum", "quality_factor"),
        "multi_factor": ("multi_factor", "quality_factor", "value_factor", "growth_factor"),
        "macro_timing": ("macro_timing", "quality_factor", "value_factor", "ma_cross"),
        "sector_rotation": ("sector_rotation", "north_capital_track", "quality_factor", "ma_cross"),
        "north_capital_track": ("north_capital_track", "sector_rotation", "quality_factor", "growth_factor"),
        "margin_divergence": ("margin_divergence", "sector_rotation", "value_factor", "quality_factor"),
    }

    def __init__(self):
        self.policy_version = get_spawn_policy_version()
        self.last_report: dict = {
            "summary": {
                "policy_version": self.policy_version,
                "candidate_count": 0,
                "source_counts": {},
                "strategy_type_counts": {},
                "quota_fill_count": 0,
                "signal_trigger_count": 0,
                "threshold_hit_count": 0,
                "parameter_source_counts": {},
                "quota_fill_mode_counts": {},
                "quota_fill_quality_counts": {},
                "historical_distribution_count": 0,
                "historical_guided_quota_fill_count": 0,
                "signal_aligned_quota_fill_count": 0,
                "no_signal_quota_fill_count": 0,
                "effective_quota_fill_count": 0,
            }
        }

    def get_last_report(self) -> dict:
        return self.last_report

    @staticmethod
    def _threshold(field: str, operator: str, threshold: Any, actual: Any, label: Optional[str] = None) -> dict:
        item = {
            "field": field,
            "operator": operator,
            "threshold": threshold,
            "actual": actual,
            "matched": True,
        }
        if label:
            item["label"] = label
        return item

    @classmethod
    def record_rejections(cls, rejected_candidates: list):
        """Record rejected candidate signatures to avoid regenerating them."""
        for candidate in (rejected_candidates or []):
            sig = cls._quick_signature(candidate)
            if sig:
                cls._rejection_memory.add(sig)
        # Keep memory bounded
        if len(cls._rejection_memory) > cls._REJECTION_MEMORY_SIZE:
            excess = len(cls._rejection_memory) - cls._REJECTION_MEMORY_SIZE
            # Remove oldest (set doesn't preserve order, so just trim)
            for _ in range(excess):
                cls._rejection_memory.pop()

    @staticmethod
    def _quick_signature(candidate: dict) -> str:
        """Generate a lightweight signature for quick comparison."""
        if not candidate:
            return ""
        strategy_type = str(candidate.get("strategy_type") or "").strip()
        params = candidate.get("params") or {}
        # Use only key numeric params rounded to reduce jitter noise
        key_items = []
        for k in sorted(list(params.keys())[:6]):
            v = params[k]
            if isinstance(v, (int, float)):
                key_items.append(f"{k}:{round(float(v), 1)}")
        return f"{strategy_type}|{'|'.join(key_items)}" if strategy_type else ""

    @staticmethod
    def _build_generation_reason(
        source: str,
        reason: str,
        trigger_signal: Optional[dict] = None,
        trigger_thresholds: Optional[List[dict]] = None,
        quota_fill: Optional[dict] = None,
        kind: str = "signal_trigger",
    ) -> dict:
        return {
            "kind": kind,
            "source": source,
            "summary": reason,
            "trigger_signal": trigger_signal or {},
            "trigger_thresholds": list(trigger_thresholds or []),
            "quota_fill": quota_fill,
        }

    @staticmethod
    def _normalize_text_list(*values: Any, limit: int = 8) -> List[str]:
        items: List[str] = []
        queue = list(values)
        while queue:
            value = queue.pop(0)
            if isinstance(value, (list, tuple, set)):
                queue[:0] = list(value)
                continue
            token = str(value or "").strip()
            if token and token not in items:
                items.append(token)
        return items[: max(1, int(limit or 8))]

    @classmethod
    def _build_event_prefilter(
        cls,
        *,
        observed_sources: Optional[List[str]] = None,
        evidence_summary: str = "",
        event_id: str = "",
        theme_code: str = "",
        focus_industries: Optional[List[str]] = None,
        event_anchor: Optional[dict[str, Any]] = None,
        confirmation_count: Optional[int] = None,
        anchor_strength: Any = None,
        required: bool = True,
        profile: str = "announcement_flow_sector_v1",
        min_confirmations: int = 1,
    ) -> dict:
        allowed_sources = ["market_events_normalized"]
        normalized_sources = [
            token
            for token in cls._normalize_text_list(observed_sources, limit=6)
            if token in allowed_sources
        ]
        normalized_focus = cls._normalize_text_list(focus_industries, limit=3)
        min_conf = max(1, int(min_confirmations or 1))
        normalized_event_anchor = dict(event_anchor or {})
        normalized_event_anchor = {
            "source": str(normalized_event_anchor.get("source") or "").strip() or None,
            "id": str(normalized_event_anchor.get("id") or "").strip() or None,
            "type": str(normalized_event_anchor.get("type") or "").strip() or None,
            "strength": normalized_event_anchor.get("strength"),
            "theme_code": str(normalized_event_anchor.get("theme_code") or "").strip() or None,
            "focus_industries": cls._normalize_text_list(
                normalized_event_anchor.get("focus_industries"),
                normalized_focus,
                limit=3,
            ),
            "target_symbols": _normalize_target_codes(normalized_event_anchor.get("target_symbols"), limit=6),
            "source_doc_uids": cls._normalize_text_list(
                normalized_event_anchor.get("source_doc_uids"),
                limit=8,
            ),
            "source_tier": str(normalized_event_anchor.get("source_tier") or "").strip() or None,
            "reliability_score": normalized_event_anchor.get("reliability_score"),
            "evidence_time": str(normalized_event_anchor.get("evidence_time") or "").strip() or None,
            "provider_chain": cls._normalize_text_list(
                normalized_event_anchor.get("provider_chain"),
                limit=6,
            ),
            "verified_event_anchor": bool(normalized_event_anchor.get("verified_event_anchor")),
            "validation_summary": dict(normalized_event_anchor.get("validation_summary") or {}),
            "occurrence_status": str(normalized_event_anchor.get("occurrence_status") or "").strip() or None,
            "alpha_confirmation_status": str(normalized_event_anchor.get("alpha_confirmation_status") or "").strip() or None,
            "confidence_cap_reason": str(normalized_event_anchor.get("confidence_cap_reason") or "").strip() or None,
            "needs_alpha_confirmation": bool(normalized_event_anchor.get("needs_alpha_confirmation")),
            "conflict_count": int(normalized_event_anchor.get("conflict_count") or 0),
        }
        if not any(
            normalized_event_anchor.get(key) not in (None, "", [], {})
            for key in ("source", "id", "type", "theme_code", "target_symbols")
        ):
            normalized_event_anchor = {}
        effective_confirmation_count = (
            max(0, int(confirmation_count))
            if confirmation_count is not None
            else len(normalized_sources)
        )
        effective_anchor_strength = (
            anchor_strength
            if anchor_strength not in (None, "", [], {})
            else normalized_event_anchor.get("strength")
        )
        passed = (not required) or (
            cls._event_anchor_has_explicit_source(normalized_event_anchor)
            and effective_confirmation_count >= min_conf
        )
        status = "passed" if passed else ("missing" if not normalized_sources else "insufficient_confirmations")
        return {
            "required": bool(required),
            "profile": str(profile or "announcement_flow_sector_v1").strip() or "announcement_flow_sector_v1",
            "allowed_sources": list(allowed_sources),
            "observed_sources": normalized_sources,
            "observed_confirmation_count": len(normalized_sources),
            "confirmation_count": effective_confirmation_count,
            "min_confirmations": min_conf,
            "passed": passed,
            "status": status,
            "event_id": str(event_id or "").strip() or None,
            "theme_code": str(theme_code or "").strip() or None,
            "focus_industries": normalized_focus,
            "evidence_summary": str(evidence_summary or "").strip() or None,
            "event_anchor": dict(normalized_event_anchor),
            "anchor_strength": effective_anchor_strength,
        }

    @classmethod
    def _snapshot_event_anchor(
        cls,
        snapshot: Optional[dict],
        *,
        source: str = "",
        trigger_signal: Optional[dict] = None,
    ) -> dict:
        payload = dict(snapshot or {})
        source_name = str(source or payload.get("source") or "").strip().lower()
        trigger_field = str((trigger_signal or {}).get("field") or payload.get("trigger_field") or "").strip().lower()
        event_id = str(payload.get("event_id") or "").strip()
        announcement_id = str(payload.get("announcement_id") or "").strip()
        announcement_ids = cls._normalize_text_list(payload.get("announcement_ids"), limit=4)
        event_type = str(payload.get("event_type") or payload.get("catalyst_type") or "").strip().lower()
        theme_code = str(payload.get("theme_code") or "").strip()
        hot_sectors = cls._normalize_text_list(payload.get("hot_sectors"), limit=3)
        if event_id or announcement_id or announcement_ids or event_type in {"announcement", "earnings", "filing", "news"} or source_name == "announcement":
            anchor_id = event_id or announcement_id or (announcement_ids[0] if announcement_ids else "") or event_type or "announcement"
            return {
                "source": "announcement",
                "id": anchor_id,
                "type": event_type or "announcement",
                "strength": cls._safe_float(payload.get("confidence") or payload.get("intensity") or 1.0),
                "theme_code": theme_code or None,
                "focus_industries": list(hot_sectors),
                "target_symbols": _normalize_target_codes(payload.get("target_symbols"), limit=6),
            }
        if source_name == "fund_flow" or trigger_field in {"north_fund_3d_net", "margin_5d_change_pct"}:
            return {
                "source": "fund_flow",
                "id": trigger_field or source_name or "fund_flow",
                "type": "fund_flow",
                "strength": cls._safe_float((trigger_signal or {}).get("value") or payload.get(trigger_field) or 0.0),
                "focus_industries": list(hot_sectors),
                "target_symbols": _normalize_target_codes(payload.get("target_symbols"), limit=6),
            }

        event_driven = dict(payload.get("event_driven") or {})
        event_items = [dict(item or {}) for item in list(event_driven.get("events") or []) if isinstance(item, dict)]
        preferred_focus = set(hot_sectors)
        ranked_events: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for event in event_items:
            themes = [dict(item or {}) for item in list(event.get("themes") or []) if isinstance(item, dict)]
            for theme in themes:
                source = str(theme.get("source") or event.get("source") or "").strip().lower()
                source_tier = str(theme.get("source_tier") or event.get("source_tier") or "").strip().lower()
                source_doc_uids = cls._normalize_text_list(
                    theme.get("source_doc_uids") or event.get("source_doc_uids"),
                    limit=8,
                )
                event_anchor_id = str(
                    theme.get("event_anchor_id")
                    or event.get("event_anchor_id")
                    or event.get("event_id")
                    or ""
                ).strip()
                validation_summary = dict(theme.get("validation_summary") or event.get("validation_summary") or {})
                alpha_status = str(
                    theme.get("alpha_confirmation_status")
                    or event.get("alpha_confirmation_status")
                    or validation_summary.get("alpha_confirmation_status")
                    or ""
                ).strip().lower()
                occurrence_status = str(
                    theme.get("occurrence_status")
                    or event.get("occurrence_status")
                    or validation_summary.get("occurrence_status")
                    or ""
                ).strip().lower()
                conflict_count = int(theme.get("conflict_count") or event.get("conflict_count") or validation_summary.get("conflict_count") or 0)
                if not (
                    bool(theme.get("verified_event_anchor") or event.get("verified_event_anchor"))
                    and source == "market_events_normalized"
                    and source_tier in {"tier_a", "tier_b"}
                    and source_doc_uids
                    and event_anchor_id
                    and (not occurrence_status or occurrence_status.startswith("verified"))
                    and alpha_status not in {"news_only_rejected", "source_degraded", "conflicted"}
                    and conflict_count <= 0
                ):
                    continue
                focus_name = str(theme.get("theme_name") or theme.get("theme_code") or "").strip()
                target_symbols = _normalize_target_codes(theme.get("target_symbols"), limit=6)
                score_summary = dict(theme.get("score_summary") or {})
                avg_score = cls._safe_float(score_summary.get("avg_final_score") or 0.0)
                confidence = cls._safe_float(event.get("confidence") or event.get("intensity") or 0.0)
                focus_bonus = 1.0 if (focus_name and focus_name in preferred_focus) else 0.0
                target_bonus = 0.5 if target_symbols else 0.0
                ranked_events.append((avg_score + confidence + focus_bonus + target_bonus, event, theme))
        if ranked_events:
            ranked_events.sort(key=lambda item: item[0], reverse=True)
            _score, event, theme = ranked_events[0]
            focus_name = str(theme.get("theme_name") or theme.get("theme_code") or "").strip()
            return {
                "source": "market_events_normalized",
                "id": str(
                    theme.get("event_anchor_id")
                    or event.get("event_anchor_id")
                    or event.get("event_id")
                    or ""
                ).strip() or None,
                "type": str(event.get("event_type") or "sector_catalyst").strip() or "sector_catalyst",
                "strength": round(
                    max(
                        cls._safe_float((theme.get("score_summary") or {}).get("avg_final_score") or 0.0),
                        cls._safe_float(event.get("confidence") or event.get("intensity") or 0.0),
                    ),
                    4,
                ),
                "theme_code": str(theme.get("theme_code") or "").strip() or None,
                "focus_industries": cls._normalize_text_list(focus_name, hot_sectors, limit=3),
                "target_symbols": _normalize_target_codes(theme.get("target_symbols"), limit=6),
                "source_doc_uids": cls._normalize_text_list(
                    theme.get("source_doc_uids") or event.get("source_doc_uids"),
                    limit=8,
                ),
                "source_tier": str(theme.get("source_tier") or event.get("source_tier") or "").strip() or None,
                "reliability_score": theme.get("reliability_score") or event.get("reliability_score"),
                "evidence_time": theme.get("evidence_time") or event.get("evidence_time"),
                "provider_chain": cls._normalize_text_list(
                    theme.get("provider_chain") or event.get("provider_chain"),
                    limit=6,
                ),
                "verified_event_anchor": True,
                "validation_summary": dict(theme.get("validation_summary") or event.get("validation_summary") or {}),
                "occurrence_status": theme.get("occurrence_status") or event.get("occurrence_status"),
                "alpha_confirmation_status": theme.get("alpha_confirmation_status") or event.get("alpha_confirmation_status"),
                "confidence_cap_reason": theme.get("confidence_cap_reason") or event.get("confidence_cap_reason"),
                "needs_alpha_confirmation": bool(theme.get("needs_alpha_confirmation") or event.get("needs_alpha_confirmation")),
                "conflict_count": int(theme.get("conflict_count") or event.get("conflict_count") or 0),
            }
        return {}

    @classmethod
    def _snapshot_event_prefilter(
        cls,
        snapshot: Optional[dict],
        *,
        source: str = "",
        trigger_signal: Optional[dict] = None,
    ) -> dict:
        payload = dict(snapshot or {})
        observed_sources: List[str] = []
        evidence_parts: List[str] = []
        theme_code = str(payload.get("theme_code") or "").strip()
        hot_sectors = cls._normalize_text_list(payload.get("hot_sectors"), limit=3)
        source_name = str(source or payload.get("source") or "").strip().lower()
        trigger_field = str((trigger_signal or payload.get("trigger_signal") or {}).get("field") or "").strip().lower()
        event_anchor = cls._snapshot_event_anchor(payload, source=source_name, trigger_signal=trigger_signal)
        anchor_source = str(event_anchor.get("source") or "").strip().lower()
        if anchor_source == "announcement":
            observed_sources.append("announcement")
            evidence_parts.append(f"announcement:{event_anchor.get('id') or event_anchor.get('type') or 'announcement'}")
        if anchor_source == "market_events_normalized":
            observed_sources.append("market_events_normalized")
            sector_focus = cls._normalize_text_list(event_anchor.get("focus_industries"), hot_sectors, limit=2)
            evidence_parts.append(
                f"market_events_normalized:{event_anchor.get('id') or event_anchor.get('theme_code') or ','.join(sector_focus) or 'event'}"
            )
        if source_name == "fund_flow" or trigger_field in {"north_fund_3d_net", "margin_5d_change_pct"}:
            observed_sources.append("fund_flow")
            evidence_parts.append(f"fund_flow:{trigger_field or source_name}")
        return cls._build_event_prefilter(
            observed_sources=observed_sources,
            evidence_summary=" | ".join(part for part in evidence_parts if part),
            event_id=str(event_anchor.get("id") or payload.get("event_id") or "").strip(),
            theme_code=theme_code,
            focus_industries=cls._normalize_text_list(event_anchor.get("focus_industries"), hot_sectors, limit=3),
            event_anchor=event_anchor,
            confirmation_count=len(observed_sources),
            anchor_strength=event_anchor.get("strength"),
            required=True,
        )

    @staticmethod
    def _event_anchor_has_explicit_source(event_anchor: Optional[dict[str, Any]]) -> bool:
        payload = dict(event_anchor or {})
        source = str(payload.get("source") or "").strip().lower()
        if source == "market_events_normalized":
            source_tier = str(payload.get("source_tier") or "").strip().lower()
            source_doc_uids = [
                str(item or "").strip()
                for item in list(payload.get("source_doc_uids") or [])
                if str(item or "").strip()
            ]
            return bool(
                payload.get("verified_event_anchor")
                and source_tier in {"tier_a", "tier_b"}
                and source_doc_uids
                and str(payload.get("id") or "").strip()
                and str(payload.get("alpha_confirmation_status") or "").strip().lower()
                not in {"news_only_rejected", "source_degraded", "conflicted"}
                and int(payload.get("conflict_count") or 0) <= 0
            )
        return False

    @classmethod
    def _focus_industry_target_symbols(
        cls,
        strategy_type: str,
        candidate: Optional[dict],
        snapshot: Optional[dict],
        *,
        limit: int,
    ) -> List[str]:
        if str(strategy_type or "").strip().lower() != "event_structure_breakout":
            return []
        payload = dict(candidate or {})
        hints = cls._normalize_text_list(
            dict(payload.get("event_prefilter") or {}).get("focus_industries"),
            dict(payload.get("research_task") or {}).get("focus_industries"),
            dict(payload.get("research_task") or {}).get("hot_sectors"),
            dict(snapshot or {}).get("hot_sectors"),
            limit=4,
        )
        selected: List[str] = []
        seen: set[str] = set()
        for hint in hints:
            lowered = hint.lower()
            for keyword, codes in cls._EVENT_FOCUS_TARGETS_BY_KEYWORD.items():
                normalized_keyword = str(keyword or "").strip().lower()
                if not normalized_keyword:
                    continue
                if normalized_keyword not in lowered and lowered not in normalized_keyword:
                    continue
                for code in codes:
                    token = str(code or "").strip()
                    if not token or token in seen:
                        continue
                    seen.add(token)
                    selected.append(token)
        return _normalize_target_codes(selected, limit=limit)

    @classmethod
    def _event_anchor_target_symbols(
        cls,
        strategy_type: str,
        candidate: Optional[dict],
        snapshot: Optional[dict],
        *,
        limit: int,
    ) -> List[str]:
        if str(strategy_type or "").strip().lower() != "event_structure_breakout":
            return []
        payload = dict(candidate or {})
        event_prefilter = dict(payload.get("event_prefilter") or {})
        event_anchor = dict(event_prefilter.get("event_anchor") or payload.get("event_anchor") or {})
        if not event_anchor:
            event_anchor = cls._snapshot_event_anchor(
                snapshot,
                source=str(dict(payload.get("generation_reason") or {}).get("source") or ""),
                trigger_signal=dict(payload.get("trigger_signal") or {}),
            )
        return _normalize_target_codes(event_anchor.get("target_symbols"), limit=limit)
    @staticmethod
    def _high_precision_candidate_fields(
        *,
        preferred_regime: str,
        avoid_regime: str,
        holding_rationale: str,
        failure_mode: dict,
        entry_selectivity: str = "strict",
        trade_density_preference: str = "low",
        slippage_bps: float = 4.0,
        max_position_pct: float = 0.12,
        capacity_bucket: str = "small",
        min_days: int = 2,
        max_days: int = 8,
        entry_bias: str = "oversold_repair_with_mean_reversion_confirmation",
        exit_bias: str = "mean_reversion_completion_or_rsi_reset",
        event_prefilter: Optional[dict] = None,
        candidate_origin: str = "research_signal",
        event_anchor: Optional[dict[str, Any]] = None,
        target_pool_source: str = "explicit_task",
    ) -> dict:
        event_prefilter_payload = dict(event_prefilter or {})
        resolved_event_anchor = dict(
            event_anchor
            or event_prefilter_payload.get("event_anchor")
            or {}
        )
        cost_grid = {
            "base_case": {
                "commission_rate": 0.00025,
                "slippage_bps": slippage_bps,
                "tradability_filter": True,
                "slippage_model": "fixed",
                "market_impact_bps": 1.0,
            },
            "stress_cases": [
                {
                    "label": "base_plus_50pct_cost",
                    "commission_rate": 0.000375,
                    "slippage_bps": round(slippage_bps * 1.5, 2),
                }
            ],
            "source": "snapshot_local_high_precision",
        }
        validation_profile = {
            "profile": "trade_rule_validation",
            "validation_focus": "candidate_target_only",
            "primary_validation_layer": "target",
            "objective_profile": "high_precision",
            "trade_density_preference": trade_density_preference,
            "regime_required": True,
            "cost_robust_required": True,
            "entry_selectivity": entry_selectivity,
            "preferred_regime": preferred_regime,
            "avoid_regime": avoid_regime,
            "event_prefilter_required": bool(event_prefilter_payload.get("required")),
            "event_prefilter_profile": str(event_prefilter_payload.get("profile") or "").strip() or None,
            "event_prefilter_min_confirmations": (
                int(event_prefilter_payload.get("min_confirmations") or 0)
                if event_prefilter_payload.get("min_confirmations") is not None
                else None
            ),
        }
        hypothesis_artifact = {
            "objective_profile": "high_precision",
            "trade_density_preference": trade_density_preference,
            "entry_selectivity": entry_selectivity,
            "holding_rationale": holding_rationale,
            "failure_mode": dict(failure_mode or {}),
            "cost_sensitivity_grid": dict(cost_grid),
            "capacity_assumption": {
                "max_position_pct": max_position_pct,
                "capacity_bucket": capacity_bucket,
                "symbol_count": 4,
            },
            "market_regime_assumption": {
                "preferred_regime": preferred_regime,
                "avoid_regime": avoid_regime,
            },
            "cost_robust_required": True,
            "event_prefilter": dict(event_prefilter_payload),
            "event_anchor": dict(resolved_event_anchor),
        }
        tags = ["high_precision", "regime_required"]
        if event_prefilter_payload.get("required"):
            tags.append("event_prefilter_required")
        return {
            "validation_profile": validation_profile,
            "holding_horizon": {
                "min_days": min_days,
                "max_days": max_days,
                "expected_turnover_band": trade_density_preference,
            },
            "trade_plan": {
                "entry_bias": entry_bias,
                "exit_bias": exit_bias,
            },
            "holding_rationale": holding_rationale,
            "failure_mode": dict(failure_mode or {}),
            "cost_sensitivity_grid": dict(cost_grid),
            "capacity_assumption": {
                "max_position_pct": max_position_pct,
                "capacity_bucket": capacity_bucket,
                "symbol_count": 4,
            },
            "market_regime_assumption": {
                "summary": holding_rationale,
                "preferred_regime": preferred_regime,
                "avoid_regime": avoid_regime,
            },
            "hypothesis_artifact": hypothesis_artifact,
            "event_prefilter": dict(event_prefilter_payload),
            "candidate_origin": str(candidate_origin or "research_signal").strip() or "research_signal",
            "event_anchor": dict(resolved_event_anchor),
            "target_pool_source": str(target_pool_source or "explicit_task").strip() or "explicit_task",
            "tags": tags,
        }

    @staticmethod
    def _high_precision_margin_divergence_params() -> dict:
        return {
            "fear_threshold": 43,
            "greed_threshold": 60,
            "lookback": 12,
            "rebound_window": 3,
            "repair_drawdown_floor": -0.06,
            "repair_rebound_pct": 0.012,
            "dryup_window": 3,
            "dryup_max_ratio": 0.9,
            "liquidity_window": 8,
            "entry_volume_floor_ratio": 1.0,
            "structure_window": 4,
            "structure_close_location_min": 0.58,
            "structure_body_return_min": 0.002,
            "max_hold_bars": 8,
            "adverse_volume_break_ratio": 0.72,
            "adverse_close_break_pct": -0.012,
            "max_active_symbols": 2,
            "universe_selection_profile": "liquidity_divergence_fit_v1",
        }

    @staticmethod
    def _high_precision_event_structure_breakout_params() -> dict:
        return {
            "breakout_window": 12,
            "breakout_buffer_pct": 0.002,
            "contraction_window": 5,
            "contraction_max_range_ratio": 0.06,
            "volume_window": 8,
            "breakout_volume_ratio_min": 1.0,
            "structure_window": 4,
            "structure_close_location_min": 0.62,
            "structure_body_return_min": 0.003,
            "event_impulse_window": 5,
            "event_impulse_threshold": 0.015,
            "max_hold_bars": 8,
            "breakout_failure_close_buffer": -0.012,
            "adverse_volume_ratio_max": 0.85,
            "max_active_symbols": 3,
            "universe_selection_profile": "event_structure_breakout_fit_v1",
            "event_prefilter_enabled": True,
            "event_prefilter_profile": "announcement_flow_sector_v1",
            "event_prefilter_min_confirmations": 1,
        }

    @classmethod
    def _trend_cluster_count(cls, candidates: Optional[List[dict]]) -> int:
        return sum(
            1
            for item in list(candidates or [])
            if str((item or {}).get("strategy_type") or "").strip() in cls._TREND_CLUSTER_TYPES
        )

    @classmethod
    def _diversification_debt(cls, candidates: Optional[List[dict]]) -> List[str]:
        present = {
            str((item or {}).get("strategy_type") or "").strip()
            for item in list(candidates or [])
            if str((item or {}).get("strategy_type") or "").strip()
        }
        debt: List[str] = []
        for group_name, members in cls._DIVERSIFICATION_GROUPS.items():
            if not any(strategy_type in present for strategy_type in members):
                debt.append(group_name)
        return debt

    @classmethod
    def _pool_profile_distribution(cls, candidates: Optional[List[dict]]) -> Dict[str, int]:
        distribution: Dict[str, int] = {}
        for item in list(candidates or []):
            strategy_type = str((item or {}).get("strategy_type") or "").strip()
            profile = cls._POOL_PROFILE_BY_TYPE.get(strategy_type, "unknown")
            distribution[profile] = distribution.get(profile, 0) + 1
        return distribution

    @classmethod
    def _local_generation_cap(cls, strategy_type: str) -> Optional[int]:
        normalized = str(strategy_type or "").strip().lower()
        if not normalized:
            return None
        cap = cls._LOCAL_GENERATION_CAPS.get(normalized)
        return int(cap) if cap is not None else None

    @staticmethod
    def _build_spawn_report(
        candidates: List[dict],
        *,
        event_ready: bool = False,
        event_ready_supplemental: bool = False,
        source_raw_counts: Optional[Dict[str, int]] = None,
        source_budget_caps: Optional[Dict[str, Optional[int]]] = None,
        source_budget_weights: Optional[Dict[str, Optional[float]]] = None,
        signal_feedback_summary: Optional[dict] = None,
    ) -> dict:
        source_counts: Dict[str, int] = {}
        strategy_type_counts: Dict[str, int] = {}
        quota_fill_count = 0
        signal_trigger_count = 0
        threshold_hit_count = 0
        parameter_source_counts: Dict[str, int] = {}
        quota_fill_mode_counts: Dict[str, int] = {}
        quota_fill_quality_counts: Dict[str, int] = {}
        quota_fill_feedback_limited_count = 0
        quota_fill_feedback_limited_type_counts: Dict[str, int] = {}
        historical_quota_fill_count = 0
        signal_aligned_quota_fill_count = 0
        no_signal_quota_fill_count = 0
        for candidate in candidates:
            generation_reason = candidate.get("generation_reason") or {}
            source = str(generation_reason.get("source") or "unknown")
            strategy_type = str(candidate.get("strategy_type") or "unknown")
            parameter_source = str(candidate.get("parameter_source") or "").strip()
            source_counts[source] = source_counts.get(source, 0) + 1
            strategy_type_counts[strategy_type] = strategy_type_counts.get(strategy_type, 0) + 1
            if parameter_source:
                parameter_source_counts[parameter_source] = parameter_source_counts.get(parameter_source, 0) + 1
            threshold_hit_count += len(candidate.get("trigger_thresholds") or [])
            if candidate.get("quota_fill"):
                quota_fill_count += 1
                fill_meta = dict(candidate.get("quota_fill") or {})
                fill_mode = str(fill_meta.get("fill_source_mode") or "unknown").strip()
                fill_quality = str(fill_meta.get("fill_quality_tier") or "unknown").strip()
                if bool(fill_meta.get("feedback_limited")):
                    quota_fill_feedback_limited_count += 1
                    quota_fill_feedback_limited_type_counts[strategy_type] = (
                        quota_fill_feedback_limited_type_counts.get(strategy_type, 0) + 1
                    )
                if fill_mode:
                    quota_fill_mode_counts[fill_mode] = quota_fill_mode_counts.get(fill_mode, 0) + 1
                if fill_quality:
                    quota_fill_quality_counts[fill_quality] = quota_fill_quality_counts.get(fill_quality, 0) + 1
                if fill_mode == "historical_guided":
                    historical_quota_fill_count += 1
                elif fill_mode == "signal_aligned":
                    signal_aligned_quota_fill_count += 1
                elif fill_mode == "no_signal_fallback":
                    no_signal_quota_fill_count += 1
            else:
                signal_trigger_count += 1
        raw_counts = dict(source_raw_counts or {})
        budget_caps = dict(source_budget_caps or {})
        budget_weights = dict(source_budget_weights or {})
        signal_feedback = dict(signal_feedback_summary or {})
        trimmed_count = sum(
            max(0, int(raw_counts.get(source, 0) or 0) - int(source_counts.get(source, 0) or 0))
            for source in raw_counts
        )
        trend_cluster_count = StrategySpawner._trend_cluster_count(candidates)
        diversification_debt = StrategySpawner._diversification_debt(candidates)
        generation_quality = summarize_generation_quality(candidates)
        return {
            "summary": {
                "policy_version": get_spawn_policy_version(),
                "candidate_count": len(candidates),
                "source_counts": source_counts,
                "strategy_type_counts": strategy_type_counts,
                "quota_fill_count": quota_fill_count,
                "signal_trigger_count": signal_trigger_count,
                "threshold_hit_count": threshold_hit_count,
                "event_ready": bool(event_ready),
                "event_ready_supplemental": bool(event_ready_supplemental),
                "source_raw_counts": raw_counts,
                "source_budget_caps": budget_caps,
                "source_budget_weights": budget_weights,
                "source_trimmed_count": trimmed_count,
                "signal_feedback_limited_count": int(signal_feedback.get("signal_feedback_limited_count") or 0),
                "signal_feedback_limited_type_counts": dict(
                    signal_feedback.get("signal_feedback_limited_type_counts") or {}
                ),
                "signal_feedback_factor_by_type": dict(
                    signal_feedback.get("signal_feedback_factor_by_type") or {}
                ),
                "parameter_source_counts": parameter_source_counts,
                "historical_distribution_count": int(parameter_source_counts.get("historical_distribution") or 0),
                "quota_fill_mode_counts": quota_fill_mode_counts,
                "quota_fill_quality_counts": quota_fill_quality_counts,
                "quota_fill_feedback_limited_count": quota_fill_feedback_limited_count,
                "quota_fill_feedback_limited_type_counts": quota_fill_feedback_limited_type_counts,
                "historical_guided_quota_fill_count": historical_quota_fill_count,
                "signal_aligned_quota_fill_count": signal_aligned_quota_fill_count,
                "no_signal_quota_fill_count": no_signal_quota_fill_count,
                "effective_quota_fill_count": max(quota_fill_count - no_signal_quota_fill_count, 0),
                "trend_cluster_ratio": round(trend_cluster_count / len(candidates), 4) if candidates else 0.0,
                "diversification_debt": diversification_debt,
                "pool_profile_distribution": StrategySpawner._pool_profile_distribution(candidates),
                "direction_counts": dict(generation_quality.get("direction_counts") or {}),
                "confidence_distribution": dict(generation_quality.get("confidence_distribution") or {}),
                "evidence_source_counts": dict(generation_quality.get("evidence_source_counts") or {}),
                "template_dominance_count": int(generation_quality.get("template_dominance_count") or 0),
                "factor_backed_candidate_count": int(generation_quality.get("factor_backed_candidate_count") or 0),
                "event_backed_candidate_count": int(generation_quality.get("event_backed_candidate_count") or 0),
                "non_proxy_evidence_ratio_mean": generation_quality.get("non_proxy_evidence_ratio_mean"),
                "generation_quality_flags": list(generation_quality.get("generation_quality_flags") or []),
            }
        }

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _factor_research(snapshot: dict) -> dict:
        return dict(snapshot.get("factor_research") or {})

    @classmethod
    def _factor_maps(cls, snapshot: dict) -> tuple[Dict[str, float], Dict[str, str]]:
        artifact = cls._factor_research(snapshot)
        ranked_factors = list(artifact.get("ranked_factors") or [])
        factor_ic: Dict[str, float] = {}
        factor_trend: Dict[str, str] = {}
        for item in ranked_factors:
            name = str((item or {}).get("factor_name") or "").strip()
            if not name:
                continue
            factor_ic[name] = cls._safe_float((item or {}).get("ic_value"))
            factor_trend[name] = str((item or {}).get("trend") or "flat").strip().lower() or "flat"
        if factor_ic or factor_trend:
            return factor_ic, factor_trend
        active_candidate_pool = dict(artifact.get("active_candidate_pool") or {})
        top_candidates = list(active_candidate_pool.get("top_candidates") or [])
        for item in top_candidates:
            family = str((item or {}).get("family") or "").strip().lower()
            if not family:
                continue
            factor_ic[family] = max(
                factor_ic.get(family, 0.0),
                cls._safe_float((item or {}).get("total_score")) / 100.0,
            )
            factor_trend[family] = "rising"
        if factor_ic or factor_trend:
            return factor_ic, factor_trend
        return dict(snapshot.get("factor_ic") or {}), dict(snapshot.get("factor_ic_trend") or {})

    @classmethod
    def _strong_rising_factor_names(cls, snapshot: dict, minimum_ic: float = 0.04) -> List[str]:
        factor_ic, factor_trend = cls._factor_maps(snapshot)
        return [
            name
            for name, ic_value in factor_ic.items()
            if cls._safe_float(ic_value) >= minimum_ic and str(factor_trend.get(name) or "").strip().lower() == "rising"
        ]

    @classmethod
    def _factor_preferred_strategy_types(cls, snapshot: dict) -> List[str]:
        artifact = cls._factor_research(snapshot)
        preferred = [
            str(item).strip()
            for item in list(artifact.get("preferred_strategy_types") or [])
            if str(item).strip() in CATEGORY_MINIMUMS
        ]
        if preferred:
            return preferred

        factor_ic, factor_trend = cls._factor_maps(snapshot)
        derived: List[str] = []
        for factor_name in ("momentum", "value", "quality", "growth"):
            trend = str(factor_trend.get(factor_name) or "").strip().lower()
            ic_value = cls._safe_float(factor_ic.get(factor_name))
            if trend == "rising" and ic_value > 0.0:
                mapped = tuple(preferred_strategy_types_for_factor(factor_name, default=[]))
                for strategy_type in mapped:
                    if strategy_type in CATEGORY_MINIMUMS and strategy_type not in derived:
                        derived.append(strategy_type)
        return derived

    @staticmethod
    def _is_tradable_target_code(code: str) -> bool:
        """PR-U1: 判断股票代码是否适合作为策略目标。

        排除：
        - 北交所（920xxx、8xxxxx）—— 流动性极差
        - B 股（200xxx、900xxx）—— 外币计价
        """
        c = str(code or "").strip()
        if not c or len(c) < 6:
            return False
        if c.startswith("920") or c.startswith("8"):
            return False
        if c.startswith("200") or c.startswith("900"):
            return False
        return True

    @classmethod
    def _snapshot_target_symbol_budget(cls, strategy_type: str) -> int:
        normalized = str(strategy_type or "").strip().lower()
        return max(0, int(cls._SNAPSHOT_TARGET_SYMBOL_BUDGET_BY_TYPE.get(normalized, 0) or 0))

    @classmethod
    def _snapshot_target_family_aliases(cls, strategy_type: str) -> tuple[str, ...]:
        normalized = str(strategy_type or "").strip().lower()
        aliases = cls._SNAPSHOT_TARGET_FAMILY_ALIASES.get(normalized)
        if aliases:
            return tuple(str(item).strip().lower() for item in aliases if str(item).strip())
        return (normalized,) if normalized else tuple()

    @classmethod
    def _snapshot_target_symbols(cls, strategy_type: str, snapshot: dict) -> List[str]:
        budget = cls._snapshot_target_symbol_budget(strategy_type)
        aliases = cls._snapshot_target_family_aliases(strategy_type)
        if budget <= 0 or not aliases:
            return []

        allocation = dict(cls._factor_research(snapshot).get("stock_family_allocation") or {})
        ranked_matches: list[tuple[float, str]] = []
        for raw_code, raw_item in allocation.items():
            code = str(raw_code or "").strip()
            payload = dict(raw_item or {})
            if not code:
                continue
            plans = [
                dict(plan or {})
                for plan in list(payload.get("family_plans") or [])
                if isinstance(plan, dict)
            ]
            families = [
                str(item or "").strip().lower()
                for item in list(payload.get("families") or [])
                if str(item or "").strip()
            ]

            matched_alias_index: Optional[int] = None
            matched_rank: Optional[int] = None
            matched_budget = 0.0
            matched_penalty = 0.0
            for alias_index, alias in enumerate(aliases):
                if plans:
                    for fallback_rank, plan in enumerate(plans, 1):
                        family = str(plan.get("family") or "").strip().lower()
                        if family != alias:
                            continue
                        matched_alias_index = alias_index
                        matched_rank = max(1, int(plan.get("family_rank") or fallback_rank))
                        matched_budget = max(
                            0.0,
                            min(
                                cls._safe_float(plan.get("budget_weight") or plan.get("budget")),
                                1.0,
                            ),
                        )
                        matched_penalty = max(
                            0.0,
                            min(cls._safe_float(plan.get("failure_penalty")), 1.0),
                        )
                        break
                    if matched_alias_index is not None:
                        break
                elif alias in families:
                    matched_alias_index = alias_index
                    matched_rank = max(1, families.index(alias) + 1)
                    break
            if matched_alias_index is None:
                continue

            # PR-U1: 排除不适合做策略目标的股票（北交所、B股、老三板）
            if not cls._is_tradable_target_code(code):
                continue

            priority = max(0.0, min(cls._safe_float(payload.get("priority")), 1.0))
            top_family = str(payload.get("top_family") or "").strip().lower()
            source_bonus = 1.5 if str(payload.get("source_mode") or "").strip().lower() == "stock_universe_projection" else 0.0
            alias_bonus = max(0.0, 8.0 - matched_alias_index * 2.0)
            rank_bonus = max(0.0, 16.0 - (max(1, int(matched_rank or 1)) - 1) * 4.0)
            exact_bonus = 4.0 if top_family == str(strategy_type or "").strip().lower() else 0.0
            score = (
                priority * 100.0
                + alias_bonus
                + rank_bonus
                + matched_budget * 10.0
                - matched_penalty * 12.0
                + source_bonus
                + exact_bonus
            )
            ranked_matches.append((round(score, 4), code))

        ranked_matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return _normalize_target_codes([code for _score, code in ranked_matches], limit=budget)
    @classmethod
    def _apply_snapshot_target_alignment(cls, candidate: dict, snapshot: dict) -> dict:
        item = dict(candidate or {})
        if not item:
            return {}
        strategy_type = str(item.get("strategy_type") or "").strip().lower()
        if not strategy_type:
            return item
        existing_targets = _normalize_target_codes(
            [
                item.get("requested_target_symbols"),
                item.get("target_symbols"),
                item.get("stock_pool"),
                dict(item.get("research_task") or {}).get("target_symbols"),
                dict(item.get("research_task") or {}).get("stock_pool"),
            ],
            limit=12,
        )
        if existing_targets:
            return item

        target_pool_source = str(item.get("target_pool_source") or "").strip().lower() or None
        target_symbols = cls._event_anchor_target_symbols(
            strategy_type,
            item,
            snapshot,
            limit=max(1, cls._snapshot_target_symbol_budget(strategy_type)),
        )
        if target_symbols:
            target_pool_source = "event_anchor"
        if not target_symbols and strategy_type != "event_structure_breakout":
            target_symbols = cls._snapshot_target_symbols(strategy_type, snapshot)
            if target_symbols:
                target_pool_source = "explicit_task"
        if not target_symbols:
            target_symbols = cls._focus_industry_target_symbols(
                strategy_type,
                item,
                snapshot,
                limit=max(1, cls._snapshot_target_symbol_budget(strategy_type)),
            )
            if target_symbols:
                target_pool_source = "static_fallback"
        if not target_symbols:
            return item

        candidate_family = str(item.get("candidate_family") or strategy_type).strip().lower() or strategy_type
        focus_industries = cls._normalize_text_list(
            dict(item.get("event_prefilter") or {}).get("focus_industries"),
            dict(item.get("research_task") or {}).get("focus_industries"),
            dict(snapshot or {}).get("hot_sectors"),
            limit=3,
        )
        research_task = {
            **dict(item.get("research_task") or {}),
            "task_source": "snapshot",
            "preferred_strategy_types": [strategy_type],
            "allowed_strategy_types": [strategy_type],
            "strategy_preferences": [strategy_type],
            "candidate_family": candidate_family,
            "target_symbols": list(target_symbols),
            "stock_pool": {"selection_mode": "explicit", "symbols": list(target_symbols)},
            "target_symbol_policy": "strict_intersection",
            "universe_expansion_policy": "forbid",
            "validation_focus": "candidate_target_only",
            "focus_industries": list(focus_industries),
            "preference_strength": "soft",
            "preference_reason": f"snapshot_local_spawn:{strategy_type}",
            "gate_1_representative_count": min(3, len(target_symbols)),
            "synthetic_local_spawn": True,
            "target_pool_source": target_pool_source or "explicit_task",
        }
        tags = list(item.get("tags") or [])
        for tag in ("targeted_universe", "synthetic_local_spawn"):
            if tag not in tags:
                tags.append(tag)

        return {
            **item,
            "candidate_family": candidate_family,
            "research_task": research_task,
            "requested_target_symbols": list(target_symbols),
            "target_symbols": list(target_symbols),
            "stock_pool": {"selection_mode": "explicit", "symbols": list(target_symbols)},
            "target_pool_source": target_pool_source or "explicit_task",
            "tags": tags,
        }

    def spawn(self, snapshot: dict) -> List[dict]:
        event_ready = self._event_research_ready(snapshot)
        event_ready_supplemental = self._event_ready_supports_local_fill(snapshot)
        source_batches = self._build_signal_batches(snapshot)
        signal_candidates, source_raw_counts, source_budget_caps, source_budget_weights = self._merge_signal_batches(
            source_batches,
            event_ready=event_ready,
            event_ready_supplemental=event_ready_supplemental,
        )
        signal_candidates, signal_feedback_summary = self._apply_family_feedback_to_signal_candidates(
            snapshot,
            signal_candidates,
        )
        signal_candidates += self._expand_signal_variants(snapshot, signal_candidates)
        quota_candidates = self._fill_gaps(snapshot, signal_candidates)
        candidates = [
            self._apply_snapshot_target_alignment(candidate, snapshot)
            for candidate in [*signal_candidates, *quota_candidates]
        ]
        candidates = [
            apply_evidence_first_candidate(candidate, snapshot=snapshot)
            for candidate in candidates
        ]
        self.last_report = self._build_spawn_report(
            candidates,
            event_ready=event_ready,
            event_ready_supplemental=event_ready_supplemental,
            source_raw_counts=source_raw_counts,
            source_budget_caps=source_budget_caps,
            source_budget_weights=source_budget_weights,
            signal_feedback_summary=signal_feedback_summary,
        )
        return candidates

    def _build_signal_batches(self, snapshot: dict) -> Dict[str, List[dict]]:
        return {
            "event_driven": self._from_event_driven(snapshot),
            "fear_greed": self._from_fear_greed(snapshot),
            "factor_ic": self._from_factor_ic(snapshot),
            "factor_pool": self._from_factor_pool(snapshot),
            "volatility": self._from_volatility(snapshot),
            "fund_flow": self._from_fund_flow(snapshot),
        }

    def _apply_family_feedback_to_signal_candidates(
        self,
        snapshot: dict,
        signal_candidates: List[dict],
    ) -> tuple[List[dict], dict]:
        candidates = [dict(item or {}) for item in list(signal_candidates or []) if isinstance(item, dict)]
        if not candidates:
            return [], {
                "signal_feedback_limited_count": 0,
                "signal_feedback_limited_type_counts": {},
                "signal_feedback_factor_by_type": {},
            }

        type_counts: Dict[str, int] = {}
        type_labels: Dict[str, str] = {}
        type_has_governed_signal: Dict[str, bool] = {}
        for item in candidates:
            strategy_type = str(item.get("strategy_type") or "").strip()
            if not strategy_type:
                continue
            normalized = strategy_type.lower()
            type_counts[normalized] = type_counts.get(normalized, 0) + 1
            type_labels.setdefault(normalized, strategy_type)
            generation_source = str(dict(item.get("generation_reason") or {}).get("source") or "").strip().lower()
            if (
                generation_source == "factor_pool"
                or str(item.get("factor_pool_factor_id") or "").strip()
                or str(item.get("source_candidate_artifact_id") or "").strip()
            ):
                type_has_governed_signal[normalized] = True

        allowed_by_type: Dict[str, int] = {}
        factor_by_type: Dict[str, float] = {}
        for normalized, count in type_counts.items():
            label = type_labels.get(normalized, normalized)
            factor = self._family_negative_feedback_factor(label, snapshot)
            factor_by_type[normalized] = factor
            if factor >= 1.0:
                allowed_by_type[normalized] = count
            elif factor <= 0:
                allowed_by_type[normalized] = 0
            else:
                allowed_by_type[normalized] = max(0, int(round(count * factor)))
            if (
                allowed_by_type.get(normalized, 0) <= 0
                and factor > 0
                and bool(type_has_governed_signal.get(normalized))
            ):
                allowed_by_type[normalized] = 1

        kept: List[dict] = []
        kept_counts: Dict[str, int] = {}
        filtered_counts: Dict[str, int] = {}
        for item in candidates:
            strategy_type = str(item.get("strategy_type") or "").strip()
            normalized = strategy_type.lower()
            if not normalized:
                kept.append(item)
                continue
            allowed = allowed_by_type.get(normalized, type_counts.get(normalized, 0))
            current = kept_counts.get(normalized, 0)
            if current < allowed:
                kept.append(item)
                kept_counts[normalized] = current + 1
                continue
            filtered_counts[strategy_type] = filtered_counts.get(strategy_type, 0) + 1

        limited_factors = {
            type_labels.get(normalized, normalized): round(float(factor), 4)
            for normalized, factor in factor_by_type.items()
            if factor < 1.0
        }
        return kept, {
            "signal_feedback_limited_count": sum(filtered_counts.values()),
            "signal_feedback_limited_type_counts": filtered_counts,
            "signal_feedback_factor_by_type": limited_factors,
        }

    @staticmethod
    def _event_ready_source_cap(*, event_ready_supplemental: bool) -> int:
        return max(0, SPAWNER_EVENT_SOURCE_BASE_CAP + (SPAWNER_EVENT_SOURCE_SUPPLEMENTAL_BONUS if event_ready_supplemental else 0))

    @staticmethod
    def _event_ready_source_weights(*, event_ready_supplemental: bool) -> Dict[str, float]:
        return get_event_ready_source_weights(
            event_ready_supplemental=event_ready_supplemental
        )

    @staticmethod
    def _weighted_source_cap(raw_count: int, *, weight: float, minimum_floor: int) -> int:
        if raw_count <= 0:
            return 0
        scaled = int(round(raw_count * max(0.0, weight)))
        return max(1, min(raw_count, max(minimum_floor, scaled)))

    @classmethod
    def _merge_signal_batches(
        cls,
        source_batches: Dict[str, List[dict]],
        *,
        event_ready: bool,
        event_ready_supplemental: bool,
    ) -> tuple[List[dict], Dict[str, int], Dict[str, Optional[int]], Dict[str, Optional[float]]]:
        source_raw_counts = {source: len(list(items or [])) for source, items in dict(source_batches or {}).items()}
        if not event_ready:
            ordered = [
                *list(source_batches.get("event_driven") or []),
                *list(source_batches.get("fear_greed") or []),
                *list(source_batches.get("factor_ic") or []),
                *list(source_batches.get("factor_pool") or []),
                *list(source_batches.get("volatility") or []),
                *list(source_batches.get("fund_flow") or []),
            ]
            return (
                ordered,
                source_raw_counts,
                {source: None for source in source_raw_counts},
                {source: None for source in source_raw_counts},
            )

        local_source_floor = cls._event_ready_source_cap(event_ready_supplemental=event_ready_supplemental)
        source_weights = cls._event_ready_source_weights(event_ready_supplemental=event_ready_supplemental)
        capped_batches: Dict[str, List[dict]] = {}
        source_budget_caps: Dict[str, Optional[int]] = {}
        source_budget_weights: Dict[str, Optional[float]] = {}

        for source in ("event_driven", "fear_greed", "factor_ic", "factor_pool", "volatility", "fund_flow"):
            items = list(source_batches.get(source) or [])
            weight = float(source_weights.get(source, 1.0) or 0.0)
            if source in {"event_driven", "factor_ic", "factor_pool"}:
                capped_batches[source] = items
                source_budget_caps[source] = None
                source_budget_weights[source] = weight
                continue
            cap = cls._weighted_source_cap(
                len(items),
                weight=weight,
                minimum_floor=local_source_floor,
            )
            capped_batches[source] = items[:cap]
            source_budget_caps[source] = cap
            source_budget_weights[source] = weight

        ordered = [
            *capped_batches["event_driven"],
            *capped_batches["fear_greed"],
            *capped_batches["factor_ic"],
            *capped_batches["factor_pool"],
            *capped_batches["volatility"],
            *capped_batches["fund_flow"],
        ]
        return ordered, source_raw_counts, source_budget_caps, source_budget_weights

    @staticmethod
    def _event_research_ready(snapshot: dict) -> bool:
        event_driven = dict(snapshot.get("event_driven") or {})
        return bool(int(event_driven.get("event_count") or 0) or int(event_driven.get("tasks_ready_count") or 0))

    @staticmethod
    def _event_ready_supports_local_fill(snapshot: dict) -> bool:
        strong_factor_count = len(StrategySpawner._strong_rising_factor_names(snapshot, minimum_ic=0.04))
        fear_greed = float(snapshot.get("fear_greed_index") or 50.0)
        volatility = float(dict(snapshot.get("fg_components") or {}).get("volatility") or 50.0)
        north_3d = abs(float(snapshot.get("north_fund_3d_net") or 0.0)) >= 5_000_000_000
        margin_5d = abs(float(snapshot.get("margin_5d_change_pct") or 0.0)) >= 2.0
        extreme_fg = abs(fear_greed - 50.0) >= 18.0
        extreme_volatility = volatility <= 35.0 or volatility >= 65.0
        return bool(strong_factor_count or extreme_fg or extreme_volatility or north_3d or margin_5d)

    @staticmethod
    def _generated_type_counts(candidates: List[dict]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in list(candidates or []):
            strategy_type = str(item.get("strategy_type") or "").strip()
            if not strategy_type:
                continue
            counts[strategy_type] = counts.get(strategy_type, 0) + 1
        return counts

    @classmethod
    def _preferred_fill_types(cls, snapshot: dict, current_counts: Optional[Dict[str, int]] = None) -> List[str]:
        preferred: List[str] = []

        def add(*types: str) -> None:
            for strategy_type in types:
                if strategy_type in CATEGORY_MINIMUMS and strategy_type not in preferred:
                    preferred.append(strategy_type)

        fear_greed = int(snapshot.get("fear_greed_index") or 50)
        if fear_greed <= 35:
            add("rsi", "gap_fill", "mean_reversion_short", "quality_factor", "macro_timing")
        elif fear_greed >= 65:
            add("event_structure_breakout", "growth_factor", "quality_factor", "north_capital_track", "sector_rotation", "volatility_breakout")
        else:
            add("event_structure_breakout", "quality_factor", "north_capital_track", "sector_rotation", "gap_fill", "ma_cross")

        north_3d = float(snapshot.get("north_fund_3d_net") or 0.0)
        if north_3d >= 5_000_000_000:
            add("north_capital_track", "quality_factor", "growth_factor", "sector_rotation")
        elif north_3d <= -5_000_000_000:
            add("value_factor", "macro_timing", "rsi", "quality_factor")

        margin_5d = float(snapshot.get("margin_5d_change_pct") or 0.0)
        if margin_5d >= 2.0:
            add("event_structure_breakout", "volatility_breakout", "sector_rotation", "north_capital_track")
        elif margin_5d <= -2.0:
            add("rsi", "gap_fill", "value_factor")

        add(*cls._factor_preferred_strategy_types(snapshot))

        event_driven = dict(snapshot.get("event_driven") or {})
        if int(event_driven.get("event_count") or 0) > 0 or int(event_driven.get("tasks_ready_count") or 0) > 0:
            add("event_structure_breakout", "quality_factor", "north_capital_track", "sector_rotation", "gap_fill")

        if not preferred:
            add("event_structure_breakout", "quality_factor", "north_capital_track", "gap_fill", "ma_cross")

        counts = current_counts or {}
        return sorted(preferred, key=lambda strategy_type: (int(counts.get(strategy_type) or 0), preferred.index(strategy_type)))

    @staticmethod
    def _quota_fill_budget(snapshot: dict, signal_candidate_count: int) -> int:
        completeness = dict(snapshot.get("completeness") or {})
        completion_ratio = float(completeness.get("completion_ratio") or 1.0)
        event_ready = StrategySpawner._event_research_ready(snapshot)
        has_historical_distribution = ParameterDistributionRegistry.from_snapshot(snapshot).has_any_distribution()
        target_total = SPAWNER_TARGET_TOTAL
        if completion_ratio < 1.0:
            target_total = min(target_total, max(4, int(round(SPAWNER_TARGET_TOTAL * 0.75))))
        budget = max(0, target_total - max(0, int(signal_candidate_count or 0)))
        if event_ready:
            if signal_candidate_count <= 0 or not StrategySpawner._event_ready_supports_local_fill(snapshot):
                return 0
            return min(budget, SPAWNER_EVENT_FILL_BUDGET_MAX)
        if signal_candidate_count <= 0 and not has_historical_distribution:
            return min(budget, 1)
        return min(budget, SPAWNER_FILL_BUDGET_MAX)

    @staticmethod
    def _signal_expansion_budget(snapshot: dict, signal_candidate_count: int) -> int:
        if signal_candidate_count <= 0:
            return 0
        remaining = max(0, SPAWNER_TARGET_TOTAL - max(0, int(signal_candidate_count or 0)))
        if remaining <= 0:
            return 0
        strong_factor_count = len(StrategySpawner._strong_rising_factor_names(snapshot, minimum_ic=0.04))
        extreme_fg = abs(float(snapshot.get("fear_greed_index") or 50.0) - 50.0) >= 18.0
        north_3d = abs(float(snapshot.get("north_fund_3d_net") or 0.0)) >= 5_000_000_000
        margin_5d = abs(float(snapshot.get("margin_5d_change_pct") or 0.0)) >= 2.0
        signal_strength = strong_factor_count + int(extreme_fg) + int(north_3d) + int(margin_5d)
        if signal_strength <= 0:
            return 0
        return min(remaining, SPAWNER_FILL_BUDGET_MAX, max(1, signal_strength + 1))

    @staticmethod
    def _family_negative_feedback_factor(strategy_type: str, snapshot: dict) -> float:
        family_feedback = dict((snapshot or {}).get("family_gate_feedback") or {})
        normalized_type = str(strategy_type or "").strip().lower()
        if not normalized_type:
            return 1.0
        entry = dict(
            family_feedback.get(normalized_type)
            or family_feedback.get(str(strategy_type or "").strip())
            or {}
        )
        if not entry:
            return 1.0
        try:
            ema = float(entry.get("ema_submit_count") or 0.0)
        except (TypeError, ValueError):
            ema = 1.0
        try:
            raw_failure_rate = entry.get("gate_failure_rate")
            if raw_failure_rate is None:
                raw_failure_rate = entry.get("submission_gate_failure_rate")
            gate_failure_rate = float(raw_failure_rate or 0.0)
        except (TypeError, ValueError):
            gate_failure_rate = 0.0
        if bool(entry.get("freeze_active")):
            return 0.0
        if bool(entry.get("suppressed")) or bool(entry.get("suppress_active")):
            return 0.15
        if bool(entry.get("cooldown_active")) or gate_failure_rate >= 0.95:
            return 0.25
        if gate_failure_rate >= 0.75:
            return 0.5
        if ema >= 1.0:
            return 1.0
        if ema >= 0.3:
            return 0.6
        return 0.25

    def _expand_signal_variants(self, snapshot: dict, signal_candidates: List[dict]) -> List[dict]:
        expansion_budget = self._signal_expansion_budget(snapshot, len(signal_candidates))
        if expansion_budget <= 0:
            return []
        parameter_registry = ParameterDistributionRegistry.from_snapshot(snapshot)

        current_counts = self._generated_type_counts(signal_candidates)
        threshold_hits: Dict[str, int] = {}
        preferred_types = self._preferred_fill_types(snapshot, current_counts)
        for item in list(signal_candidates or []):
            strategy_type = str(item.get("strategy_type") or "").strip()
            if not strategy_type:
                continue
            threshold_hits[strategy_type] = threshold_hits.get(strategy_type, 0) + len(item.get("trigger_thresholds") or [])

        ranked_types = sorted(
            current_counts.keys(),
            key=lambda strategy_type: (
                int(current_counts.get(strategy_type) or 0),
                int(threshold_hits.get(strategy_type) or 0),
                -(preferred_types.index(strategy_type) if strategy_type in preferred_types else len(preferred_types)),
            ),
            reverse=True,
        )
        if not ranked_types:
            return []

        existing_keys = {
            (
                str(item.get("strategy_type") or ""),
                json.dumps(item.get("params") or {}, sort_keys=True, ensure_ascii=False, default=str),
            )
            for item in list(signal_candidates or [])
        }
        out: List[dict] = []
        variation_counts: Dict[str, int] = {}

        for strategy_type in ranked_types:
            if len(out) >= expansion_budget:
                break
            generation_cap = self._local_generation_cap(strategy_type)
            existing_total_for_type = int(current_counts.get(strategy_type) or 0) + int(variation_counts.get(strategy_type) or 0)
            if generation_cap is not None and existing_total_for_type >= generation_cap:
                continue
            desired_variants = min(
                3,
                max(
                    1,
                    int(current_counts.get(strategy_type) or 0) - 1 + (1 if int(threshold_hits.get(strategy_type) or 0) >= 3 else 0),
                ),
            )
            # PR-S12: 按负反馈系数缩减（最少 0，跳过该 type）
            feedback_factor = self._family_negative_feedback_factor(strategy_type, snapshot)
            desired_variants = max(0, int(round(desired_variants * feedback_factor)))
            if generation_cap is not None:
                desired_variants = min(desired_variants, max(0, generation_cap - existing_total_for_type))
            if desired_variants <= 0:
                continue
            for _ in range(desired_variants):
                if len(out) >= expansion_budget:
                    break
                slot_index = int(current_counts.get(strategy_type) or 0) + int(variation_counts.get(strategy_type) or 0)
                params, parameter_source, parameter_sample_count = self._resolved_varied_defaults(
                    strategy_type,
                    slot_index,
                    snapshot=snapshot,
                    registry=parameter_registry,
                )
                key = (strategy_type, json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str))
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                variation_counts[strategy_type] = int(variation_counts.get(strategy_type) or 0) + 1
                candidate = self._make(
                    strategy_type,
                    params,
                    f"{strategy_type} 强信号延展参数变体#{variation_counts[strategy_type]}",
                    source="signal_variation",
                    trigger_signal={
                        "field": f"signal_type_counts.{strategy_type}",
                        "value": int(current_counts.get(strategy_type) or 0),
                        "threshold_hits": int(threshold_hits.get(strategy_type) or 0),
                        "parameter_source": parameter_source,
                    },
                    trigger_thresholds=[
                        self._threshold(
                            f"signal_type_counts.{strategy_type}",
                            ">=",
                            1,
                            int(current_counts.get(strategy_type) or 0),
                            "强信号变体扩容",
                        )
                    ],
                )
                candidate["parameter_source"] = parameter_source
                candidate["parameter_sample_count"] = parameter_sample_count
                out.append(candidate)
        return out

    def _from_fear_greed(self, snapshot: dict) -> List[dict]:
        out: List[dict] = []
        fear_greed = snapshot.get("fear_greed_index", 50)
        if fear_greed < 30:
            out.append(
                self._make(
                    "margin_divergence",
                    self._high_precision_margin_divergence_params(),
                    f"恐贪{fear_greed}，高精度流动性背离修复",
                    source="fear_greed",
                    trigger_signal={"field": "fear_greed_index", "value": fear_greed, "level": "fear"},
                    trigger_thresholds=[self._threshold("fear_greed_index", "<", 30, fear_greed, "恐贪阈值")],
                    extras=self._high_precision_candidate_fields(
                        preferred_regime="liquidity_repair_with_volume_reexpansion",
                        avoid_regime="volume_vacuum_or_failed_rebound",
                        holding_rationale="只在恐慌释放后出现缩量止跌、放量修复和结构转强共振时参与，避免把普通反抽误当成修复。",
                        failure_mode={
                            "primary_failure_mode": "failed_rebound_after_liquidity_dryup",
                            "secondary_failure_mode": "false_reexpansion",
                        },
                        entry_bias="liquidity_divergence_repair_confirmation",
                        exit_bias="liquidity_break_or_time_stop",
                    ),
                )
            )
            out.append(self._make("value_factor", {"lookback": 60, "buy_quantile": 0.85, "sell_quantile": 0.15}, f"恐贪{fear_greed}，恐惧期精选价值", source="fear_greed", trigger_signal={"field": "fear_greed_index", "value": fear_greed, "level": "fear"}, trigger_thresholds=[self._threshold("fear_greed_index", "<", 30, fear_greed, "恐贪阈值")]))
        elif fear_greed > 70:
            out.append(self._make("momentum", {"lookback": 5, "threshold": 0.01}, f"恐贪{fear_greed}，贪婪期短周期动量", source="fear_greed", trigger_signal={"field": "fear_greed_index", "value": fear_greed, "level": "greed"}, trigger_thresholds=[self._threshold("fear_greed_index", ">", 70, fear_greed, "恐贪阈值")]))
            out.append(self._make("momentum", {"lookback": 10, "threshold": 0.02}, f"恐贪{fear_greed}，贪婪期中周期动量", source="fear_greed", trigger_signal={"field": "fear_greed_index", "value": fear_greed, "level": "greed"}, trigger_thresholds=[self._threshold("fear_greed_index", ">", 70, fear_greed, "恐贪阈值")]))
            out.append(self._make("growth_factor", {"lookback": 40, "buy_quantile": 0.85, "sell_quantile": 0.15}, f"恐贪{fear_greed}，贪婪期成长加速", source="fear_greed", trigger_signal={"field": "fear_greed_index", "value": fear_greed, "level": "greed"}, trigger_thresholds=[self._threshold("fear_greed_index", ">", 70, fear_greed, "恐贪阈值")]))
        else:
            out.append(self._make("ma_cross", {"short_period": 5, "long_period": 20}, f"恐贪{fear_greed}，中性标准均线", source="fear_greed", trigger_signal={"field": "fear_greed_index", "value": fear_greed, "level": "neutral"}, trigger_thresholds=[self._threshold("fear_greed_index", ">=", 30, fear_greed, "恐贪下界"), self._threshold("fear_greed_index", "<=", 70, fear_greed, "恐贪上界")]))
            out.append(self._make("momentum", {"lookback": 20, "threshold": 0.02}, f"恐贪{fear_greed}，中性标准动量", source="fear_greed", trigger_signal={"field": "fear_greed_index", "value": fear_greed, "level": "neutral"}, trigger_thresholds=[self._threshold("fear_greed_index", ">=", 30, fear_greed, "恐贪下界"), self._threshold("fear_greed_index", "<=", 70, fear_greed, "恐贪上界")]))
        return out
    def _factor_pool_oos_passed(self, factor: dict) -> bool:
        """ALPHA-WIRING-V1 (P-D a)：因子是否满足 OOS/鲁棒证据门。

        读 validation_summary.evidence_summary（factory.py 在 promote 时写入），要求：
        - sample_dates ≥ MIN_SAMPLE_DATES（样本期足够，OOS 才有意义）
        - rank_ic_ir ≥ MIN_RANK_IC_IR（信息比率达标，剔除噪声因子）
        - lookahead_risk != 'high'（无前视风险）
        evidence_summary 缺失视为未过（保守）。
        """
        validation_summary = dict(factor.get("validation_summary") or {})
        evidence = dict(validation_summary.get("evidence_summary") or {})
        if not evidence:
            return False
        sample_dates = self._safe_float(evidence.get("sample_dates"))
        rank_ic_ir = self._safe_float(evidence.get("rank_ic_ir"))
        lookahead_risk = str(evidence.get("lookahead_risk") or "").strip().lower()
        if sample_dates < float(STRATEGY_FACTORY_FACTOR_POOL_OOS_MIN_SAMPLE_DATES):
            return False
        if rank_ic_ir < float(STRATEGY_FACTORY_FACTOR_POOL_OOS_MIN_RANK_IC_IR):
            return False
        if lookahead_risk == "high":
            return False
        return True

    @classmethod
    def _factor_pool_strategy_type_options(cls, factor: dict) -> List[str]:
        payload = dict(factor or {})
        validation_summary = dict(payload.get("validation_summary") or {})
        options: List[str] = []

        def add_many(values) -> None:
            if isinstance(values, str):
                values = [values]
            for value in list(values or []):
                strategy_type = str(value or "").strip()
                if strategy_type in CATEGORY_MINIMUMS and strategy_type not in options:
                    options.append(strategy_type)

        add_many(payload.get("preferred_strategy_types"))
        add_many(validation_summary.get("preferred_strategy_types"))
        for key in (
            payload.get("family"),
            payload.get("name"),
            payload.get("factor_name"),
            payload.get("factor_id"),
        ):
            add_many(preferred_strategy_types_for_factor(str(key or ""), default=[]))
        if not options:
            options.append("multi_factor")
        return options

    def _select_factor_pool_strategy_type(
        self,
        factor: dict,
        snapshot: dict,
        generated_counts: Dict[str, int],
    ) -> str:
        options = self._factor_pool_strategy_type_options(factor)
        total_generated = sum(int(value or 0) for value in generated_counts.values())
        trend_generated = sum(
            int(generated_counts.get(strategy_type) or 0)
            for strategy_type in self._TREND_CLUSTER_TYPES
        )
        ranked: List[tuple[float, int, str]] = []
        for index, strategy_type in enumerate(options):
            feedback_factor = self._family_negative_feedback_factor(strategy_type, snapshot)
            current_count = int(generated_counts.get(strategy_type) or 0)
            trend_penalty = 0.0
            if strategy_type in self._TREND_CLUSTER_TYPES:
                projected_total = total_generated + 1
                projected_trend = trend_generated + 1
                if projected_total > 0 and projected_trend / projected_total > 0.5:
                    trend_penalty = 2.0
            profile = self._POOL_PROFILE_BY_TYPE.get(strategy_type, "unknown")
            profile_count = sum(
                int(count or 0)
                for existing_type, count in generated_counts.items()
                if self._POOL_PROFILE_BY_TYPE.get(existing_type, "unknown") == profile
            )
            score = (
                max(0.0, float(feedback_factor or 0.0)) * 10.0
                - current_count * 1.5
                - profile_count * 0.5
                - trend_penalty
                - index * 0.05
            )
            ranked.append((score, -index, strategy_type))
        ranked.sort(reverse=True)
        return ranked[0][2] if ranked else "multi_factor"

    @staticmethod
    def _compact_factor_pool_validation_summary(factor: dict) -> dict:
        validation_summary = dict((factor or {}).get("validation_summary") or {})
        if not validation_summary:
            return {}
        compact = {
            "source": "active_factor_pool",
            "quality_status": validation_summary.get("quality_status"),
            "quality_score": validation_summary.get("quality_score"),
            "metrics": dict(validation_summary.get("metrics") or {}),
            "rating": dict(validation_summary.get("rating") or {}),
            "evidence_summary": dict(validation_summary.get("evidence_summary") or {}),
            "quick_evidence": dict(validation_summary.get("quick_evidence") or {}),
            "qc_labels": dict(validation_summary.get("qc_labels") or {}),
            "qc_shelf_decision": dict(validation_summary.get("qc_shelf_decision") or {}),
            "qc_autoshelf_applied": bool(validation_summary.get("qc_autoshelf_applied")),
            "promotion_block_reasons": list(validation_summary.get("promotion_block_reasons") or []),
        }
        persisted_outputs = dict(validation_summary.get("persisted_outputs") or {})
        if persisted_outputs:
            compact["persisted_outputs"] = {
                key: persisted_outputs.get(key)
                for key in ("enabled", "ic_history_rows", "ic_history_rows_total", "factor_key")
                if persisted_outputs.get(key) not in (None, "", [], {})
            }
        return {
            key: value
            for key, value in compact.items()
            if value not in (None, "", [], {})
        }

    def _from_factor_pool(self, snapshot: dict) -> List[dict]:
        out: List[dict] = []
        factor_research = dict(snapshot.get("factor_research") or {})
        pool_payload = dict(
            factor_research.get("factory_pool_payload")
            or snapshot.get("factory_pool_payload")
            or {}
        )
        factors = [
            dict(item or {})
            for item in list(
                pool_payload.get("factors")
                or factor_research.get("factory_pool_factors")
                or snapshot.get("factory_pool_factors")
                or []
            )
            if isinstance(item, dict)
        ]

        for factor in factors[:8]:
            factor_id = str(factor.get("factor_id") or "").strip()
            factor_name = str(
                factor.get("name")
                or factor.get("factor_name")
                or factor_id
            ).strip()
            factor_dsl = str(
                factor.get("expression_dsl")
                or factor.get("factor_dsl")
                or factor.get("dsl")
                or ""
            ).strip()
            if not factor_id or not factor_name or not factor_dsl:
                continue

            # ALPHA-WIRING-V1 (P-D a)：OOS/鲁棒证据门（默认 OFF，零变化）。
            # ON 时只放行 validation_summary.evidence_summary 满足 OOS 质量的因子。
            if STRATEGY_FACTORY_FACTOR_POOL_OOS_GATE_ENABLED and not self._factor_pool_oos_passed(factor):
                continue

            family = str(factor.get("family") or factor_name).strip().lower()
            strategy_type = self._select_factor_pool_strategy_type(
                factor,
                snapshot,
                self._generated_type_counts(out),
            )
            fitness = self._safe_float(factor.get("fitness"))
            decay_rate = self._safe_float(factor.get("decay_rate"))
            current_ic = self._safe_float(factor.get("current_ic"))
            grade = str(factor.get("admission_grade") or factor.get("grade") or "").strip()
            engine = str(factor.get("generation_engine") or "").strip()
            validation_summary = dict(factor.get("validation_summary") or {})
            factor_pool_validation_summary = self._compact_factor_pool_validation_summary(factor)
            source_artifact_id = str(
                factor.get("source_validation_artifact_id")
                or validation_summary.get("artifact_id")
                or validation_summary.get("validation_artifact_id")
                or factor_id
            ).strip()
            candidate_provenance = {
                "source_candidate_artifact_id": source_artifact_id,
                "source_validation_artifact_id": source_artifact_id,
                "candidate_family": family or strategy_type,
                "candidate_registry_stage": str(factor.get("registry_stage") or "active_factor_pool"),
                "generator_mode": "factor_pool",
                "generator_type": "factor_pool",
                "alpha_source": "factor_mining_active_pool",
                "risk_level": str(dict(factor.get("risk_audit") or {}).get("overall_risk_level") or ""),
                "validation_score": validation_summary.get("total_score")
                or dict(validation_summary.get("rating") or {}).get("total_score"),
            }
            params = {
                "factor_dsl": factor_dsl,
                "factor_name": factor_name,
                "fitness": fitness,
                "grade": grade,
                "engine": engine,
                "factor_pool_factor_id": factor_id,
                "factor_pool_fitness": fitness,
                "factor_pool_grade": grade,
                "factor_pool_engine": engine,
                "factor_pool_current_ic": current_ic,
                "factor_pool_decay_rate": decay_rate,
                "factor_pool_validation_summary": factor_pool_validation_summary,
                "lookback": int(factor.get("expected_holding_period") or 60),
                "source_candidate_artifact_id": source_artifact_id,
                "candidate_provenance": candidate_provenance,
                "validation_profile": {
                    "profile": "factor_rank_validation",
                    "validation_focus": "broad_generalization",
                    "primary_validation_layer": "combined",
                    "factor_pool_candidate": True,
                },
                "targeting_policy": {
                    "target_symbol_policy": "prefer_intersection",
                    "universe_expansion_policy": "allow_market_fallback",
                    "validation_focus": "broad_generalization",
                },
            }
            if strategy_type == "multi_factor":
                params["factor_weights"] = {factor_name: 1.0}
            extras = {
                "source_candidate_artifact_id": source_artifact_id,
                "candidate_origin": "factor_pool",
                "candidate_family": family or strategy_type,
                "generator_mode": "factor_pool",
                "generator_type": "factor_pool",
                "factor_pool_factor_id": factor_id,
                "candidate_provenance": candidate_provenance,
                "validation_profile": {
                    "profile": "factor_rank_validation",
                    "validation_focus": "broad_generalization",
                    "primary_validation_layer": "combined",
                    "factor_pool_candidate": True,
                },
                "metadata": {
                    "factor_pool_factor_id": factor_id,
                    "factor_pool_factor_name": factor_name,
                },
                "factor_pool_metadata": {
                    "factor_id": factor_id,
                    "factor_name": factor_name,
                    "family": family,
                    "fitness": fitness,
                    "grade": grade,
                    "engine": engine,
                    "current_ic": current_ic,
                    "decay_rate": decay_rate,
                    "source_validation_artifact_id": source_artifact_id,
                    "validation_summary": factor_pool_validation_summary,
                },
                "tags": ["factor_pool", "active_factor_pool"],
            }
            out.append(
                self._make(
                    strategy_type,
                    params,
                    f"active factor pool: {factor_name}",
                    source="factor_pool",
                    trigger_signal={
                        "field": "factor_pool",
                        "factor_id": factor_id,
                        "factor_name": factor_name,
                        "fitness": fitness,
                        "current_ic": current_ic,
                    },
                    trigger_thresholds=[
                        self._threshold(
                            f"factor_pool.{factor_name}.fitness",
                            ">",
                            0.0,
                            fitness,
                            "active pool factor",
                        )
                    ],
                    extras=extras,
                )
            )
        return out

    def _from_factor_ic(self, snapshot: dict) -> List[dict]:
        out: List[dict] = []
        factor_ic, trend = self._factor_maps(snapshot)

        for factor_name, ic_value in factor_ic.items():
            trend_value = trend.get(factor_name, "flat")
            if ic_value > STRATEGY_FACTORY_FACTOR_IC_CLASSIC_MIN_IC and trend_value == "rising":
                if factor_name == "momentum":
                    event_prefilter = self._snapshot_event_prefilter(
                        snapshot,
                        source="factor_ic",
                        trigger_signal={"field": "factor_ic", "factor": factor_name, "value": ic_value, "trend": trend_value},
                    )
                    if event_prefilter.get("passed"):
                        out.append(
                            self._make(
                                "event_structure_breakout",
                                self._high_precision_event_structure_breakout_params(),
                                f"momentum IC={ic_value:.3f}上升，事件锚确认后高精度事件结构突破",
                                source="factor_ic",
                                trigger_signal={"field": "factor_ic", "factor": factor_name, "value": ic_value, "trend": trend_value},
                                trigger_thresholds=[
                                    self._threshold(f"factor_ic.{factor_name}", ">", 0.03, ic_value, "IC阈值"),
                                    self._threshold(f"factor_ic_trend.{factor_name}", "==", "rising", trend_value, "趋势阈值"),
                                ],
                                extras=self._high_precision_candidate_fields(
                                    preferred_regime="event_follow_through_with_structure_confirmation",
                                    avoid_regime="false_breakout_or_post_event_mean_reversion",
                                    holding_rationale="只在催化或强势脉冲后经历缩量整理并放量突破时参与，优先结构延续而不是普通趋势追涨。",
                                    failure_mode={
                                        "primary_failure_mode": "false_breakout_after_event_impulse",
                                        "secondary_failure_mode": "late_entry_after_extension",
                                    },
                                    entry_selectivity="strict_event_breakout",
                                    max_position_pct=0.14,
                                    capacity_bucket="mid",
                                    min_days=3,
                                    max_days=12,
                                    entry_bias="event_structure_breakout_confirmation",
                                    exit_bias="breakout_failure_or_time_stop",
                                    event_prefilter=event_prefilter,
                                    candidate_origin="research_signal",
                                    event_anchor=dict(event_prefilter.get("event_anchor") or {}),
                                    target_pool_source=(
                                        "event_anchor"
                                        if self._event_anchor_has_explicit_source(event_prefilter.get("event_anchor"))
                                        else "static_fallback"
                                    ),
                                ),
                            )
                        )
                    for lookback in [5, 10, 20]:
                        out.append(self._make("momentum", {"lookback": lookback, "threshold": 0.02}, f"momentum IC={ic_value:.3f}上升，{lookback}日动量", source="factor_ic", trigger_signal={"field": "factor_ic", "factor": factor_name, "value": ic_value, "trend": trend_value}, trigger_thresholds=[self._threshold(f"factor_ic.{factor_name}", ">", 0.03, ic_value, "IC阈值"), self._threshold(f"factor_ic_trend.{factor_name}", "==", "rising", trend_value, "趋势阈值")]))
                elif factor_name == "value":
                    out.append(self._make("value_factor", {"lookback": 60, "buy_quantile": 0.8, "sell_quantile": 0.2}, f"value IC={ic_value:.3f}上升", source="factor_ic", trigger_signal={"field": "factor_ic", "factor": factor_name, "value": ic_value, "trend": trend_value}, trigger_thresholds=[self._threshold(f"factor_ic.{factor_name}", ">", 0.03, ic_value, "IC阈值"), self._threshold(f"factor_ic_trend.{factor_name}", "==", "rising", trend_value, "趋势阈值")]))
                elif factor_name == "quality":
                    out.append(self._make("quality_factor", {"lookback": 60, "buy_quantile": 0.8, "sell_quantile": 0.2}, f"quality IC={ic_value:.3f}上升", source="factor_ic", trigger_signal={"field": "factor_ic", "factor": factor_name, "value": ic_value, "trend": trend_value}, trigger_thresholds=[self._threshold(f"factor_ic.{factor_name}", ">", 0.03, ic_value, "IC阈值"), self._threshold(f"factor_ic_trend.{factor_name}", "==", "rising", trend_value, "趋势阈值")]))
                elif factor_name == "reversal":
                    out.append(self._make("margin_divergence", self._high_precision_margin_divergence_params(), f"reversal IC={ic_value:.3f}上升，高精度流动性修复", source="factor_ic", trigger_signal={"field": "factor_ic", "factor": factor_name, "value": ic_value, "trend": trend_value}, trigger_thresholds=[self._threshold(f"factor_ic.{factor_name}", ">", 0.03, ic_value, "IC阈值"), self._threshold(f"factor_ic_trend.{factor_name}", "==", "rising", trend_value, "趋势阈值")], extras=self._high_precision_candidate_fields(preferred_regime="liquidity_repair_with_volume_reexpansion", avoid_regime="volume_vacuum_or_failed_rebound", holding_rationale="只有在中期下跌后出现缩量止跌、放量修复和结构转强时才参与，避免把普通噪声反抽误当成高精度修复。", failure_mode={"primary_failure_mode": "factor_reversal_false_positive", "secondary_failure_mode": "false_reexpansion"}, entry_bias="liquidity_divergence_repair_confirmation", exit_bias="liquidity_break_or_time_stop")))
            elif ic_value < STRATEGY_FACTORY_FACTOR_IC_CLASSIC_FALLING_MAX_IC and trend_value == "falling" and factor_name == "momentum":
                out.append(self._make("margin_divergence", self._high_precision_margin_divergence_params(), f"momentum IC={ic_value:.3f}下降，转高精度流动性修复", source="factor_ic", trigger_signal={"field": "factor_ic", "factor": factor_name, "value": ic_value, "trend": trend_value}, trigger_thresholds=[self._threshold(f"factor_ic.{factor_name}", "<", -0.02, ic_value, "IC阈值"), self._threshold(f"factor_ic_trend.{factor_name}", "==", "falling", trend_value, "趋势阈值")], extras=self._high_precision_candidate_fields(preferred_regime="liquidity_repair_with_volume_reexpansion", avoid_regime="volume_vacuum_or_failed_rebound", holding_rationale="只有在趋势衰减后出现缩量止跌与放量修复共振时才参与，避免追着噪声做均值回归。", failure_mode={"primary_failure_mode": "trend_break_without_repair", "secondary_failure_mode": "overtrading"}, entry_bias="liquidity_divergence_repair_confirmation", exit_bias="liquidity_break_or_time_stop")))

        weights: Dict[str, float] = {}
        factor_signal_count = 0
        for factor_name in ["value", "quality", "growth"]:
            ic_value = factor_ic.get(factor_name, 0)
            trend_value = trend.get(factor_name, "flat")
            if trend_value == "rising":
                weights[factor_name] = max(0.1, 0.33 + ic_value * 2)
                if float(ic_value or 0.0) > 0.0:
                    factor_signal_count += 1
            elif trend_value == "falling":
                weights[factor_name] = max(0.05, 0.33 - abs(ic_value) * 2)
                if float(ic_value or 0.0) < 0.0:
                    factor_signal_count += 1
            else:
                weights[factor_name] = 0.33
        total = sum(weights.values()) or 1.0
        weights = {key: round(value / total, 2) for key, value in weights.items()}
        if factor_signal_count > 0:
            out.append(self._make("multi_factor", {"factor_weights": weights, "lookback": 60}, f"IC驱动多因子权重: {weights}", source="factor_ic", trigger_signal={"field": "factor_ic_weights", "value": weights}, trigger_thresholds=[self._threshold("factor_ic_weights", "derived_from", {"positive_ic": 0.0, "trend_preference": "rising"}, {"factor_ic": factor_ic, "factor_ic_trend": trend, "weights": weights}, "权重派生规则")]))
        # ALPHA-WIRING-V1 (P-A)：泛因子 IC 接入（默认 OFF，零变化）。
        # 把已挖出但被经典分支忽略的因子（gp_*/rl_* 等，IC 过阈值）接回生成链，
        # 每个候选带合成的 prediction_contract + evidence_chain，使其成为可证伪假设，
        # 而非缺语义契约的 fixed_defaults 填充物。
        if STRATEGY_FACTORY_FACTOR_IC_GENERIC_INTAKE_ENABLED:
            out.extend(self._generic_factor_ic_candidates(factor_ic, trend))
        return out

    def _generic_factor_ic_candidates(self, factor_ic: Dict[str, float], trend: Dict[str, str]) -> List[dict]:
        """ALPHA-WIRING-V1 (P-A)：为已挖出但被经典分支忽略的因子生成带语义契约的候选。

        方向取 IC 符号，horizon 取默认持有期，claim/evidence 由因子名 + 近况 IC +
        趋势构成可证伪断言。按 |IC| 从大到小取前 N 个，受 MAX_FACTORS 配额约束。
        """
        classic_names = {"momentum", "value", "quality", "reversal", "growth"}
        min_abs_ic = float(STRATEGY_FACTORY_FACTOR_IC_GENERIC_MIN_ABS_IC)
        max_factors = int(STRATEGY_FACTORY_FACTOR_IC_GENERIC_MAX_FACTORS)
        scored: List[tuple] = []
        for name, raw_ic in dict(factor_ic or {}).items():
            lowered = str(name or "").strip().lower()
            if not lowered or lowered in classic_names:
                continue
            ic_value = self._safe_float(raw_ic)
            if abs(ic_value) < min_abs_ic:
                continue
            trend_value = str(trend.get(name) or "flat").strip().lower() or "flat"
            scored.append((str(name).strip(), ic_value, trend_value))
        scored.sort(key=lambda item: abs(item[1]), reverse=True)
        out: List[dict] = []
        for name, ic_value, trend_value in scored[:max_factors]:
            out.append(self._make_generic_factor_candidate(name, ic_value, trend_value))
        return out

    def _make_generic_factor_candidate(self, factor_name: str, ic_value: float, trend_value: str) -> dict:
        """ALPHA-WIRING-V1 (P-A)：把单个已验证因子封装成带语义契约的候选。"""
        strategy_types = preferred_strategy_types_for_factor(factor_name, default=["multi_factor"])
        strategy_type = str((strategy_types or ["multi_factor"])[0] or "multi_factor")
        direction = "up" if ic_value >= 0 else "down"
        horizon_days = 20
        ic_rounded = round(float(ic_value), 4)
        thesis = (
            f"挖掘因子 {factor_name} 近况 IC={ic_value:.3f}（趋势 {trend_value}），"
            f"预期对 {strategy_type} 族标的产生 {direction} 方向 {horizon_days} 日前向收益。"
        )
        params = {
            "factor_name": factor_name,
            "factor_ic": ic_rounded,
            "factor_ic_trend": trend_value,
            "lookback": horizon_days,
        }
        if strategy_type == "multi_factor":
            params["factor_weights"] = {factor_name: 1.0}
        evidence_chain = {
            "contract_version": "strategy_factory.semantic_contract.v1",
            "producer": "strategy_factory.factor_ic_generic_intake",
            "generation_mode": "factor_ic_validated",
            "thesis": thesis,
            "evidences": [
                {
                    "evidence_id": "ev_factor_ic",
                    "source_type": "factor_ic_validated",
                    "direction": direction,
                    "summary": thesis,
                    "proxy_only": False,
                    "horizon_days": horizon_days,
                    "claim_ids": ["claim_entry"],
                    "support_metric": {
                        "factor_name": factor_name,
                        "ic_value": ic_rounded,
                        "trend": trend_value,
                    },
                },
            ],
        }
        prediction_contract = {
            "contract_version": "strategy_factory.prediction_contract.v1",
            "producer": "strategy_factory.factor_ic_generic_intake",
            "generation_mode": "factor_ic_validated",
            "primary_horizon_days": horizon_days,
            "target": "forward_return_positive" if direction == "up" else "forward_return_negative",
            "claims": [
                {
                    "claim_id": "claim_entry",
                    "claim_type": "entry",
                    "summary": thesis,
                    "expected_move": direction,
                    "expected_horizon": horizon_days,
                    "evidence_ids": ["ev_factor_ic"],
                    "failure_condition": f"{factor_name} IC 转向或前向收益与预测方向背离",
                },
            ],
        }
        extras = {
            "candidate_origin": "factor_ic_generic_intake",
            "candidate_family": strategy_type,
            "evidence_chain": evidence_chain,
            "prediction_contract": prediction_contract,
            "tags": ["factor_ic_generic_intake", "factor_ic_validated"],
        }
        return self._make(
            strategy_type,
            params,
            f"泛因子接入: {factor_name} IC={ic_value:.3f} 趋势{trend_value}",
            source="factor_ic",
            trigger_signal={"field": "factor_ic", "factor": factor_name, "value": ic_value, "trend": trend_value},
            trigger_thresholds=[
                self._threshold(
                    f"factor_ic.{factor_name}",
                    "abs>=",
                    float(STRATEGY_FACTORY_FACTOR_IC_GENERIC_MIN_ABS_IC),
                    ic_value,
                    "泛因子IC阈值",
                ),
            ],
            extras=extras,
        )

    def _from_volatility(self, snapshot: dict) -> List[dict]:
        out: List[dict] = []
        volatility = snapshot.get("fg_components", {}).get("volatility", 50)
        if volatility < 35:
            out.append(self._make("ma_cross", {"short_period": 10, "long_period": 60}, f"波动率{volatility}，高波动长周期均线", source="volatility", trigger_signal={"field": "fg_components.volatility", "value": volatility}, trigger_thresholds=[self._threshold("fg_components.volatility", "<", 35, volatility, "波动率阈值")]))
            out.append(self._make("macro_timing", {"fear_threshold": 24, "greed_threshold": 74, "lookback": 36}, f"波动率{volatility}，高精度宏观择时", source="volatility", trigger_signal={"field": "fg_components.volatility", "value": volatility}, trigger_thresholds=[self._threshold("fg_components.volatility", "<", 35, volatility, "波动率阈值")], extras=self._high_precision_candidate_fields(preferred_regime="panic_repair_with_volatility_stabilization", avoid_regime="mid_regime_whipsaw", holding_rationale="只有在高波动后恐慌修复且波动结构开始稳定时才入场，避免宏观噪声来回打脸。", failure_mode={"primary_failure_mode": "regime_whipsaw", "secondary_failure_mode": "late_entry_after_volatility_spike"}, max_position_pct=0.14, capacity_bucket="mid", min_days=6, max_days=24, entry_bias="panic_repair_after_regime_confirmation", exit_bias="greed_extreme_or_regime_break")))
        elif volatility > 65:
            out.append(self._make("ma_cross", {"short_period": 3, "long_period": 15}, f"波动率{volatility}，低波动短周期均线", source="volatility", trigger_signal={"field": "fg_components.volatility", "value": volatility}, trigger_thresholds=[self._threshold("fg_components.volatility", ">", 65, volatility, "波动率阈值")]))
        return out

    def _from_event_driven(self, snapshot: dict) -> List[dict]:
        out: List[dict] = []
        event_driven = dict(snapshot.get("event_driven") or {})
        events = [dict(item or {}) for item in list(event_driven.get("events") or []) if isinstance(item, dict)]
        for event in events[:4]:
            event_id = str(event.get("event_id") or "").strip()
            event_type = str(event.get("event_type") or "event").strip().lower() or "event"
            event_name = str(event.get("event_name") or event.get("summary") or event_id or "event").strip()
            themes = [dict(item or {}) for item in list(event.get("themes") or []) if isinstance(item, dict)]
            for theme in themes[:3]:
                source = str(theme.get("source") or event.get("source") or "").strip().lower()
                source_tier = str(theme.get("source_tier") or event.get("source_tier") or "").strip().lower()
                source_doc_uids = self._normalize_text_list(
                    theme.get("source_doc_uids") or event.get("source_doc_uids"),
                    limit=8,
                )
                event_anchor_id = str(
                    theme.get("event_anchor_id")
                    or event.get("event_anchor_id")
                    or event_id
                    or ""
                ).strip()
                validation_summary = dict(theme.get("validation_summary") or event.get("validation_summary") or {})
                alpha_status = str(
                    theme.get("alpha_confirmation_status")
                    or event.get("alpha_confirmation_status")
                    or validation_summary.get("alpha_confirmation_status")
                    or ""
                ).strip().lower()
                occurrence_status = str(
                    theme.get("occurrence_status")
                    or event.get("occurrence_status")
                    or validation_summary.get("occurrence_status")
                    or ""
                ).strip().lower()
                conflict_count = int(
                    theme.get("conflict_count")
                    or event.get("conflict_count")
                    or validation_summary.get("conflict_count")
                    or 0
                )
                if not (
                    bool(theme.get("verified_event_anchor") or event.get("verified_event_anchor"))
                    and source == "market_events_normalized"
                    and source_tier in {"tier_a", "tier_b"}
                    and source_doc_uids
                    and event_anchor_id
                    and (not occurrence_status or occurrence_status.startswith("verified"))
                    and alpha_status not in {"news_only_rejected", "source_degraded", "conflicted"}
                    and conflict_count <= 0
                ):
                    continue
                strategy_preferences = {
                    str(item or "").strip()
                    for item in list(theme.get("preferred_strategy_types") or theme.get("strategy_preferences") or [])
                    if str(item or "").strip()
                }
                if strategy_preferences and "event_structure_breakout" not in strategy_preferences:
                    continue
                target_symbols = _normalize_target_codes(theme.get("target_symbols"), limit=3)
                if not target_symbols:
                    continue
                focus_name = str(theme.get("theme_name") or theme.get("theme_code") or event_name).strip()
                event_anchor = {
                    "source": "market_events_normalized",
                    "id": event_anchor_id,
                    "type": event_type,
                    "strength": round(
                        max(
                            self._safe_float((theme.get("score_summary") or {}).get("avg_final_score") or 0.0),
                            self._safe_float(event.get("confidence") or event.get("intensity") or 0.0),
                        ),
                        4,
                    ),
                    "theme_code": str(theme.get("theme_code") or "").strip() or None,
                    "focus_industries": self._normalize_text_list(focus_name, limit=3),
                    "target_symbols": list(target_symbols),
                    "source_doc_uids": list(source_doc_uids),
                    "source_tier": source_tier,
                    "reliability_score": theme.get("reliability_score") or event.get("reliability_score"),
                    "evidence_time": theme.get("evidence_time") or event.get("evidence_time"),
                    "provider_chain": self._normalize_text_list(
                        theme.get("provider_chain") or event.get("provider_chain"),
                        limit=6,
                    ),
                    "verified_event_anchor": True,
                    "validation_summary": validation_summary,
                    "occurrence_status": theme.get("occurrence_status") or event.get("occurrence_status"),
                    "alpha_confirmation_status": theme.get("alpha_confirmation_status") or event.get("alpha_confirmation_status"),
                    "confidence_cap_reason": theme.get("confidence_cap_reason") or event.get("confidence_cap_reason"),
                    "needs_alpha_confirmation": bool(theme.get("needs_alpha_confirmation") or event.get("needs_alpha_confirmation")),
                    "conflict_count": conflict_count,
                }
                observed_sources = ["market_events_normalized"]
                event_prefilter = self._build_event_prefilter(
                    observed_sources=observed_sources,
                    evidence_summary=str(event.get("summary") or event_name or "").strip(),
                    event_id=event_id,
                    theme_code=str(theme.get("theme_code") or "").strip(),
                    focus_industries=self._normalize_text_list(focus_name, limit=3),
                    event_anchor=event_anchor,
                    confirmation_count=len(observed_sources),
                    anchor_strength=event_anchor.get("strength"),
                    required=True,
                )
                research_task = {
                    "task_source": "event_driven",
                    "preferred_strategy_types": ["event_structure_breakout"],
                    "allowed_strategy_types": ["event_structure_breakout"],
                    "strategy_preferences": ["event_structure_breakout"],
                    "candidate_family": "event_structure_breakout",
                    "target_symbols": list(target_symbols),
                    "stock_pool": {"selection_mode": "explicit", "symbols": list(target_symbols)},
                    "target_symbol_policy": "strict_intersection",
                    "universe_expansion_policy": "forbid",
                    "validation_focus": "candidate_target_only",
                    "focus_industries": list(event_anchor.get("focus_industries") or []),
                    "event_id": event_id or None,
                    "event_anchor_id": event_anchor_id,
                    "source_doc_uids": list(source_doc_uids),
                    "source_tier": source_tier,
                    "reliability_score": event_anchor.get("reliability_score"),
                    "evidence_time": event_anchor.get("evidence_time"),
                    "verified_event_anchor": True,
                    "event_validation_summary": validation_summary,
                    "alpha_confirmation_status": event_anchor.get("alpha_confirmation_status"),
                    "confidence_cap_reason": event_anchor.get("confidence_cap_reason"),
                    "needs_alpha_confirmation": bool(event_anchor.get("needs_alpha_confirmation")),
                    "conflict_count": conflict_count,
                    "event_type": event_type,
                    "theme_code": event_anchor.get("theme_code"),
                    "event_name": event_name,
                    "preference_strength": "strong",
                    "preference_reason": f"event_anchor:market_events_normalized:{event_anchor.get('id')}",
                    "gate_1_representative_count": min(3, len(target_symbols)),
                    "synthetic_local_spawn": True,
                    "target_pool_source": "event_anchor",
                }
                extras = {
                    **self._high_precision_candidate_fields(
                        preferred_regime="event_follow_through_with_structure_confirmation",
                        avoid_regime="false_breakout_or_post_event_mean_reversion",
                        holding_rationale="只在明确事件锚存在后，等待缩量整理与放量突破确认再参与，避免把普通板块热度误当成可审计事件延续。",
                        failure_mode={
                            "primary_failure_mode": "false_breakout_after_weak_event_confirmation",
                            "secondary_failure_mode": "event_extension_without_follow_through",
                        },
                        entry_selectivity="strict_event_breakout",
                        max_position_pct=0.14,
                        capacity_bucket="mid",
                        min_days=3,
                        max_days=12,
                        entry_bias="event_structure_breakout_confirmation",
                        exit_bias="breakout_failure_or_time_stop",
                        event_prefilter=event_prefilter,
                        candidate_origin="research_signal",
                        event_anchor=event_anchor,
                        target_pool_source="event_anchor",
                    ),
                    "research_task": research_task,
                    "requested_target_symbols": list(target_symbols),
                    "target_symbols": list(target_symbols),
                    "stock_pool": {"selection_mode": "explicit", "symbols": list(target_symbols)},
                }
                out.append(
                    self._make(
                        "event_structure_breakout",
                        self._high_precision_event_structure_breakout_params(),
                        f"{focus_name or event_name} 事件锚确认后的高精度结构突破",
                        source="event_driven",
                        trigger_signal={
                            "field": "event_driven",
                            "event_id": event_id,
                            "event_anchor_id": event_anchor_id,
                            "source_doc_uids": list(source_doc_uids),
                            "source_tier": source_tier,
                            "reliability_score": event_anchor.get("reliability_score"),
                            "verified_event_anchor": True,
                            "event_validation_summary": validation_summary,
                            "alpha_confirmation_status": event_anchor.get("alpha_confirmation_status"),
                            "confidence_cap_reason": event_anchor.get("confidence_cap_reason"),
                            "needs_alpha_confirmation": bool(event_anchor.get("needs_alpha_confirmation")),
                            "conflict_count": conflict_count,
                            "event_type": event_type,
                            "theme_code": event_anchor.get("theme_code"),
                            "target_symbols": list(target_symbols),
                        },
                        trigger_thresholds=[
                            self._threshold("event_id", "!=", "", event_id or None, "事件锚存在"),
                            self._threshold("event_target_symbols", ">=", 1, len(target_symbols), "事件目标池可用"),
                        ],
                        extras=extras,
                    )
                )
        return out

    def _from_fund_flow(self, snapshot: dict) -> List[dict]:
        out: List[dict] = []
        north_3d = snapshot.get("north_fund_3d_net", 0)
        margin_5d = snapshot.get("margin_5d_change_pct", 0)

        if north_3d > 5_000_000_000:
            north_anchor = self._snapshot_event_anchor(
                snapshot,
                source="fund_flow",
                trigger_signal={"field": "north_fund_3d_net", "value": north_3d},
            )
            north_prefilter = self._build_event_prefilter(
                observed_sources=["fund_flow"],
                evidence_summary=f"north_fund_3d_net:{north_3d:.0f}",
                event_anchor=north_anchor,
                confirmation_count=1,
                anchor_strength=north_anchor.get("strength"),
                required=True,
            )
            if self._event_anchor_has_explicit_source(north_anchor):
                out.append(self._make("event_structure_breakout", self._high_precision_event_structure_breakout_params(), f"北向3日净流入{north_3d / 1e8:.0f}亿，高精度事件结构突破", source="fund_flow", trigger_signal={"field": "north_fund_3d_net", "value": north_3d}, trigger_thresholds=[self._threshold("north_fund_3d_net", ">", 5_000_000_000, north_3d, "北向净流入阈值")], extras=self._high_precision_candidate_fields(preferred_regime="event_follow_through_with_structure_confirmation", avoid_regime="false_breakout_or_post_event_mean_reversion", holding_rationale="只在资金显著回流后等待缩量整理并放量突破再参与，避免把普通脉冲误当成结构延续。", failure_mode={"primary_failure_mode": "false_breakout_after_flow_impulse", "secondary_failure_mode": "cost_drag_after_chasing_extension"}, entry_selectivity="strict_event_breakout", max_position_pct=0.14, capacity_bucket="mid", min_days=3, max_days=12, entry_bias="event_structure_breakout_confirmation", exit_bias="breakout_failure_or_time_stop", event_prefilter=north_prefilter, candidate_origin="research_signal", event_anchor=north_anchor, target_pool_source="event_anchor" if _normalize_target_codes(north_anchor.get("target_symbols"), limit=6) else "static_fallback")))
            out.append(self._make("growth_factor", {"lookback": self._jitter(40, 30, 60), "buy_quantile": 0.85, "sell_quantile": 0.15}, f"北向3日净流入{north_3d / 1e8:.0f}亿，成长加速", source="fund_flow", trigger_signal={"field": "north_fund_3d_net", "value": north_3d}, trigger_thresholds=[self._threshold("north_fund_3d_net", ">", 5_000_000_000, north_3d, "北向净流入阈值")]))
            out.append(self._make("quality_factor", {"lookback": self._jitter(60, 40, 80), "buy_quantile": 0.8, "sell_quantile": 0.2}, f"北向3日净流入{north_3d / 1e8:.0f}亿，质量优选", source="fund_flow", trigger_signal={"field": "north_fund_3d_net", "value": north_3d}, trigger_thresholds=[self._threshold("north_fund_3d_net", ">", 5_000_000_000, north_3d, "北向净流入阈值")]))
        elif north_3d < -5_000_000_000:
            out.append(self._make("value_factor", {"lookback": self._jitter(60, 40, 80), "buy_quantile": 0.85, "sell_quantile": 0.15}, f"北向3日净流出{abs(north_3d) / 1e8:.0f}亿，价值防御", source="fund_flow", trigger_signal={"field": "north_fund_3d_net", "value": north_3d}, trigger_thresholds=[self._threshold("north_fund_3d_net", "<", -5_000_000_000, north_3d, "北向净流出阈值")]))
            out.append(self._make("macro_timing", {"fear_threshold": 24, "greed_threshold": 74, "lookback": 36}, f"北向3日净流出{abs(north_3d) / 1e8:.0f}亿，高精度宏观择时", source="fund_flow", trigger_signal={"field": "north_fund_3d_net", "value": north_3d}, trigger_thresholds=[self._threshold("north_fund_3d_net", "<", -5_000_000_000, north_3d, "北向净流出阈值")], extras=self._high_precision_candidate_fields(preferred_regime="panic_repair_with_volatility_stabilization", avoid_regime="mid_regime_whipsaw", holding_rationale="北向明显撤退后只等待风险偏好修复再参与，避免在中性阶段来回切换。", failure_mode={"primary_failure_mode": "regime_whipsaw", "secondary_failure_mode": "false_risk_repair"}, max_position_pct=0.14, capacity_bucket="mid", min_days=6, max_days=24, entry_bias="panic_repair_after_regime_confirmation", exit_bias="greed_extreme_or_regime_break")))

        if margin_5d > 2.0:
            margin_anchor = self._snapshot_event_anchor(
                snapshot,
                source="fund_flow",
                trigger_signal={"field": "margin_5d_change_pct", "value": margin_5d},
            )
            margin_prefilter = self._build_event_prefilter(
                observed_sources=["fund_flow"],
                evidence_summary=f"margin_5d_change_pct:{margin_5d:.2f}",
                event_anchor=margin_anchor,
                confirmation_count=1,
                anchor_strength=margin_anchor.get("strength"),
                required=True,
            )
            if self._event_anchor_has_explicit_source(margin_anchor):
                out.append(self._make("event_structure_breakout", self._high_precision_event_structure_breakout_params(), f"融资5日增速{margin_5d:.1f}%，高精度事件结构突破", source="fund_flow", trigger_signal={"field": "margin_5d_change_pct", "value": margin_5d}, trigger_thresholds=[self._threshold("margin_5d_change_pct", ">", 2.0, margin_5d, "融资增速阈值")], extras=self._high_precision_candidate_fields(preferred_regime="event_follow_through_with_structure_confirmation", avoid_regime="false_breakout_or_post_event_mean_reversion", holding_rationale="只有在融资脉冲后出现缩量整理并放量突破时才参与，避免在纯脉冲扩张阶段直接追高。", failure_mode={"primary_failure_mode": "breakout_without_follow_through", "secondary_failure_mode": "overtrading"}, entry_selectivity="strict_event_breakout", max_position_pct=0.14, capacity_bucket="mid", min_days=3, max_days=12, entry_bias="event_structure_breakout_confirmation", exit_bias="breakout_failure_or_time_stop", event_prefilter=margin_prefilter, candidate_origin="research_signal", event_anchor=margin_anchor, target_pool_source="event_anchor" if _normalize_target_codes(margin_anchor.get("target_symbols"), limit=6) else "static_fallback")))
            out.append(self._make("momentum", {"lookback": self._jitter(5, 3, 10), "threshold": 0.01}, f"融资5日增速{margin_5d:.1f}%，短周期动量", source="fund_flow", trigger_signal={"field": "margin_5d_change_pct", "value": margin_5d}, trigger_thresholds=[self._threshold("margin_5d_change_pct", ">", 2.0, margin_5d, "融资增速阈值")]))
        elif margin_5d < -2.0:
            out.append(self._make("margin_divergence", self._high_precision_margin_divergence_params(), f"融资5日降速{abs(margin_5d):.1f}%，高精度流动性修复", source="fund_flow", trigger_signal={"field": "margin_5d_change_pct", "value": margin_5d}, trigger_thresholds=[self._threshold("margin_5d_change_pct", "<", -2.0, margin_5d, "融资降速阈值")], extras=self._high_precision_candidate_fields(preferred_regime="liquidity_repair_with_volume_reexpansion", avoid_regime="volume_vacuum_or_failed_rebound", holding_rationale="融资回落后只等待缩量止跌、放量修复和结构转强共振再参与，优先低频高胜率而不是频繁抄底。", failure_mode={"primary_failure_mode": "failed_rebound_after_liquidity_dryup", "secondary_failure_mode": "cost_fragility"}, entry_bias="liquidity_divergence_repair_confirmation", exit_bias="liquidity_break_or_time_stop")))
        return out

    @classmethod
    def _coverage_fill_priority(cls, current_candidates: Optional[List[dict]]) -> List[str]:
        debt = cls._diversification_debt(current_candidates)
        preferred: List[str] = []

        def add(*types: str) -> None:
            for strategy_type in types:
                if strategy_type in CATEGORY_MINIMUMS and strategy_type not in preferred:
                    preferred.append(strategy_type)

        for item in debt:
            if item == "quality_defensive":
                add("quality_factor", "value_factor")
            elif item == "mean_reversion":
                add("gap_fill", "mean_reversion_short", "rsi")
            elif item == "flow_rotation":
                add("north_capital_track", "sector_rotation")
        return preferred

    def _fill_gaps(self, snapshot: dict, current_candidates: Optional[List[dict]] = None) -> List[dict]:
        current_candidates = list(current_candidates or [])
        current_counts = self._generated_type_counts(current_candidates)
        fill_budget = self._quota_fill_budget(snapshot, len(current_candidates))
        if fill_budget <= 0:
            return []
        parameter_registry = ParameterDistributionRegistry.from_snapshot(snapshot)

        preferred_types = list(
            dict.fromkeys(
                [
                    *self._coverage_fill_priority(current_candidates),
                    *self._preferred_fill_types(snapshot, current_counts),
                ]
            )
        )
        preferred_types = sorted(
            preferred_types,
            key=lambda strategy_type: (
                -int(parameter_registry.sample_count(strategy_type) or 0),
                preferred_types.index(strategy_type),
            ),
        )
        out: List[dict] = []
        fill_counts: Dict[str, int] = {}

        def maybe_add(strategy_type: str, preferred_rank: int) -> bool:
            if strategy_type == "momentum":
                return False
            event_prefilter = {}
            event_anchor: dict[str, Any] = {}
            if strategy_type == "event_structure_breakout":
                event_prefilter = self._snapshot_event_prefilter(
                    snapshot,
                    source="quota_fill",
                    trigger_signal={"field": "quota_fill", "strategy_type": strategy_type},
                )
                event_anchor = dict(event_prefilter.get("event_anchor") or {})
                if not self._event_anchor_has_explicit_source(event_anchor):
                    return False
                if not event_prefilter.get("passed"):
                    return False
            current = int(current_counts.get(strategy_type) or 0) + int(fill_counts.get(strategy_type) or 0)
            desired_generated_count = 1 if preferred_rank > 2 else 2
            original_desired_generated_count = desired_generated_count
            feedback_factor = self._family_negative_feedback_factor(strategy_type, snapshot)
            if feedback_factor <= 0:
                return False
            desired_generated_count = max(0, int(round(desired_generated_count * feedback_factor)))
            if desired_generated_count <= 0:
                return False
            generation_cap = self._local_generation_cap(strategy_type)
            if generation_cap is not None:
                desired_generated_count = min(desired_generated_count, generation_cap)
                if current >= generation_cap:
                    return False
            if current >= desired_generated_count:
                return False
            existing_total = len(current_candidates) + len(out)
            existing_trend = self._trend_cluster_count(current_candidates) + self._trend_cluster_count(out)
            if strategy_type in self._TREND_CLUSTER_TYPES and existing_total > 0 and existing_trend / existing_total >= 0.5:
                return False
            projected_total = len(current_candidates) + len(out) + 1
            projected_trend = (
                self._trend_cluster_count(current_candidates)
                + self._trend_cluster_count(out)
                + (1 if strategy_type in self._TREND_CLUSTER_TYPES else 0)
            )
            if strategy_type in self._TREND_CLUSTER_TYPES and projected_total > 0 and projected_trend / projected_total > 0.5:
                return False
            slot_index = int(fill_counts.get(strategy_type) or 0) + 1
            params, parameter_source, parameter_sample_count = self._resolved_varied_defaults(
                strategy_type,
                slot_index - 1,
                snapshot=snapshot,
                registry=parameter_registry,
            )
            fill_source_mode = self._quota_fill_source_mode(
                strategy_type,
                snapshot=snapshot,
                current_candidates=current_candidates,
                parameter_source=parameter_source,
                parameter_sample_count=parameter_sample_count,
            )
            fill_quality_tier = self._quota_fill_quality_tier(fill_source_mode)
            quota_fill = {
                "strategy_type": strategy_type,
                "current_count": int(current_counts.get(strategy_type) or 0),
                "minimum_required": CATEGORY_MINIMUMS.get(strategy_type, 0),
                "desired_generated_count": desired_generated_count,
                "original_desired_generated_count": original_desired_generated_count,
                "feedback_factor": feedback_factor,
                "feedback_limited": feedback_factor < 1.0,
                "fill_budget": fill_budget,
                "preferred_rank": preferred_rank,
                "slot_index": slot_index,
                "parameter_source": parameter_source,
                "parameter_sample_count": parameter_sample_count,
                "fill_source_mode": fill_source_mode,
                "fill_quality_tier": fill_quality_tier,
            }
            extras = None
            if strategy_type == "event_structure_breakout":
                extras = self._high_precision_candidate_fields(
                    preferred_regime="event_follow_through_with_structure_confirmation",
                    avoid_regime="false_breakout_or_post_event_mean_reversion",
                    holding_rationale="只在公告/资金/板块催化先行确认后，再等待缩量整理与放量突破的结构延续段参与，避免把无催化噪声突破误判成事件延续。",
                    failure_mode={
                        "primary_failure_mode": "breakout_without_external_catalyst",
                        "secondary_failure_mode": "false_breakout_after_weak_event_confirmation",
                    },
                    entry_selectivity="strict_event_breakout",
                    max_position_pct=0.14,
                    capacity_bucket="mid",
                    min_days=3,
                    max_days=12,
                    entry_bias="event_structure_breakout_confirmation",
                    exit_bias="breakout_failure_or_time_stop",
                    event_prefilter=event_prefilter,
                    candidate_origin="anchored_quota_fill",
                    event_anchor=event_anchor,
                    target_pool_source=(
                        "event_anchor"
                        if _normalize_target_codes(event_anchor.get("target_symbols"), limit=6)
                        else "static_fallback"
                    ),
                )
            candidate = self._make(
                strategy_type,
                params,
                f"{strategy_type}研究信号不足，按市场状态补位#{slot_index}",
                source="quota_fill",
                trigger_signal={
                    "field": f"generated_type_counts.{strategy_type}",
                    "value": int(current_counts.get(strategy_type) or 0),
                    "parameter_source": parameter_source,
                    "fill_source_mode": fill_source_mode,
                },
                trigger_thresholds=[
                    self._threshold(
                        f"generated_type_counts.{strategy_type}",
                        "<",
                        desired_generated_count,
                        int(current_counts.get(strategy_type) or 0),
                        "研究候选补位阈值",
                    )
                ],
                quota_fill=quota_fill,
                kind="quota_fill",
                extras=extras,
            )
            candidate["parameter_source"] = parameter_source
            candidate["parameter_sample_count"] = parameter_sample_count
            out.append(candidate)
            fill_counts[strategy_type] = slot_index
            return True

        for pass_index in range(2):
            for preferred_rank, strategy_type in enumerate(preferred_types, 1):
                if len(out) >= fill_budget:
                    break
                if pass_index == 0 and int(current_counts.get(strategy_type) or 0) > 0:
                    continue
                maybe_add(strategy_type, preferred_rank)
            if len(out) >= fill_budget:
                break

        return out[:fill_budget]

    @staticmethod
    def _jitter(base: int, lo: int, hi: int) -> int:
        delta = max(1, int(base * 0.2))
        return max(lo, min(hi, base + random.randint(-delta, delta)))

    @staticmethod
    def _jitter_f(base: float, lo: float, hi: float) -> float:
        delta = max(0.01, base * 0.15)
        return round(max(lo, min(hi, base + random.uniform(-delta, delta))), 2)

    @staticmethod
    def _snapshot_regime_inputs(snapshot: Optional[dict] = None) -> dict[str, float]:
        payload = dict(snapshot or {})
        return {
            "fear_greed": StrategySpawner._safe_float(payload.get("fear_greed_index") or 50.0),
            "volatility": StrategySpawner._safe_float(dict(payload.get("fg_components") or {}).get("volatility") or 50.0),
            "north_3d": StrategySpawner._safe_float(payload.get("north_fund_3d_net") or 0.0),
            "margin_5d": StrategySpawner._safe_float(payload.get("margin_5d_change_pct") or 0.0),
        }
    def _legacy_varied_defaults(self, strategy_type: str, idx: int, snapshot: Optional[dict] = None) -> dict:
        regime = self._snapshot_regime_inputs(snapshot)
        fear_greed = regime["fear_greed"]
        volatility = regime["volatility"]
        north_3d = regime["north_3d"]
        margin_5d = regime["margin_5d"]

        if strategy_type == "momentum":
            if fear_greed >= 68 and north_3d > 0 and margin_5d > 0:
                lookbacks = [5, 10, 20]
                threshold_base = 0.016
            elif fear_greed <= 42 or north_3d < 0 or volatility >= 65:
                lookbacks = [20, 30, 45]
                threshold_base = 0.028
            else:
                lookbacks = [10, 20, 30]
                threshold_base = 0.022
            lookback = lookbacks[idx % len(lookbacks)]
            return {
                "lookback": self._jitter(lookback, 5, 50),
                "threshold": self._jitter_f(threshold_base, 0.008, 0.05),
            }
        if strategy_type == "ma_cross":
            if volatility >= 65 or fear_greed <= 45:
                pairs = [(8, 34), (10, 55), (13, 89)]
            elif fear_greed >= 68 and north_3d > 0:
                pairs = [(5, 21), (8, 34), (13, 55)]
            else:
                pairs = [(5, 20), (8, 34), (13, 55)]
            short_period, long_period = pairs[idx % len(pairs)]
            short_period = self._jitter(short_period, 3, 15)
            long_period = self._jitter(long_period, max(short_period + 8, 18), 120)
            return {"short_period": short_period, "long_period": long_period}
        if strategy_type == "rsi":
            if fear_greed <= 40 or north_3d < 0:
                periods = [6, 10, 14]
                oversold_base = 22
                overbought_base = 76
            else:
                periods = [10, 14, 21]
                oversold_base = 24
                overbought_base = 72
            period = periods[idx % len(periods)]
            return {
                "rsi_period": self._jitter(period, 4, 28),
                "oversold": self._jitter(oversold_base, 18, 34),
                "overbought": self._jitter(overbought_base, 64, 82),
            }
        if strategy_type == "volatility_breakout":
            lookbacks = [8, 13, 21] if volatility >= 60 else [10, 15, 20]
            lookback = lookbacks[idx % len(lookbacks)]
            threshold_base = 0.03 if volatility >= 60 else 0.025
            return {"lookback": self._jitter(lookback, 5, 30), "threshold": self._jitter_f(threshold_base, 0.01, 0.06)}
        if strategy_type == "event_structure_breakout":
            breakout_window = self._jitter(12 if volatility >= 60 or north_3d > 0 else 14, 10, 20)
            return {
                "breakout_window": breakout_window,
                "breakout_buffer_pct": self._jitter_f(0.002 if north_3d > 0 else 0.004, 0.002, 0.008),
                "contraction_window": self._jitter(5, 3, 8),
                "contraction_max_range_ratio": self._jitter_f(0.06, 0.04, 0.08),
                "volume_window": self._jitter(8, 5, 12),
                "breakout_volume_ratio_min": self._jitter_f(1.0, 0.95, 1.2),
                "structure_window": self._jitter(4, 3, 6),
                "structure_close_location_min": self._jitter_f(0.62, 0.55, 0.7),
                "structure_body_return_min": self._jitter_f(0.003, 0.0015, 0.005),
                "event_impulse_window": self._jitter(5, 3, 8),
                "event_impulse_threshold": self._jitter_f(0.015 if north_3d > 0 else 0.02, 0.01, 0.035),
                "max_hold_bars": self._jitter(8, 5, 12),
                "breakout_failure_close_buffer": self._jitter_f(-0.012, -0.018, -0.008),
                "adverse_volume_ratio_max": self._jitter_f(0.85, 0.75, 0.95),
                "max_active_symbols": 3,
                "universe_selection_profile": "event_structure_breakout_fit_v1",
            }
        if strategy_type == "gap_fill":
            oversold_base = 22 if fear_greed <= 45 else 20
            overbought_base = 66 if volatility >= 60 else 62
            return {
                "rsi_period": self._jitter(5 if fear_greed <= 45 else 7, 3, 12),
                "oversold": self._jitter(oversold_base, 16, 30),
                "overbought": self._jitter(overbought_base, 56, 74),
            }
        if strategy_type == "mean_reversion_short":
            oversold_base = 20 if fear_greed <= 38 or north_3d < 0 else 18
            overbought_base = 76 if volatility >= 55 else 72
            base_period = 8 if fear_greed <= 38 or volatility <= 40 else 10
            return {
                "rsi_period": self._jitter(base_period, 4, 14),
                "oversold": self._jitter(oversold_base, 16, 26),
                "overbought": self._jitter(overbought_base, 68, 82),
            }
        if strategy_type == "value_factor":
            lookback_base = 72 if north_3d < 0 or fear_greed <= 45 else 60
            return {
                "lookback": self._jitter(lookback_base, 30, 100),
                "buy_quantile": self._jitter_f(0.82, 0.72, 0.9),
                "sell_quantile": self._jitter_f(0.18, 0.1, 0.28),
            }
        if strategy_type == "quality_factor":
            lookback_base = 72 if volatility >= 60 or north_3d < 0 else 60
            return {
                "lookback": self._jitter(lookback_base, 30, 100),
                "buy_quantile": self._jitter_f(0.8, 0.72, 0.9),
                "sell_quantile": self._jitter_f(0.2, 0.1, 0.28),
            }
        if strategy_type == "growth_factor":
            lookback_base = 36 if north_3d > 0 and fear_greed >= 60 else 48
            return {
                "lookback": self._jitter(lookback_base, 25, 80),
                "buy_quantile": self._jitter_f(0.82, 0.72, 0.92),
                "sell_quantile": self._jitter_f(0.18, 0.08, 0.28),
            }
        if strategy_type == "multi_factor":
            weights = {
                "value": random.uniform(0.2, 0.5),
                "quality": random.uniform(0.2, 0.5),
                "growth": random.uniform(0.2, 0.5),
            }
            total = sum(weights.values())
            weights = {key: round(value / total, 2) for key, value in weights.items()}
            lookback_base = 72 if north_3d < 0 else 60
            return {"factor_weights": weights, "lookback": self._jitter(lookback_base, 30, 100)}
        if strategy_type == "macro_timing":
            return {
                "fear_threshold": self._jitter(35 if north_3d < 0 else 32, 24, 45),
                "greed_threshold": self._jitter(68 if north_3d > 0 else 64, 55, 78),
                "lookback": self._jitter(24 if volatility >= 60 else 20, 10, 40),
            }
        if strategy_type == "sector_rotation":
            weights = {"momentum": 0.45, "quality": 0.3, "value": 0.25}
            return {"factor_weights": weights, "lookback": self._jitter(24 if volatility >= 60 else 20, 10, 45)}
        if strategy_type == "north_capital_track":
            threshold_base = 0.012 if north_3d > 0 else 0.018
            return {"lookback": self._jitter(15, 5, 30), "threshold": self._jitter_f(threshold_base, 0.005, 0.04)}
        if strategy_type == "margin_divergence":
            return {
                "fear_threshold": self._jitter(44 if margin_5d < 0 else 40, 34, 50),
                "greed_threshold": self._jitter(62 if margin_5d > 0 else 60, 54, 70),
                "lookback": self._jitter(12, 8, 20),
                "rebound_window": self._jitter(3, 2, 5),
                "repair_drawdown_floor": self._jitter_f(-0.06, -0.09, -0.04),
                "repair_rebound_pct": self._jitter_f(0.012, 0.008, 0.02),
                "dryup_window": 3,
                "dryup_max_ratio": self._jitter_f(0.9, 0.82, 0.98),
                "liquidity_window": 8,
                "entry_volume_floor_ratio": self._jitter_f(1.0, 0.92, 1.1),
                "structure_window": 4,
                "structure_close_location_min": self._jitter_f(0.58, 0.52, 0.68),
                "structure_body_return_min": self._jitter_f(0.002, 0.001, 0.004),
                "max_hold_bars": self._jitter(8, 5, 12),
                "adverse_volume_break_ratio": self._jitter_f(0.72, 0.62, 0.82),
                "adverse_close_break_pct": self._jitter_f(-0.012, -0.02, -0.008),
                "max_active_symbols": 2,
                "universe_selection_profile": "liquidity_divergence_fit_v1",
            }
        return {}

    def _resolved_varied_defaults(
        self,
        strategy_type: str,
        idx: int,
        *,
        snapshot: Optional[dict] = None,
        registry: Optional[ParameterDistributionRegistry] = None,
        research_task: Optional[dict] = None,
    ) -> tuple[dict, str, int]:
        parameter_registry = registry or ParameterDistributionRegistry.from_snapshot(snapshot)
        sampled = parameter_registry.sample(strategy_type, idx)
        if sampled:
            return (
                dict(sampled.get("params") or {}),
                str(sampled.get("source") or "historical_distribution"),
                int(sampled.get("sample_count") or 0),
            )
        # PR-S21：没有历史分布时，若 task 携带了 stock_profile 驱动的
        # param_search_space / profile_param_band，则用它做"画像驱动"采样，
        # 让本地路径也能在同 family 跨股票产生差异化参数。
        task = dict(research_task or {})
        param_band = (
            task.get("param_search_space")
            or task.get("profile_param_band")
            or {}
        )
        band_sample = ParameterDistributionRegistry.sample_from_param_band(param_band, idx=idx)
        if band_sample:
            base_params = self._legacy_varied_defaults(strategy_type, idx, snapshot=snapshot)
            merged = dict(base_params or {})
            merged.update(dict(band_sample.get("params") or {}))
            return (
                merged,
                "profile_param_band",
                0,
            )
        return self._legacy_varied_defaults(strategy_type, idx, snapshot=snapshot), "fixed_defaults", 0

    def _varied_defaults(self, strategy_type: str, idx: int, snapshot: Optional[dict] = None) -> dict:
        params, _, _ = self._resolved_varied_defaults(strategy_type, idx, snapshot=snapshot)
        return params

    def _quota_fill_source_mode(
        self,
        strategy_type: str,
        *,
        snapshot: Optional[dict] = None,
        current_candidates: Optional[list[dict]] = None,
        parameter_source: str,
        parameter_sample_count: int,
    ) -> str:
        if parameter_source == "historical_distribution" and parameter_sample_count >= 3:
            return "historical_guided"
        if current_candidates:
            return "signal_aligned"
        return "no_signal_fallback"

    @staticmethod
    def _quota_fill_quality_tier(fill_source_mode: str) -> str:
        if fill_source_mode == "historical_guided":
            return "oos_validated_history"
        if fill_source_mode == "signal_aligned":
            return "market_signal_aligned"
        return "fallback_only"


    @staticmethod
    def _make(
        strategy_type: str,
        params: dict,
        reason: str = "",
        *,
        source: str = "unknown",
        trigger_signal: Optional[dict] = None,
        trigger_thresholds: Optional[List[dict]] = None,
        quota_fill: Optional[dict] = None,
        kind: str = "signal_trigger",
        extras: Optional[dict] = None,
    ) -> dict:
        generation_reason = StrategySpawner._build_generation_reason(
            source=source,
            reason=reason,
            trigger_signal=trigger_signal,
            trigger_thresholds=trigger_thresholds,
            quota_fill=quota_fill,
            kind=kind,
        )
        payload = {
            "strategy_type": strategy_type,
            "params": params,
            "spawn_reason": reason,
            "generation_reason": generation_reason,
            "trigger_signal": generation_reason["trigger_signal"],
            "trigger_thresholds": generation_reason["trigger_thresholds"],
            "quota_fill": quota_fill,
        }
        if extras:
            payload.update(dict(extras))
        return payload

