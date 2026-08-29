"""Candidate domain models for the strategy factory pipeline.

These dataclasses provide a stable object model for the candidate lifecycle:
  spawn → autonomy enrichment → gate-0/1/2 → dedup → gate-3 submission.

P4 implementation: explicit domain layer replaces ad-hoc dict manipulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CandidateSource(str, Enum):
    SNAPSHOT = "snapshot"
    AUTONOMY = "autonomy"
    EVENT = "event"
    REVISION = "revision"
    UNKNOWN = "unknown"


class CandidateLifecycleStage(str, Enum):
    SPAWNED = "spawned"
    GATE_0 = "gate_0"
    GATE_1 = "gate_1"
    GATE_2_BACKTEST = "gate_2_backtest"
    DEDUP = "dedup"
    GATE_3_SUBMISSION = "gate_3_submission"
    SUBMITTED = "submitted"
    REJECTED = "rejected"


@dataclass
class CandidateSpec:
    """Minimal description of a candidate entering the pipeline."""

    strategy_type: str
    params: dict[str, Any] = field(default_factory=dict)
    source: CandidateSource = CandidateSource.UNKNOWN
    tags: list[str] = field(default_factory=list)
    target_symbols: list[str] = field(default_factory=list)
    stock_pool: dict[str, Any] = field(default_factory=dict)
    research_task: dict[str, Any] = field(default_factory=dict)
    event_context: dict[str, Any] = field(default_factory=dict)
    task_run_id: Optional[str] = None
    factory_attempt_count: int = 0
    factory_selected_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateSpec":
        source_raw = str(data.get("source") or "").strip().lower() or "unknown"
        try:
            source = CandidateSource(source_raw)
        except ValueError:
            source = CandidateSource.UNKNOWN
        return cls(
            strategy_type=str(data.get("strategy_type") or "").strip(),
            params=dict(data.get("params") or {}),
            source=source,
            tags=list(data.get("tags") or []),
            target_symbols=list(data.get("target_symbols") or []),
            stock_pool=dict(data.get("stock_pool") or {}),
            research_task=dict(data.get("research_task") or {}),
            event_context=dict(data.get("event_context") or {}),
            task_run_id=data.get("task_run_id"),
            factory_attempt_count=int(data.get("factory_attempt_count") or 0),
            factory_selected_count=int(data.get("factory_selected_count") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_type": self.strategy_type,
            "params": self.params,
            "source": self.source.value,
            "tags": self.tags,
            "target_symbols": self.target_symbols,
            "stock_pool": self.stock_pool,
            "research_task": self.research_task,
            "event_context": self.event_context,
            "task_run_id": self.task_run_id,
            "factory_attempt_count": self.factory_attempt_count,
            "factory_selected_count": self.factory_selected_count,
        }


@dataclass
class CandidateEvaluation:
    """Result of gate evaluation for a single candidate."""

    strategy_type: str
    gate: str
    passed: bool
    score: Optional[float] = None
    failure_reason: Optional[str] = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateEvaluation":
        return cls(
            strategy_type=str(data.get("strategy_type") or "").strip(),
            gate=str(data.get("gate") or "").strip(),
            passed=bool(data.get("passed")),
            score=data.get("score"),
            failure_reason=data.get("failure_reason"),
            metrics=dict(data.get("metrics") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_type": self.strategy_type,
            "gate": self.gate,
            "passed": self.passed,
            "score": self.score,
            "failure_reason": self.failure_reason,
            "metrics": self.metrics,
        }


@dataclass
class CandidateDedupDecision:
    """Result of the deduplication check for a single candidate."""

    strategy_type: str
    is_duplicate: bool
    duplicate_of: Optional[str] = None
    similarity_score: Optional[float] = None
    dedup_reason: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateDedupDecision":
        return cls(
            strategy_type=str(data.get("strategy_type") or "").strip(),
            is_duplicate=bool(data.get("is_duplicate")),
            duplicate_of=data.get("duplicate_of"),
            similarity_score=data.get("similarity_score"),
            dedup_reason=data.get("dedup_reason"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_type": self.strategy_type,
            "is_duplicate": self.is_duplicate,
            "duplicate_of": self.duplicate_of,
            "similarity_score": self.similarity_score,
            "dedup_reason": self.dedup_reason,
        }


@dataclass
class CandidateSubmissionDecision:
    """Gate-3 submission outcome for a single candidate."""

    strategy_type: str
    submitted: bool
    passed_quality_gate: bool
    provisional: bool = False
    failure_reason: Optional[str] = None
    strategy_id: Optional[str] = None
    gate_3_score: Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateSubmissionDecision":
        return cls(
            strategy_type=str(data.get("strategy_type") or "").strip(),
            submitted=bool(data.get("submitted")),
            passed_quality_gate=bool(data.get("passed_quality_gate")),
            provisional=bool(data.get("provisional")),
            failure_reason=data.get("failure_reason"),
            strategy_id=data.get("strategy_id"),
            gate_3_score=data.get("gate_3_score"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_type": self.strategy_type,
            "submitted": self.submitted,
            "passed_quality_gate": self.passed_quality_gate,
            "provisional": self.provisional,
            "failure_reason": self.failure_reason,
            "strategy_id": self.strategy_id,
            "gate_3_score": self.gate_3_score,
        }


@dataclass
class CandidatePipelineReport:
    """Aggregated report for a complete candidate pipeline run."""

    total_spawned: int = 0
    autonomy_generated: int = 0
    gate_0_passed: int = 0
    gate_0_failed: int = 0
    pre_gate_passed: int = 0
    pre_gate_failed: int = 0
    gate_1_passed: int = 0
    gate_1_failed: int = 0
    gate_2_passed: int = 0
    gate_2_failed: int = 0
    after_dedup: int = 0
    gate_3_passed: int = 0
    gate_3_failed: int = 0
    gate_3_provisional: int = 0
    submitted: int = 0
    failure_reason_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_spawned": self.total_spawned,
            "autonomy_generated": self.autonomy_generated,
            "gate_0_passed": self.gate_0_passed,
            "gate_0_failed": self.gate_0_failed,
            "pre_gate_passed": self.pre_gate_passed,
            "pre_gate_failed": self.pre_gate_failed,
            "gate_1_passed": self.gate_1_passed,
            "gate_1_failed": self.gate_1_failed,
            "gate_2_passed": self.gate_2_passed,
            "gate_2_failed": self.gate_2_failed,
            "after_dedup": self.after_dedup,
            "gate_3_passed": self.gate_3_passed,
            "gate_3_failed": self.gate_3_failed,
            "gate_3_provisional": self.gate_3_provisional,
            "submitted": self.submitted,
            "failure_reason_counts": self.failure_reason_counts,
        }


__all__ = [
    "CandidateSource",
    "CandidateLifecycleStage",
    "CandidateSpec",
    "CandidateEvaluation",
    "CandidateDedupDecision",
    "CandidateSubmissionDecision",
    "CandidatePipelineReport",
]
