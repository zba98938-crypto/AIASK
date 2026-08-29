"""Success-path run summary builder for factory cycles.

P2 refactor: extract the large success summary assembly from ``cycle_runner`` so
the runner focuses on orchestration rather than payload shaping.
"""

from __future__ import annotations

from typing import Any

from .services.readiness_service import (
    resolve_governed_pool_state,
    resolve_market_temperature_context,
)

_VALIDATION_GRADE_ORDER: dict[str, int] = {
    "D": 0,
    "C": 1,
    "B": 2,
    "A": 3,
    "S": 4,
    "SS": 5,
    "SSS": 6,
}

def _walk_dict_values(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dict_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dict_values(child)


def _aggregate_validation_grade_counts(submit_result: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for strategy in list(submit_result.get("strategies") or []):
        payload = dict(strategy or {})
        grade = _strategy_validation_grade(payload)
        if grade:
            counts[grade] = counts.get(grade, 0) + 1
    return counts


def _strategy_validation_grade(payload: dict[str, Any]) -> str:
    return str(
        payload.get("effective_validation_grade")
        or payload.get("validation_grade")
        or payload.get("raw_validation_grade")
        or ""
    ).strip().upper()


def _is_quality_production_record(payload: dict[str, Any]) -> bool:
    item = dict(payload or {})
    if item.get("gate3_quality_recorded") or item.get("gate3_record_quality_qualified"):
        return True
    if item.get("created_strategy_pool") or item.get("submitted"):
        return True
    status = str(item.get("status") or "").strip().lower()
    return status in {"submitted", "incubating", "listed"} and not bool(
        item.get("record_only") or item.get("gate3_record_only") or item.get("created_audit_only")
    )


def _aggregate_quality_validation_grade_counts(submit_result: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for strategy in list(submit_result.get("strategies") or []):
        payload = dict(strategy or {})
        if not _is_quality_production_record(payload):
            continue
        grade = _strategy_validation_grade(payload)
        if grade:
            counts[grade] = counts.get(grade, 0) + 1
    return counts


def _aggregate_diagnostic_validation_grade_counts(submit_result: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for strategy in list(submit_result.get("strategies") or []):
        payload = dict(strategy or {})
        if _is_quality_production_record(payload):
            continue
        grade = _strategy_validation_grade(payload)
        if grade:
            counts[grade] = counts.get(grade, 0) + 1
    return counts


def _top_validation_grade(counts: dict[str, int]) -> str | None:
    grades = [
        str(grade or "").strip().upper()
        for grade, count in dict(counts or {}).items()
        if _safe_int(count) > 0 and str(grade or "").strip().upper() in _VALIDATION_GRADE_ORDER
    ]
    if not grades:
        return None
    return max(grades, key=lambda grade: _VALIDATION_GRADE_ORDER.get(grade, -1))


def _aggregate_statistical_metric_missing_counts(submit_result: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for payload in _walk_dict_values(submit_result):
        for key, count in dict(payload.get("statistical_metric_missing_counts") or {}).items():
            token = str(key or "").strip()
            if token:
                counts[token] = counts.get(token, 0) + int(count or 0)
        for key in list(payload.get("missing_statistical_metrics") or []):
            token = str(key or "").strip()
            if token:
                counts[token] = counts.get(token, 0) + 1
    return counts


def _aggregate_metric_source_audit(submit_result: dict[str, Any]) -> dict[str, dict[str, int]]:
    audit_counts: dict[str, dict[str, int]] = {}
    for payload in _walk_dict_values(submit_result):
        audit = dict(payload.get("metric_source_audit") or {})
        for metric_name, source in audit.items():
            metric = str(metric_name or "").strip()
            source_name = str(source or "unknown").strip() or "unknown"
            if not metric:
                continue
            metric_bucket = audit_counts.setdefault(metric, {})
            metric_bucket[source_name] = metric_bucket.get(source_name, 0) + 1
    return audit_counts


def _aggregate_submission_lane_counts(submit_result: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for strategy in list(submit_result.get("strategies") or []):
        payload = dict(strategy or {})
        lane = str(payload.get("submission_lane") or "").strip().lower()
        if lane:
            counts[lane] = counts.get(lane, 0) + 1
    return counts


def _aggregate_submission_action_type_counts(submit_result: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for strategy in list(submit_result.get("strategies") or []):
        payload = dict(strategy or {})
        action_type = str(payload.get("submission_action_type") or "").strip().lower()
        if action_type:
            counts[action_type] = counts.get(action_type, 0) + 1
    return counts


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _resolve_event_task_count(
    autonomy_summary: dict[str, Any],
    task_scan_summary: dict[str, Any],
    task_source_counts: dict[str, Any],
) -> int:
    """Prefer explicit counts, but keep selected event-driven tasks visible.

    A real event scan can select event-driven tasks while later autonomy work
    is skipped or produces no candidates. In that path the autonomy summary may
    report zero, but the scan/source counts still prove Strategy Factory had
    real event tasks to consume.
    """

    return max(
        _safe_int(autonomy_summary.get("event_task_count")),
        _safe_int(task_scan_summary.get("event_task_count")),
        _safe_int(task_source_counts.get("event_driven")),
    )


def _add_count(counts: dict[str, int], key: str, value: Any) -> None:
    count = _safe_int(value)
    if count > 0:
        counts[key] = counts.get(key, 0) + count


def _set_count_at_least(counts: dict[str, int], key: str, value: Any) -> None:
    count = _safe_int(value)
    if count > 0:
        counts[key] = max(_safe_int(counts.get(key)), count)


def _build_llm_status_counts(autonomy_summary: dict[str, Any]) -> dict[str, int]:
    counts = dict(
        autonomy_summary.get("llm_status_counts")
        or autonomy_summary.get("external_llm_status_counts")
        or {}
    )
    request_status_counts = dict(autonomy_summary.get("external_llm_request_status_counts") or {})

    _set_count_at_least(counts, "provider_cooldown_skip", request_status_counts.get("cooldown_skip"))
    error_text = " ".join(
        str(autonomy_summary.get(key) or "")
        for key in ("external_llm_last_error_type", "external_llm_last_error")
    ).lower()
    failed_count = _safe_int(request_status_counts.get("failed"))
    if failed_count > 0:
        if "502" in error_text or "bad gateway" in error_text:
            _set_count_at_least(counts, "provider_http_502", failed_count)
        elif "5xx" in error_text or "server error" in error_text:
            _set_count_at_least(counts, "provider_http_5xx", failed_count)
        elif error_text:
            _set_count_at_least(counts, "provider_error", failed_count)
    return counts


def _build_autonomy_task_briefs(autonomy_summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "task_id": (item.get("task") or {}).get("task_id"),
            "task_source": (item.get("task") or {}).get("task_source"),
            "opportunity_type": (item.get("task") or {}).get("opportunity_type"),
            "target_symbols": list((item.get("task") or {}).get("target_symbols") or []),
            "candidate_family": (item.get("task") or {}).get("candidate_family"),
            "source_candidate_artifact_id": (item.get("task") or {}).get("source_candidate_artifact_id"),
            "factor_name": (item.get("task") or {}).get("factor_name"),
            "generation_limit": (item.get("task") or {}).get("generation_limit"),
            "generated_count": item.get("generated_count", 0),
        }
        for item in list(autonomy_summary.get("task_results") or [])
    ]
