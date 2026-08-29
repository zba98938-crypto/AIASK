"""Research task domain models for the strategy factory.

ResearchTaskSpec describes a single research task dispatched to the autonomy
gateway. ResearchTaskResult captures the outcome after execution.

P4 implementation: replaces ad-hoc dict construction in _run_autonomy_batches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ResearchTaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ResearchTaskSpec:
    """Specification for a single autonomy research task."""

    task_id: str
    task_key: str
    task_source: str
    opportunity_type: str = ""
    candidate_family: str = ""
    factor_name: str = ""
    generation_limit: int = 4
    target_symbols: list[str] = field(default_factory=list)
    stock_pool: dict[str, Any] = field(default_factory=dict)
    event_context: dict[str, Any] = field(default_factory=dict)
    event_id: Optional[str] = None
    theme_code: Optional[str] = None
    source_candidate_artifact_id: Optional[str] = None
    research_task: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchTaskSpec":
        return cls(
            task_id=str(data.get("task_id") or "").strip(),
            task_key=str(data.get("task_key") or data.get("task_id") or "").strip(),
            task_source=str(data.get("task_source") or "").strip(),
            opportunity_type=str(data.get("opportunity_type") or "").strip(),
            candidate_family=str(data.get("candidate_family") or "").strip(),
            factor_name=str(data.get("factor_name") or "").strip(),
            generation_limit=int(data.get("generation_limit") or 4),
            target_symbols=list(data.get("target_symbols") or []),
            stock_pool=dict(data.get("stock_pool") or {}),
            event_context=dict(data.get("event_context") or {}),
            event_id=data.get("event_id"),
            theme_code=data.get("theme_code"),
            source_candidate_artifact_id=data.get("source_candidate_artifact_id"),
            research_task=dict(data),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_key": self.task_key,
            "task_source": self.task_source,
            "opportunity_type": self.opportunity_type,
            "candidate_family": self.candidate_family,
            "factor_name": self.factor_name,
            "generation_limit": self.generation_limit,
            "target_symbols": self.target_symbols,
            "stock_pool": self.stock_pool,
            "event_context": self.event_context,
            "event_id": self.event_id,
            "theme_code": self.theme_code,
            "source_candidate_artifact_id": self.source_candidate_artifact_id,
        }


@dataclass
class ResearchTaskResult:
    """Outcome of a single research task execution."""

    task_spec: ResearchTaskSpec
    status: ResearchTaskStatus
    generated_count: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    experiments: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    task_run_id: Optional[str] = None
    evidence_count: int = 0
    external_llm_status: str = ""
    attempt_count: int = 0
    selected_count: int = 0
    elapsed_seconds: float = 0.0
    last_error_type: Optional[str] = None
    last_error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in {ResearchTaskStatus.COMPLETED, ResearchTaskStatus.SKIPPED}

    def to_brief(self) -> dict[str, Any]:
        return {
            "task_id": self.task_spec.task_id,
            "task_source": self.task_spec.task_source,
            "opportunity_type": self.task_spec.opportunity_type,
            "candidate_family": self.task_spec.candidate_family,
            "source_candidate_artifact_id": self.task_spec.source_candidate_artifact_id,
            "factor_name": self.task_spec.factor_name,
            "generation_limit": self.task_spec.generation_limit,
            "generated_count": self.generated_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_spec.to_dict(),
            "task_run_id": self.task_run_id,
            "task_source": self.task_spec.task_source,
            "event_id": self.task_spec.event_id,
            "theme_code": self.task_spec.theme_code,
            "status": self.status.value,
            "generated_count": self.generated_count,
            "evidence_count": self.evidence_count,
            "error": self.error,
            "external_llm_status": self.external_llm_status,
            "attempt_count": self.attempt_count,
            "selected_count": self.selected_count,
            "elapsed_seconds": self.elapsed_seconds,
            "last_error_type": self.last_error_type,
            "last_error": self.last_error,
        }


@dataclass
class ResearchBatchResult:
    """Aggregated result for all research tasks in one autonomy batch."""

    task_results: list[ResearchTaskResult] = field(default_factory=list)
    generated_candidates: list[dict[str, Any]] = field(default_factory=list)
    experiments: list[dict[str, Any]] = field(default_factory=list)
    persistence_failures: list[dict[str, Any]] = field(default_factory=list)
    stage_payload: dict[str, Any] = field(default_factory=dict)
    full_market_topn: dict[str, Any] = field(default_factory=dict)
    full_market_score_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def completed_count(self) -> int:
        return sum(1 for r in self.task_results if r.status == ResearchTaskStatus.COMPLETED)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.task_results if r.status == ResearchTaskStatus.FAILED)

    @property
    def overall_external_status(self) -> str:
        if not self.task_results:
            return "skipped"
        completed = self.completed_count
        failed = self.failed_count
        if failed == 0 and completed > 0:
            return "succeeded"
        if failed > 0 and completed == 0:
            return "failed"
        if failed > 0 or completed > 0:
            return "partial"
        return "skipped"

    def to_stage_dict(self) -> dict[str, Any]:
        task_count = len(self.task_results)
        task_source_counts: dict[str, int] = {}
        event_task_count = 0
        total_evidence = 0
        total_attempts = 0
        total_selected = 0
        total_elapsed = 0.0
        last_error_type: Optional[str] = None
        last_error: Optional[str] = None
        status_counts: dict[str, int] = {}

        for r in self.task_results:
            src = r.task_spec.task_source or "unknown"
            task_source_counts[src] = task_source_counts.get(src, 0) + 1
            if src == "event":
                event_task_count += 1
            total_evidence += r.evidence_count
            total_attempts += r.attempt_count
            total_selected += r.selected_count
            total_elapsed += r.elapsed_seconds
            if r.last_error_type:
                last_error_type = r.last_error_type
                last_error = r.last_error
            st = r.external_llm_status or "unknown"
            status_counts[st] = status_counts.get(st, 0) + 1

        base = {
            "task_count": task_count,
            "task_source_counts": task_source_counts,
            "event_task_count": event_task_count,
            "snapshot_task_count": int(task_source_counts.get("snapshot", 0)),
            "event_evidence_count": total_evidence,
            "completed_task_count": self.completed_count,
            "failed_task_count": self.failed_count,
            "generated_count": len(self.generated_candidates),
            "experiment_count": len(self.experiments),
            "task_run_ids": [
                r.task_run_id for r in self.task_results if r.task_run_id is not None
            ],
            "external_llm_status": self.overall_external_status,
            "external_llm_status_counts": status_counts,
            "external_llm_attempt_count": total_attempts,
            "external_llm_selected_count": total_selected,
            "external_llm_last_error_type": last_error_type,
            "external_llm_last_error": last_error,
            "external_llm_elapsed_seconds": round(total_elapsed, 4),
            "persistence_failures": self.persistence_failures,
            "persistence_failure_count": len(self.persistence_failures),
            "task_results": [r.to_dict() for r in self.task_results],
        }
        payload = dict(self.stage_payload or {})
        payload.update(base)
        return payload


__all__ = [
    "ResearchTaskSpec",
    "ResearchTaskResult",
    "ResearchTaskStatus",
    "ResearchBatchResult",
]
