"""Research-plane candidate and task origin helpers.

P3 goal: make local-rule generation, external autonomy generation, and
governed candidate activation explicit in research artifacts instead of leaving
them implicit in ad-hoc generator/task fields.
"""

from __future__ import annotations

from typing import Any

LOCAL_RULE_CANDIDATE_ORIGIN = "local_rule"
EXTERNAL_AUTONOMY_CANDIDATE_ORIGIN = "external_autonomy"
GOVERNED_CANDIDATE_ACTIVATION_ORIGIN = "governed_candidate_activation"
UNKNOWN_RESEARCH_CANDIDATE_ORIGIN = "unknown"
OPEN_RESEARCH_TASK_ORIGIN = "open_research"

_EXTERNAL_AUTONOMY_GENERATOR_MODES = frozenset(
    {
        "external_llm",
        "pipeline_staged",
        "llm_proxy",
        "llm_proxy_fallback",
        "rl_bandit",
    }
)


def _string(value: Any) -> str:
    return str(value or "").strip()


def _normalized_text(value: Any) -> str:
    return _string(value).lower()


def _source_candidate_artifact_id(payload: dict[str, Any]) -> str:
    params = dict(payload.get("params") or {})
    research_task = dict(payload.get("research_task") or {})
    candidate_provenance = dict(
        payload.get("candidate_provenance")
        or params.get("candidate_provenance")
        or {}
    )
    for source in (payload, research_task, params, candidate_provenance):
        token = _string(source.get("source_candidate_artifact_id"))
        if token:
            return token
    return ""


def classify_research_candidate_origin(candidate: dict[str, Any] | None) -> str:
    payload = dict(candidate or {})
    params = dict(payload.get("params") or {})
    research_task = dict(payload.get("research_task") or {})
    candidate_provenance = dict(
        payload.get("candidate_provenance")
        or params.get("candidate_provenance")
        or {}
    )
    if _source_candidate_artifact_id(payload):
        return GOVERNED_CANDIDATE_ACTIVATION_ORIGIN

    generator_mode = (
        _normalized_text(payload.get("generator_mode"))
        or _normalized_text(payload.get("generator_type"))
        or _normalized_text(params.get("generator_type"))
        or _normalized_text(candidate_provenance.get("generator_mode"))
        or _normalized_text(candidate_provenance.get("generator_type"))
    )
    if generator_mode in _EXTERNAL_AUTONOMY_GENERATOR_MODES:
        return EXTERNAL_AUTONOMY_CANDIDATE_ORIGIN

    has_autonomy_contract = bool(
        _string(payload.get("experiment_id"))
        or _string(params.get("experiment_id"))
        or _string(payload.get("task_run_id"))
        or _string(params.get("task_run_id"))
        or _string(candidate_provenance.get("task_id"))
        or _string(research_task.get("task_id"))
        or _string(research_task.get("task_key"))
        or _string(research_task.get("event_id"))
    )
    if has_autonomy_contract:
        return EXTERNAL_AUTONOMY_CANDIDATE_ORIGIN

    if payload.get("generation_reason") or _string(payload.get("spawn_reason")):
        return LOCAL_RULE_CANDIDATE_ORIGIN

    if generator_mode:
        return LOCAL_RULE_CANDIDATE_ORIGIN

    return UNKNOWN_RESEARCH_CANDIDATE_ORIGIN


def classify_research_task_origin(task: dict[str, Any] | None) -> str:
    payload = dict(task or {})
    return (
        GOVERNED_CANDIDATE_ACTIVATION_ORIGIN
        if _source_candidate_artifact_id(payload)
        else OPEN_RESEARCH_TASK_ORIGIN
    )


def count_candidate_origins(candidates: list[dict[str, Any]] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in list(candidates or []):
        origin = classify_research_candidate_origin(dict(item or {}))
        counts[origin] = counts.get(origin, 0) + 1
    return counts


def count_task_origins(tasks: list[dict[str, Any]] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in list(tasks or []):
        origin = classify_research_task_origin(dict(item or {}))
        counts[origin] = counts.get(origin, 0) + 1
    return counts


__all__ = [
    "EXTERNAL_AUTONOMY_CANDIDATE_ORIGIN",
    "GOVERNED_CANDIDATE_ACTIVATION_ORIGIN",
    "LOCAL_RULE_CANDIDATE_ORIGIN",
    "OPEN_RESEARCH_TASK_ORIGIN",
    "UNKNOWN_RESEARCH_CANDIDATE_ORIGIN",
    "classify_research_candidate_origin",
    "classify_research_task_origin",
    "count_candidate_origins",
    "count_task_origins",
]
