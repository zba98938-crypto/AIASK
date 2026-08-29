
"""Candidate-generation context for strategy autonomy services."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from typing import Any, Optional
from uuid import uuid4

def _extract_event_context(*args, **kwargs):
    from strategy_factory.api import extract_event_context
    return extract_event_context(*args, **kwargs)


def _apply_resolved_candidate_envelope(candidate: Optional[dict[str, Any]]) -> dict[str, Any]:
    from strategy_factory.api.semantic_contract import apply_resolved_candidate_envelope

    return apply_resolved_candidate_envelope(candidate)

from .artifact_registry import register_experiment
from .strategy_generators import LLMProxyStrategyGenerator, RuleStrategyGenerator
from .strategy_optimizer import BanditParameterOptimizer
from .strategy_reviewer import MultiAgentStrategyReviewer
from .strategy_spec import StrategySpec, _safe_normalize_research_task

logger = logging.getLogger("akshare_mcp.services.strategy_autonomy_components")


_EXPERIMENT_SEMANTIC_FIELDS = (
    "candidate_contract_snapshot",
    "candidate_contract_hash",
    "execution_contract_hash",
    "candidate_identity_signature",
    "candidate_lineage_contract",
    "evidence_chain",
    "prediction_contract",
    "confidence_contract",
    "claim_to_trade_plan_map",
    "trade_plan_to_dsl_map",
    "dsl_support_audit",
    "execution_semantic_mode",
    "execution_semantic_gap",
    "execution_semantic_gap_reasons",
    "semantic_runtime_match",
    "runtime_family_data_source",
    "proxy_runtime_used",
    "diagnostic_only",
    "execution_readiness_tier",
    "semantic_contract_missing_fields",
)


def _env_bool(*names: str, default: bool) -> bool:
    for name in names:
        raw = os.getenv(str(name or "").strip())
        if raw is None:
            continue
        text = str(raw).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return bool(default)


def _normalized_research_task_payload(research_task: Optional[dict[str, Any]]) -> dict[str, Any]:
    normalized = _safe_normalize_research_task(research_task)
    return dict(normalized or research_task or {})


def _preferred_strategy_types(
    research_task: Optional[dict[str, Any]],
    *,
    limit: int = 8,
) -> list[str]:
    task = _normalized_research_task_payload(research_task)
    preferred: list[str] = []
    for item in list(task.get("preferred_strategy_types") or task.get("strategy_preferences") or []):
        token = str(item or "").strip()
        if token and token not in preferred:
            preferred.append(token)
        if len(preferred) >= max(1, int(limit or 8)):
            break
    return preferred


def _allowed_strategy_types(
    research_task: Optional[dict[str, Any]],
    *,
    limit: int = 12,
) -> list[str]:
    task = _normalized_research_task_payload(research_task)
    allowed: list[str] = []
    for item in list(task.get("allowed_strategy_types") or []):
        token = str(item or "").strip()
        if token and token not in allowed:
            allowed.append(token)
        if len(allowed) >= max(1, int(limit or 12)):
            break
    return allowed


def _llm_report_suppresses_candidate_fallback(llm_report: Optional[dict[str, Any]]) -> bool:
    payload = dict(llm_report or {})
    external_provider = dict(payload.get("external_provider") or {})
    local_generator = dict(payload.get("local_generator") or {})
    if bool(payload.get("post_pipeline_fallback_suppressed")):
        return True
    if bool(external_provider.get("monolithic_fallback_suppressed")):
        return True
    if bool(external_provider.get("local_fallback_suppressed")):
        return True
    if bool(local_generator.get("local_fallback_suppressed")):
        return True
    reason = str(payload.get("post_pipeline_suppression_reason") or "").strip().lower()
    return reason in {
        "provider_output_format_failure",
        "staged_pipeline_empty",
        "pipeline_timeout",
        "pipeline_exception",
    }


class CandidateGenerationService:
    def __init__(
        self,
        *,
        rule_generator: Optional[RuleStrategyGenerator] = None,
        llm_generator: Optional[LLMProxyStrategyGenerator] = None,
        optimizer: Optional[BanditParameterOptimizer] = None,
    ):
        self.rule_generator = rule_generator or RuleStrategyGenerator()
        self.llm_generator = llm_generator or LLMProxyStrategyGenerator()
        self.optimizer = optimizer or BanditParameterOptimizer()

    @staticmethod
    def _task_source(research_task: Optional[dict[str, Any]]) -> str:
        return str((research_task or {}).get("task_source") or "").strip().lower()

    @staticmethod
    def _has_generation_task_constraints(research_task: Optional[dict[str, Any]]) -> bool:
        task = dict(research_task or {})
        for key in (
            "task_id",
            "task_key",
            "task_source",
            "opportunity_type",
            "event_id",
            "theme_code",
            "target_symbols",
            "stock_pool",
            "strategy_preferences",
            "preferred_strategy_types",
            "allowed_strategy_types",
            "candidate_family",
            "factor_name",
        ):
            if task.get(key) not in (None, "", [], {}):
                return True
        return False

    @classmethod
    def _bulk_llm_enabled(cls) -> bool:
        return _env_bool(
            "STRATEGY_FACTORY_BULK_LLM_ENABLED",
            "STRATEGY_FACTORY_BULK_STOCK_MATRIX_LLM_ENABLED",
            default=False,
        )

    @classmethod
    def _bulk_optimizer_enabled(cls) -> bool:
        return _env_bool(
            "STRATEGY_FACTORY_BULK_OPTIMIZER_ENABLED",
            "STRATEGY_FACTORY_BULK_STOCK_MATRIX_OPTIMIZER_ENABLED",
            default=False,
        )

    @classmethod
    def _l2_hypothesis_enabled(cls) -> bool:
        return _env_bool(
            "STRATEGY_FACTORY_L2_HYPOTHESIS_ENABLED",
            "STRATEGY_FACTORY_L2_ENABLED",
            default=True,
        )

    @classmethod
    def _l2_hypothesis_replay_enabled(cls) -> bool:
        return _env_bool(
            "STRATEGY_FACTORY_L2_HYPOTHESIS_REPLAY_ENABLED",
            "STRATEGY_FACTORY_L2_REPLAY_ENABLED",
            default=True,
        )

    @classmethod
    def _skipped_llm_report(
        cls,
        *,
        task_source: str,
        requested_limit: int,
        reason: str,
        optimizer_enabled: bool,
    ) -> dict[str, Any]:
        mode = "rule_only" if not optimizer_enabled else "rule_plus_optimizer"
        return {
            "mode": mode,
            "external_provider": {
                "status": "skipped",
                "skip_reason": reason,
                "task_source": task_source,
                "requested_limit": int(requested_limit or 0),
                "selected_count": 0,
                "elapsed_seconds": 0.0,
                "requests": [],
            },
            "optimizer": {
                "status": "enabled" if optimizer_enabled else "skipped",
                "skip_reason": None if optimizer_enabled else reason,
            },
        }

    @staticmethod
    def _bulk_primary_family(research_task: Optional[dict[str, Any]]) -> Optional[str]:
        task = _normalized_research_task_payload(research_task)
        preferred = _preferred_strategy_types(task)
        if preferred:
            return preferred[0]
        family = str(task.get("candidate_family") or "").strip()
        return family or None

    @classmethod
    def _bulk_rule_variant_params(
        cls,
        strategy_type: str,
        *,
        base_params: dict[str, Any],
        variant_index: int,
    ) -> dict[str, Any]:
        variants: dict[str, list[dict[str, Any]]] = {
            "momentum": [
                {"lookback": 8, "threshold": 0.008},
                {"lookback": 15, "threshold": 0.015},
                {"lookback": 22, "threshold": 0.022},
            ],
            "ma_cross": [
                {"short_period": 5, "long_period": 20},
                {"short_period": 8, "long_period": 30},
                {"short_period": 10, "long_period": 40},
            ],
            "rsi": [
                {"rsi_period": 6, "oversold": 24, "overbought": 68},
                {"rsi_period": 10, "oversold": 28, "overbought": 72},
                {"rsi_period": 14, "oversold": 32, "overbought": 76},
            ],
            "value_factor": [
                {"lookback": 40, "buy_quantile": 0.75, "sell_quantile": 0.25},
                {"lookback": 60, "buy_quantile": 0.80, "sell_quantile": 0.20},
                {"lookback": 90, "buy_quantile": 0.85, "sell_quantile": 0.15},
            ],
            "quality_factor": [
                {"lookback": 30, "buy_quantile": 0.70, "sell_quantile": 0.30},
                {"lookback": 50, "buy_quantile": 0.75, "sell_quantile": 0.25},
                {"lookback": 70, "buy_quantile": 0.82, "sell_quantile": 0.18},
            ],
            "growth_factor": [
                {"lookback": 24, "buy_quantile": 0.72, "sell_quantile": 0.28},
                {"lookback": 40, "buy_quantile": 0.78, "sell_quantile": 0.22},
                {"lookback": 60, "buy_quantile": 0.84, "sell_quantile": 0.16},
            ],
            "multi_factor": [
                {"lookback": 24, "factor_weights": {"value": 0.45, "quality": 0.35, "momentum": 0.20}},
                {"lookback": 36, "factor_weights": {"value": 0.35, "quality": 0.35, "momentum": 0.30}},
                {"lookback": 48, "factor_weights": {"value": 0.25, "quality": 0.35, "momentum": 0.40}},
            ],
            "macro_timing": [
                {"risk_on_threshold": 0.50, "rebalance_days": 5},
                {"risk_on_threshold": 0.55, "rebalance_days": 10},
                {"risk_on_threshold": 0.60, "rebalance_days": 15},
            ],
            "volatility_breakout": [
                {"lookback": 10, "threshold": 0.012},
                {"lookback": 16, "threshold": 0.018},
                {"lookback": 24, "threshold": 0.024},
            ],
            "gap_fill": [
                {"gap_threshold": 0.015, "rsi_period": 4, "oversold": 22, "overbought": 56},
                {"gap_threshold": 0.020, "rsi_period": 6, "oversold": 24, "overbought": 60},
                {"gap_threshold": 0.030, "rsi_period": 8, "oversold": 28, "overbought": 64},
            ],
            "mean_reversion_short": [
                {"rsi_period": 4, "oversold": 22, "overbought": 58},
                {"rsi_period": 6, "oversold": 26, "overbought": 62},
                {"rsi_period": 8, "oversold": 30, "overbought": 66},
            ],
            "sector_rotation": [
                {"lookback": 15, "factor_weights": {"momentum": 0.50, "quality": 0.30, "value": 0.20}},
                {"lookback": 20, "factor_weights": {"momentum": 0.45, "quality": 0.30, "value": 0.25}},
                {"lookback": 30, "factor_weights": {"momentum": 0.35, "quality": 0.35, "value": 0.30}},
            ],
            "north_capital_track": [
                {"lookback": 10, "threshold": 0.010},
                {"lookback": 15, "threshold": 0.015},
                {"lookback": 20, "threshold": 0.020},
            ],
            "margin_divergence": [
                {"fear_threshold": 35, "greed_threshold": 55, "lookback": 10},
                {"fear_threshold": 40, "greed_threshold": 60, "lookback": 15},
                {"fear_threshold": 45, "greed_threshold": 65, "lookback": 20},
            ],
        }
        options = list(variants.get(strategy_type) or [])
        if not options:
            return dict(base_params or {})
        selected = dict(options[variant_index % len(options)])
        return {**dict(base_params or {}), **selected}

    @classmethod
    def _expand_bulk_rule_specs(
        cls,
        rule_specs: list[StrategySpec],
        *,
        research_task: Optional[dict[str, Any]],
        limit: int,
    ) -> list[StrategySpec]:
        requested_limit = max(1, int(limit or 1))
        if not rule_specs:
            return []
        primary_family = cls._bulk_primary_family(research_task)
        base_spec = None
        if primary_family:
            for item in rule_specs:
                if str(item.strategy_type or "").strip().lower() == primary_family:
                    base_spec = item
                    break
        if base_spec is None:
            base_spec = rule_specs[0]

        expanded: list[StrategySpec] = []
        seen: set[str] = set()
        for variant_index in range(requested_limit):
            params = cls._bulk_rule_variant_params(
                str(base_spec.strategy_type or "").strip().lower(),
                base_params=dict(base_spec.params or {}),
                variant_index=variant_index,
            )
            key = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
            if key in seen:
                continue
            seen.add(key)
            metadata = {
                **dict(base_spec.metadata or {}),
                "bulk_rule_variant": {
                    "variant_index": variant_index + 1,
                    "variant_count": requested_limit,
                    "task_source": cls._task_source(research_task),
                },
            }
            expanded.append(
                StrategySpec(
                    strategy_type=base_spec.strategy_type,
                    params=params,
                    name=f"{base_spec.name} V{variant_index + 1}",
                    description=base_spec.description,
                    tags=list(base_spec.tags or []),
                    metadata=metadata,
                )
            )
        return expanded or [base_spec]

    async def select_parents(self, db, parent_strategy_id: Optional[str] = None) -> list[dict]:
        if parent_strategy_id:
            try:
                strategy = await db.get_strategy(parent_strategy_id)
            except Exception as exc:
                logger.warning(
                    "CandidateGenerationService: failed to load parent strategy %s, continuing without parents: %s",
                    parent_strategy_id,
                    exc,
                )
                return []
            return [strategy] if strategy else []
        parents = []
        for status in ("incubating", "listed"):
            try:
                parents.extend(await db.list_strategies(status, limit=5))
            except Exception as exc:
                logger.warning(
                    "CandidateGenerationService: failed to load %s parents, continuing with partial context: %s",
                    status,
                    exc,
                )
        return parents[:3]

    async def build_shared_generation_context(
        self,
        db,
        *,
        snapshot: dict,
        parent_strategy_id: Optional[str] = None,
    ) -> dict[str, Any]:
        parents = await self.select_parents(db, parent_strategy_id=parent_strategy_id)
        history_summary = await self.llm_generator._recent_experiments(db, parent_strategies=parents)
        research_context = await self.llm_generator.build_shared_research_context(
            db,
            snapshot=snapshot,
            parent_strategies=parents,
            history_summary=history_summary,
        )
        return {
            "parent_strategies": parents,
            "history_summary": history_summary,
            "research_context": research_context,
        }

    async def generate(
        self,
        db,
        *,
        snapshot: dict,
        limit: int,
        research_task: dict[str, Any],
        parent_strategy_id: Optional[str] = None,
    ) -> dict:
        shared_generation_context = dict(snapshot.get("_shared_generation_context") or {})
        task_source = self._task_source(research_task)
        bulk_stock_matrix_task = task_source == "bulk_stock_matrix"
        bulk_llm_enabled = self._bulk_llm_enabled() if bulk_stock_matrix_task else True
        bulk_optimizer_enabled = self._bulk_optimizer_enabled() if bulk_stock_matrix_task else True
        normalized_research_task = _normalized_research_task_payload(research_task)
        needs_parent_context = bool(parent_strategy_id or bulk_llm_enabled or bulk_optimizer_enabled or not bulk_stock_matrix_task)
        parents = (
            [dict(item or {}) for item in list(shared_generation_context.get("parent_strategies") or [])]
            if needs_parent_context
            else []
        )
        if parent_strategy_id:
            parents = await self.select_parents(db, parent_strategy_id=parent_strategy_id)
        elif needs_parent_context and not parents:
            parents = await self.select_parents(db, parent_strategy_id=parent_strategy_id)
        task_preferences = _preferred_strategy_types(normalized_research_task)
        effective_limit = max(1, min(int(limit or 3), 10))
        if bulk_stock_matrix_task:
            primary_family = self._bulk_primary_family(research_task)
            base_rule_specs = self.rule_generator.generate(
                snapshot,
                limit=1,
                preferred_types=[primary_family] if primary_family else (task_preferences or None),
            )
            if not base_rule_specs:
                base_rule_specs = self.rule_generator.generate(
                    snapshot,
                    limit=1,
                    preferred_types=task_preferences or None,
                )
            bulk_rule_limit = (
                effective_limit
                if not bulk_llm_enabled and not bulk_optimizer_enabled
                else 1
            )
            rule_specs = self._expand_bulk_rule_specs(
                list(base_rule_specs or []),
                research_task=research_task,
                limit=bulk_rule_limit,
            )
        else:
            has_generation_task_constraints = self._has_generation_task_constraints(research_task)
            rule_limit = (
                max(0, limit // 3)
                if has_generation_task_constraints
                else max(1, limit // 2 or 1)
            )
            rule_specs = (
                self.rule_generator.generate(
                    snapshot,
                    limit=rule_limit,
                    preferred_types=task_preferences or None,
                )
                if rule_limit > 0
                else []
            )
        llm_specs: list[StrategySpec] = []
        replay_specs: list[StrategySpec] = []
        llm_disabled_by_scheduler = bool(research_task.get("disable_external_llm"))
        llm_skip_reason = str(research_task.get("external_llm_skip_reason") or "provider_health_blocked").strip()
        optimizer_disabled_by_scheduler = bool(research_task.get("disable_optimizer"))
        optimizer_skip_reason = str(research_task.get("optimizer_skip_reason") or "generator_mode_cooldown").strip()
        if llm_disabled_by_scheduler:
            llm_report = self._skipped_llm_report(
                task_source=task_source,
                requested_limit=effective_limit,
                reason=llm_skip_reason or "provider_health_blocked",
                optimizer_enabled=bulk_optimizer_enabled if bulk_stock_matrix_task else True,
            )
        elif bulk_stock_matrix_task and not bulk_llm_enabled:
            llm_report = self._skipped_llm_report(
                task_source=task_source,
                requested_limit=effective_limit,
                reason="bulk_stock_matrix_llm_disabled",
                optimizer_enabled=bulk_optimizer_enabled,
            )
        else:
            llm_specs = await self.llm_generator.generate(
                db,
                limit=max(1, limit),
                snapshot=snapshot,
                parent_strategies=parents,
                research_task=research_task,
            )
            llm_report = self.llm_generator.get_last_report() if hasattr(self.llm_generator, "get_last_report") else {}
            if bulk_stock_matrix_task and not bulk_optimizer_enabled:
                llm_report = {
                    **dict(llm_report or {}),
                    "optimizer": {
                        "status": "skipped",
                        "skip_reason": "bulk_stock_matrix_optimizer_disabled",
                    },
                }
            elif optimizer_disabled_by_scheduler:
                llm_report = {
                    **dict(llm_report or {}),
                    "optimizer": {
                        "status": "skipped",
                        "skip_reason": optimizer_skip_reason or "generator_mode_cooldown",
                    },
                }
        suppress_candidate_fallback = _llm_report_suppresses_candidate_fallback(llm_report)
        replay_report: dict[str, Any] = {
            "status": "disabled" if not self._l2_hypothesis_replay_enabled() else "not_needed",
            "selected_count": 0,
        }
        replay_trigger_reason: Optional[str] = None
        if suppress_candidate_fallback:
            replay_report = {
                "status": "skipped_after_llm_failure",
                "skip_reason": "candidate_fallback_suppressed",
                "selected_count": 0,
            }
        elif self._l2_hypothesis_enabled() and self._l2_hypothesis_replay_enabled() and hasattr(
            self.llm_generator,
            "replay_persisted_specs",
        ):
            provider_status = str(
                dict(dict(llm_report or {}).get("external_provider") or {}).get("status") or ""
            ).strip().lower()
            if llm_disabled_by_scheduler:
                replay_trigger_reason = llm_skip_reason or "provider_health_blocked"
            elif bulk_stock_matrix_task and not bulk_llm_enabled:
                replay_trigger_reason = "bulk_stock_matrix_llm_disabled"
            elif not llm_specs:
                replay_trigger_reason = provider_status or "empty_external_selection"
        if replay_trigger_reason:
            try:
                replay_result = await self.llm_generator.replay_persisted_specs(
                    db,
                    limit=max(1, effective_limit),
                    snapshot=snapshot,
                    parent_strategies=parents,
                    research_task=research_task,
                    trigger_reason=replay_trigger_reason,
                )
                replay_specs = list(dict(replay_result or {}).get("specs") or [])
                replay_report = {
                    "status": "succeeded" if replay_specs else "empty",
                    "trigger_reason": replay_trigger_reason,
                    **dict(dict(replay_result or {}).get("report") or {}),
                }
            except Exception as exc:
                logger.warning(
                    "CandidateGenerationService: replay_persisted_specs failed, continuing without replay: %s",
                    exc,
                )
                replay_report = {
                    "status": "failed",
                    "trigger_reason": replay_trigger_reason,
                    "error": str(exc),
                    "selected_count": 0,
                }
        llm_report = {
            **dict(llm_report or {}),
            "replay_provider": dict(replay_report or {}),
            "candidate_fallback_suppressed": bool(suppress_candidate_fallback),
        }
        evolved_specs: list[StrategySpec] = []
        if optimizer_disabled_by_scheduler:
            llm_report = {
                **dict(llm_report or {}),
                "optimizer": {
                    "status": "skipped",
                    "skip_reason": optimizer_skip_reason or "generator_mode_cooldown",
                },
            }
        if optimizer_disabled_by_scheduler:
            logger.info(
                "CandidateGenerationService: scheduler disabled optimizer for task %s (%s)",
                research_task.get("task_id") or research_task.get("task_key") or "unknown",
                optimizer_skip_reason or "generator_mode_cooldown",
            )
        elif bulk_stock_matrix_task and not bulk_optimizer_enabled:
            logger.info(
                "CandidateGenerationService: bulk_stock_matrix task %s using rule-first generation without optimizer",
                research_task.get("task_id") or research_task.get("task_key") or "unknown",
            )
        elif suppress_candidate_fallback:
            llm_report = {
                **dict(llm_report or {}),
                "optimizer": {
                    "status": "skipped_after_llm_failure",
                    "skip_reason": "candidate_fallback_suppressed",
                },
            }
        else:
            for parent in parents[:2]:
                evolved_specs.extend(await self.optimizer.evolve(db, parent, limit=2))

        merged_specs: list[StrategySpec] = []
        seen = set()
        if suppress_candidate_fallback:
            rule_specs = []
            replay_specs = []
            evolved_specs = []
            prioritized_specs = list(llm_specs)
        else:
            prioritized_specs = (
                [*llm_specs, *replay_specs, *evolved_specs, *rule_specs]
                if (llm_specs or replay_specs or evolved_specs)
                else [*rule_specs, *llm_specs, *replay_specs, *evolved_specs]
            )
        for spec in prioritized_specs:
            if research_task and not dict(spec.metadata or {}).get("research_task"):
                spec.metadata = {**dict(spec.metadata or {}), "research_task": research_task}
            key = (spec.strategy_type, json.dumps(spec.params or {}, sort_keys=True, ensure_ascii=False, default=str))
            if key in seen:
                continue
            seen.add(key)
            merged_specs.append(spec)
            if len(merged_specs) >= effective_limit:
                break

        return {
            "parents": parents,
            "rule_specs": rule_specs,
            "llm_specs": llm_specs,
            "replay_specs": replay_specs,
            "evolved_specs": evolved_specs,
            "merged_specs": merged_specs,
            "llm_report": llm_report,
        }
