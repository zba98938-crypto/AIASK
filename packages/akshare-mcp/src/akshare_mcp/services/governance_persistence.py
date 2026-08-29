from __future__ import annotations

from typing import Any

from ..storage import get_db
from .governance_monitor import GovernanceReport


async def persist_governance_report_snapshot(
    report: GovernanceReport,
    *,
    scope_type: str,
    scope_id: str | None = None,
) -> dict[str, Any]:
    db = get_db()
    payload = {
        "scope_type": str(scope_type or report.target_type or "system"),
        "scope_id": scope_id if scope_id is not None else report.target_id,
        "overall_status": report.overall_status,
        "issues": list(report.issues or []),
        "payload_jsonb": report.to_dict(),
        "generated_at": report.checked_at,
    }
    if hasattr(db, "save_governance_report_snapshot"):
        return await db.save_governance_report_snapshot(payload)
    return payload


async def get_latest_governance_report_snapshot(
    *,
    scope_type: str,
    scope_id: str | None = None,
) -> dict[str, Any] | None:
    db = get_db()
    if hasattr(db, "get_latest_governance_report_snapshot"):
        return await db.get_latest_governance_report_snapshot(scope_type, scope_id)
    return None
