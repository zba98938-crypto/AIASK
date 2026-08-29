"""Helpers for executing one autonomy research task."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4

from ._autonomy_task_result import (
    build_completed_task_result,
    build_external_request_metrics,
    build_failed_task_result,
    enrich_candidates_with_task_metrics,
)


@dataclass(slots=True)
class AutonomyTaskExecutionContext:
    task: dict[str, Any]
    task_semaphore: Any
    db: Any
    snapshot: dict[str, Any]
    autonomy_gateway: Any
    persist_task_evidence: Callable[[Any, dict[str, Any]], Awaitable[list[dict[str, Any]]]]
    extract_event_context: Callable[[dict[str, Any]], dict[str, Any]]
    call_optional_async: Callable[..., Awaitable[Any]]
    record_persistence_failure: Callable[[str, Exception], None]
    generate_for_research_task: Callable[[Any, Any, dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]
    extract_cycle_llm_generation: Callable[[dict[str, Any]], dict[str, Any]]
    extract_cycle_lifecycle: Callable[[dict[str, Any]], dict[str, Any]]
    extract_cycle_generated_count: Callable[[dict[str, Any]], int]
    extract_cycle_reviewed_count: Callable[[dict[str, Any]], int]
    extract_cycle_candidates: Callable[[dict[str, Any]], list[dict[str, Any]]]
    extract_cycle_experiments: Callable[[dict[str, Any]], list[dict[str, Any]]]
    enrich_candidate_targeting: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    build_research_task_run_result_summary: Callable[[dict[str, Any]], dict[str, Any]]
    summarize_request_status_counts: Callable[[list[dict[str, Any]]], dict[str, int]]
    count_network_requests: Callable[[list[dict[str, Any]]], int]
    count_real_requests: Callable[[list[dict[str, Any]]], int]
    request_is_compatibility_failure: Callable[[dict[str, Any]], bool]
    request_is_empty_200_response: Callable[[dict[str, Any]], bool]
    normalize_external_request_status: Callable[[Any], str]
    summarize_autonomy_lifecycle: Callable[[dict[str, Any]], dict[str, Any]]
    autonomy_phase_order: list[str] | tuple[str, ...]


@dataclass(slots=True)
class AutonomyTaskExecutionResult:
    task_result_summary: dict[str, Any]
    generated_candidates: list[dict[str, Any]]
    experiments: list[dict[str, Any]]
    external_status: str
    request_metrics: dict[str, Any]
    selected_count: int
    evidence_count: int
    last_error_type: str | None
    last_error: str | None
    elapsed_seconds: float


async def execute_autonomy_task(context: AutonomyTaskExecutionContext) -> AutonomyTaskExecutionResult:
    evidence_rows: list[dict[str, Any]] = []
    task_run: dict[str, Any] = {"id": None}
    enriched_task = dict(context.task or {})
    failed_phase = "preparing"
    async with context.task_semaphore:
        try:
            try:
                evidence_rows = await context.persist_task_evidence(
                    context.db,
                    {**context.task, "snapshot_date": context.snapshot.get("date")},
                )
            except Exception as exc:
                context.record_persistence_failure("save_factory_task_evidence", exc)
                evidence_rows = []

            event_context = context.extract_event_context(context.task)
            try:
                task_run = (
                    await context.call_optional_async(
                        context.db,
                        "save_strategy_task_run",
                        {
                            "strategy_id": None,
                            "task_name": "strategy_research_task",
                            "task_scope": "strategy_factory",
                            "task_key": context.task.get("task_key") or context.task.get("task_id"),
                            "status": "running",
                            "trace_id": uuid4().hex[:12],
                            "payload": {
                                "research_task": context.task,
                                "event_context": event_context,
                                "task_source": context.task.get("task_source"),
                                "evidence_count": len(evidence_rows),
                                "snapshot_date": context.snapshot.get("date"),
                            },
                        },
                        default={"id": None},
                    )
                    or {"id": None}
                )
            except Exception as exc:
                context.record_persistence_failure("save_strategy_task_run", exc)
                task_run = {"id": None}

            enriched_task = {
                **context.task,
                "task_run_id": task_run.get("id"),
                "event_context": event_context,
                "evidence_count": len(evidence_rows),
                "evidence_refs": [
                    {
                        "id": item.get("id"),
                        "evidence_type": item.get("evidence_type"),
                        "symbol": item.get("symbol"),
                        "weight": item.get("weight"),
                    }
                    for item in evidence_rows
                ],
            }
            failed_phase = "generating"
            cycle = await context.generate_for_research_task(
                context.autonomy_gateway,
                context.db,
                context.snapshot,
                enriched_task,
            )
            llm_generation = context.extract_cycle_llm_generation(cycle)
            lifecycle = context.extract_cycle_lifecycle(cycle)
            lifecycle_summary = context.summarize_autonomy_lifecycle(lifecycle)
            external_provider = dict(llm_generation.get("external_provider") or {})
            status = str(external_provider.get("status") or "unknown")
            request_metrics = build_external_request_metrics(
                list(external_provider.get("requests") or []),
                summarize_request_status_counts=context.summarize_request_status_counts,
                count_network_requests=context.count_network_requests,
                count_real_requests=context.count_real_requests,
                request_is_compatibility_failure=context.request_is_compatibility_failure,
                request_is_empty_200_response=context.request_is_empty_200_response,
                normalize_request_status=context.normalize_external_request_status,
            )
            selected_count = int(external_provider.get("selected_count") or 0)
            task_result = build_completed_task_result(
                enriched_task=enriched_task,
                task_run_id=task_run.get("id"),
                evidence_count=len(evidence_rows),
                generated_count=context.extract_cycle_generated_count(cycle),
                reviewed_count=context.extract_cycle_reviewed_count(cycle),
                external_status=status,
                llm_generation=llm_generation,
                lifecycle=lifecycle,
                lifecycle_summary=lifecycle_summary,
                request_metrics=request_metrics,
            )
            task_result_summary = context.build_research_task_run_result_summary(task_result)
            if task_run.get("id") is not None:
                try:
                    await context.call_optional_async(
                        context.db,
                        "update_strategy_task_run",
                        task_run["id"],
                        status="completed",
                        result=task_result_summary,
                    )
                except Exception as exc:
                    context.record_persistence_failure("update_strategy_task_run", exc)
            return AutonomyTaskExecutionResult(
                task_result_summary=task_result_summary,
                generated_candidates=enrich_candidates_with_task_metrics(
                    context.extract_cycle_candidates(cycle),
                    enriched_task=enriched_task,
                    enrich_candidate_targeting=context.enrich_candidate_targeting,
                    request_metrics=request_metrics,
                    selected_count=selected_count,
                ),
                experiments=list(context.extract_cycle_experiments(cycle) or []),
                external_status=status,
                request_metrics=request_metrics,
                selected_count=selected_count,
                evidence_count=len(evidence_rows),
                last_error_type=external_provider.get("last_error_type"),
                last_error=external_provider.get("last_error"),
                elapsed_seconds=float(external_provider.get("elapsed_seconds") or 0.0),
            )
        except (Exception, asyncio.CancelledError) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            failure_lifecycle = dict(getattr(exc, "autonomy_lifecycle", {}) or {})
            if not failure_lifecycle:
                failure_lifecycle = {
                    "state": "failed",
                    "current_phase": failed_phase,
                    "failed_phase": failed_phase,
                    "terminal_phase": "failed",
                    "phase_order": list(context.autonomy_phase_order),
                    "phase_status_counts": {"failed": 1},
                    "completed_phase_count": 0,
                    "event_count": 0,
                    "events": [],
                }
            lifecycle_summary = context.summarize_autonomy_lifecycle(failure_lifecycle)
            task_result = build_failed_task_result(
                enriched_task=enriched_task,
                task_run_id=getattr(exc, "autonomy_task_run_id", None) or task_run.get("id"),
                evidence_count=len(evidence_rows),
                error="cancelled" if cancelled else str(exc),
                lifecycle=failure_lifecycle,
                lifecycle_summary=lifecycle_summary,
            )
            task_result_summary = context.build_research_task_run_result_summary(task_result)
            if task_run.get("id") is not None:
                try:
                    await context.call_optional_async(
                        context.db,
                        "update_strategy_task_run",
                        task_run["id"],
                        status="failed",
                        error="cancelled" if cancelled else str(exc),
                        result=task_result_summary,
                    )
                except Exception as update_exc:
                    context.record_persistence_failure("update_strategy_task_run", update_exc)
            if cancelled:
                raise
            return AutonomyTaskExecutionResult(
                task_result_summary=task_result_summary,
                generated_candidates=[],
                experiments=[],
                external_status="failed",
                request_metrics={},
                selected_count=0,
                evidence_count=len(evidence_rows),
                last_error_type=exc.__class__.__name__,
                last_error=str(exc),
                elapsed_seconds=0.0,
            )
