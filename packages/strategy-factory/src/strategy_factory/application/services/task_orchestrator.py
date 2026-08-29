"""TaskOrchestrator – schedules and runs autonomy research tasks.

Wraps the core logic of StrategyFactoryScheduler._run_autonomy_batches,
providing a service boundary for the research task execution phase.

P4 refactor: this service owns "what tasks to run and how to run them",
decoupled from the scheduling loop.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ...domain.research_tasks import ResearchBatchResult, ResearchTaskSpec, ResearchTaskStatus

logger = logging.getLogger(__name__)


class TaskOrchestrator:
    """Runs a batch of autonomy research tasks via the scheduler helpers.

    This is a thin coordination layer that delegates actual execution to the
    existing ``StrategyFactoryScheduler._run_autonomy_batches`` method while
    exposing the ``ResearchBatchResult`` domain model as the return type.

    Usage::

        orchestrator = TaskOrchestrator(scheduler)
        batch = await orchestrator.run(db, snapshot)
        # batch.generated_candidates, batch.completed_count, etc.
    """

    def __init__(self, scheduler: Any) -> None:
        self._scheduler = scheduler

    async def run(
        self,
        db: Any,
        snapshot: dict[str, Any],
    ) -> ResearchBatchResult:
        """Execute all autonomy tasks for this factory cycle.

        Args:
            db: Repository adapter.
            snapshot: Current market snapshot.

        Returns:
            ResearchBatchResult with typed access to candidates, experiments
            and task-level metrics.
        """
        raw = await self._scheduler._run_autonomy_batches(db, snapshot)
        stage = dict(raw.get("stage") or {})
        candidates = list(raw.get("candidates") or [])
        experiments = list(raw.get("experiments") or [])
        full_market_topn = dict(raw.get("full_market_topn") or {})
        full_market_score_rows = [
            dict(item or {})
            for item in list(raw.get("full_market_score_rows") or [])
            if isinstance(item, dict)
        ]
        persistence_failures = list(stage.get("persistence_failures") or [])

        task_results_raw = list(stage.get("task_results") or [])
        task_results = []
        for item in task_results_raw:
            task_dict = dict(item.get("task") or {})
            spec = ResearchTaskSpec.from_dict(task_dict)
            status_raw = str(item.get("status") or "").strip().lower()
            try:
                status = ResearchTaskStatus(status_raw)
            except ValueError:
                status = ResearchTaskStatus.FAILED if status_raw == "failed" else ResearchTaskStatus.COMPLETED
            task_results.append(
                ResearchTaskResult(
                    task_spec=spec,
                    status=status,
                    generated_count=int(item.get("generated_count") or 0),
                    error=item.get("error"),
                    task_run_id=str(item["task_run_id"]) if item.get("task_run_id") is not None else None,
                    evidence_count=int(item.get("evidence_count") or 0),
                    external_llm_status=str(item.get("external_llm_status") or ""),
                    attempt_count=int(item.get("attempt_count") or 0),
                    selected_count=int(item.get("selected_count") or 0),
                    elapsed_seconds=float(item.get("elapsed_seconds") or 0.0),
                    last_error_type=item.get("last_error_type"),
                    last_error=item.get("last_error"),
                )
            )

        return ResearchBatchResult(
            task_results=task_results,
            generated_candidates=candidates,
            experiments=experiments,
            persistence_failures=persistence_failures,
            stage_payload=stage,
            full_market_topn=full_market_topn,
            full_market_score_rows=full_market_score_rows,
        )

    def build_task_briefs(self, batch: ResearchBatchResult) -> list[dict[str, Any]]:
        """Return condensed task briefs for use in run summary."""
        return [r.to_brief() for r in batch.task_results]

    @staticmethod
    def classify_tasks(
        tasks: list[dict[str, Any]],
    ) -> tuple[list[ResearchTaskSpec], dict[str, int]]:
        """Parse raw task dicts into typed specs and compute source counts.

        Returns:
            (specs, source_count_map)
        """
        specs: list[ResearchTaskSpec] = []
        source_counts: dict[str, int] = {}
        for raw in list(tasks or []):
            spec = ResearchTaskSpec.from_dict(raw)
            specs.append(spec)
            src = spec.task_source or "unknown"
            source_counts[src] = source_counts.get(src, 0) + 1
        return specs, source_counts


# Re-export for convenience
from ...domain.research_tasks import ResearchTaskResult  # noqa: E402

__all__ = [
    "TaskOrchestrator",
]
