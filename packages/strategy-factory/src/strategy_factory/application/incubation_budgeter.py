"""Dynamic incubation slot allocator for the P2 factory lane."""


from __future__ import annotations

import math
import os
from typing import Any

from ._budget_feedback import (
    extract_feedback_root,
    extract_generator_mode,
    extract_target_pool_id,
    normalize_feedback_input_contract,
    resolve_feedback_metrics,
)
from ..domain.constants import (
    FACTORY_INCUBATION_EXPLORATION_RATIO,
    FACTORY_INCUBATION_FORMAL_SLOT_COUNT,
    FACTORY_INCUBATION_OBSERVE_SLOT_COUNT,
)
import logging

logger = logging.getLogger(__name__)

_BASE_PRIORITY_SHARPE_WEIGHT = float(
    os.environ.get("STRATEGY_FACTORY_PRIORITY_SHARPE_WEIGHT", "10.0") or 10.0
)
_BASE_PRIORITY_TOTAL_RETURN_WEIGHT = float(
    os.environ.get("STRATEGY_FACTORY_PRIORITY_TOTAL_RETURN_WEIGHT", "3.0") or 3.0
)
_BASE_PRIORITY_MAX_DRAWDOWN_WEIGHT = float(
    os.environ.get("STRATEGY_FACTORY_PRIORITY_MAX_DRAWDOWN_WEIGHT", "8.0") or 8.0
)

class IncubationBudgeter:
    """Allocate candidates into formal / observe / deferred incubation tracks."""

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _candidate_family(candidate: dict[str, Any]) -> str:
        payload = dict(candidate or {})
        research_task = dict(payload.get("research_task") or {})
        params = dict(payload.get("params") or {})
        candidate_provenance = dict(params.get("candidate_provenance") or {})
        return str(
            payload.get("candidate_family")
            or research_task.get("candidate_family")
            or params.get("candidate_family")
            or candidate_provenance.get("candidate_family")
            or payload.get("strategy_type")
            or "unknown"
        ).strip().lower() or "unknown"

    @staticmethod
    def _candidate_contract_value(candidate: dict[str, Any], key: str, default: Any = None) -> Any:
        payload = dict(candidate or {})
        params = dict(payload.get("params") or {})
        if key in payload:
            return payload.get(key)
        if key in params:
            return params.get(key)
        return default

    @classmethod
    def _is_formal_runtime_ready_candidate(cls, candidate: dict[str, Any]) -> bool:
        readiness_tier = str(
            cls._candidate_contract_value(candidate, "execution_readiness_tier") or ""
        ).strip().lower()
        trade_contract_status = str(
            cls._candidate_contract_value(candidate, "trade_prediction_contract_status") or ""
        ).strip().lower()
        semantic_runtime_match = cls._candidate_contract_value(
            candidate,
            "semantic_runtime_match",
        )
        proxy_runtime_used = bool(
            cls._candidate_contract_value(candidate, "proxy_runtime_used")
        )
        diagnostic_only = bool(
            cls._candidate_contract_value(candidate, "diagnostic_only")
        )
        observation_gap = bool(
            cls._candidate_contract_value(
                candidate,
                "trade_prediction_contract_observation_gap",
            )
        )
        execution_semantic_gap = bool(
            cls._candidate_contract_value(candidate, "execution_semantic_gap")
        )
        return (
            readiness_tier == "formal_runtime_ready"
            and trade_contract_status == "ready"
            and semantic_runtime_match is True
            and not proxy_runtime_used
            and not diagnostic_only
            and not observation_gap
            and not execution_semantic_gap
        )

    @staticmethod
    def _task_feedback_override(candidate: dict[str, Any]) -> dict[str, Any]:
        research_task = dict((candidate or {}).get("research_task") or {})
        if not research_task:
            return {}
        field_map = {
            "feedback_control_mode": "control_mode",
            "feedback_legacy_control_mode": "legacy_control_mode",
            "feedback_skill_control_mode": "skill_control_mode",
            "feedback_control_reasons": "control_reasons",
            "feedback_legacy_control_reasons": "legacy_control_reasons",
            "feedback_skill_control_reasons": "skill_control_reasons",
            "feedback_cooldown_active": "cooldown_active",
            "feedback_suppressed": "suppressed",
            "feedback_skill_cooldown_active": "skill_cooldown_active",
            "feedback_skill_suppressed": "skill_suppressed",
            "feedback_relaxed_throttle_active": "relaxed_throttle_active",
            "feedback_control_relaxed": "control_relaxed",
            "feedback_control_relaxed_mode": "control_relaxed_mode",
            "feedback_control_original_mode": "control_original_mode",
            "feedback_control_relax_reason": "control_relax_reason",
            "feedback_generation_limited": "generation_limited",
        }
        override: dict[str, Any] = {}
        for source_key, target_key in field_map.items():
            value = research_task.get(source_key)
            if value in (None, "", [], {}):
                continue
            override[target_key] = value
        return override

    @staticmethod
    def _resolve_budget_feedback_root(snapshot: dict[str, Any]) -> dict[str, Any]:
        factor_research = dict(snapshot.get("factor_research") or {})
        for payload in (
            factor_research.get("lifecycle_feedback_input"),
            factor_research.get("budget_feedback"),
            snapshot.get("family_gate_feedback"),
        ):
            if isinstance(payload, dict):
                feedback_root = dict(
                    normalize_feedback_input_contract(payload).get("feedback") or {}
                )
                if feedback_root:
                    return feedback_root
        return {}
    @classmethod
    def _candidate_feedback(
        cls,
        candidate: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        budget_feedback_root: Any = None,
    ) -> dict[str, Any]:
        feedback_root = (
            dict(budget_feedback_root or {})
            if isinstance(budget_feedback_root, dict)
            else cls._resolve_budget_feedback_root(snapshot)
        )
        family_name = cls._candidate_family(candidate)
        feedback_metrics = resolve_feedback_metrics(
            feedback_root,
            family=family_name,
            target_pool_id=extract_target_pool_id(candidate),
            generator_mode=extract_generator_mode(candidate),
        )
        task_feedback_override = cls._task_feedback_override(candidate)
        if task_feedback_override:
            feedback_metrics = {
                **feedback_metrics,
                **task_feedback_override,
            }
        feedback_scope = {
            "family": family_name,
            "target_pool_id": feedback_metrics.get("target_pool_id"),
            "generator_mode": feedback_metrics.get("generator_mode"),
            "family_feedback_available": bool(feedback_metrics.get("family_feedback_available")),
            "target_pool_feedback_available": bool(feedback_metrics.get("target_pool_feedback_available")),
            "generator_mode_feedback_available": bool(feedback_metrics.get("generator_mode_feedback_available")),
            "control_mode": feedback_metrics.get("control_mode"),
            "legacy_control_mode": feedback_metrics.get("legacy_control_mode"),
            "skill_control_mode": feedback_metrics.get("skill_control_mode"),
            "cooldown_active": bool(feedback_metrics.get("cooldown_active")),
            "suppressed": bool(feedback_metrics.get("suppressed")),
            "family_freeze_active": bool(feedback_metrics.get("family_freeze_active")),
            "target_pool_freeze_active": bool(feedback_metrics.get("target_pool_freeze_active")),
            "generator_mode_freeze_active": bool(feedback_metrics.get("generator_mode_freeze_active")),
            "skill_cooldown_active": bool(feedback_metrics.get("skill_cooldown_active")),
            "skill_suppressed": bool(feedback_metrics.get("skill_suppressed")),
            "skill_family_freeze_active": bool(feedback_metrics.get("skill_family_freeze_active")),
            "skill_target_pool_freeze_active": bool(
                feedback_metrics.get("skill_target_pool_freeze_active")
            ),
            "skill_generator_mode_freeze_active": bool(
                feedback_metrics.get("skill_generator_mode_freeze_active")
            ),
            "paper_skill_lcb": cls._safe_float(feedback_metrics.get("paper_skill_lcb")),
            "paper_recent_skill_lcb": cls._safe_float(
                feedback_metrics.get("paper_recent_skill_lcb")
            ),
            "paper_stability_gap": cls._safe_float(feedback_metrics.get("paper_stability_gap")),
            "paper_coverage_ratio": cls._safe_float(
                feedback_metrics.get("paper_coverage_ratio"),
                1.0,
            ),
            "execution_conversion_efficiency": (
                cls._safe_float(feedback_metrics.get("execution_conversion_efficiency"))
                if feedback_metrics.get("execution_conversion_efficiency_available")
                else None
            ),
            "execution_conversion_efficiency_available": bool(
                feedback_metrics.get("execution_conversion_efficiency_available")
            ),
            "budget_feedback_action": feedback_metrics.get("budget_feedback_action"),
            "budget_action_applied": bool(feedback_metrics.get("budget_action_applied")),
            "prediction_axis": feedback_metrics.get("prediction_axis"),
            "execution_axis": feedback_metrics.get("execution_axis"),
            "execution_optimization_queue": bool(
                feedback_metrics.get("execution_optimization_queue")
            ),
            "small_budget_observe": bool(feedback_metrics.get("small_budget_observe")),
            "prioritize_scale": bool(feedback_metrics.get("prioritize_scale")),
            "cool_or_freeze": bool(feedback_metrics.get("cool_or_freeze")),
            "retain_family": bool(feedback_metrics.get("retain_family")),
            "reduce_budget": bool(feedback_metrics.get("reduce_budget")),
            "no_expansion": bool(feedback_metrics.get("no_expansion")),
        }
        feedback_scope["feedback_available"] = any(
            bool(feedback_scope.get(key))
            for key in (
                "family_feedback_available",
                "target_pool_feedback_available",
                "generator_mode_feedback_available",
            )
        )
        return {
            "root": feedback_root,
            "metrics": feedback_metrics,
            "scope": feedback_scope,
        }

    @staticmethod
    def _expected_regimes(candidate: dict[str, Any]) -> list[str]:
        payload = dict(candidate or {})
        params = dict(payload.get("params") or {})
        candidate_provenance = dict(params.get("candidate_provenance") or {})
        values = (
            payload.get("expected_regime")
            or params.get("expected_regime")
            or candidate_provenance.get("expected_regime")
            or []
        )
        if not isinstance(values, list):
            values = [values]
        return [
            str(item or "").strip().lower()
            for item in list(values or [])
            if str(item or "").strip()
        ]

    @staticmethod
    def _market_regime(snapshot: dict[str, Any]) -> str:
        fg = IncubationBudgeter._safe_float(snapshot.get("fear_greed_index"), 50.0)
        if fg >= 60:
            return "trend"
        if fg <= 40:
            return "mean_reversion"
        return "rotation"

    @classmethod
    def _regime_match_bonus(cls, candidate: dict[str, Any], snapshot: dict[str, Any]) -> float:
        market_regime = cls._market_regime(snapshot)
        expected_regimes = cls._expected_regimes(candidate)
        regime_fit = str(
            dict(candidate.get("research_task") or {}).get("regime_fit")
            or candidate.get("regime_fit")
            or dict(candidate.get("params") or {}).get("regime_fit")
            or ""
        ).strip().lower()
        if market_regime == "trend" and (
            "trend" in expected_regimes or "trend" in regime_fit or "breakout" in regime_fit
        ):
            return 6.0
        if market_regime == "mean_reversion" and (
            "mean_reversion" in expected_regimes
            or "mean_reversion" in regime_fit
            or "reversal" in regime_fit
        ):
            return 6.0
        if market_regime == "rotation" and (
            "rotation" in expected_regimes or "rotation" in regime_fit or "balanced" in regime_fit
        ):
            return 5.0
        return 0.0

    @classmethod
    def _priority_score(
        cls,
        candidate: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        budget_feedback_root: Any = None,
    ) -> float:
        payload = dict(candidate or {})
        metrics = dict(payload.get("backtest_metrics") or {})
        research_task = dict(payload.get("research_task") or {})
        params = dict(payload.get("params") or {})
        candidate_provenance = dict(params.get("candidate_provenance") or {})

        sharpe = cls._safe_float(metrics.get("sharpe_ratio"))
        total_return = cls._safe_float(metrics.get("total_return"))
        max_drawdown = max(0.0, cls._safe_float(metrics.get("max_drawdown")))
        validation_score = cls._safe_float(
            payload.get("candidate_validation_score")
            or candidate_provenance.get("validation_score")
            or params.get("candidate_validation_score")
        )
        task_priority = cls._safe_float(
            payload.get("priority")
            or research_task.get("priority")
            or payload.get("matrix_priority_score")
            or research_task.get("matrix_priority_score")
        )
        stock_family_priority = cls._safe_float(
            payload.get("stock_family_priority")
            or research_task.get("stock_family_priority")
        )
        registry_stage = str(
            payload.get("candidate_registry_stage")
            or candidate_provenance.get("candidate_registry_stage")
            or params.get("candidate_registry_stage")
            or ""
        ).strip().lower()
        risk_level = str(
            payload.get("risk_level")
            or research_task.get("risk_level")
            or params.get("risk_level")
            or candidate_provenance.get("risk_level")
            or ""
        ).strip().lower()
        active_family_names = {
            str(item or "").strip().lower()
            for item in list(((snapshot.get("factor_research") or {}).get("summary") or {}).get("active_family_names") or [])
            if str(item or "").strip()
        }
        family_name = cls._candidate_family(payload)

        score = 0.0
        score += max(-1.0, min(sharpe, 3.0)) * _BASE_PRIORITY_SHARPE_WEIGHT
        score += max(-0.2, min(total_return, 0.6)) * _BASE_PRIORITY_TOTAL_RETURN_WEIGHT
        score -= min(max_drawdown, 0.6) * _BASE_PRIORITY_MAX_DRAWDOWN_WEIGHT
        score += min(max(validation_score, 0.0), 100.0) * 0.25
        score += max(task_priority, 0.0) * 0.18
        score += max(stock_family_priority, 0.0) * 12.0
        score += cls._regime_match_bonus(payload, snapshot)
        if family_name in active_family_names:
            score += 4.0
        if registry_stage == "champion":
            score += 5.0
        elif registry_stage == "challenger":
            score += 4.0
        elif registry_stage == "governed":
            score += 3.0
        if risk_level == "low":
            score += 3.0
        elif risk_level == "high":
            score -= 4.0

        feedback_payload = cls._candidate_feedback(
            payload,
            snapshot,
            budget_feedback_root=budget_feedback_root,
        )
        feedback_metrics = dict(feedback_payload.get("metrics") or {})
        feedback_scope = dict(feedback_payload.get("scope") or {})
        feedback_priority_adjustment = cls._safe_float(feedback_metrics.get("priority_adjustment"))
        feedback_budget_multiplier = cls._safe_float(feedback_metrics.get("budget_multiplier"), 1.0)
        if bool(feedback_scope.get("feedback_available")):
            feedback_skill_priority_adjustment = cls._safe_float(
                feedback_metrics.get("skill_priority_adjustment")
            )
            feedback_skill_budget_multiplier = cls._safe_float(
                feedback_metrics.get("skill_budget_multiplier"),
                1.0,
            )
            paper_skill_lcb = cls._safe_float(feedback_metrics.get("paper_skill_lcb"))
            paper_recent_skill_lcb = cls._safe_float(
                feedback_metrics.get("paper_recent_skill_lcb"),
                paper_skill_lcb,
            )
            paper_stability_gap = max(
                0.0,
                cls._safe_float(feedback_metrics.get("paper_stability_gap")),
            )
            paper_coverage_ratio = max(
                0.0,
                min(cls._safe_float(feedback_metrics.get("paper_coverage_ratio"), 1.0), 1.0),
            )
            execution_conversion_efficiency_available = bool(
                feedback_metrics.get("execution_conversion_efficiency_available")
            )
            execution_conversion_efficiency = cls._safe_float(
                feedback_metrics.get("execution_conversion_efficiency")
            )
            score += feedback_priority_adjustment * 0.45
            score += feedback_skill_priority_adjustment
            score += max(-0.12, min(paper_skill_lcb, 0.18)) * 55.0
            score += max(-0.12, min(paper_recent_skill_lcb, 0.18)) * 34.0
            score -= max(paper_stability_gap - 0.05, 0.0) * 40.0
            score += max(min(paper_coverage_ratio, 1.0) - 0.50, -0.50) * 14.0
            if execution_conversion_efficiency_available:
                score += max(min(execution_conversion_efficiency, 0.40), -0.10) * 28.0
            combined_budget_multiplier = (
                feedback_budget_multiplier * 0.35
                + feedback_skill_budget_multiplier * 0.65
            )
            score *= max(0.68, min(1.35, 0.45 + combined_budget_multiplier * 0.55))
        else:
            score += feedback_priority_adjustment
            score *= max(0.7, min(1.3, 0.7 + feedback_budget_multiplier * 0.3))
        control_mode = str(feedback_metrics.get("control_mode") or "").strip().lower()
        skill_control_mode = str(feedback_metrics.get("skill_control_mode") or "").strip().lower()
        if control_mode == "cooldown":
            score -= 9.0
        elif control_mode == "suppress":
            score -= 24.0
        elif control_mode == "freeze":
            score -= 36.0
        if skill_control_mode == "cooldown":
            score -= 6.0
        elif skill_control_mode == "suppress":
            score -= 16.0
        elif skill_control_mode == "freeze":
            score -= 28.0

        # P2-D 反馈回路：对纯 family EMA 的轻量兼容，避免没有 P3 feedback 时退化。
        if not bool(feedback_scope.get("feedback_available")):
            family_feedback = dict((snapshot.get("family_gate_feedback") or {}).get(family_name) or {})
            ema_submit = cls._safe_float(family_feedback.get("ema_submit_count"), -1.0)
            if ema_submit >= 0.0:
                if ema_submit > 3.0:
                    score += 5.0
                elif ema_submit > 1.0:
                    score += 2.5
                elif ema_submit < 0.3:
                    score -= 2.0

        return round(score, 4)
    @classmethod
    def _is_exploration_candidate(
        cls,
        candidate: dict[str, Any],
        *,
        dominant_families: set[str],
        active_family_names: set[str],
    ) -> bool:
        family_name = cls._candidate_family(candidate)
        if family_name not in dominant_families:
            return True
        if family_name not in active_family_names:
            return True
        params = dict(candidate.get("params") or {})
        candidate_provenance = dict(params.get("candidate_provenance") or {})
        registry_stage = str(
            params.get("candidate_registry_stage")
            or candidate.get("candidate_registry_stage")
            or candidate_provenance.get("candidate_registry_stage")
            or ""
        ).strip().lower()
        return registry_stage not in {"champion", "challenger", "governed"}

    @staticmethod
    def _is_selectable_entry(entry: dict[str, Any]) -> bool:
        if str(entry.get("feedback_control_mode") or "normal").strip().lower() != "normal":
            return False
        if str(entry.get("feedback_skill_control_mode") or "normal").strip().lower() != "normal":
            return False
        for flag in (
            "feedback_cooldown_active",
            "feedback_suppressed",
            "feedback_family_freeze_active",
            "feedback_target_pool_freeze_active",
            "feedback_generator_mode_freeze_active",
            "feedback_skill_cooldown_active",
            "feedback_skill_suppressed",
            "feedback_skill_family_freeze_active",
            "feedback_skill_target_pool_freeze_active",
            "feedback_skill_generator_mode_freeze_active",
        ):
            if bool(entry.get(flag)):
                return False
        return True

    @classmethod
    def plan(
        cls,
        candidates: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        formal_slots = max(1, int(FACTORY_INCUBATION_FORMAL_SLOT_COUNT))
        observe_slots = max(0, int(FACTORY_INCUBATION_OBSERVE_SLOT_COUNT))
        total_budget = formal_slots + observe_slots
        if not candidates:
            return {
                "plans": {},
                "summary": {
                    "formal_slots": formal_slots,
                    "observe_slots": observe_slots,
                    "exploration_reserved_slots": 0,
                    "track_counts": {
                        "formal_incubation": 0,
                        "observe_incubation": 0,
                        "deferred_budget_queue": 0,
                    },
                    "family_counts": {},
                    "dominant_families": [],
                },
            }

        family_counts: dict[str, int] = {}
        family_best_scores: dict[str, float] = {}
        budget_feedback_root = cls._resolve_budget_feedback_root(snapshot)
        feedback_family_names: set[str] = set()
        feedback_target_pool_ids: set[str] = set()
        feedback_generator_modes: set[str] = set()
        feedback_candidate_count = 0
        feedback_budget_multiplier_values: list[float] = []
        feedback_priority_adjustment_values: list[float] = []
        feedback_skill_budget_multiplier_values: list[float] = []
        feedback_skill_priority_adjustment_values: list[float] = []
        feedback_paper_skill_lcb_values: list[float] = []
        feedback_paper_recent_skill_lcb_values: list[float] = []
        feedback_paper_stability_gap_values: list[float] = []
        feedback_paper_coverage_ratio_values: list[float] = []
        feedback_execution_conversion_efficiency_values: list[float] = []
        feedback_budget_promoted_count = 0
        feedback_budget_constrained_count = 0
        feedback_budget_action_counts: dict[str, int] = {}
        feedback_dual_axis_action_count = 0
        feedback_execution_optimization_queue_count = 0
        feedback_small_budget_observe_count = 0
        feedback_prioritize_scale_count = 0
        feedback_cool_or_freeze_count = 0
        feedback_controlled_count = 0
        feedback_cooldown_count = 0
        feedback_suppressed_count = 0
        feedback_freeze_count = 0
        feedback_skill_budget_promoted_count = 0
        feedback_skill_budget_constrained_count = 0
        feedback_skill_controlled_count = 0
        feedback_skill_cooldown_count = 0
        feedback_skill_suppressed_count = 0
        feedback_skill_freeze_count = 0
        feedback_target_pool_freeze_count = 0
        feedback_generator_mode_freeze_count = 0
        feedback_skill_target_pool_freeze_count = 0
        feedback_skill_generator_mode_freeze_count = 0
        active_family_names = {
            str(item or "").strip().lower()
            for item in list(((snapshot.get("factor_research") or {}).get("summary") or {}).get("active_family_names") or [])
            if str(item or "").strip()
        }
        entries: list[dict[str, Any]] = []
        for candidate in list(candidates or []):
            family_name = cls._candidate_family(candidate)
            feedback_payload = cls._candidate_feedback(
                candidate,
                snapshot,
                budget_feedback_root=budget_feedback_root,
            )
            feedback_metrics = dict(feedback_payload.get("metrics") or {})
            feedback_scope = dict(feedback_payload.get("scope") or {})
            score = cls._priority_score(
                candidate,
                snapshot,
                budget_feedback_root=budget_feedback_root,
            )
            family_counts[family_name] = family_counts.get(family_name, 0) + 1
            family_best_scores[family_name] = max(score, family_best_scores.get(family_name, score))
            if bool(feedback_scope.get("feedback_available")):
                feedback_candidate_count += 1
                feedback_family_names.add(family_name)
                target_pool_id = str(feedback_scope.get("target_pool_id") or "").strip()
                generator_mode = str(feedback_scope.get("generator_mode") or "").strip().lower()
                if target_pool_id and bool(feedback_scope.get("target_pool_feedback_available")):
                    feedback_target_pool_ids.add(target_pool_id)
                if generator_mode and bool(feedback_scope.get("generator_mode_feedback_available")):
                    feedback_generator_modes.add(generator_mode)
                feedback_budget_multiplier = cls._safe_float(feedback_metrics.get("budget_multiplier"), 1.0)
                feedback_priority_adjustment = cls._safe_float(feedback_metrics.get("priority_adjustment"))
                feedback_skill_budget_multiplier = cls._safe_float(
                    feedback_metrics.get("skill_budget_multiplier"),
                    1.0,
                )
                feedback_skill_priority_adjustment = cls._safe_float(
                    feedback_metrics.get("skill_priority_adjustment")
                )
                feedback_budget_multiplier_values.append(feedback_budget_multiplier)
                feedback_priority_adjustment_values.append(feedback_priority_adjustment)
                feedback_skill_budget_multiplier_values.append(feedback_skill_budget_multiplier)
                feedback_skill_priority_adjustment_values.append(feedback_skill_priority_adjustment)
                feedback_paper_skill_lcb_values.append(
                    cls._safe_float(feedback_metrics.get("paper_skill_lcb"))
                )
                feedback_paper_recent_skill_lcb_values.append(
                    cls._safe_float(feedback_metrics.get("paper_recent_skill_lcb"))
                )
                feedback_paper_stability_gap_values.append(
                    cls._safe_float(feedback_metrics.get("paper_stability_gap"))
                )
                feedback_paper_coverage_ratio_values.append(
                    cls._safe_float(feedback_metrics.get("paper_coverage_ratio"), 1.0)
                )
                if bool(feedback_metrics.get("execution_conversion_efficiency_available")):
                    feedback_execution_conversion_efficiency_values.append(
                        cls._safe_float(
                            feedback_metrics.get("execution_conversion_efficiency")
                        )
                    )
                budget_action = str(
                    feedback_metrics.get("budget_feedback_action") or ""
                ).strip().lower()
                if budget_action:
                    feedback_dual_axis_action_count += 1
                    feedback_budget_action_counts[budget_action] = (
                        feedback_budget_action_counts.get(budget_action, 0) + 1
                    )
                if bool(feedback_metrics.get("execution_optimization_queue")):
                    feedback_execution_optimization_queue_count += 1
                if bool(feedback_metrics.get("small_budget_observe")):
                    feedback_small_budget_observe_count += 1
                if bool(feedback_metrics.get("prioritize_scale")):
                    feedback_prioritize_scale_count += 1
                if bool(feedback_metrics.get("cool_or_freeze")):
                    feedback_cool_or_freeze_count += 1
                if feedback_budget_multiplier > 1.02 or feedback_priority_adjustment > 0.5:
                    feedback_budget_promoted_count += 1
                if feedback_budget_multiplier < 0.98 or feedback_priority_adjustment < -0.5:
                    feedback_budget_constrained_count += 1
                if feedback_skill_budget_multiplier > 1.02 or feedback_skill_priority_adjustment > 0.5:
                    feedback_skill_budget_promoted_count += 1
                if feedback_skill_budget_multiplier < 0.98 or feedback_skill_priority_adjustment < -0.5:
                    feedback_skill_budget_constrained_count += 1
            control_mode = str(feedback_metrics.get("control_mode") or "").strip().lower()
            skill_control_mode = str(feedback_metrics.get("skill_control_mode") or "").strip().lower()
            if control_mode and control_mode != "normal":
                feedback_controlled_count += 1
            if control_mode == "cooldown":
                feedback_cooldown_count += 1
            elif control_mode == "suppress":
                feedback_suppressed_count += 1
            elif control_mode == "freeze":
                feedback_freeze_count += 1
            if skill_control_mode and skill_control_mode != "normal":
                feedback_skill_controlled_count += 1
            if skill_control_mode == "cooldown":
                feedback_skill_cooldown_count += 1
            elif skill_control_mode == "suppress":
                feedback_skill_suppressed_count += 1
            elif skill_control_mode == "freeze":
                feedback_skill_freeze_count += 1
            if bool(feedback_metrics.get("target_pool_freeze_active")):
                feedback_target_pool_freeze_count += 1
            if bool(feedback_metrics.get("generator_mode_freeze_active")):
                feedback_generator_mode_freeze_count += 1
            if bool(feedback_metrics.get("skill_target_pool_freeze_active")):
                feedback_skill_target_pool_freeze_count += 1
            if bool(feedback_metrics.get("skill_generator_mode_freeze_active")):
                feedback_skill_generator_mode_freeze_count += 1
            entries.append(
                {
                    "marker": id(candidate),
                    "candidate": candidate,
                    "family": family_name,
                    "priority_score": score,
                    "feedback_metrics": feedback_metrics,
                    "feedback_scope": feedback_scope,
                    "feedback_budget_multiplier": cls._safe_float(
                        feedback_metrics.get("budget_multiplier"),
                        1.0,
                    ),
                    "feedback_priority_adjustment": cls._safe_float(
                        feedback_metrics.get("priority_adjustment")
                    ),
                    "feedback_failure_penalty_adjustment": cls._safe_float(
                        feedback_metrics.get("failure_penalty_adjustment")
                    ),
                    "feedback_legacy_budget_multiplier": cls._safe_float(
                        feedback_metrics.get("legacy_budget_multiplier"),
                        1.0,
                    ),
                    "feedback_legacy_priority_adjustment": cls._safe_float(
                        feedback_metrics.get("legacy_priority_adjustment")
                    ),
                    "feedback_skill_budget_multiplier": cls._safe_float(
                        feedback_metrics.get("skill_budget_multiplier"),
                        1.0,
                    ),
                    "feedback_skill_priority_adjustment": cls._safe_float(
                        feedback_metrics.get("skill_priority_adjustment")
                    ),
                    "feedback_skill_failure_penalty_adjustment": cls._safe_float(
                        feedback_metrics.get("skill_failure_penalty_adjustment")
                    ),
                    "feedback_control_mode": control_mode or "normal",
                    "feedback_legacy_control_mode": str(
                        feedback_metrics.get("legacy_control_mode") or control_mode or "normal"
                    ),
                    "feedback_skill_control_mode": skill_control_mode or "normal",
                    "feedback_control_reasons": list(feedback_metrics.get("control_reasons") or []),
                    "feedback_legacy_control_reasons": list(
                        feedback_metrics.get("legacy_control_reasons") or []
                    ),
                    "feedback_skill_control_reasons": list(
                        feedback_metrics.get("skill_control_reasons") or []
                    ),
                    "feedback_cooldown_active": bool(feedback_metrics.get("cooldown_active")),
                    "feedback_suppressed": bool(feedback_metrics.get("suppressed")),
                    "feedback_family_freeze_active": bool(feedback_metrics.get("family_freeze_active")),
                    "feedback_target_pool_freeze_active": bool(feedback_metrics.get("target_pool_freeze_active")),
                    "feedback_generator_mode_freeze_active": bool(feedback_metrics.get("generator_mode_freeze_active")),
                    "feedback_skill_cooldown_active": bool(
                        feedback_metrics.get("skill_cooldown_active")
                    ),
                    "feedback_skill_suppressed": bool(feedback_metrics.get("skill_suppressed")),
                    "feedback_skill_family_freeze_active": bool(
                        feedback_metrics.get("skill_family_freeze_active")
                    ),
                    "feedback_skill_target_pool_freeze_active": bool(
                        feedback_metrics.get("skill_target_pool_freeze_active")
                    ),
                    "feedback_skill_generator_mode_freeze_active": bool(
                        feedback_metrics.get("skill_generator_mode_freeze_active")
                    ),
                    "feedback_paper_skill_lcb": cls._safe_float(
                        feedback_metrics.get("paper_skill_lcb")
                    ),
                    "feedback_paper_recent_skill_lcb": cls._safe_float(
                        feedback_metrics.get("paper_recent_skill_lcb")
                    ),
                    "feedback_paper_stability_gap": cls._safe_float(
                        feedback_metrics.get("paper_stability_gap")
                    ),
                    "feedback_paper_coverage_ratio": cls._safe_float(
                        feedback_metrics.get("paper_coverage_ratio"),
                        1.0,
                    ),
                    "feedback_execution_conversion_efficiency": (
                        cls._safe_float(feedback_metrics.get("execution_conversion_efficiency"))
                        if feedback_metrics.get("execution_conversion_efficiency_available")
                        else None
                    ),
                    "feedback_execution_conversion_efficiency_available": bool(
                        feedback_metrics.get("execution_conversion_efficiency_available")
                    ),
                    "feedback_budget_action": feedback_metrics.get("budget_feedback_action"),
                    "feedback_budget_action_applied": bool(
                        feedback_metrics.get("budget_action_applied")
                    ),
                    "feedback_prediction_axis": feedback_metrics.get("prediction_axis"),
                    "feedback_execution_axis": feedback_metrics.get("execution_axis"),
                    "feedback_retain_family": bool(feedback_metrics.get("retain_family")),
                    "feedback_reduce_budget": bool(feedback_metrics.get("reduce_budget")),
                    "feedback_execution_optimization_queue": bool(
                        feedback_metrics.get("execution_optimization_queue")
                    ),
                    "feedback_small_budget_observe": bool(
                        feedback_metrics.get("small_budget_observe")
                    ),
                    "feedback_prioritize_scale": bool(
                        feedback_metrics.get("prioritize_scale")
                    ),
                    "feedback_cool_or_freeze": bool(feedback_metrics.get("cool_or_freeze")),
                    "feedback_no_expansion": bool(feedback_metrics.get("no_expansion")),
                    "feedback_effective_signal": str(
                        feedback_metrics.get("effective_feedback_signal")
                        or "legacy_paper_hit_ratio"
                    ),
                }
            )

        dominant_family_pairs = sorted(
            family_best_scores.items(),
            key=lambda item: (-float(item[1]), -int(family_counts.get(item[0]) or 0), item[0]),
        )
        dominant_families = {family for family, _score in dominant_family_pairs[:3]}
        sorted_entries = sorted(
            entries,
            key=lambda item: (-float(item["priority_score"]), item["family"], item["marker"]),
        )
        selectable_entries = [
            entry
            for entry in sorted_entries
            if cls._is_selectable_entry(entry)
        ]
        formal_ready_entries = [
            entry
            for entry in selectable_entries
            if cls._is_formal_runtime_ready_candidate(dict(entry.get("candidate") or {}))
        ]
        exploration_reserved_slots = (
            min(total_budget, max(1, int(math.ceil(total_budget * FACTORY_INCUBATION_EXPLORATION_RATIO))))
            if total_budget > 0 and FACTORY_INCUBATION_EXPLORATION_RATIO > 0.0
            else 0
        )
        formal_family_cap = max(1, int(math.ceil(formal_slots * 0.45)))
        observe_family_cap = max(1, int(math.ceil(max(observe_slots, 1) * 0.55)))

        selected_formal: list[dict[str, Any]] = []
        selected_observe: list[dict[str, Any]] = []
        family_track_counts: dict[str, dict[str, int]] = {}
        selected_markers: set[int] = set()
        formal_runtime_ready_selected_count = 0

        def _formal_priority_key(entry: dict[str, Any]) -> tuple[int, float, str, int]:
            candidate = dict(entry.get("candidate") or {})
            return (
                0 if cls._is_formal_runtime_ready_candidate(candidate) else 1,
                -float(entry.get("priority_score") or 0.0),
                str(entry.get("family") or ""),
                int(entry.get("marker") or 0),
            )

        def _select_with_cap(
            target: list[dict[str, Any]],
            *,
            limit: int,
            family_cap: int,
            prioritize_formal_ready: bool = False,
        ) -> None:
            nonlocal formal_runtime_ready_selected_count
            ranked_entries = (
                sorted(selectable_entries, key=_formal_priority_key)
                if prioritize_formal_ready
                else selectable_entries
            )
            for entry in ranked_entries:
                if len(target) >= limit:
                    break
                marker = int(entry["marker"])
                if marker in selected_markers:
                    continue
                family_name = str(entry["family"])
                track_family_counts = family_track_counts.setdefault(family_name, {})
                if int(track_family_counts.get("selected") or 0) >= family_cap:
                    continue
                target.append(entry)
                selected_markers.add(marker)
                track_family_counts["selected"] = int(track_family_counts.get("selected") or 0) + 1
                if prioritize_formal_ready and cls._is_formal_runtime_ready_candidate(
                    dict(entry.get("candidate") or {})
                ):
                    formal_runtime_ready_selected_count += 1
            for entry in ranked_entries:
                if len(target) >= limit:
                    break
                marker = int(entry["marker"])
                if marker in selected_markers:
                    continue
                family_name = str(entry["family"])
                track_family_counts = family_track_counts.setdefault(family_name, {})
                target.append(entry)
                selected_markers.add(marker)
                track_family_counts["selected"] = int(track_family_counts.get("selected") or 0) + 1
                if prioritize_formal_ready and cls._is_formal_runtime_ready_candidate(
                    dict(entry.get("candidate") or {})
                ):
                    formal_runtime_ready_selected_count += 1

        _select_with_cap(
            selected_formal,
            limit=formal_slots,
            family_cap=formal_family_cap,
            prioritize_formal_ready=True,
        )
        _select_with_cap(selected_observe, limit=observe_slots, family_cap=observe_family_cap)

        selected_combined = [*selected_formal, *selected_observe]
        selected_exploration_count = sum(
            1
            for entry in selected_combined
            if cls._is_exploration_candidate(
                dict(entry.get("candidate") or {}),
                dominant_families=dominant_families,
                active_family_names=active_family_names,
            )
        )
        if exploration_reserved_slots > selected_exploration_count and observe_slots > 0:
            exploration_pool = [
                entry
                for entry in selectable_entries
                if int(entry["marker"]) not in selected_markers
                and cls._is_exploration_candidate(
                    dict(entry.get("candidate") or {}),
                    dominant_families=dominant_families,
                    active_family_names=active_family_names,
                )
            ]
            # PR-S11: 按 priority_score 升序找替换目标（最小质量损失原则），
            # 取代旧的"末尾反向找第一个非探索候选"
            def _entry_score(entry: dict[str, Any]) -> float:
                try:
                    return float(entry.get("priority_score") or 0.0)
                except (TypeError, ValueError) as _swallowed:
                    logger.warning("packages/strategy-factory/src/strategy_factory/application/incubation_budgeter.py:909: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                    return 0.0
            while (
                exploration_pool
                and selected_exploration_count < exploration_reserved_slots
                and selected_observe
            ):
                promoted = exploration_pool.pop(0)
                # 在 selected_observe 中找所有"非探索"候选，按 priority_score 升序选最低
                non_exploration_indices = [
                    index
                    for index in range(len(selected_observe))
                    if not cls._is_exploration_candidate(
                        dict(selected_observe[index].get("candidate") or {}),
                        dominant_families=dominant_families,
                        active_family_names=active_family_names,
                    )
                ]
                if not non_exploration_indices:
                    break
                replaced_index = min(
                    non_exploration_indices,
                    key=lambda idx: _entry_score(selected_observe[idx]),
                )
                # 仅当 promoted 的分数高于 replaced 时才替换；否则放弃，避免反向降级
                if _entry_score(promoted) < _entry_score(selected_observe[replaced_index]):
                    break
                removed = selected_observe[replaced_index]
                selected_markers.discard(int(removed["marker"]))
                selected_observe[replaced_index] = promoted
                selected_markers.add(int(promoted["marker"]))
                selected_exploration_count += 1

        plans: dict[int, dict[str, Any]] = {}
        track_counts = {
            "formal_incubation": 0,
            "observe_incubation": 0,
            "deferred_budget_queue": 0,
        }
        rank = 0
        for track_name, bucket in (
            ("formal_incubation", selected_formal),
            ("observe_incubation", selected_observe),
        ):
            for entry in bucket:
                rank += 1
                candidate = dict(entry.get("candidate") or {})
                plan = {
                    "track": track_name,
                    "rank": rank,
                    "priority_score": float(entry.get("priority_score") or 0.0),
                    "family": entry.get("family"),
                    "feedback_metrics": dict(entry.get("feedback_metrics") or {}),
                    "feedback_scope": dict(entry.get("feedback_scope") or {}),
                    "feedback_budget_multiplier": float(entry.get("feedback_budget_multiplier") or 1.0),
                    "feedback_priority_adjustment": float(entry.get("feedback_priority_adjustment") or 0.0),
                    "feedback_failure_penalty_adjustment": float(
                        entry.get("feedback_failure_penalty_adjustment") or 0.0
                    ),
                    "feedback_control_mode": str(entry.get("feedback_control_mode") or "normal"),
                    "feedback_legacy_control_mode": str(
                        entry.get("feedback_legacy_control_mode") or "normal"
                    ),
                    "feedback_skill_control_mode": str(
                        entry.get("feedback_skill_control_mode") or "normal"
                    ),
                    "feedback_control_reasons": list(entry.get("feedback_control_reasons") or []),
                    "feedback_legacy_control_reasons": list(
                        entry.get("feedback_legacy_control_reasons") or []
                    ),
                    "feedback_skill_control_reasons": list(
                        entry.get("feedback_skill_control_reasons") or []
                    ),
                    "feedback_cooldown_active": bool(entry.get("feedback_cooldown_active")),
                    "feedback_suppressed": bool(entry.get("feedback_suppressed")),
                    "feedback_family_freeze_active": bool(entry.get("feedback_family_freeze_active")),
                    "feedback_target_pool_freeze_active": bool(entry.get("feedback_target_pool_freeze_active")),
                    "feedback_generator_mode_freeze_active": bool(entry.get("feedback_generator_mode_freeze_active")),
                    "feedback_skill_cooldown_active": bool(
                        entry.get("feedback_skill_cooldown_active")
                    ),
                    "feedback_skill_suppressed": bool(entry.get("feedback_skill_suppressed")),
                    "feedback_skill_family_freeze_active": bool(
                        entry.get("feedback_skill_family_freeze_active")
                    ),
                    "feedback_skill_target_pool_freeze_active": bool(
                        entry.get("feedback_skill_target_pool_freeze_active")
                    ),
                    "feedback_skill_generator_mode_freeze_active": bool(
                        entry.get("feedback_skill_generator_mode_freeze_active")
                    ),
                    "feedback_legacy_budget_multiplier": cls._safe_float(
                        entry.get("feedback_legacy_budget_multiplier"),
                        1.0,
                    ),
                    "feedback_legacy_priority_adjustment": float(
                        entry.get("feedback_legacy_priority_adjustment") or 0.0
                    ),
                    "feedback_skill_budget_multiplier": cls._safe_float(
                        entry.get("feedback_skill_budget_multiplier"),
                        1.0,
                    ),
                    "feedback_skill_priority_adjustment": float(
                        entry.get("feedback_skill_priority_adjustment") or 0.0
                    ),
                    "feedback_skill_failure_penalty_adjustment": float(
                        entry.get("feedback_skill_failure_penalty_adjustment") or 0.0
                    ),
                    "feedback_paper_skill_lcb": cls._safe_float(
                        entry.get("feedback_paper_skill_lcb")
                    ),
                    "feedback_paper_recent_skill_lcb": cls._safe_float(
                        entry.get("feedback_paper_recent_skill_lcb")
                    ),
                    "feedback_paper_stability_gap": cls._safe_float(
                        entry.get("feedback_paper_stability_gap")
                    ),
                    "feedback_paper_coverage_ratio": cls._safe_float(
                        entry.get("feedback_paper_coverage_ratio"),
                        1.0,
                    ),
                    "feedback_execution_conversion_efficiency": entry.get(
                        "feedback_execution_conversion_efficiency"
                    ),
                    "feedback_execution_conversion_efficiency_available": bool(
                        entry.get("feedback_execution_conversion_efficiency_available")
                    ),
                    "feedback_budget_action": entry.get("feedback_budget_action"),
                    "feedback_budget_action_applied": bool(
                        entry.get("feedback_budget_action_applied")
                    ),
                    "feedback_prediction_axis": entry.get("feedback_prediction_axis"),
                    "feedback_execution_axis": entry.get("feedback_execution_axis"),
                    "feedback_retain_family": bool(entry.get("feedback_retain_family")),
                    "feedback_reduce_budget": bool(entry.get("feedback_reduce_budget")),
                    "feedback_execution_optimization_queue": bool(
                        entry.get("feedback_execution_optimization_queue")
                    ),
                    "feedback_small_budget_observe": bool(
                        entry.get("feedback_small_budget_observe")
                    ),
                    "feedback_prioritize_scale": bool(entry.get("feedback_prioritize_scale")),
                    "feedback_cool_or_freeze": bool(entry.get("feedback_cool_or_freeze")),
                    "feedback_no_expansion": bool(entry.get("feedback_no_expansion")),
                    "feedback_effective_signal": str(
                        entry.get("feedback_effective_signal") or "legacy_paper_hit_ratio"
                    ),
                    "exploration_candidate": cls._is_exploration_candidate(
                        candidate,
                        dominant_families=dominant_families,
                        active_family_names=active_family_names,
                    ),
                }
                plans[int(entry["marker"])] = plan
                track_counts[track_name] += 1

        for entry in sorted_entries:
            marker = int(entry["marker"])
            if marker in plans:
                continue
            rank += 1
            plans[marker] = {
                "track": "deferred_budget_queue",
                "rank": rank,
                "priority_score": float(entry.get("priority_score") or 0.0),
                "family": entry.get("family"),
                "feedback_metrics": dict(entry.get("feedback_metrics") or {}),
                "feedback_scope": dict(entry.get("feedback_scope") or {}),
                "feedback_budget_multiplier": float(entry.get("feedback_budget_multiplier") or 1.0),
                "feedback_priority_adjustment": float(entry.get("feedback_priority_adjustment") or 0.0),
                "feedback_failure_penalty_adjustment": float(
                    entry.get("feedback_failure_penalty_adjustment") or 0.0
                ),
                "feedback_control_mode": str(entry.get("feedback_control_mode") or "normal"),
                "feedback_legacy_control_mode": str(
                    entry.get("feedback_legacy_control_mode") or "normal"
                ),
                "feedback_skill_control_mode": str(
                    entry.get("feedback_skill_control_mode") or "normal"
                ),
                "feedback_control_reasons": list(entry.get("feedback_control_reasons") or []),
                "feedback_legacy_control_reasons": list(
                    entry.get("feedback_legacy_control_reasons") or []
                ),
                "feedback_skill_control_reasons": list(
                    entry.get("feedback_skill_control_reasons") or []
                ),
                "feedback_cooldown_active": bool(entry.get("feedback_cooldown_active")),
                "feedback_suppressed": bool(entry.get("feedback_suppressed")),
                "feedback_family_freeze_active": bool(entry.get("feedback_family_freeze_active")),
                "feedback_target_pool_freeze_active": bool(entry.get("feedback_target_pool_freeze_active")),
                "feedback_generator_mode_freeze_active": bool(entry.get("feedback_generator_mode_freeze_active")),
                "feedback_skill_cooldown_active": bool(
                    entry.get("feedback_skill_cooldown_active")
                ),
                "feedback_skill_suppressed": bool(entry.get("feedback_skill_suppressed")),
                "feedback_skill_family_freeze_active": bool(
                    entry.get("feedback_skill_family_freeze_active")
                ),
                "feedback_skill_target_pool_freeze_active": bool(
                    entry.get("feedback_skill_target_pool_freeze_active")
                ),
                "feedback_skill_generator_mode_freeze_active": bool(
                    entry.get("feedback_skill_generator_mode_freeze_active")
                ),
                "feedback_legacy_budget_multiplier": cls._safe_float(
                    entry.get("feedback_legacy_budget_multiplier"),
                    1.0,
                ),
                "feedback_legacy_priority_adjustment": float(
                    entry.get("feedback_legacy_priority_adjustment") or 0.0
                ),
                "feedback_skill_budget_multiplier": cls._safe_float(
                    entry.get("feedback_skill_budget_multiplier"),
                    1.0,
                ),
                "feedback_skill_priority_adjustment": float(
                    entry.get("feedback_skill_priority_adjustment") or 0.0
                ),
                "feedback_skill_failure_penalty_adjustment": float(
                    entry.get("feedback_skill_failure_penalty_adjustment") or 0.0
                ),
                "feedback_paper_skill_lcb": cls._safe_float(
                    entry.get("feedback_paper_skill_lcb")
                ),
                "feedback_paper_recent_skill_lcb": cls._safe_float(
                    entry.get("feedback_paper_recent_skill_lcb")
                ),
                "feedback_paper_stability_gap": cls._safe_float(
                    entry.get("feedback_paper_stability_gap")
                ),
                "feedback_paper_coverage_ratio": cls._safe_float(
                    entry.get("feedback_paper_coverage_ratio"),
                    1.0,
                ),
                "feedback_execution_conversion_efficiency": entry.get(
                    "feedback_execution_conversion_efficiency"
                ),
                "feedback_execution_conversion_efficiency_available": bool(
                    entry.get("feedback_execution_conversion_efficiency_available")
                ),
                "feedback_budget_action": entry.get("feedback_budget_action"),
                "feedback_budget_action_applied": bool(
                    entry.get("feedback_budget_action_applied")
                ),
                "feedback_prediction_axis": entry.get("feedback_prediction_axis"),
                "feedback_execution_axis": entry.get("feedback_execution_axis"),
                "feedback_retain_family": bool(entry.get("feedback_retain_family")),
                "feedback_reduce_budget": bool(entry.get("feedback_reduce_budget")),
                "feedback_execution_optimization_queue": bool(
                    entry.get("feedback_execution_optimization_queue")
                ),
                "feedback_small_budget_observe": bool(
                    entry.get("feedback_small_budget_observe")
                ),
                "feedback_prioritize_scale": bool(entry.get("feedback_prioritize_scale")),
                "feedback_cool_or_freeze": bool(entry.get("feedback_cool_or_freeze")),
                "feedback_no_expansion": bool(entry.get("feedback_no_expansion")),
                "feedback_effective_signal": str(
                    entry.get("feedback_effective_signal") or "legacy_paper_hit_ratio"
                ),
                "exploration_candidate": cls._is_exploration_candidate(
                    dict(entry.get("candidate") or {}),
                    dominant_families=dominant_families,
                    active_family_names=active_family_names,
                ),
            }
            track_counts["deferred_budget_queue"] += 1

        return {
            "plans": plans,
            "summary": {
                "formal_slots": formal_slots,
                "observe_slots": observe_slots,
                "formal_family_cap": formal_family_cap,
                "observe_family_cap": observe_family_cap,
                "exploration_reserved_slots": exploration_reserved_slots,
                "exploration_selected_count": selected_exploration_count,
                "track_counts": track_counts,
                "formal_runtime_ready_candidate_count": len(formal_ready_entries),
                "formal_runtime_ready_selected_count": formal_runtime_ready_selected_count,
                "family_counts": dict(sorted(family_counts.items(), key=lambda item: (-item[1], item[0]))),
                "dominant_families": [family for family, _score in dominant_family_pairs[:3]],
                "feedback_available": bool(budget_feedback_root),
                "feedback_candidate_count": feedback_candidate_count,
                "feedback_family_count": len(feedback_family_names),
                "feedback_target_pool_scope_count": len(feedback_target_pool_ids),
                "feedback_generator_mode_scope_count": len(feedback_generator_modes),
                "feedback_budget_multiplier_avg": round(
                    sum(feedback_budget_multiplier_values) / len(feedback_budget_multiplier_values),
                    4,
                )
                if feedback_budget_multiplier_values
                else 0.0,
                "feedback_priority_adjustment_avg": round(
                    sum(feedback_priority_adjustment_values) / len(feedback_priority_adjustment_values),
                    4,
                )
                if feedback_priority_adjustment_values
                else 0.0,
                "feedback_skill_budget_multiplier_avg": round(
                    sum(feedback_skill_budget_multiplier_values)
                    / len(feedback_skill_budget_multiplier_values),
                    4,
                )
                if feedback_skill_budget_multiplier_values
                else 0.0,
                "feedback_skill_priority_adjustment_avg": round(
                    sum(feedback_skill_priority_adjustment_values)
                    / len(feedback_skill_priority_adjustment_values),
                    4,
                )
                if feedback_skill_priority_adjustment_values
                else 0.0,
                "feedback_paper_skill_lcb_avg": round(
                    sum(feedback_paper_skill_lcb_values) / len(feedback_paper_skill_lcb_values),
                    4,
                )
                if feedback_paper_skill_lcb_values
                else 0.0,
                "feedback_paper_recent_skill_lcb_avg": round(
                    sum(feedback_paper_recent_skill_lcb_values)
                    / len(feedback_paper_recent_skill_lcb_values),
                    4,
                )
                if feedback_paper_recent_skill_lcb_values
                else 0.0,
                "feedback_paper_stability_gap_avg": round(
                    sum(feedback_paper_stability_gap_values)
                    / len(feedback_paper_stability_gap_values),
                    4,
                )
                if feedback_paper_stability_gap_values
                else 0.0,
                "feedback_paper_coverage_ratio_avg": round(
                    sum(feedback_paper_coverage_ratio_values)
                    / len(feedback_paper_coverage_ratio_values),
                    4,
                )
                if feedback_paper_coverage_ratio_values
                else 0.0,
                "feedback_execution_conversion_efficiency_avg": round(
                    sum(feedback_execution_conversion_efficiency_values)
                    / len(feedback_execution_conversion_efficiency_values),
                    4,
                )
                if feedback_execution_conversion_efficiency_values
                else 0.0,
                "feedback_budget_action_counts": feedback_budget_action_counts,
                "feedback_dual_axis_action_count": feedback_dual_axis_action_count,
                "feedback_execution_optimization_queue_count": (
                    feedback_execution_optimization_queue_count
                ),
                "feedback_small_budget_observe_count": feedback_small_budget_observe_count,
                "feedback_prioritize_scale_count": feedback_prioritize_scale_count,
                "feedback_cool_or_freeze_count": feedback_cool_or_freeze_count,
                "feedback_budget_promoted_count": feedback_budget_promoted_count,
                "feedback_budget_constrained_count": feedback_budget_constrained_count,
                "feedback_skill_budget_promoted_count": feedback_skill_budget_promoted_count,
                "feedback_skill_budget_constrained_count": feedback_skill_budget_constrained_count,
                "feedback_controlled_count": feedback_controlled_count,
                "feedback_cooldown_count": feedback_cooldown_count,
                "feedback_suppressed_count": feedback_suppressed_count,
                "feedback_freeze_count": feedback_freeze_count,
                "feedback_skill_controlled_count": feedback_skill_controlled_count,
                "feedback_skill_cooldown_count": feedback_skill_cooldown_count,
                "feedback_skill_suppressed_count": feedback_skill_suppressed_count,
                "feedback_skill_freeze_count": feedback_skill_freeze_count,
                "feedback_target_pool_freeze_count": feedback_target_pool_freeze_count,
                "feedback_generator_mode_freeze_count": feedback_generator_mode_freeze_count,
                "feedback_skill_target_pool_freeze_count": feedback_skill_target_pool_freeze_count,
                "feedback_skill_generator_mode_freeze_count": (
                    feedback_skill_generator_mode_freeze_count
                ),
                "priority_score_avg": round(
                    sum(float(item.get("priority_score") or 0.0) for item in sorted_entries) / len(sorted_entries),
                    4,
                )
                if sorted_entries
                else 0.0,
            },
        }


__all__ = ["IncubationBudgeter"]
