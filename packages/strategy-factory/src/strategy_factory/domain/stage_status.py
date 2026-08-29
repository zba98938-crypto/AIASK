"""Stage-status vocabulary and run-status resolution for factory cycles.

Phase 3 of the extraction plan: this is orchestration *decision* logic, so it
belongs to Strategy Factory rather than to the AKShare host. It moved as a
closed set of pure functions — no database, no host services, no I/O — which is
what made it the safe first candidate.

The host's ``FactorScheduler`` now delegates here, keeping its ``cls._x()`` call
shape so its 12 internal call sites did not have to change.

Fail-closed is the governing rule: an unrecognised stage status becomes
``failed``, never ``completed``. The previous host default reported ``timeout``,
``cancelled`` and ``crashed`` stages as completed, which then made a broken run
resolve to ``success``.
"""

from __future__ import annotations

from typing import Any, Mapping


#: Closed stage vocabulary. Callers branch on these, so it must not grow ad hoc.
STAGE_STATUSES: tuple[str, ...] = ("completed", "partial", "skipped", "failed")

_COMPLETED_TOKENS = frozenset({"completed", "complete", "success", "succeeded", "done", "ok"})
_PARTIAL_TOKENS = frozenset({"partial", "degraded", "warning", "warn"})
_SKIPPED_TOKENS = frozenset({"skipped", "disabled", "not_needed", "noop"})
#: Explicit failure tokens. ``timeout``/``cancelled``/``crashed`` are named here
#: because they were the ones silently passing as completed.
_FAILED_TOKENS = frozenset(
    {
        "failed",
        "error",
        "timeout",
        "timed_out",
        "cancelled",
        "canceled",
        "crashed",
        "blocked",
    }
)

#: Run-level statuses this module can resolve.
RUN_STATUSES: tuple[str, ...] = ("success", "partial", "skipped", "failed")

_QUALITY_FAILED = "failed"
_QUALITY_DEGRADED_TOKENS = frozenset({"partial", "degraded"})
_QUALITY_STALE = "stale"


def normalize_stage_status(value: Any) -> str:
    """Map an arbitrary stage status onto :data:`STAGE_STATUSES`.

    Fails closed: anything unrecognised — including ``None`` and ``""`` — becomes
    ``failed`` rather than being optimistically treated as completed.
    """
    token = str(value or "").strip().lower()
    if token in _COMPLETED_TOKENS:
        return "completed"
    if token in _PARTIAL_TOKENS:
        return "partial"
    if token in _SKIPPED_TOKENS:
        return "skipped"
    if token in _FAILED_TOKENS:
        return "failed"
    return "failed"


def build_stage_result(
    stage: str,
    *,
    status: Any,
    payload: Mapping[str, Any] | None = None,
    retry_boundary: str | None = None,
) -> dict[str, Any]:
    """Assemble one stage's result record.

    ``ok`` is false only for ``failed``; ``degraded`` marks ``partial``. The
    caller's payload is preserved, with counts derived from it when absent.
    """
    normalized_status = normalize_stage_status(status)
    data = dict(payload or {})
    warnings = list(data.get("warnings") or [])
    failures = list(data.get("failures") or data.get("failed_batches") or [])
    result: dict[str, Any] = {
        "stage": stage,
        "status": normalized_status,
        "ok": normalized_status != "failed",
        "degraded": normalized_status == "partial",
        "warning_count": int(data.get("warning_count") or len(warnings)),
        "failure_count": int(data.get("failure_count") or len(failures)),
        "attempt_count": int(data.get("attempt_count") or 1),
        **data,
    }
    if retry_boundary:
        result["retry_boundary"] = retry_boundary
        result["retryable"] = normalized_status in {"failed", "partial"}
    return result


def summarize_stage_results(stages: Mapping[str, Mapping[str, Any]] | None) -> dict[str, Any]:
    """Count stages per status and name the ones needing attention."""
    counts = {"completed": 0, "partial": 0, "skipped": 0, "failed": 0}
    failed_stages: list[str] = []
    partial_stages: list[str] = []
    skipped_stages: list[str] = []
    for stage_name, payload in dict(stages or {}).items():
        status = normalize_stage_status((payload or {}).get("status"))
        counts[status] = int(counts.get(status, 0)) + 1
        if status == "failed":
            failed_stages.append(stage_name)
        elif status == "partial":
            partial_stages.append(stage_name)
        elif status == "skipped":
            skipped_stages.append(stage_name)
    return {
        "stage_status_counts": counts,
        "failed_stage_count": len(failed_stages),
        "partial_stage_count": len(partial_stages),
        "skipped_stage_count": len(skipped_stages),
        "failed_stages": failed_stages,
        "partial_stages": partial_stages,
        "skipped_stages": skipped_stages,
    }


def resolve_run_status(stages: Mapping[str, Mapping[str, Any]] | None) -> str:
    """Resolve the overall run status from its stages.

    A failure alongside any completed or partial stage yields ``partial`` (part
    of the run produced usable output); a failure with nothing else yields
    ``failed``. All-skipped yields ``skipped``. No stages at all is ``success``
    because nothing could have failed.
    """
    resolved = dict(stages or {})
    summary = summarize_stage_results(resolved)
    if summary["failed_stage_count"] > 0:
        completed = int(summary["stage_status_counts"].get("completed", 0))
        if summary["partial_stage_count"] == 0 and completed == 0:
            return "failed"
        return "partial"
    if summary["partial_stage_count"] > 0:
        return "partial"
    if resolved and summary["skipped_stage_count"] == len(resolved):
        return "skipped"
    return "success"


def quality_status(flags: Any) -> str:
    """Reduce quality flags to a single verdict.

    Precedence is severity-ordered: ``failed`` beats ``degraded`` beats
    ``stale``; absent any of those the run is ``fresh``.
    """
    normalized = [
        str(flag or "").strip().lower()
        for flag in list(flags or [])
        if str(flag or "").strip()
    ]
    if _QUALITY_FAILED in normalized:
        return "failed"
    if any(token in normalized for token in _QUALITY_DEGRADED_TOKENS):
        return "degraded"
    if _QUALITY_STALE in normalized:
        return "stale"
    return "fresh"


def build_run_summary(result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compact, storable summary of one factor-scheduler run."""
    payload = dict(result or {})
    stages = dict(payload.get("stages") or {})
    lineage = dict(payload.get("lineage") or {})
    llm_validation = dict(payload.get("llm_validation") or {})
    llm_provider = dict(payload.get("llm_provider") or {})
    llm_mining = dict(payload.get("llm_mining") or {})
    llm_payload = dict(llm_mining.get("data") or {})
    quality_flags = list(payload.get("quality_flags") or [])
    return {
        "run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "computed": int(payload.get("computed") or 0),
        "errors": int(payload.get("errors") or 0),
        "universe_size": int(payload.get("universe_size") or 0),
        "quality_status": quality_status(quality_flags),
        "quality_flags": quality_flags,
        "stages": {
            name: {
                "status": str((stage_payload or {}).get("status") or ""),
                "failure_count": int((stage_payload or {}).get("failure_count") or 0),
                "warning_count": int((stage_payload or {}).get("warning_count") or 0),
            }
            for name, stage_payload in stages.items()
        },
        "stage_summary": summarize_stage_results(stages),
        "llm_generation_artifact_id": lineage.get("llm_generation_artifact_id"),
        "validation_artifact_ids": list(llm_validation.get("validation_artifact_ids") or []),
        "generated_candidate_count": int(llm_validation.get("generated_candidate_count") or 0),
        "validated_candidate_count": int(llm_validation.get("validated_candidate_count") or 0),
        "active_pool_count_after_run": int(llm_validation.get("active_pool_count_after_run") or 0),
        "governed_active_count_after_run": int(
            llm_validation.get("governed_active_count_after_run") or 0
        ),
        "llm_generation_mode": llm_payload.get("generation_mode"),
        "llm_fallback_used": bool(llm_payload.get("fallback_used")),
        "llm_fallback_reason": llm_payload.get("fallback_reason"),
        "llm_allow_local_rule_fallback": llm_payload.get("allow_local_rule_fallback"),
        "llm_provider_gate_status": llm_payload.get("provider_gate_status"),
        "llm_provider_gate_reason": llm_payload.get("provider_gate_reason"),
        "llm_provider_health_status": llm_provider.get("health_status"),
        "llm_provider_ready": bool(llm_provider.get("ready")),
        "llm_provider_enabled": bool(llm_provider.get("enabled")),
        "llm_provider_rebuild_count": int(llm_provider.get("rebuild_count") or 0),
        "llm_provider_last_error_type": llm_provider.get("last_error_type"),
    }


__all__ = [
    "RUN_STATUSES",
    "STAGE_STATUSES",
    "build_run_summary",
    "build_stage_result",
    "normalize_stage_status",
    "quality_status",
    "resolve_run_status",
    "summarize_stage_results",
]
