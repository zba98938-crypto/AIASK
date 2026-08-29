"""Factor Mining contracts and DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FactorMiningResult:
    """Result of a factor mining cycle."""

    success: bool
    run_id: str
    trigger: str
    started_at: str
    completed_at: str
    raw_candidate_count: int
    evolved_count: int
    validated_count: int
    admitted_count: int
    quarantine_count: int
    active_promoted_count: int
    cycle_active_promoted_count: int = 0
    reappraisal_promoted_count: int = 0
    pool_size: int = 0
    engines_used: list[str] = field(default_factory=list)
    validation_universe_health: dict[str, Any] = field(default_factory=dict)
    quality_summary: dict[str, Any] = field(default_factory=dict)
    quarantine_reappraisal: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False
    reason: str | None = None
    error: str | None = None
