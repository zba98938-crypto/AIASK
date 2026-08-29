"""Strategy factory application services.

Each service handles a single stage of the factory pipeline:

- ReadinessService    – evaluates snapshot + factor readiness
- TaskOrchestrator   – schedules and runs autonomy research tasks
- CandidatePipeline  – spawn → gate-0/1/2 → dedup → gate-3 submission
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AlphaResearchService",
    "CandidatePipeline",
    "StrategyLifecycleCoordinator",
    "ReadinessService",
    "SubmissionAdmissionAuthority",
    "TaskOrchestrator",
]

_LAZY_EXPORTS = {
    "AlphaResearchService": ".alpha_research_service",
    "CandidatePipeline": ".candidate_pipeline",
    "StrategyLifecycleCoordinator": ".lifecycle_coordinator",
    "ReadinessService": ".readiness_service",
    "SubmissionAdmissionAuthority": ".admission_authority",
    "TaskOrchestrator": ".task_orchestrator",
}


def __getattr__(name: str) -> Any:
    try:
        module_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
