"""策略自治生成：本地 LLM 代理 + 参数进化 + 实验闭环。"""


from __future__ import annotations

import asyncio
from collections import Counter
import logging
from datetime import date
from typing import Any, Optional
from uuid import uuid4

from strategy_factory.api import extract_event_context as _extract_event_context
from .strategy_autonomy_components import (  # noqa: F401
    CandidateGenerationService,
    CommitteeReviewService,
    ExperimentRecorder,
)
from .strategy_autonomy_lifecycle import AutonomyLifecycleTracker

# --- Sub-module re-exports (backward compatibility) ---
from .strategy_spec import (  # noqa: F401
    DEFAULT_CODES,
    RESEARCH_CANDIDATE_POOL_LIMIT,
    RESEARCH_FINANCIAL_DETAIL_LIMIT,
    RESEARCH_KLINE_SCAN_LIMIT,
    RESEARCH_SYMBOL_DETAIL_LIMIT,
    RESEARCH_UNIVERSE_PAGE_SIZE,
    RESEARCH_UNIVERSE_SCAN_LIMIT,
    StrategySpec,
    _safe_normalize_research_task,
)
from .strategy_generators import RuleStrategyGenerator, LLMProxyStrategyGenerator  # noqa: F401
from .strategy_optimizer import BanditParameterOptimizer  # noqa: F401
from .strategy_reviewer import MultiAgentStrategyReviewer  # noqa: F401

logger = logging.getLogger(__name__)


class AutonomyCycleOrchestrator:
# --- strategy_autonomy_parts/context.py ---
    _TASK_RUN_PREVIEW_LIMIT = 3

    @classmethod
    def _preview_items(
        cls,
        items: list[dict] | tuple[dict, ...] | None,
        *,
        fields: tuple[str, ...],
        limit: Optional[int] = None,
    ) -> list[dict]:
        preview: list[dict] = []
        for item in list(items or [])[: limit or cls._TASK_RUN_PREVIEW_LIMIT]:
            if not isinstance(item, dict):
                preview.append({'value': str(item)})
                continue
            compact = {
                key: item.get(key)
                for key in fields
                if item.get(key) not in (None, "", [], {})
            }
            if not compact:
                compact = {'keys': sorted(str(key) for key in item.keys())[:8]}
            preview.append(compact)
        return preview

    @staticmethod
    def _compact_dict(
        payload: Optional[dict[str, Any]],
        *,
        keys: tuple[str, ...],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        source = dict(payload or {})
        for key in keys:
            value = source.get(key)
            if value in (None, "", [], {}):
                continue
            result[key] = value
        return result

    @staticmethod
    def _normalize_external_request_status(status: Any) -> str:
        return str(status or "").strip().lower() or "unknown"

    @classmethod
    def _summarize_external_request_status_counts(
        cls,
        requests: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in list(requests or []):
            status = cls._normalize_external_request_status(dict(item or {}).get("status"))
            counts[status] = counts.get(status, 0) + 1
        return counts

    @classmethod
    def _count_external_network_requests(
        cls,
        requests: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    ) -> int:
        total = 0
        for item in list(requests or []):
            payload = dict(item or {})
            status = cls._normalize_external_request_status(payload.get("status"))
            if status in {"compatibility_skip", "cooldown_skip"}:
                continue
            metrics = dict(payload.get("request_metrics") or {})
            try:
                attempt_count = int(metrics.get("attempt_count") or 0)
            except Exception:
                attempt_count = 0
            total += max(attempt_count, 1)
        return total

    @classmethod
    def _count_external_real_requests(
        cls,
        requests: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    ) -> int:
        total = 0
        for item in list(requests or []):
            status = cls._normalize_external_request_status(dict(item or {}).get("status"))
            if status in {"compatibility_skip", "cooldown_skip"}:
                continue
            total += 1
        return total

    @classmethod
    def _request_is_compatibility_failure(cls, request: Optional[dict[str, Any]]) -> bool:
        payload = dict(request or {})
        status = cls._normalize_external_request_status(payload.get("status"))
        if status in {"compatibility_skip", "cooldown_skip"}:
            return False
        metrics = dict(payload.get("request_metrics") or {})
        metric_status = cls._normalize_external_request_status(metrics.get("status"))
        error_type = str(payload.get("error_type") or metrics.get("last_error_type") or "").strip().lower()
        error_text = str(payload.get("error") or metrics.get("last_error") or "").strip().lower()
        return (
            metric_status == "compatibility_failed"
            or error_type == "providercompatibilityerror"
            or "missing extractable content" in error_text
        )

    @classmethod
    def _request_is_empty_200_response(cls, request: Optional[dict[str, Any]]) -> bool:
        payload = dict(request or {})
        metrics = dict(payload.get("request_metrics") or {})
        if bool(metrics.get("empty_200_response")):
            return True
        if not cls._request_is_compatibility_failure(payload):
            return False
        error_text = str(payload.get("error") or metrics.get("last_error") or "").strip().lower()
        return "missing extractable content" in error_text

    @classmethod
    def _summarize_factor_research(cls, factor_research: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(factor_research or {})
        summary = dict(payload.get('summary') or {})
        freshness_repair = dict(payload.get('freshness_repair') or {})
        compact = {
            'top_factor_names': list(
                summary.get('top_factor_names')
                or payload.get('active_factors')
                or []
            )[:6],
            'preferred_strategy_types': [
                str(item).strip()
                for item in list(payload.get('preferred_strategy_types') or [])
                if str(item).strip()
            ][:6],
            'degraded': bool(payload.get('degraded')),
        }
        compact.update(
            cls._compact_dict(
                summary,
                keys=(
                    'active_candidate_count',
                    'candidate_pool_size',
                    'registry_size',
                    'freshness_days',
                    'refresh_status',
                ),
            )
        )
        if freshness_repair:
            compact['freshness_repair'] = cls._compact_dict(
                freshness_repair,
                keys=(
                    'refresh_attempted',
                    'refresh_status',
                    'refresh_trigger',
                    'fallback_reason',
                    'stale_days',
                ),
            )
        return {key: value for key, value in compact.items() if value not in (None, "", [], {})}

    @classmethod
    def _summarize_research_task(
        cls,
        research_task: Optional[dict[str, Any]],
        *,
        factor_research: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        task = _safe_normalize_research_task(research_task) or dict(research_task or {})
        metadata = dict(task.get('metadata') or {})
        summary = cls._compact_dict(
            task,
            keys=(
                'task_id',
                'task_key',
                'task_source',
                'opportunity_type',
                'theme_code',
                'event_id',
                'candidate_family',
                'factor_name',
                'generation_limit',
                'source_candidate_artifact_id',
                'evidence_count',
                'preference_strength',
                'validation_focus',
            ),
        )
        target_symbols = list(task.get('target_symbols') or [])
        if target_symbols:
            summary['target_symbols'] = target_symbols[:12]
        preferred_strategy_types = [
            str(item).strip()
            for item in list(
                task.get('preferred_strategy_types')
                or task.get('strategy_preferences')
                or []
            )
            if str(item).strip()
        ]
        if preferred_strategy_types:
            summary['preferred_strategy_types'] = preferred_strategy_types[:6]
            summary['strategy_preferences'] = preferred_strategy_types[:6]
        allowed_strategy_types = [
            str(item).strip()
            for item in list(task.get('allowed_strategy_types') or [])
            if str(item).strip()
        ]
        if allowed_strategy_types:
            summary['allowed_strategy_types'] = allowed_strategy_types[:6]
        factor_summary = cls._summarize_factor_research(
            metadata.get('factor_research') if isinstance(metadata.get('factor_research'), dict) else factor_research
        )
        if factor_summary:
            summary['metadata'] = {'factor_research': factor_summary}
        return summary

    @classmethod
    def _summarize_event_context(cls, event_context: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(event_context or {})
        summary = cls._compact_dict(
            payload,
            keys=(
                'task_source',
                'event_id',
                'theme_code',
                'event_type',
                'opportunity_type',
                'candidate_family',
                'factor_name',
            ),
        )
        target_symbols = list(payload.get('target_symbols') or payload.get('symbols') or [])
        if target_symbols:
            summary['target_symbols'] = target_symbols[:12]
        score_summary = dict(payload.get('score_summary') or {})
        if score_summary:
            summary['score_summary'] = cls._compact_dict(
                score_summary,
                keys=(
                    'score',
                    'confidence',
                    'impact_score',
                    'urgency_score',
                    'priority',
                ),
            )
        return summary

    @classmethod
    def _summarize_llm_report(cls, llm_report: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(llm_report or {})
        external = dict(payload.get('external_provider') or {})
        requests = list(external.get('requests') or [])
        external_summary = cls._compact_dict(
            external,
            keys=(
                'enabled',
                'provider',
                'model',
                'status',
                'selected_count',
                'viable_selected_count',
                'fallback_count',
                'elapsed_seconds',
                'last_error_type',
                'last_error',
                'stage_attempt_count',
                'network_request_count',
                'real_request_count',
                'compatibility_skip_count',
                'cooldown_skip_count',
                'compatibility_failure_count',
                'compatibility_failure_ratio',
                'effective_response_count',
                'effective_response_ratio',
                'empty_200_response_count',
            ),
        )
        request_status_counts = dict(external.get('request_status_counts') or {}) or cls._summarize_external_request_status_counts(requests)
        if requests:
            real_request_count = int(external.get('real_request_count') or cls._count_external_real_requests(requests))
            compatibility_failure_count = int(
                external.get('compatibility_failure_count')
                or sum(1 for item in requests if cls._request_is_compatibility_failure(item))
            )
            effective_response_count = int(
                external.get('effective_response_count')
                or sum(
                    1
                    for item in requests
                    if cls._normalize_external_request_status(dict(item or {}).get('status')) == 'succeeded'
                )
            )
            empty_200_response_count = int(
                external.get('empty_200_response_count')
                or sum(1 for item in requests if cls._request_is_empty_200_response(item))
            )
            external_summary['request_count'] = len(requests)
            external_summary['stage_attempt_count'] = int(external.get('stage_attempt_count') or len(requests))
            external_summary['network_request_count'] = int(
                external.get('network_request_count') or cls._count_external_network_requests(requests)
            )
            external_summary['real_request_count'] = real_request_count
            external_summary['compatibility_skip_count'] = int(
                external.get('compatibility_skip_count')
                or request_status_counts.get('compatibility_skip', 0)
            )
            external_summary['cooldown_skip_count'] = int(
                external.get('cooldown_skip_count')
                or request_status_counts.get('cooldown_skip', 0)
            )
            external_summary['compatibility_failure_count'] = compatibility_failure_count
            external_summary['effective_response_count'] = effective_response_count
            external_summary['empty_200_response_count'] = empty_200_response_count
            external_summary['compatibility_failure_ratio'] = (
                external.get('compatibility_failure_ratio')
                if external.get('compatibility_failure_ratio') is not None
                else (round(compatibility_failure_count / real_request_count, 4) if real_request_count else 0.0)
            )
            external_summary['effective_response_ratio'] = (
                external.get('effective_response_ratio')
                if external.get('effective_response_ratio') is not None
                else (round(effective_response_count / real_request_count, 4) if real_request_count else 0.0)
            )
        elif request_status_counts:
            external_summary['request_count'] = int(external.get('stage_attempt_count') or 0)
        if external.get('request_limits'):
            external_summary['request_limits'] = list(external.get('request_limits') or [])[:4]
        if request_status_counts:
            external_summary['request_status_counts'] = request_status_counts
        if requests:
            external_summary['requests_preview'] = [
                {
                    **cls._compact_dict(
                        dict(item or {}),
                        keys=(
                            'request_index',
                            'request_limit',
                            'status',
                            'returned_candidate_count',
                            'compiled_candidate_count',
                            'non_executable_candidate_count',
                            'viable_candidate_count',
                            'open_dsl_candidate_count',
                            'open_dsl_compiled_candidate_count',
                            'open_dsl_viable_candidate_count',
                            'open_dsl_rejected_count',
                            'error_type',
                            'error',
                        ),
                    ),
                    'request_metrics': cls._compact_dict(
                        dict((item or {}).get('request_metrics') or {}),
                        keys=(
                            'attempt_count',
                            'prompt_chars',
                            'response_chars',
                            'elapsed_seconds',
                            'last_error_type',
                            'last_error',
                        ),
                    ),
                }
                for item in requests[: cls._TASK_RUN_PREVIEW_LIMIT]
            ]
        analysis = dict(external.get('analysis') or {})
        if analysis:
            external_summary['analysis'] = cls._compact_dict(
                analysis,
                keys=(
                    'style_bias',
                    'market_regime',
                    'theme',
                    'direction',
                    'risk_hint',
                    'confidence',
                ),
            )
        summary = cls._compact_dict(
            payload,
            keys=(
                'requested_limit',
                'market_frame_ready',
                'market_frame_rows',
                'market_frame_source',
                'selected_count',
                'pipeline_run_timeout_sec',
            ),
        )
        if payload.get('selected_generators'):
            summary['selected_generators'] = dict(payload.get('selected_generators') or {})
        if payload.get('research_context_summary'):
            summary['research_context_summary'] = dict(payload.get('research_context_summary') or {})
        if external_summary:
            summary['external_provider'] = external_summary
        return summary

    @classmethod
    def _summarize_generation_result(cls, generation_result: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(generation_result or {})
        stats = dict(payload.get('stats') or {})
        return {
            'count': int(payload.get('count') or len(payload.get('candidates') or [])),
            'stats': cls._compact_dict(
                stats,
                keys=('rule_count', 'llm_count', 'evolved_count', 'merged_count'),
            ),
            'llm_generation': cls._summarize_llm_report(payload.get('llm_generation')),
            'candidate_preview': cls._preview_items(
                payload.get('candidates') or [],
                fields=('experiment_id', 'strategy_type', 'name', 'generated_strategy_id', 'status'),
            ),
        }

    @classmethod
    def _summarize_review_result(cls, review_result: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(review_result or {})
        return {
            'reviewed_count': int(payload.get('reviewed_count') or 0),
            'rejected_count': int(payload.get('rejected_count') or 0),
            'committee_review_count': len(payload.get('committee_reviews') or []),
            'committee_review_preview': cls._preview_items(
                payload.get('committee_reviews') or [],
                fields=('experiment_id', 'decision', 'final_score', 'review_rank', 'strategy_type', 'name'),
            ),
            'champion': cls._compact_dict(
                dict(payload.get('champion') or {}),
                keys=('experiment_id', 'generated_strategy_id', 'status', 'review_rank', 'final_score'),
            ),
        }

    @classmethod
    def _summarize_experiments_result(cls, experiments_result: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(experiments_result or {})
        return {
            'count': int(payload.get('count') or len(payload.get('items') or [])),
            'status_counts': dict(payload.get('status_counts') or {}),
            'preview': cls._preview_items(
                payload.get('items') or [],
                fields=('experiment_id', 'status', 'strategy_id', 'generated_strategy_id', 'generator_type', 'optimizer_type'),
            ),
        }

    @classmethod
    def _summarize_submission_result(cls, submission_result: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = dict(submission_result or {})
        items = list(payload.get('items') or [])
        return {
            'auto_submit': bool(payload.get('auto_submit')),
            'attempted': bool(payload.get('attempted')),
            'submitted_count': int(payload.get('submitted_count') or 0),
            'passed_count': int(payload.get('passed_count') or 0),
            'failed_count': int(payload.get('failed_count') or 0),
            'provisional_passed_count': int(payload.get('provisional_passed_count') or 0),
            'failure_reason_topn': list(payload.get('failure_reason_topn') or [])[: cls._TASK_RUN_PREVIEW_LIMIT],
            'items_preview': cls._preview_items(
                items,
                fields=('experiment_id', 'strategy_id', 'passed', 'duplicate', 'reason_code'),
            ),
        }

    @staticmethod
    def _submission_metrics(submit_result: Optional[dict]) -> dict:
        payload = dict(submit_result or {})
        items = list(payload.get('items') or payload.get('strategies') or [])
        submitted_count = int(payload.get('submitted', len(items)))
        passed_count = int(
            payload.get(
                'gate_3_passed',
                payload.get(
                    'passed_quality_gate',
                    len([item for item in items if item.get('passed')]),
                ),
            )
        )
        failed_count = int(payload.get('gate_3_failed', max(submitted_count - passed_count, 0)))
        return {
            'submitted_count': submitted_count,
            'passed_count': passed_count,
            'failed_count': failed_count,
        }

    def __init__(self):
        self.generation_service = CandidateGenerationService(
            rule_generator=RuleStrategyGenerator(),
            llm_generator=LLMProxyStrategyGenerator(),
            optimizer=BanditParameterOptimizer(),
        )
        self.review_service = CommitteeReviewService(reviewer=MultiAgentStrategyReviewer())
        self.experiment_recorder = ExperimentRecorder()
        self.rule_generator = self.generation_service.rule_generator
        self.llm_generator = self.generation_service.llm_generator
        self.optimizer = self.generation_service.optimizer
        self.reviewer = self.review_service.reviewer

    async def _select_parents(self, db, parent_strategy_id: Optional[str] = None) -> list[dict]:
        return await self.generation_service.select_parents(db, parent_strategy_id=parent_strategy_id)

    @staticmethod
    def _attach_lineage(spec: StrategySpec, *, parent_strategy_id: Optional[str], task_run_id: Optional[int], review_rank: Optional[int] = None, is_champion: bool = False) -> StrategySpec:
        return CommitteeReviewService.attach_lineage(
            spec,
            parent_strategy_id=parent_strategy_id,
            task_run_id=task_run_id,
            review_rank=review_rank,
            is_champion=is_champion,
        )

    @staticmethod
    def _review_score(spec: StrategySpec) -> float:
        return CommitteeReviewService.review_score(spec)

    async def _record_experiment(self, db, spec: StrategySpec, source: str, snapshot: dict, task_run: dict) -> dict:
        return await self.experiment_recorder.record_experiment(db, spec, source, snapshot, task_run)

    async def generate_factory_candidates(self, db, snapshot: dict, limit: int = 3, research_task: Optional[dict[str, Any]] = None, source: str = 'strategy_factory') -> dict:
        cycle = await self.run_cycle(db, snapshot=snapshot, limit=limit, source=source, auto_submit=False, research_task=research_task)
        return cycle

    @staticmethod
    def _build_generation_result(candidates: list[dict], generation_stats: dict, llm_report: dict[str, Any]) -> dict:
        return {
            'count': len(candidates),
            'stats': generation_stats,
            'llm_generation': llm_report,
            'candidates': candidates,
        }

    @staticmethod
    def _build_review_result(reviewed_specs: list[StrategySpec], rejected_count: int, committee_reviews: list[dict[str, Any]], champion: Optional[dict[str, Any]]) -> dict:
        return {
            'reviewed_count': len(reviewed_specs),
            'rejected_count': rejected_count,
            'committee_reviews': committee_reviews,
            'champion': champion,
        }

    @staticmethod
    def _build_experiments_result(experiments: list[dict]) -> dict:
        status_counts = Counter(
            str((item or {}).get('status') or 'unknown')
            for item in list(experiments or [])
        )
        return {
            'count': len(experiments),
            'items': experiments,
            'status_counts': dict(status_counts),
        }
# --- strategy_autonomy_parts/specs.py ---

    @staticmethod
    def _build_submission_result(auto_submit: bool, submit_result: Optional[dict], candidates: list[dict]) -> dict:
        payload = dict(submit_result or {})
        items = list(payload.get('items') or payload.get('strategies') or [])
        submitted_count = int(payload.get('submitted', len(items) if auto_submit and items else 0))
        inferred_passed_count = len([item for item in items if item.get('passed')])
        passed_count = int(payload.get('gate_3_passed', payload.get('passed_quality_gate', inferred_passed_count)))
        failed_count = int(payload.get('gate_3_failed', max(submitted_count - passed_count, 0)))
        return {
            'auto_submit': auto_submit,
            'attempted': bool(auto_submit and candidates),
            'submitted_count': submitted_count,
            'passed_count': passed_count,
            'failed_count': failed_count,
            'provisional_passed_count': int(payload.get('gate_3_provisional_passed', 0)),
            'failure_reason_topn': list(payload.get('gate_3_failure_reason_topn') or []),
            'items': items,
            'result': payload or None,
        }

    @staticmethod
    def _build_task_run_result(task_run: dict, *, source: str, snapshot_date: Any, research_task: dict, event_context: dict) -> dict:
        return {
            'id': task_run.get('id'),
            'trace_id': task_run.get('trace_id'),
            'status': 'completed',
            'source': source,
            'snapshot_date': snapshot_date,
            'research_task': research_task,
            'event_context': event_context,
        }

    @classmethod
    def _build_task_run_result_summary(
        cls,
        *,
        task_run: dict,
        status: str,
        source: str,
        snapshot_date: Any,
        research_task: Optional[dict[str, Any]],
        event_context: Optional[dict[str, Any]],
        factor_research: Optional[dict[str, Any]],
        full_result: dict[str, Any],
        artifact_info: Optional[dict[str, Any]],
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        generation_result = dict(full_result.get('generation') or {})
        review_result = dict(full_result.get('review') or {})
        experiments_result = dict(full_result.get('experiments') or {})
        submission_result = dict(full_result.get('submission') or {})
        summary = {
            'storage_mode': 'summary_with_external_payload' if artifact_info else 'summary_only',
            'task_run_id': task_run.get('id'),
            'trace_id': task_run.get('trace_id'),
            'status': status,
            'source': source,
            'snapshot_date': snapshot_date,
            'task_source': dict(research_task or {}).get('task_source'),
            'research_task': cls._summarize_research_task(
                research_task,
                factor_research=factor_research,
            ),
            'event_context': cls._summarize_event_context(event_context),
            'factor_research': cls._summarize_factor_research(factor_research),
            'generated_count': int(
                full_result.get('generated_count')
                or generation_result.get('count')
                or 0
            ),
            'reviewed_count': int(
                full_result.get('reviewed_count')
                or review_result.get('reviewed_count')
                or 0
            ),
            'rejected_count': int(
                full_result.get('rejected_count')
                or review_result.get('rejected_count')
                or 0
            ),
            'generation': cls._summarize_generation_result(generation_result),
            'review': cls._summarize_review_result(review_result),
            'experiments': cls._summarize_experiments_result(experiments_result),
            'submission': cls._summarize_submission_result(submission_result),
            'champion': cls._compact_dict(
                dict(full_result.get('champion') or review_result.get('champion') or {}),
                keys=('experiment_id', 'generated_strategy_id', 'status', 'review_rank', 'final_score'),
            ),
            'lifecycle': dict(full_result.get('lifecycle') or {}),
        }
        if artifact_info:
            summary['full_result_artifact'] = dict(artifact_info)
        if error:
            summary['error'] = error
        return summary

    @staticmethod
    def _build_cycle_result(
        *,
        task_run_result: dict,
        generation_result: dict,
        review_result: dict,
        experiments_result: dict,
        submission_result: dict,
        snapshot_date: Any,
        research_task: dict,
        factor_research: dict,
        event_context: dict,
        lifecycle: dict,
    ) -> dict:
        experiment_items = list(experiments_result.get('items') or [])
        return {
            'task_run': task_run_result,
            'generation': generation_result,
            'review': review_result,
            'experiments': experiments_result,
            'submission': submission_result,
            'lifecycle': lifecycle,
            'task_run_id': task_run_result.get('id'),
            'snapshot_date': snapshot_date,
            'research_task': research_task,
            'factor_research': factor_research,
            'generated_count': generation_result.get('count', 0),
            'reviewed_count': review_result.get('reviewed_count', 0),
            'rejected_count': review_result.get('rejected_count', 0),
            'committee_reviews': list(review_result.get('committee_reviews') or []),
            'candidates': list(generation_result.get('candidates') or []),
            'experiment_records': experiment_items,
            'champion': review_result.get('champion'),
            'submitted': submission_result.get('result'),
            'llm_generation': generation_result.get('llm_generation') or {},
            'event_context': event_context,
            'generation_stats': generation_result.get('stats') or {},
            'artifacts': {
                'experiments': experiment_items,
            },
        }


    async def _prepare_cycle_context(
        self,
        db,
        *,
        snapshot: Optional[dict],
        research_task: Optional[dict[str, Any]],
    ) -> tuple[dict, dict, dict, dict]:
        resolved_snapshot = snapshot or (
            await db.get_daily_snapshot() if hasattr(db, 'get_daily_snapshot') else None
        ) or {'date': str(date.today())}
        resolved_task = dict(research_task or {})
        factor_research = dict(resolved_snapshot.get('factor_research') or {})
        task_metadata = dict(resolved_task.get('metadata') or {})
        task_factor_research = (
            task_metadata.get('factor_research')
            if isinstance(task_metadata.get('factor_research'), dict)
            else factor_research
        )
        if task_factor_research:
            task_metadata['factor_research'] = dict(task_factor_research)
        elif task_metadata.get('factor_research') not in (None, '', [], {}):
            task_metadata.pop('factor_research', None)
        if task_metadata:
            resolved_task = {**resolved_task, 'metadata': task_metadata}
        event_context = _extract_event_context(resolved_task)
        return resolved_snapshot, resolved_task, factor_research, event_context

    async def _create_cycle_task_run(
        self,
        db,
        *,
        parent_strategy_id: Optional[str],
        source: str,
        limit: int,
        snapshot: dict,
        research_task: dict,
        event_context: dict,
        auto_submit: bool,
    ) -> dict:
        trace_id = uuid4().hex[:12]
        payload = {
            'strategy_id': parent_strategy_id,
            'task_name': 'strategy_ai_cycle',
            'task_scope': source,
            'task_key': research_task.get('task_key') or parent_strategy_id or str(snapshot.get('date') or date.today()),
            'status': 'running',
            'trace_id': trace_id,
            'payload': {
                'limit': limit,
                'parent_strategy_id': parent_strategy_id,
                'snapshot_date': snapshot.get('date'),
                'research_task': research_task,
                'event_context': event_context,
                'auto_submit': auto_submit,
            },
        }
        try:
            persisted = await db.save_strategy_task_run(payload)
        except Exception as exc:
            logger.warning(
                'StrategyAutonomyService: save task run failed, continuing without persistence: %s',
                exc,
            )
            return {
                'id': None,
                'trace_id': trace_id,
                'persistence_error': str(exc),
            }
        if not isinstance(persisted, dict):
            persisted = {}
        return {
            'id': persisted.get('id'),
            'trace_id': persisted.get('trace_id') or trace_id,
            **persisted,
        }

    async def _run_cycle_pipeline(
        self,
        db,
        *,
        snapshot: dict,
        limit: int,
        source: str,
        parent_strategy_id: Optional[str],
        research_task: dict,
        task_run: dict,
        auto_submit: bool,
        lifecycle: AutonomyLifecycleTracker,
    ) -> dict:
        lifecycle.enter_phase('generating', detail={
            'source': source,
            'limit': int(limit or 0),
            'parent_strategy_id': parent_strategy_id,
        })
        generation_batch = await self.generation_service.generate(
            db,
            snapshot=snapshot,
            limit=limit,
            research_task=research_task,
            parent_strategy_id=parent_strategy_id,
        )
        llm_report = dict(generation_batch.get('llm_report') or {})
        rule_specs = list(generation_batch.get('rule_specs') or [])
        llm_specs = list(generation_batch.get('llm_specs') or [])
        evolved_specs = list(generation_batch.get('evolved_specs') or [])
        merged_specs = list(generation_batch.get('merged_specs') or [])
        lifecycle.complete_phase('generating', metrics={
            'rule_count': len(rule_specs),
            'llm_count': len(llm_specs),
            'evolved_count': len(evolved_specs),
            'merged_count': len(merged_specs),
        })

        lifecycle.enter_phase('reviewing', detail={'candidate_count': len(merged_specs)})
        review_batch = self.review_service.review_candidates(
            merged_specs,
            snapshot=snapshot,
            parent_strategy_id=parent_strategy_id,
            task_run_id=task_run.get('id'),
        )
        reviewed_specs = list(review_batch.get('reviewed_specs') or [])
        committee_reviews = list(review_batch.get('committee_reviews') or [])
        rejected_count = int(review_batch.get('rejected_count') or 0)
        lifecycle.complete_phase('reviewing', metrics={
            'reviewed_count': len(reviewed_specs),
            'rejected_count': rejected_count,
            'committee_review_count': len(committee_reviews),
        })

        lifecycle.enter_phase('recording', detail={'reviewed_count': len(reviewed_specs)})
        experiment_batch = await self.experiment_recorder.record_candidates(
            db,
            reviewed_specs,
            source=source,
            snapshot=snapshot,
            task_run=task_run,
        )
        experiments = list(experiment_batch.get('experiments') or [])
        candidates = list(experiment_batch.get('candidates') or [])
        champion = dict(experiment_batch.get('champion') or {}) or None
        lifecycle.complete_phase('recording', metrics={
            'experiment_count': len(experiments),
            'candidate_count': len(candidates),
            'champion_present': champion is not None,
        })

        submit_result = None
        if auto_submit and candidates:
            lifecycle.enter_phase('submitting', detail={'candidate_count': len(candidates)})
            from strategy_factory.api import StrategySubmitter
            submit_result = await StrategySubmitter().submit(candidates, snapshot, db)
            submission_batch = await self.experiment_recorder.apply_submission_results(db, experiments, submit_result)
            experiments = list(submission_batch.get('experiments') or [])
            by_experiment = dict(submission_batch.get('items_by_experiment') or {})
            if champion is not None:
                champion_item = by_experiment.get(champion.get('experiment_id')) or {}
                champion['generated_strategy_id'] = champion_item.get('strategy_id')
                champion['status'] = 'accepted' if champion_item.get('passed') else 'rejected'
            lifecycle.complete_phase('submitting', metrics=self._submission_metrics(submit_result))
        elif auto_submit:
            lifecycle.skip_phase('submitting', reason='no_candidates')
        else:
            lifecycle.skip_phase('submitting', reason='auto_submit_disabled')

        return {
            'llm_report': llm_report,
            'rule_specs': rule_specs,
            'llm_specs': llm_specs,
            'evolved_specs': evolved_specs,
            'merged_specs': merged_specs,
            'reviewed_specs': reviewed_specs,
            'committee_reviews': committee_reviews,
            'rejected_count': rejected_count,
            'experiments': experiments,
            'candidates': candidates,
            'champion': champion,
            'submit_result': submit_result,
        }

    async def _save_cycle_completed_event(
        self,
        db,
        *,
        parent_strategy_id: Optional[str],
        task_run: dict,
        snapshot: dict,
        source: str,
        candidates: list[dict],
        reviewed_specs: list[StrategySpec],
        rejected_count: int,
        champion: Optional[dict[str, Any]],
        research_task: dict,
        event_context: dict,
        factor_research: dict,
        lifecycle: dict,
    ) -> None:
        if not hasattr(db, 'save_strategy_domain_event'):
            return
        try:
            await db.save_strategy_domain_event({
            'strategy_id': parent_strategy_id,
            'aggregate_type': 'strategy_ai_cycle',
            'aggregate_id': str(task_run.get('id') or snapshot.get('date') or date.today()),
            'event_type': 'strategy_ai_cycle.completed',
            'source': source,
            'severity': 'info',
            'correlation_id': task_run.get('trace_id'),
            'payload': {
                'generated_count': len(candidates),
                'reviewed_count': len(reviewed_specs),
                'rejected_count': rejected_count,
                'task_run_id': task_run.get('id'),
                'champion_experiment_id': (champion or {}).get('experiment_id'),
                'research_task': research_task,
                'event_context': event_context,
                'factor_research': factor_research,
                'lifecycle': lifecycle,
            },
            })
        except Exception as exc:
            logger.warning(
                'StrategyAutonomyService: save completed domain event failed, continuing: %s',
                exc,
            )

    async def _save_cycle_failed_event(
        self,
        db,
        *,
        parent_strategy_id: Optional[str],
        task_run: dict,
        snapshot: dict,
        source: str,
        research_task: dict,
        event_context: dict,
        factor_research: dict,
        lifecycle: dict,
        error: str,
    ) -> None:
        if not hasattr(db, 'save_strategy_domain_event'):
            return
        try:
            await db.save_strategy_domain_event({
            'strategy_id': parent_strategy_id,
            'aggregate_type': 'strategy_ai_cycle',
            'aggregate_id': str(task_run.get('id') or snapshot.get('date') or date.today()),
            'event_type': 'strategy_ai_cycle.failed',
            'source': source,
            'severity': 'error',
            'correlation_id': task_run.get('trace_id'),
            'payload': {
                'task_run_id': task_run.get('id'),
                'snapshot_date': snapshot.get('date'),
                'research_task': research_task,
                'event_context': event_context,
                'factor_research': factor_research,
                'lifecycle': lifecycle,
                'error': error,
            },
            })
        except Exception as exc:
            logger.warning(
                'StrategyAutonomyService: save failed domain event failed, continuing: %s',
                exc,
            )
# --- strategy_autonomy_parts/runtime.py ---


    async def run_cycle(
        self,
        db,
        snapshot: Optional[dict] = None,
        limit: int = 3,
        source: str = 'manual',
        parent_strategy_id: Optional[str] = None,
        auto_submit: bool = False,
        research_task: Optional[dict[str, Any]] = None,
    ) -> dict:
        snapshot, research_task, factor_research, event_context = await self._prepare_cycle_context(
            db,
            snapshot=snapshot,
            research_task=research_task,
        )
        lifecycle = AutonomyLifecycleTracker(auto_submit=auto_submit)
        task_run = {'id': None, 'trace_id': None}
        try:
            lifecycle.enter_phase('prepared', detail={
                'source': source,
                'snapshot_date': snapshot.get('date'),
                'task_source': research_task.get('task_source'),
            })
            task_run = await self._create_cycle_task_run(
                db,
                parent_strategy_id=parent_strategy_id,
                source=source,
                limit=limit,
                snapshot=snapshot,
                research_task=research_task,
                event_context=event_context,
                auto_submit=auto_submit,
            )
            lifecycle.complete_phase('prepared', metrics={
                'task_run_id': task_run.get('id'),
                'snapshot_date': snapshot.get('date'),
                'auto_submit': bool(auto_submit),
            })
            pipeline = await self._run_cycle_pipeline(
                db,
                snapshot=snapshot,
                limit=limit,
                source=source,
                parent_strategy_id=parent_strategy_id,
                research_task=research_task,
                task_run=task_run,
                auto_submit=auto_submit,
                lifecycle=lifecycle,
            )
            llm_report = dict(pipeline.get('llm_report') or {})
            rule_specs = list(pipeline.get('rule_specs') or [])
            llm_specs = list(pipeline.get('llm_specs') or [])
            evolved_specs = list(pipeline.get('evolved_specs') or [])
            merged_specs = list(pipeline.get('merged_specs') or [])
            reviewed_specs = list(pipeline.get('reviewed_specs') or [])
            committee_reviews = list(pipeline.get('committee_reviews') or [])
            rejected_count = int(pipeline.get('rejected_count') or 0)
            experiments = list(pipeline.get('experiments') or [])
            candidates = list(pipeline.get('candidates') or [])
            champion = dict(pipeline.get('champion') or {}) or None
            submit_result = pipeline.get('submit_result')
            lifecycle.enter_phase('completed', detail={
                'generated_count': len(candidates),
                'experiment_count': len(experiments),
            })

            generation_stats = {
                'rule_count': len(rule_specs),
                'llm_count': len(llm_specs),
                'evolved_count': len(evolved_specs),
                'merged_count': len(merged_specs),
                'llm_generation': llm_report,
                'research_task': research_task,
                'event_context': event_context,
                'factor_research': factor_research,
            }
            lifecycle.complete_phase('completed', metrics={
                'generated_count': len(candidates),
                'reviewed_count': len(reviewed_specs),
                'experiment_count': len(experiments),
                **self._submission_metrics(submit_result),
            })
            lifecycle_result = lifecycle.snapshot()
            task_run_result = self._build_task_run_result(
                task_run,
                source=source,
                snapshot_date=snapshot.get('date'),
                research_task=research_task,
                event_context=event_context,
            )
            task_run_result['lifecycle'] = lifecycle_result
            generation_result = self._build_generation_result(candidates, generation_stats, llm_report)
            review_result = self._build_review_result(reviewed_specs, rejected_count, committee_reviews, champion)
            experiments_result = self._build_experiments_result(experiments)
            submission_result = self._build_submission_result(auto_submit, submit_result, candidates)
            result = self._build_cycle_result(
                task_run_result=task_run_result,
                generation_result=generation_result,
                review_result=review_result,
                experiments_result=experiments_result,
                submission_result=submission_result,
                snapshot_date=snapshot.get('date'),
                research_task=research_task,
                factor_research=factor_research,
                event_context=event_context,
                lifecycle=lifecycle_result,
            )
            await self._save_cycle_completed_event(
                db,
                parent_strategy_id=parent_strategy_id,
                task_run=task_run,
                snapshot=snapshot,
                source=source,
                candidates=candidates,
                reviewed_specs=reviewed_specs,
                rejected_count=rejected_count,
                champion=champion,
                research_task=research_task,
                event_context=event_context,
                factor_research=factor_research,
                lifecycle=lifecycle_result,
            )
            if task_run.get('id') is not None:
                task_run_summary = self._build_task_run_result_summary(
                    task_run=task_run,
                    status='completed',
                    source=source,
                    snapshot_date=snapshot.get('date'),
                    research_task=research_task,
                    event_context=event_context,
                    factor_research=factor_research,
                    full_result=result,
                    artifact_info=None,
                )
                await db.update_strategy_task_run(
                    task_run['id'],
                    status='completed',
                    result=task_run_summary,
                )
            return result
        except asyncio.CancelledError as exc:
            cancellation_reason = 'cancelled'
            lifecycle.fail_phase(error=RuntimeError(cancellation_reason))
            lifecycle_result = lifecycle.snapshot()
            logger.warning(
                'StrategyAutonomyService.run_cycle cancelled '
                'task_run_id=%s source=%s task_key=%s phase=%s',
                task_run.get('id'),
                source,
                dict(research_task or {}).get('task_key'),
                lifecycle_result.get('current_phase'),
            )
            logger.debug('StrategyAutonomyService.run_cycle cancellation traceback', exc_info=True)
            if task_run.get('id') is not None:
                failure_result = {
                    'status': 'failed',
                    'snapshot_date': snapshot.get('date'),
                    'research_task': research_task,
                    'event_context': event_context,
                    'factor_research': factor_research,
                    'lifecycle': lifecycle_result,
                    'error': cancellation_reason,
                    'cancelled': True,
                }
                try:
                    await db.update_strategy_task_run(
                        task_run['id'],
                        status='failed',
                        error=cancellation_reason,
                        result=self._build_task_run_result_summary(
                            task_run=task_run,
                            status='failed',
                            source=source,
                            snapshot_date=snapshot.get('date'),
                            research_task=research_task,
                            event_context=event_context,
                            factor_research=factor_research,
                            full_result=failure_result,
                            artifact_info=None,
                            error=cancellation_reason,
                        ),
                    )
                    await self._save_cycle_failed_event(
                        db,
                        parent_strategy_id=parent_strategy_id,
                        task_run=task_run,
                        snapshot=snapshot,
                        source=source,
                        research_task=research_task,
                        event_context=event_context,
                        factor_research=factor_research,
                        lifecycle=lifecycle_result,
                        error=cancellation_reason,
                    )
                except Exception as cleanup_exc:
                    logger.debug(
                        'StrategyAutonomyService.run_cycle cancellation cleanup failed: %s',
                        cleanup_exc,
                        exc_info=True,
                    )
            setattr(exc, 'autonomy_lifecycle', lifecycle_result)
            setattr(exc, 'autonomy_task_run_id', task_run.get('id'))
            raise
        except Exception as exc:
            lifecycle.fail_phase(error=exc)
            lifecycle_result = lifecycle.snapshot()
            logger.error('StrategyAutonomyService.run_cycle failed: %s', exc, exc_info=True)
            if task_run.get('id') is not None:
                failure_result = {
                    'status': 'failed',
                    'snapshot_date': snapshot.get('date'),
                    'research_task': research_task,
                    'event_context': event_context,
                    'factor_research': factor_research,
                    'lifecycle': lifecycle_result,
                    'error': str(exc),
                }
                await db.update_strategy_task_run(
                    task_run['id'],
                    status='failed',
                    error=str(exc),
                    result=self._build_task_run_result_summary(
                        task_run=task_run,
                        status='failed',
                        source=source,
                        snapshot_date=snapshot.get('date'),
                        research_task=research_task,
                        event_context=event_context,
                        factor_research=factor_research,
                        full_result=failure_result,
                        artifact_info=None,
                        error=str(exc),
                    ),
                )
            await self._save_cycle_failed_event(
                db,
                parent_strategy_id=parent_strategy_id,
                task_run=task_run,
                snapshot=snapshot,
                source=source,
                research_task=research_task,
                event_context=event_context,
                factor_research=factor_research,
                lifecycle=lifecycle_result,
                error=str(exc),
            )
            setattr(exc, 'autonomy_lifecycle', lifecycle_result)
            setattr(exc, 'autonomy_task_run_id', task_run.get('id'))
            raise


class StrategyAutonomyService(AutonomyCycleOrchestrator):
    """Backward-compatible public entry point for autonomy orchestration."""


_strategy_autonomy_service: Optional[StrategyAutonomyService] = None


def get_strategy_autonomy_service() -> StrategyAutonomyService:
    global _strategy_autonomy_service
    if _strategy_autonomy_service is None:
        _strategy_autonomy_service = StrategyAutonomyService()
    return _strategy_autonomy_service
