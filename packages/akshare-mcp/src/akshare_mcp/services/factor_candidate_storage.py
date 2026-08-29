"""候选因子研究记忆存储：基于 artifact registry 的轻量持久层。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .artifact_registry import (
    get_artifact,
    get_artifact_async,
    list_artifacts,
    list_artifacts_async,
    register_artifact,
    register_artifact_async,
)

FACTOR_MEMORY_STRATEGY = "factor_candidate_memory"
FACTOR_MEMORY_VERSION = "p2.v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_codes(codes: Any) -> list[str]:
    if isinstance(codes, str):
        raw = [item.strip() for item in codes.replace("|", ",").replace(";", ",").split(",")]
        return [item for item in raw if item]
    if isinstance(codes, (list, tuple, set)):
        return [str(item).strip() for item in codes if str(item).strip()]
    return []


def _payload_from_artifact(artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(artifact, dict):
        return None
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else artifact
    if not isinstance(payload, dict):
        return None
    merged = deepcopy(payload)
    merged.setdefault("artifact_id", artifact.get("artifact_id"))
    merged.setdefault("strategy", artifact.get("strategy"))
    merged.setdefault("strategy_version", artifact.get("strategy_version"))
    merged.setdefault("updated_at", artifact.get("updated_at"))
    merged.setdefault("created_at", artifact.get("created_at"))
    return merged


def _matches_filters(
    record: dict[str, Any],
    *,
    codes: list[str] | None = None,
    status: str | None = None,
    family: str | None = None,
) -> bool:
    if not isinstance(record, dict):
        return False
    if str(record.get("strategy") or "").strip() != FACTOR_MEMORY_STRATEGY:
        return False

    normalized_codes = _normalize_codes(codes)
    record_codes = _normalize_codes(record.get("codes"))
    if normalized_codes and not (set(normalized_codes) & set(record_codes)):
        return False

    if status:
        record_status = str(record.get("status") or "").strip().lower()
        if record_status != str(status).strip().lower():
            return False

    if family:
        candidate = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
        record_family = str(candidate.get("family") or record.get("family") or "").strip().lower()
        if record_family != str(family).strip().lower():
            return False

    return True


def build_factor_candidate_record_artifact(
    record: dict[str, Any],
    *,
    artifact_id: Optional[str] = None,
) -> dict[str, Any]:
    payload = deepcopy(record if isinstance(record, dict) else {})
    resolved_codes = _normalize_codes(payload.get("codes"))
    aid = str(artifact_id or payload.get("artifact_id") or f"factor_memory_{int(datetime.now().timestamp())}_{uuid4().hex[:8]}")
    payload["artifact_id"] = aid
    payload["record_type"] = "factor_candidate_memory"
    payload["codes"] = resolved_codes
    payload.setdefault("status", "draft")
    payload.setdefault("tags", [])
    payload.setdefault("created_at", _now_iso())
    payload["updated_at"] = _now_iso()
    return {
        "artifact_id": aid,
        "strategy": FACTOR_MEMORY_STRATEGY,
        "strategy_version": FACTOR_MEMORY_VERSION,
        "code": ",".join(resolved_codes[:5]),
        "payload": payload,
        "created_at": payload.get("created_at"),
    }


def save_factor_candidate_record(
    record: dict[str, Any],
    *,
    artifact_id: Optional[str] = None,
) -> dict[str, Any]:
    artifact = build_factor_candidate_record_artifact(record, artifact_id=artifact_id)
    registered = register_artifact(artifact)
    return _payload_from_artifact(registered) or {}


async def save_factor_candidate_record_async(
    record: dict[str, Any],
    *,
    artifact_id: Optional[str] = None,
) -> dict[str, Any]:
    artifact = build_factor_candidate_record_artifact(record, artifact_id=artifact_id)
    registered = await register_artifact_async(artifact)
    return _payload_from_artifact(registered) or {}


async def get_factor_candidate_record_async(artifact_id: str) -> dict[str, Any] | None:
    artifact = await get_artifact_async(artifact_id)
    record = _payload_from_artifact(artifact)
    if not _matches_filters(record or {}):
        return None
    return record


def get_factor_candidate_record(artifact_id: str) -> dict[str, Any] | None:
    artifact = get_artifact(artifact_id)
    record = _payload_from_artifact(artifact)
    if not _matches_filters(record or {}):
        return None
    return record


async def list_factor_candidate_records_async(
    *,
    limit: int = 50,
    codes: list[str] | None = None,
    status: str | None = None,
    family: str | None = None,
) -> list[dict[str, Any]]:
    fetch_limit = max(50, min(1000, int(limit) * 10))
    summary_rows: list[dict[str, Any]] = []
    try:
        async_rows = await list_artifacts_async(limit=fetch_limit, strategy=FACTOR_MEMORY_STRATEGY)
        if isinstance(async_rows, list):
            summary_rows.extend([row for row in async_rows if isinstance(row, dict)])
    except Exception:
        pass
    try:
        sync_rows = list_artifacts(limit=fetch_limit, strategy=FACTOR_MEMORY_STRATEGY)
        if isinstance(sync_rows, list):
            summary_rows.extend([row for row in sync_rows if isinstance(row, dict)])
    except Exception:
        pass

    ordered_ids = []
    for row in summary_rows:
        aid = str(row.get("artifact_id") or "").strip()
        if aid and aid not in ordered_ids:
            ordered_ids.append(aid)

    results: list[dict[str, Any]] = []
    for aid in ordered_ids:
        artifact = await get_artifact_async(aid)
        record = _payload_from_artifact(artifact)
        if not _matches_filters(record or {}, codes=codes, status=status, family=family):
            continue
        results.append(record or {})

    results.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return results[: max(1, int(limit))]
