"""Signal Tracker contracts and DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class SignalTrackerResult:
    """Result of a signal tracker run cycle."""

    signals_generated: int
    signal_event_snapshots: int
    forward_returns_computed: int
    incubation_orders: int
    incubation_orders_filled: int
    incubation_nav_snapshots: int
    incubation_metrics: int
    incubation_pipeline_snapshots: int
    incubation_auto_promotions: int
    submitted_runtime_pipeline_snapshots: int
    risk_events: int
    risk_actions: int
    transitions: int
    vector_registry_updates: int
    projection_snapshots: int
    skipped_runtime_controls: int
    task_run_id: int | None
    errors: list[str]
    phase_results: dict[str, Any]
    phase_timeout_count: int = 0
    phase_timeouts: list[str] = None
    phase_error_count: int = 0
    phase_errors: list[str] = None
    runtime_universe: dict[str, int] = None
    timeout: bool = False
    elapsed_seconds: float = 0.0

    def __post_init__(self):
        if self.phase_timeouts is None:
            self.phase_timeouts = []
        if self.phase_errors is None:
            self.phase_errors = []
        if self.runtime_universe is None:
            self.runtime_universe = {
                "strategies": 0,
                "executable": 0,
                "submitted_runtime": 0,
                "paper_runtime": 0,
            }
