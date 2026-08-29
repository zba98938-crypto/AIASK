"""Review and experiment-recording services for strategy autonomy."""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Optional
from uuid import uuid4

from ._strategy_autonomy_context import (
    _EXPERIMENT_SEMANTIC_FIELDS,
    _allowed_strategy_types,
    _apply_resolved_candidate_envelope,
    _extract_event_context,
    _normalized_research_task_payload,
    _preferred_strategy_types,
    logger,
)
from .artifact_registry import register_experiment
from .strategy_reviewer import MultiAgentStrategyReviewer
from .strategy_spec import StrategySpec


class CommitteeReviewService:
    def __init__(self, *, reviewer: Optional[MultiAgentStrategyReviewer] = None):
        self.reviewer = reviewer or MultiAgentStrategyReviewer()

    @staticmethod
    def attach_lineage(
        spec: StrategySpec,
        *,
        parent_strategy_id: Optional[str],
        task_run_id: Optional[int],
        review_rank: Optional[int] = None,
        is_champion: bool = False,
    ) -> StrategySpec:
        metadata = dict(spec.metadata or {})
        if parent_strategy_id and not metadata.get("parent_strategy_id"):
            metadata["parent_strategy_id"] = parent_strategy_id
        if task_run_id is not None:
            metadata["task_run_id"] = task_run_id
        committee_review = dict(metadata.get("committee_review") or {})
        if review_rank is not None:
            committee_review["rank"] = int(review_rank)
        if is_champion:
            committee_review["is_champion"] = True
        elif review_rank is not None:
            committee_review.setdefault("is_champion", False)
        if committee_review:
            metadata["committee_review"] = committee_review
        spec.metadata = metadata
        return spec

    @staticmethod
    def review_score(spec: StrategySpec) -> float:
        review = dict((spec.metadata or {}).get("committee_review") or {})
        value = review.get("final_score")
        try:
            return float(value)
        except Exception:
            return 0.0

    def review_candidates(
        self,
        specs: list[StrategySpec],
        *,
        snapshot: dict,
        parent_strategy_id: Optional[str],
        task_run_id: Optional[int],
    ) -> dict:
        reviewed_specs: list[StrategySpec] = []
        committee_reviews: list[dict[str, Any]] = []
        rejected_count = 0
        for spec in list(specs or []):
            reviewed_spec, review = self.reviewer.review(spec, snapshot)
            committee_reviews.append({
                "strategy_type": spec.strategy_type,
                "name": spec.name,
                **review,
            })
            if reviewed_spec is None:
                rejected_count += 1
                continue
            reviewed_specs.append(
                self.attach_lineage(
                    reviewed_spec,
                    parent_strategy_id=parent_strategy_id,
                    task_run_id=task_run_id,
                )
            )

        reviewed_specs.sort(key=self.review_score, reverse=True)
        for rank, spec in enumerate(reviewed_specs, 1):
            self.attach_lineage(
                spec,
                parent_strategy_id=parent_strategy_id,
                task_run_id=task_run_id,
                review_rank=rank,
                is_champion=rank == 1,
            )
        return {
            "reviewed_specs": reviewed_specs,
            "committee_reviews": committee_reviews,
            "rejected_count": rejected_count,
        }


class ExperimentRecorder:
    _PREVIEW_LIMIT = 3

    @staticmethod
    def _compact_dict(payload: Optional[dict[str, Any]], *, keys: tuple[str, ...]) -> dict[str, Any]:
        source = dict(payload or {})
        return {
            key: source.get(key)
            for key in keys
            if source.get(key) not in (None, "", [], {})
        }

    @classmethod
    def _summarize_research_task(cls, research_task: Optional[dict[str, Any]]) -> dict[str, Any]:
        task = _normalized_research_task_payload(research_task)
        summary = cls._compact_dict(
            task,
            keys=(
                "task_id",
                "task_key",
                "task_source",
                "opportunity_type",
                "theme_code",
                "event_id",
                "candidate_family",
                "factor_name",
                "generation_limit",
                "source_candidate_artifact_id",
                "evidence_count",
                "preference_strength",
                "validation_focus",
            ),
        )
        target_symbols = list(task.get("target_symbols") or [])
        if target_symbols:
            summary["target_symbols"] = target_symbols[:12]
        preferred_strategy_types = _preferred_strategy_types(task, limit=6)
        if preferred_strategy_types:
            summary["preferred_strategy_types"] = preferred_strategy_types
            summary["strategy_preferences"] = list(preferred_strategy_types)
        allowed_strategy_types = _allowed_strategy_types(task, limit=6)
        if allowed_strategy_types:
            summary["allowed_strategy_types"] = allowed_strategy_types
        return summary

    @classmethod
    def _summarize_event_context(cls, event_context: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(event_context or {})
        summary = cls._compact_dict(
            payload,
            keys=(
                "task_source",
                "event_id",
                "theme_code",
                "event_type",
                "opportunity_type",
                "candidate_family",
                "factor_name",
            ),
        )
        target_symbols = list(payload.get("target_symbols") or payload.get("symbols") or [])
        if target_symbols:
            summary["target_symbols"] = target_symbols[:12]
        return summary

    @classmethod
    def _summarize_stock_pool(cls, stock_pool: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(stock_pool or {})
        summary = cls._compact_dict(
            payload,
            keys=("selection_mode", "universe_scope", "source", "reason"),
        )
        symbols = list(payload.get("symbols") or payload.get("target_symbols") or [])
        if symbols:
            summary["symbol_count"] = len(symbols)
            summary["symbols"] = symbols[:12]
        return summary

    @classmethod
    def _summarize_selection_logic(cls, selection_logic: Optional[list[Any]]) -> dict[str, Any]:
        items = list(selection_logic or [])
        preview: list[Any] = []
        for item in items[: cls._PREVIEW_LIMIT]:
            if isinstance(item, dict):
                preview.append({key: item.get(key) for key in list(item.keys())[:6]})
            else:
                preview.append(str(item))
        return {
            "count": len(items),
            "preview": preview,
        }

    @classmethod
    def _summarize_parameters(cls, spec: StrategySpec) -> dict[str, Any]:
        params = dict(spec.params or {})
        summary: dict[str, Any] = {}
        for key, value in params.items():
            if key in {"research_task", "event_context"}:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                summary[key] = value
            elif isinstance(value, list) and len(value) <= 12 and all(
                isinstance(item, (str, int, float, bool)) or item is None for item in value
            ):
                summary[key] = value
            elif key == "dsl" and isinstance(value, dict):
                dsl_summary = cls._compact_dict(
                    value,
                    keys=("version", "timeframe", "entry", "exit", "risk_rules"),
                )
                metadata = dict(value.get("metadata") or {})
                if metadata:
                    dsl_summary["metadata"] = {
                        meta_key: metadata.get(meta_key)
                        for meta_key in (
                            "target_symbols",
                            "stock_pool",
                            "portfolio_spec",
                            "execution_assumptions",
                            "validation_profile",
                            "targeting_policy",
                            "constraint_check",
                        )
                        if metadata.get(meta_key) not in (None, "", [], {})
                    }
                if dsl_summary:
                    summary[key] = dsl_summary
            elif key == "stock_pool" and isinstance(value, dict):
                summary[key] = cls._summarize_stock_pool(value)
        dsl_activity = dict(spec.metadata.get("dsl_activity") or {})
        if dsl_activity:
            summary["dsl_activity"] = dsl_activity
        elif params.get("dsl") not in (None, {}, []):
            summary["dsl_present"] = True
        target_symbols = list(spec.metadata.get("target_symbols") or [])
        if target_symbols:
            summary["target_symbols"] = target_symbols[:12]
        return summary

    @staticmethod
    def _candidate_field(candidate: Optional[dict[str, Any]], field_name: str) -> Any:
        payload = dict(candidate or {})
        params = dict(payload.get("params") or {})
        if payload.get(field_name) not in (None, "", [], {}):
            return payload.get(field_name)
        if params.get(field_name) not in (None, "", [], {}):
            return params.get(field_name)
        return None

    @classmethod
    def _apply_candidate_contract_fields(
        cls,
        target: dict[str, Any],
        candidate: Optional[dict[str, Any]],
    ) -> None:
        payload = dict(candidate or {})
        for field_name in _EXPERIMENT_SEMANTIC_FIELDS:
            value = cls._candidate_field(payload, field_name)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, dict):
                target[field_name] = dict(value)
            elif isinstance(value, list):
                target[field_name] = list(value)
            else:
                target[field_name] = value

    @classmethod
    def _summarize_llm_research_context(cls, context: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(context or {})
        summary = cls._compact_dict(
            payload,
            keys=(
                "snapshot_date",
                "fear_greed_index",
                "task_source",
                "candidate_family",
                "factor_name",
            ),
        )
        for key in ("target_symbols", "candidate_universe", "preferred_strategy_types", "top_factor_names"):
            items = list(payload.get(key) or [])
            if items:
                summary[key] = items[:12]
                summary[f"{key}_count"] = len(items)
        for key in ("symbol_frames", "symbol_details", "parents", "history_summary"):
            value = payload.get(key)
            if isinstance(value, dict):
                summary[f"{key}_count"] = len(value)
            elif isinstance(value, list):
                summary[f"{key}_count"] = len(value)
        return summary

    @classmethod
    def _summarize_llm_response(cls, response: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(response or {})
        request_metrics = dict(payload.get("request_metrics") or {})
        summary = cls._compact_dict(
            payload,
            keys=("provider", "model"),
        )
        candidates = list(payload.get("candidates") or [])
        if candidates:
            summary["candidate_count"] = len(candidates)
            summary["candidate_names"] = [
                str((item or {}).get("name") or "")
                for item in candidates[: cls._PREVIEW_LIMIT]
            ]
        analysis = dict(payload.get("analysis") or {})
        if analysis:
            summary["analysis"] = cls._compact_dict(
                analysis,
                keys=("style_bias", "market_regime", "theme", "direction", "confidence"),
            )
        if request_metrics:
            summary["request_metrics"] = cls._compact_dict(
                request_metrics,
                keys=(
                    "status",
                    "requested_limit",
                    "attempt_count",
                    "prompt_profile",
                    "prompt_chars",
                    "response_chars",
                    "selected_candidate_count",
                    "elapsed_seconds",
                    "last_error_type",
                    "last_error",
                ),
            )
        return summary

    @classmethod
    def _summarize_hypothesis_artifact(cls, artifact: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(artifact or {})
        if not payload:
            return {}
        summary = cls._compact_dict(
            payload,
            keys=(
                "artifact_id",
                "version",
                "provider",
                "model",
                "family_hint",
                "holding_rationale",
                "alpha_half_life",
                "position_model",
                "validation_focus",
            ),
        )
        if payload.get("alpha_hypothesis") not in (None, "", [], {}):
            summary["alpha_hypothesis"] = payload.get("alpha_hypothesis")
        if payload.get("failure_mode") not in (None, "", [], {}):
            summary["failure_mode"] = payload.get("failure_mode")
        if payload.get("target_universe_hypothesis") not in (None, "", [], {}):
            summary["target_universe_hypothesis"] = payload.get("target_universe_hypothesis")
        if payload.get("capacity_assumption") not in (None, "", [], {}):
            summary["capacity_assumption"] = payload.get("capacity_assumption")
        if payload.get("cost_sensitivity_grid") not in (None, "", [], {}):
            summary["cost_sensitivity_grid"] = payload.get("cost_sensitivity_grid")
        return summary

    @classmethod
    def _build_replay_strategy_contract(
        cls,
        *,
        spec: StrategySpec,
        research_task: Optional[dict[str, Any]],
        event_context: Optional[dict[str, Any]],
        candidate: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        metadata = dict(spec.metadata or {})
        candidate_payload = dict(candidate or {})
        contract = {
            "strategy_type": spec.strategy_type,
            "name": spec.name,
            "description": spec.description,
            "tags": list(spec.tags or []),
            "params": dict(candidate_payload.get("params") or spec.params or {}),
            "target_symbols": list(candidate_payload.get("target_symbols") or metadata.get("target_symbols") or [])[:12],
            "stock_pool": dict(candidate_payload.get("stock_pool") or metadata.get("stock_pool") or {}),
            "selection_logic": list(candidate_payload.get("selection_logic") or metadata.get("selection_logic") or []),
            "research_task": dict(candidate_payload.get("research_task") or research_task or {}),
            "event_context": dict(candidate_payload.get("event_context") or event_context or {}),
            "hypothesis_artifact": dict(candidate_payload.get("hypothesis_artifact") or metadata.get("hypothesis_artifact") or {}),
            "hypothesis_lowering_audit": dict(candidate_payload.get("hypothesis_lowering_audit") or metadata.get("hypothesis_lowering_audit") or {}),
            "holding_horizon": dict(candidate_payload.get("holding_horizon") or metadata.get("holding_horizon") or {}),
            "trade_plan": dict(candidate_payload.get("trade_plan") or metadata.get("trade_plan") or {}),
            "risk_rules": dict(candidate_payload.get("risk_rules") or metadata.get("risk_rules") or {}),
            "position_sizing": dict(candidate_payload.get("position_sizing") or metadata.get("position_sizing") or {}),
            "execution_notes": candidate_payload.get("execution_notes") or metadata.get("execution_notes"),
            "rebalance_rule": dict(candidate_payload.get("rebalance_rule") or metadata.get("rebalance_rule") or {}),
            "portfolio_spec": dict(candidate_payload.get("portfolio_spec") or metadata.get("portfolio_spec") or {}),
            "execution_assumptions": dict(candidate_payload.get("execution_assumptions") or metadata.get("execution_assumptions") or {}),
            "validation_profile": dict(candidate_payload.get("validation_profile") or metadata.get("validation_profile") or {}),
            "targeting_policy": dict(candidate_payload.get("targeting_policy") or metadata.get("targeting_policy") or {}),
            "constraint_check": dict(candidate_payload.get("constraint_check") or metadata.get("constraint_check") or {}),
        }
        cls._apply_candidate_contract_fields(contract, candidate_payload)
        return contract

    @classmethod
    def _build_persisted_experiment_payload(
        cls,
        *,
        spec: StrategySpec,
        payload: dict[str, Any],
        candidate: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        candidate_payload = dict(candidate or {})
        research_task = dict(
            candidate_payload.get("research_task")
            or (payload.get("strategy_spec") or {}).get("research_task")
            or {}
        )
        event_context = dict(
            candidate_payload.get("event_context")
            or (payload.get("strategy_spec") or {}).get("event_context")
            or {}
        )
        replay_contract = cls._build_replay_strategy_contract(
            spec=spec,
            research_task=research_task,
            event_context=event_context,
            candidate=candidate_payload,
        )
        summarized_parameters = cls._summarize_parameters(spec)
        strategy_spec = {
            "strategy_type": spec.strategy_type,
            "name": spec.name,
            "description": spec.description,
            "tags": list(spec.tags or []),
            "params": summarized_parameters,
            "target_symbols": list(candidate_payload.get("target_symbols") or spec.metadata.get("target_symbols") or [])[:12],
            "stock_pool": cls._summarize_stock_pool(candidate_payload.get("stock_pool") or spec.metadata.get("stock_pool") or {}),
            "selection_logic": cls._summarize_selection_logic(candidate_payload.get("selection_logic") or spec.metadata.get("selection_logic") or []),
            "research_task": cls._summarize_research_task(research_task),
            "event_context": cls._summarize_event_context(event_context),
            "hypothesis_artifact": cls._summarize_hypothesis_artifact(candidate_payload.get("hypothesis_artifact") or spec.metadata.get("hypothesis_artifact") or {}),
            "holding_horizon": dict(candidate_payload.get("holding_horizon") or spec.metadata.get("holding_horizon") or {}),
            "trade_plan": dict(candidate_payload.get("trade_plan") or spec.metadata.get("trade_plan") or {}),
            "risk_rules": dict(candidate_payload.get("risk_rules") or spec.metadata.get("risk_rules") or {}),
            "position_sizing": dict(candidate_payload.get("position_sizing") or spec.metadata.get("position_sizing") or {}),
            "execution_notes": candidate_payload.get("execution_notes") or spec.metadata.get("execution_notes"),
            "rebalance_rule": dict(candidate_payload.get("rebalance_rule") or spec.metadata.get("rebalance_rule") or {}),
            "portfolio_spec": dict(candidate_payload.get("portfolio_spec") or spec.metadata.get("portfolio_spec") or {}),
            "execution_assumptions": dict(candidate_payload.get("execution_assumptions") or spec.metadata.get("execution_assumptions") or {}),
            "validation_profile": dict(candidate_payload.get("validation_profile") or spec.metadata.get("validation_profile") or {}),
            "replay_contract": replay_contract,
        }
        cls._apply_candidate_contract_fields(strategy_spec, candidate_payload)
        evaluation = {
            "source": payload.get("source"),
            "task_run_id": payload.get("task_run_id"),
            "generation_reason": dict(candidate_payload.get("generation_reason") or spec.metadata.get("generation_reason") or {}),
            "committee_review": dict(spec.metadata.get("committee_review") or {}),
            "llm_analysis": dict(spec.metadata.get("llm_analysis") or {}),
            "llm_research_context": cls._summarize_llm_research_context(spec.metadata.get("llm_research_context") or {}),
            "llm_response": cls._summarize_llm_response(spec.metadata.get("llm_response") or {}),
            "target_symbols": list(candidate_payload.get("target_symbols") or spec.metadata.get("target_symbols") or [])[:12],
            "stock_pool": cls._summarize_stock_pool(candidate_payload.get("stock_pool") or spec.metadata.get("stock_pool") or {}),
            "selection_logic": cls._summarize_selection_logic(candidate_payload.get("selection_logic") or spec.metadata.get("selection_logic") or []),
            "research_scope": cls._compact_dict(
                candidate_payload.get("research_scope") or spec.metadata.get("research_scope") or {},
                keys=("scope", "symbol_count", "candidate_count", "source"),
            ),
            "hypothesis_artifact": cls._summarize_hypothesis_artifact(candidate_payload.get("hypothesis_artifact") or spec.metadata.get("hypothesis_artifact") or {}),
            "hypothesis_lowering_audit": cls._compact_dict(
                candidate_payload.get("hypothesis_lowering_audit") or spec.metadata.get("hypothesis_lowering_audit") or {},
                keys=("source", "target_symbols"),
            ),
            "research_task": cls._summarize_research_task(research_task),
            "event_context": cls._summarize_event_context(event_context),
        }
        cls._apply_candidate_contract_fields(evaluation, candidate_payload)
        persisted_payload = {
            **payload,
            "parameters": summarized_parameters,
            "strategy_spec": strategy_spec,
            "evaluation": evaluation,
            "result": dict(payload.get("result") or {}),
        }
        cls._apply_candidate_contract_fields(persisted_payload, candidate_payload)
        return persisted_payload

    async def record_experiment(self, db, spec: StrategySpec, source: str, snapshot: dict, task_run: dict) -> dict:
        experiment_id = f"exp_{int(time.time())}_{uuid4().hex[:8]}"
        hypothesis_artifact = dict(spec.metadata.get("hypothesis_artifact") or {})
        hypothesis = (
            str(hypothesis_artifact.get("alpha_hypothesis") or "").strip()
            or spec.description
            or f"{source}:{spec.strategy_type}"
        )
        artifact = register_experiment({
            "experiment_id": experiment_id,
            "hypothesis": hypothesis,
            "method": spec.metadata.get("generator_type") or source,
            "parameters": spec.params,
            "status": "running",
            "tags": spec.tags,
            "conclusion": "",
        })
        parent_strategy_id = spec.metadata.get("parent_strategy_id")
        prompt_payload = spec.metadata.get("llm_prompt")
        candidate_view = _apply_resolved_candidate_envelope(spec.to_candidate(source, experiment_id))
        research_task = dict(candidate_view.get("research_task") or spec.metadata.get("research_task") or {})
        event_context = dict(candidate_view.get("event_context") or spec.metadata.get("event_context") or {}) or _extract_event_context(research_task)
        payload = {
            "experiment_id": experiment_id,
            "strategy_id": parent_strategy_id,
            "parent_strategy_id": parent_strategy_id,
            "generated_strategy_id": spec.metadata.get("generated_strategy_id"),
            "task_run_id": task_run.get("id"),
            "source": source,
            "generator_type": spec.metadata.get("generator_type") or source,
            "optimizer_type": spec.metadata.get("optimizer_type"),
            "status": "generated",
            "hypothesis": hypothesis,
            "prompt": (str(prompt_payload) if prompt_payload is not None else str(snapshot.get("date") or date.today())),
            "parameters": dict(candidate_view.get("params") or spec.params),
            "strategy_spec": {
                "strategy_type": spec.strategy_type,
                "name": spec.name,
                "description": spec.description,
                "tags": spec.tags,
                "params": dict(candidate_view.get("params") or spec.params),
                "target_symbols": candidate_view.get("target_symbols") or spec.metadata.get("target_symbols") or [],
                "stock_pool": candidate_view.get("stock_pool") or spec.metadata.get("stock_pool") or {},
                "selection_logic": candidate_view.get("selection_logic") or spec.metadata.get("selection_logic") or [],
                "research_task": research_task,
                "event_context": event_context,
                "hypothesis_artifact": candidate_view.get("hypothesis_artifact") or hypothesis_artifact,
                "holding_horizon": candidate_view.get("holding_horizon") or spec.metadata.get("holding_horizon") or {},
                "trade_plan": candidate_view.get("trade_plan") or spec.metadata.get("trade_plan") or {},
                "risk_rules": candidate_view.get("risk_rules") or spec.metadata.get("risk_rules") or {},
                "position_sizing": candidate_view.get("position_sizing") or spec.metadata.get("position_sizing") or {},
                "execution_notes": candidate_view.get("execution_notes") or spec.metadata.get("execution_notes"),
                "rebalance_rule": candidate_view.get("rebalance_rule") or spec.metadata.get("rebalance_rule") or {},
                "portfolio_spec": candidate_view.get("portfolio_spec") or spec.metadata.get("portfolio_spec") or {},
                "execution_assumptions": candidate_view.get("execution_assumptions") or spec.metadata.get("execution_assumptions") or {},
                "validation_profile": candidate_view.get("validation_profile") or spec.metadata.get("validation_profile") or {},
                "hypothesis_lowering_audit": candidate_view.get("hypothesis_lowering_audit") or spec.metadata.get("hypothesis_lowering_audit") or {},
                "replay_contract": self._build_replay_strategy_contract(
                    spec=spec,
                    research_task=research_task,
                    event_context=event_context,
                    candidate=candidate_view,
                ),
            },
            "evaluation": {
                "source": source,
                "task_run_id": task_run.get("id"),
                "generation_reason": candidate_view.get("generation_reason") or spec.metadata.get("generation_reason") or {},
                "committee_review": spec.metadata.get("committee_review") or {},
                "llm_analysis": spec.metadata.get("llm_analysis") or {},
                "llm_research_context": spec.metadata.get("llm_research_context") or {},
                "llm_response": spec.metadata.get("llm_response") or {},
                "target_symbols": candidate_view.get("target_symbols") or spec.metadata.get("target_symbols") or [],
                "stock_pool": candidate_view.get("stock_pool") or spec.metadata.get("stock_pool") or {},
                "selection_logic": candidate_view.get("selection_logic") or spec.metadata.get("selection_logic") or [],
                "research_scope": candidate_view.get("research_scope") or spec.metadata.get("research_scope") or {},
                "hypothesis_artifact": candidate_view.get("hypothesis_artifact") or hypothesis_artifact,
                "hypothesis_lowering_audit": candidate_view.get("hypothesis_lowering_audit") or spec.metadata.get("hypothesis_lowering_audit") or {},
                "research_task": research_task,
                "event_context": event_context,
            },
            "result": {},
            "parent_experiment_id": None,
            "artifact_id": artifact.get("artifact_id"),
        }
        self._apply_candidate_contract_fields(payload["strategy_spec"], candidate_view)
        self._apply_candidate_contract_fields(payload["evaluation"], candidate_view)
        self._apply_candidate_contract_fields(payload, candidate_view)
        persisted_payload = self._build_persisted_experiment_payload(
            spec=spec,
            payload=payload,
            candidate=candidate_view,
        )
        try:
            return await db.save_strategy_generation_experiment(persisted_payload)
        except Exception as exc:
            logger.warning(
                "ExperimentRecorder: save experiment failed, continuing without persistence: %s",
                exc,
            )
            return {
                **persisted_payload,
                "persistence_error": str(exc),
            }

    async def record_candidates(self, db, specs: list[StrategySpec], *, source: str, snapshot: dict, task_run: dict) -> dict:
        experiments = []
        candidates = []
        champion = None
        for spec in list(specs or []):
            experiment = await self.record_experiment(db, spec, source, snapshot, task_run)
            experiments.append(experiment)
            candidate = spec.to_candidate(source, experiment["experiment_id"])
            candidates.append(candidate)
            if champion is None:
                committee_review = dict((experiment.get("evaluation") or {}).get("committee_review") or {})
                champion = {
                    "experiment_id": experiment.get("experiment_id"),
                    "strategy_type": spec.strategy_type,
                    "name": spec.name,
                    "final_score": committee_review.get("final_score"),
                    "decision": committee_review.get("decision"),
                    "parent_strategy_id": experiment.get("parent_strategy_id") or experiment.get("strategy_id"),
                }
        return {
            "experiments": experiments,
            "candidates": candidates,
            "champion": champion,
        }

    async def apply_submission_results(self, db, experiments: list[dict], submit_result: Optional[dict]) -> dict:
        payload = dict(submit_result or {})
        submission_items = list(payload.get("items") or payload.get("strategies") or [])
        by_experiment = {item.get("experiment_id"): item for item in submission_items}
        updated_experiments = []
        for experiment in list(experiments or []):
            item = by_experiment.get(experiment.get("experiment_id")) or {}
            evaluation = dict(experiment.get("evaluation") or {})
            evaluation["submission_result"] = item
            updated = await db.save_strategy_generation_experiment({
                **experiment,
                "strategy_id": experiment.get("parent_strategy_id") or experiment.get("strategy_id"),
                "generated_strategy_id": item.get("strategy_id") or experiment.get("generated_strategy_id"),
                "status": "accepted" if item.get("passed") else "rejected",
                "evaluation": evaluation,
                "result": item,
            })
            updated_experiments.append(updated)
        return {
            "experiments": updated_experiments,
            "items_by_experiment": by_experiment,
        }
