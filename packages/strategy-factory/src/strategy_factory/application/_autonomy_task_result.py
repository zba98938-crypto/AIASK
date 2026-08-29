"""Helpers for autonomy per-task request metrics and result payloads."""

from __future__ import annotations

import math
from typing import Any, Callable


def build_external_request_metrics(
    requests: list[dict[str, Any]],
    *,
    summarize_request_status_counts: Callable[[list[dict[str, Any]]], dict[str, int]],
    count_network_requests: Callable[[list[dict[str, Any]]], int],
    count_real_requests: Callable[[list[dict[str, Any]]], int],
    request_is_compatibility_failure: Callable[[dict[str, Any]], bool],
    request_is_empty_200_response: Callable[[dict[str, Any]], bool],
    normalize_request_status: Callable[[Any], str],
) -> dict[str, Any]:
    task_requests = list(requests or [])
    request_status_counts = dict(summarize_request_status_counts(task_requests) or {})
    attempt_count = len(task_requests)
    network_request_count = int(count_network_requests(task_requests) or 0)
    real_request_count = int(count_real_requests(task_requests) or 0)
    compatibility_skip_count = int(request_status_counts.get("compatibility_skip", 0) or 0)
    cooldown_skip_count = int(request_status_counts.get("cooldown_skip", 0) or 0)
    compatibility_failure_count = sum(
        1 for item in task_requests if request_is_compatibility_failure(dict(item or {}))
    )
    effective_response_count = sum(
        1
        for item in task_requests
        if normalize_request_status(dict(item or {}).get("status")) == "succeeded"
    )
    empty_200_response_count = sum(
        1 for item in task_requests if request_is_empty_200_response(dict(item or {}))
    )
    return {
        "attempt_count": attempt_count,
        "network_request_count": network_request_count,
        "real_request_count": real_request_count,
        "compatibility_skip_count": compatibility_skip_count,
        "cooldown_skip_count": cooldown_skip_count,
        "compatibility_failure_count": compatibility_failure_count,
        "effective_response_count": effective_response_count,
        "empty_200_response_count": empty_200_response_count,
        "compatibility_failure_ratio": (
            round(compatibility_failure_count / real_request_count, 4) if real_request_count else 0.0
        ),
        "effective_response_ratio": (
            round(effective_response_count / real_request_count, 4) if real_request_count else 0.0
        ),
        "request_status_counts": request_status_counts,
    }


def build_completed_task_result(
    *,
    enriched_task: dict[str, Any],
    task_run_id: Any,
    evidence_count: int,
    generated_count: int,
    reviewed_count: int,
    external_status: str,
    llm_generation: dict[str, Any],
    lifecycle: dict[str, Any],
    lifecycle_summary: dict[str, Any],
    request_metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task": dict(enriched_task or {}),
        "task_run_id": task_run_id,
        "task_source": enriched_task.get("task_source"),
        "event_id": enriched_task.get("event_id"),
        "theme_code": enriched_task.get("theme_code"),
        "evidence_count": int(evidence_count or 0),
        "status": "completed",
        "generated_count": int(generated_count or 0),
        "reviewed_count": int(reviewed_count or 0),
        "external_llm_status": external_status,
        "external_llm_attempt_count": int(request_metrics.get("attempt_count") or 0),
        "external_llm_network_request_count": int(request_metrics.get("network_request_count") or 0),
        "external_llm_real_request_count": int(request_metrics.get("real_request_count") or 0),
        "external_llm_compatibility_skip_count": int(request_metrics.get("compatibility_skip_count") or 0),
        "external_llm_cooldown_skip_count": int(request_metrics.get("cooldown_skip_count") or 0),
        "external_llm_compatibility_failure_count": int(
            request_metrics.get("compatibility_failure_count") or 0
        ),
        "external_llm_effective_response_count": int(
            request_metrics.get("effective_response_count") or 0
        ),
        "external_llm_empty_200_response_count": int(
            request_metrics.get("empty_200_response_count") or 0
        ),
        "external_llm_compatibility_failure_ratio": float(
            request_metrics.get("compatibility_failure_ratio") or 0.0
        ),
        "external_llm_effective_response_ratio": float(
            request_metrics.get("effective_response_ratio") or 0.0
        ),
        "external_llm_request_status_counts": dict(request_metrics.get("request_status_counts") or {}),
        "llm_generation": dict(llm_generation or {}),
        "lifecycle": dict(lifecycle or {}),
        "lifecycle_summary": dict(lifecycle_summary or {}),
    }


def build_failed_task_result(
    *,
    enriched_task: dict[str, Any],
    task_run_id: Any,
    evidence_count: int,
    error: str,
    lifecycle: dict[str, Any],
    lifecycle_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task": dict(enriched_task or {}),
        "task_run_id": task_run_id,
        "task_source": enriched_task.get("task_source"),
        "event_id": enriched_task.get("event_id"),
        "theme_code": enriched_task.get("theme_code"),
        "evidence_count": int(evidence_count or 0),
        "status": "failed",
        "generated_count": 0,
        "error": str(error or ""),
        "lifecycle": dict(lifecycle or {}),
        "lifecycle_summary": dict(lifecycle_summary or {}),
    }


def enrich_candidates_with_task_metrics(
    candidates: list[dict[str, Any]],
    *,
    enriched_task: dict[str, Any],
    enrich_candidate_targeting: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    request_metrics: dict[str, Any],
    selected_count: int,
) -> list[dict[str, Any]]:
    enriched_candidates: list[dict[str, Any]] = []
    task_local_attempt_count = int(
        request_metrics.get("real_request_count")
        or request_metrics.get("effective_response_count")
        or request_metrics.get("attempt_count")
        or 0
    )
    candidate_local_attempt_count = (
        max(1, int(math.ceil(task_local_attempt_count / max(int(selected_count or 0), 1))))
        if task_local_attempt_count > 0 and int(selected_count or 0) > 0
        else task_local_attempt_count
    )
    for candidate in list(candidates or []):
        enriched = enrich_candidate_targeting(candidate, enriched_task)
        params = dict(enriched.get("params") or {})
        params["task_attempt_count"] = int(request_metrics.get("attempt_count") or 0)
        params["task_stage_attempt_count"] = int(request_metrics.get("attempt_count") or 0)
        params["candidate_local_attempt_count"] = int(candidate_local_attempt_count or 0)
        params["task_local_attempt_count"] = int(task_local_attempt_count or 0)
        params["candidate_local_selected_count"] = 1 if int(selected_count or 0) > 0 else 0
        params["task_local_selected_count"] = int(selected_count or 0)
        params["task_network_request_count"] = int(request_metrics.get("network_request_count") or 0)
        params["task_real_request_count"] = int(request_metrics.get("real_request_count") or 0)
        params["task_compatibility_skip_count"] = int(request_metrics.get("compatibility_skip_count") or 0)
        params["task_cooldown_skip_count"] = int(request_metrics.get("cooldown_skip_count") or 0)
        params["task_compatibility_failure_count"] = int(
            request_metrics.get("compatibility_failure_count") or 0
        )
        params["task_effective_response_count"] = int(
            request_metrics.get("effective_response_count") or 0
        )
        params["task_empty_200_response_count"] = int(
            request_metrics.get("empty_200_response_count") or 0
        )
        params["task_compatibility_failure_ratio"] = float(
            request_metrics.get("compatibility_failure_ratio") or 0.0
        )
        params["task_effective_response_ratio"] = float(
            request_metrics.get("effective_response_ratio") or 0.0
        )
        params["task_selected_count"] = int(selected_count or 0)
        enriched["params"] = params
        enriched_candidates.append(enriched)
    return enriched_candidates
