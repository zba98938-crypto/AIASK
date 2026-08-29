"""Shared helpers for stock deep analysis."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_id(run_id: str, suffix: str | None = None) -> str:
    return run_id if not suffix else f"{run_id}:{suffix}"


def _existing_target_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    target = dict(source.get("target") or {})
    code = str(target.get("code") or source.get("code") or "").strip()
    if not code:
        return {}
    return {
        "query": str(source.get("query") or code),
        "resolved": True,
        "code": code,
        "name": str(target.get("name") or source.get("name") or "").strip(),
        "resolution_mode": "existing_run",
        "candidates": [],
    }


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _response_data(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return dict(data) if isinstance(data, dict) else {}


def _extract_lineage(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    lineage: dict[str, Any] = {}
    for key in LINEAGE_REFERENCE_KEYS:
        value = source.get(key)
        if value not in (None, "", []):
            lineage[key] = value
    explicit = source.get("lineage")
    if isinstance(explicit, dict):
        for key, value in explicit.items():
            if value not in (None, "", []):
                lineage.setdefault(str(key), value)
    return lineage


def _stage_result(stage: str, *, status: str, success: bool, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "stage": stage,
        "status": status,
        "success": success,
        "detail": dict(detail or {}),
        "updated_at": _utcnow_iso(),
    }

