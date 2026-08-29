"""Artifact persistence helpers for stock deep analysis."""

from __future__ import annotations

import asyncio
from typing import Any

from ...tools.manager_protocol import LINEAGE_REFERENCE_KEYS
from ...utils import normalize_code
from ..artifact_registry import get_artifact_async, list_artifacts_async, register_artifact_async
from .constants import ANALYSIS_STRATEGY, ANALYSIS_VERSION, _SUMMARY_ONLY_FIELDS
from .shared import _artifact_id

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


async def _persist_artifact(
    *,
    artifact_id: str,
    artifact_type: str,
    code: str,
    run_id: str,
    task: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return await register_artifact_async(
        {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "strategy": ANALYSIS_STRATEGY,
            "strategy_version": ANALYSIS_VERSION,
            "code": code,
            "run_id": run_id,
            "task": task,
            "payload": payload,
            "lineage": {"run_id": run_id, "security_code": code},
        }
    )


async def _load_existing_run_summary(run_id: str) -> dict[str, Any]:
    artifact = await get_artifact_async(str(run_id or "").strip())
    return dict((artifact or {}).get("payload") or {})


async def _load_existing_analysis_input(run_id: str) -> dict[str, Any]:
    run_key = str(run_id or "").strip()
    if not run_key:
        return {}
    input_artifact, summary_artifact = await asyncio.gather(
        get_artifact_async(_artifact_id(run_key, "input")),
        get_artifact_async(run_key),
    )
    if input_artifact:
        payload = dict(input_artifact.get("payload") or {})
        if payload:
            return payload
    summary_payload = dict((summary_artifact or {}).get("payload") or {})
    return dict(summary_payload.get("analysis_input") or {})

async def get_analysis_run_summary(run_id: str) -> dict[str, Any]:
    artifact = await get_artifact_async(str(run_id or "").strip())
    if artifact:
        payload = dict(artifact.get("payload") or {})
        if payload:
            return payload
    return {
        "run_id": str(run_id or "").strip(),
        "found": False,
        "error": f"analysis run not found: {run_id}",
    }


async def get_analysis_report_bundle(run_id: str) -> dict[str, Any]:
    artifact = await get_artifact_async(_artifact_id(str(run_id or "").strip(), "report"))
    if artifact:
        payload = dict(artifact.get("payload") or {})
        if payload:
            return payload
    return {
        "run_id": str(run_id or "").strip(),
        "found": False,
        "error": f"analysis report not found: {run_id}",
    }


async def find_latest_analysis_run_id(code: str) -> str | None:
    resolved_code = normalize_code(code)
    if not resolved_code:
        return None
    rows = await list_artifacts_async(limit=200, strategy=ANALYSIS_STRATEGY)
    for row in rows:
        artifact_id = str((row or {}).get("artifact_id") or "").strip()
        if not artifact_id or ":" in artifact_id:
            continue
        if str((row or {}).get("code") or "").strip() != resolved_code:
            continue
        return artifact_id
    return None


async def get_latest_analysis_summary_for_code(code: str) -> dict[str, Any]:
    latest_run_id = await find_latest_analysis_run_id(code)
    if not latest_run_id:
        resolved_code = normalize_code(code)
        return {
            "code": resolved_code,
            "found": False,
            "error": f"no deep analysis run found for {resolved_code}",
        }
    payload = await get_analysis_run_summary(latest_run_id)
    if payload.get("found") is False:
        return payload
    return payload


def compact_run_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return {field: payload.get(field) for field in _SUMMARY_ONLY_FIELDS if field in payload}
