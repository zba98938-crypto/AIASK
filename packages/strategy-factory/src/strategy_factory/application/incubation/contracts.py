"""Incubation Factory public contracts owned by Strategy Factory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class IncubationPhaseResult:
    """Result of a single incubation phase."""

    phase_name: str
    success: bool
    strategies_processed: int
    duration_sec: float
    error: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class PromotionDecision:
    """Promotion decision for a strategy in incubation."""

    strategy_id: str
    should_promote: bool
    reason: str
    blocker_type: str | None = None
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class BlockerSummary:
    """Summary of blockers preventing strategy promotion."""

    strategy_id: str
    blocker_count: int
    blocker_types: list[str]
    primary_blocker: str | None = None
    resolution_hint: str | None = None


@dataclass(frozen=True)
class IncubationRunSummary:
    """Summary of one complete incubation cycle run."""

    run_id: str
    trigger: str
    start_time: datetime
    end_time: datetime
    duration_sec: float
    status: str  # 'completed', 'partial', 'failed'
    phases: list[IncubationPhaseResult]
    strategies_intake: int
    strategies_verified: int
    strategies_promoted: int
    paper_orders_filled: int
    paper_orders_rejected: int
    phase_failures: list[dict[str, Any]]
    error: str | None = None


__all__ = [
    "BlockerSummary",
    "IncubationPhaseResult",
    "IncubationRunSummary",
    "PromotionDecision",
]
