"""Research-plane contract exports.

Keeps the physical research package as the P3 entry point while preserving the
existing top-level contract module as the implementation location.
"""

from __future__ import annotations

from ..research_plane_contract import (
    CANDIDATE_ARTIFACT_CONTRACT_VERSION,
    RESEARCH_ARTIFACT_CONTRACT_VERSION,
    RESEARCH_EVIDENCE_ARTIFACT_CONTRACT_VERSION,
    RESEARCH_PLANE_CONTRACT_VERSION,
    TASK_ARTIFACT_CONTRACT_VERSION,
    build_candidate_artifact,
    build_research_artifact,
    build_research_evidence_artifact,
    build_research_plane_artifact,
    build_task_artifact,
)

__all__ = [
    "CANDIDATE_ARTIFACT_CONTRACT_VERSION",
    "RESEARCH_ARTIFACT_CONTRACT_VERSION",
    "RESEARCH_EVIDENCE_ARTIFACT_CONTRACT_VERSION",
    "RESEARCH_PLANE_CONTRACT_VERSION",
    "TASK_ARTIFACT_CONTRACT_VERSION",
    "build_candidate_artifact",
    "build_research_artifact",
    "build_research_evidence_artifact",
    "build_research_plane_artifact",
    "build_task_artifact",
]
