"""Helpers for attaching artifact contracts to autonomy stage summaries."""

from __future__ import annotations

from typing import Any, Callable

from .candidate_contract import apply_resolved_candidate_envelope


def attach_autonomy_stage_artifacts(
    *,
    stage: dict[str, Any],
    scan_task_artifact: dict[str, Any],
    bulk_task_artifact: dict[str, Any],
    generated_candidates: list[dict[str, Any]],
    all_experiments: list[dict[str, Any]],
    build_task_artifact: Callable[[dict[str, Any]], dict[str, Any]],
    build_candidate_artifact: Callable[[list[dict[str, Any]]], dict[str, Any]],
    build_research_evidence_artifact: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(stage or {})
    scan_artifact = dict(scan_task_artifact or {})
    bulk_artifact = dict(bulk_task_artifact or {})
    task_artifact = build_task_artifact(payload)
    resolved_candidates = [
        apply_resolved_candidate_envelope(dict(candidate or {}))
        for candidate in list(generated_candidates or [])
        if isinstance(candidate, dict)
    ]
    candidate_artifact = build_candidate_artifact(resolved_candidates)
    evidence_artifact = build_research_evidence_artifact(
        payload,
        experiments=list(all_experiments or []),
    )
    payload.update(
        {
            "scan_task_artifact": scan_artifact,
            "bulk_task_artifact": bulk_artifact,
            "task_artifact": task_artifact,
            "candidate_artifact": candidate_artifact,
            "evidence_artifact": evidence_artifact,
            "scan_task_artifact_contract_version": scan_artifact.get("contract_version"),
            "scan_task_artifact_available": bool(scan_artifact.get("available")),
            "bulk_task_artifact_contract_version": bulk_artifact.get("contract_version"),
            "bulk_task_artifact_available": bool(bulk_artifact.get("available")),
            "task_artifact_contract_version": task_artifact.get("contract_version"),
            "task_artifact_available": bool(task_artifact.get("available")),
            "candidate_artifact_contract_version": candidate_artifact.get("contract_version"),
            "candidate_artifact_available": bool(candidate_artifact.get("available")),
            "evidence_artifact_contract_version": evidence_artifact.get("contract_version"),
            "evidence_artifact_available": bool(evidence_artifact.get("available")),
        }
    )
    return payload
