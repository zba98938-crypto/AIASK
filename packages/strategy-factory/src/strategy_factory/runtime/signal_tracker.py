"""Canonical SignalTracker runtime owned by Strategy Factory.

This module provides the runtime wrapper and lifecycle management.
The actual phase orchestration logic has been migrated to the application layer.
"""

from __future__ import annotations

from datetime import date, time as dt_time
from typing import Any

from ..application.signal_tracker import SignalTrackerOrchestrator
from ..infrastructure.runtime_services import (
    get_signal_tracker_runtime_factory,
    get_signal_tracker_runtime_support_factory,
)
from .provider_adapters import LegacySignalTrackerProvider


class SignalTrackerRuntime:
    """Host-neutral runtime with Strategy Factory-owned SignalTracker orchestration.

    This class now delegates to the application-layer orchestrator for actual
    phase execution logic. The support object is wrapped as a provider.
    """

    def __init__(self, support: Any):
        self._support = support
        provider = support
        if support is not None and not callable(getattr(support, "get_db", None)):
            provider = LegacySignalTrackerProvider(support)
        self._orchestrator = SignalTrackerOrchestrator(provider) if provider else None

    def preflight(self) -> dict[str, Any]:
        return {
            "available": self._support is not None,
            "runtime_type": type(self._support).__name__ if self._support is not None else None,
        }

    def status(self) -> dict[str, Any]:
        status = getattr(self._support, "status", None)
        if callable(status):
            return dict(status() or {})
        return self.preflight()

    def start(self) -> Any:
        starter = getattr(self._support, "start", None)
        if callable(starter):
            return starter()
        return None

    def stop(self) -> Any:
        stopper = getattr(self._support, "stop", None)
        if callable(stopper):
            return stopper()
        return None

    async def shutdown(self, grace_sec: float = 5.0) -> Any:
        shutdown = getattr(self._support, "shutdown", None)
        if callable(shutdown):
            return await shutdown(grace_sec=grace_sec)
        return None

    async def run_once(self, *, as_of: date | None = None) -> dict[str, Any]:
        """Execute one complete signal tracking cycle.

        Now delegates to the application-layer orchestrator which owns the
        phase A-H execution logic.
        """
        if self._orchestrator is None:
            return {
                "signals_generated": 0,
                "signal_event_snapshots": 0,
                "forward_returns_computed": 0,
                "incubation_orders": 0,
                "incubation_orders_filled": 0,
                "incubation_nav_snapshots": 0,
                "incubation_metrics": 0,
                "incubation_pipeline_snapshots": 0,
                "incubation_auto_promotions": 0,
                "submitted_runtime_pipeline_snapshots": 0,
                "risk_events": 0,
                "risk_actions": 0,
                "transitions": 0,
                "vector_registry_updates": 0,
                "projection_snapshots": 0,
                "skipped_runtime_controls": 0,
                "task_run_id": None,
                "errors": ["signal_tracker_support_missing"],
                "phase_results": {},
                "runtime_universe": {
                    "strategies": 0,
                    "executable": 0,
                    "submitted_runtime": 0,
                    "paper_runtime": 0,
                },
                "timeout": False,
                "elapsed_seconds": 0.0,
            }

        result = await self._orchestrator.run_cycle(as_of=as_of)
        phases = result.get("phase_results")
        if isinstance(phases, dict):
            aliases = {
                "A": "phase_a_load_universe",
                "B": "phase_c_forward_returns",
                "F": "phase_f_risk_scan",
            }
            for public_name, internal_name in aliases.items():
                if public_name not in phases and internal_name in phases:
                    phases[public_name] = phases[internal_name]
        return result


def build_signal_tracker_runtime(
    *,
    run_time: dt_time | None = None,
    support: Any | None = None,
) -> SignalTrackerRuntime:
    if support is None:
        support_factory = get_signal_tracker_runtime_support_factory()
        support = support_factory(run_time=run_time) if run_time else support_factory()
    return SignalTrackerRuntime(support)


def get_signal_tracker_runtime() -> SignalTrackerRuntime:
    return build_signal_tracker_runtime()


__all__ = [
    "SignalTrackerRuntime",
    "build_signal_tracker_runtime",
    "get_signal_tracker_runtime",
]
