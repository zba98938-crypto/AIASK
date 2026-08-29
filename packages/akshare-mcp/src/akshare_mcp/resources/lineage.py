"""Lineage and dataset-quality resources."""

from __future__ import annotations

from typing import Any

from ..services.artifact_registry import get_artifact_async
from ..storage import get_db
from ..tools.manager_protocol import LINEAGE_REFERENCE_KEYS


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


async def build_run_payload(run_id: str) -> dict[str, Any]:
    resolved_run_id = str(run_id or "").strip()
    db = get_db()
    if hasattr(db, "get_strategy_factory_run"):
        row = await db.get_strategy_factory_run(resolved_run_id)
        if row:
            payload = dict(row)
            return {
                "uri": f"resource://run/{resolved_run_id}",
                "run_id": resolved_run_id,
                "found": True,
                "kind": "strategy_factory_run",
                "summary": {
                    "status": payload.get("status"),
                    "elapsed_seconds": payload.get("elapsed_seconds"),
                    "started_at": payload.get("started_at"),
                    "completed_at": payload.get("completed_at"),
                },
                "lineage": _extract_lineage(payload),
                "payload": payload,
            }

    artifact = await get_artifact_async(resolved_run_id)
    if artifact:
        payload = dict(artifact)
        nested_payload = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}
        quality = nested_payload.get("quality") or payload.get("quality") or {}
        return {
            "uri": f"resource://run/{resolved_run_id}",
            "run_id": resolved_run_id,
            "found": True,
            "kind": str(payload.get("artifact_type") or payload.get("strategy") or "artifact"),
            "summary": {
                "updated_at": payload.get("updated_at"),
                "registered_at": payload.get("registered_at"),
                "artifact_type": payload.get("artifact_type"),
                "strategy": payload.get("strategy"),
            },
            "lineage": _extract_lineage({**payload, **nested_payload}),
            "quality": quality,
            "payload": payload,
        }

    return {
        "uri": f"resource://run/{resolved_run_id}",
        "run_id": resolved_run_id,
        "found": False,
        "error": f"run not found: {resolved_run_id}",
    }


async def build_dataset_quality_payload(dataset_id: str) -> dict[str, Any]:
    resolved_dataset_id = str(dataset_id or "").strip()
    artifact = await get_artifact_async(resolved_dataset_id)
    if artifact:
        payload = dict(artifact)
        nested_payload = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}
        quality = nested_payload.get("quality") or payload.get("quality") or nested_payload.get("quality_meta") or {}
        return {
            "uri": f"resource://dataset/{resolved_dataset_id}/quality",
            "dataset_id": resolved_dataset_id,
            "found": True,
            "kind": str(payload.get("artifact_type") or "artifact"),
            "quality": quality,
            "lineage": _extract_lineage({**payload, **nested_payload, "dataset_id": resolved_dataset_id}),
            "payload": payload,
        }

    return {
        "uri": f"resource://dataset/{resolved_dataset_id}/quality",
        "dataset_id": resolved_dataset_id,
        "found": False,
        "quality_contract": {
            "required_fields": [],
            "minimum_quality_threshold": 0.95,
            "expected_outputs": [
                "accepted_count",
                "rejected_count",
                "missing_by_field",
                "minimum_quality_passed",
            ],
        },
        "message": "No persisted dataset quality snapshot found. Run data_quality_workflow with persist_artifact=true to materialize one.",
    }


def register(mcp) -> None:
    @mcp.resource(
        "resource://run/{run_id}",
        name="run_snapshot",
        title="Run Snapshot",
        description="Read-only run or artifact snapshot for workflow lineage inspection",
        mime_type="application/json",
    )
    async def run_snapshot(run_id: str) -> dict[str, Any]:
        return await build_run_payload(run_id)

    @mcp.resource(
        "resource://dataset/{dataset_id}/quality",
        name="dataset_quality_snapshot",
        title="Dataset Quality Snapshot",
        description="Read-only dataset quality or validation snapshot when materialized",
        mime_type="application/json",
    )
    async def dataset_quality_snapshot(dataset_id: str) -> dict[str, Any]:
        return await build_dataset_quality_payload(dataset_id)
