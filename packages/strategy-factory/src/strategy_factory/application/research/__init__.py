"""Research-plane lazy exports.

The research package is the physical P3 boundary for factor research, task
planning, local spawning, and autonomy generation. Exports are lazy so the
package can be referenced from top-level contract modules without creating
import cycles.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MAP: dict[str, tuple[str, str]] = {
    "CANDIDATE_ARTIFACT_CONTRACT_VERSION": (".contracts", "CANDIDATE_ARTIFACT_CONTRACT_VERSION"),
    "EXTERNAL_AUTONOMY_CANDIDATE_ORIGIN": (".candidate_origin", "EXTERNAL_AUTONOMY_CANDIDATE_ORIGIN"),
    "FactorResearchBuilder": (".factor_research", "FactorResearchBuilder"),
    "GOVERNED_CANDIDATE_ACTIVATION_ORIGIN": (".candidate_origin", "GOVERNED_CANDIDATE_ACTIVATION_ORIGIN"),
    "LOCAL_RULE_CANDIDATE_ORIGIN": (".candidate_origin", "LOCAL_RULE_CANDIDATE_ORIGIN"),
    "MarketOpportunityScanner": (".opportunity", "MarketOpportunityScanner"),
    "OPEN_RESEARCH_TASK_ORIGIN": (".candidate_origin", "OPEN_RESEARCH_TASK_ORIGIN"),
    "RESEARCH_ARTIFACT_CONTRACT_VERSION": (".contracts", "RESEARCH_ARTIFACT_CONTRACT_VERSION"),
    "RESEARCH_EVIDENCE_ARTIFACT_CONTRACT_VERSION": (".contracts", "RESEARCH_EVIDENCE_ARTIFACT_CONTRACT_VERSION"),
    "RESEARCH_PLANE_CONTRACT_VERSION": (".contracts", "RESEARCH_PLANE_CONTRACT_VERSION"),
    "ResearchGenerationResult": (".runner", "ResearchGenerationResult"),
    "ResearchPlaneRunner": (".runner", "ResearchPlaneRunner"),
    "StockStrategyMatrixPlanner": (".matrix", "StockStrategyMatrixPlanner"),
    "StrategySpawner": (".spawner", "StrategySpawner"),
    "TASK_ARTIFACT_CONTRACT_VERSION": (".contracts", "TASK_ARTIFACT_CONTRACT_VERSION"),
    "UNKNOWN_RESEARCH_CANDIDATE_ORIGIN": (".candidate_origin", "UNKNOWN_RESEARCH_CANDIDATE_ORIGIN"),
    "build_candidate_artifact": (".contracts", "build_candidate_artifact"),
    "build_research_artifact": (".contracts", "build_research_artifact"),
    "build_research_evidence_artifact": (".contracts", "build_research_evidence_artifact"),
    "build_research_plane_artifact": (".contracts", "build_research_plane_artifact"),
    "build_task_artifact": (".contracts", "build_task_artifact"),
    "classify_research_candidate_origin": (".candidate_origin", "classify_research_candidate_origin"),
    "classify_research_task_origin": (".candidate_origin", "classify_research_task_origin"),
    "count_candidate_origins": (".candidate_origin", "count_candidate_origins"),
    "count_task_origins": (".candidate_origin", "count_task_origins"),
}

__all__ = list(_EXPORT_MAP)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORT_MAP[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name, __name__)
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
