"""Target resolution helpers for stock deep analysis."""

from __future__ import annotations

from typing import Any

from ...storage import get_db
from ...utils import normalize_code
from .constants import SUPPORTED_ANALYSIS_TASKS
from .shared import _safe_float

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


def _task_profile(task: str) -> dict[str, Any]:
    normalized = str(task or "deep_analysis").strip().lower()
    if normalized == "quick_scan":
        return {
            "task": normalized,
            "include_report": True,
            "include_trade_plan": False,
            "section_keys": ["overview", "trend_and_structure", "risks_and_counterpoints", "action_plan"],
            "title": "Quick Scan",
        }
    if normalized == "trade_plan":
        return {
            "task": normalized,
            "include_report": True,
            "include_trade_plan": True,
            "section_keys": [
                "overview",
                "trend_and_structure",
                "events_and_catalysts",
                "risks_and_counterpoints",
                "action_plan",
            ],
            "title": "Trade Plan",
        }
    return {
        "task": normalized if normalized in SUPPORTED_ANALYSIS_TASKS else "deep_analysis",
        "include_report": normalized != "recover_gaps",
        "include_trade_plan": False,
        "section_keys": [
            "overview",
            "valuation",
            "financial_quality",
            "trend_and_structure",
            "events_and_catalysts",
            "risks_and_counterpoints",
            "action_plan",
            "evidence_and_gaps",
        ],
        "title": "Deep Analysis",
    }


def _normalize_task(task: str | None) -> str:
    normalized = str(task or "deep_analysis").strip().lower()
    return normalized if normalized in SUPPORTED_ANALYSIS_TASKS else "deep_analysis"


async def _resolve_target(query: str, *, db_factory=get_db) -> dict[str, Any]:
    raw = str(query or "").strip()
    normalized = normalize_code(raw)
    if len(normalized) == 6 and normalized.isdigit():
        return {
            "query": raw,
            "resolved": True,
            "code": normalized,
            "name": "",
            "resolution_mode": "direct_code",
            "candidates": [],
        }

    if not raw:
        return {
            "query": raw,
            "resolved": False,
            "code": "",
            "name": "",
            "resolution_mode": "empty_query",
            "candidates": [],
        }

    db = db_factory()
    rows: list[dict[str, Any]] = []
    try:
        async with db.acquire() as conn:
            fetched = await conn.fetch(
                """
                SELECT code, stock_name, industry, market_cap
                FROM stocks
                WHERE stock_name LIKE $1
                   OR code LIKE $2
                   OR (industry IS NOT NULL AND industry LIKE $1)
                ORDER BY
                  CASE WHEN stock_name = $3 THEN 0 ELSE 1 END,
                  market_cap DESC NULLS LAST
                LIMIT 8
                """,
                f"%{raw}%",
                f"%{raw}%",
                raw,
            )
            rows = [dict(row) for row in fetched]
    except Exception:
        rows = []

    candidates = [
        {
            "code": str(item.get("code") or ""),
            "name": str(item.get("stock_name") or item.get("name") or ""),
            "industry": item.get("industry"),
            "market_cap": _safe_float(item.get("market_cap")),
        }
        for item in rows
        if str(item.get("code") or "").strip()
    ]

    exact_name = [item for item in candidates if item.get("name") == raw]
    if len(exact_name) == 1:
        selected = exact_name[0]
        return {
            "query": raw,
            "resolved": True,
            "code": selected["code"],
            "name": selected["name"],
            "resolution_mode": "exact_name_match",
            "candidates": candidates,
        }

    if len(candidates) == 1:
        selected = candidates[0]
        return {
            "query": raw,
            "resolved": True,
            "code": selected["code"],
            "name": selected["name"],
            "resolution_mode": "single_candidate",
            "candidates": candidates,
        }

    return {
        "query": raw,
        "resolved": False,
        "code": "",
        "name": "",
        "resolution_mode": "ambiguous_name" if candidates else "not_found",
        "candidates": candidates,
    }
