"""Strategy review resources."""

from __future__ import annotations

from typing import Any

from ..services.domain_projection import get_strategy_domain_projection_service
from ..storage import get_db


async def build_strategy_review_payload(strategy_id: str) -> dict[str, Any]:
    resolved_strategy_id = str(strategy_id or "").strip()
    db = get_db()
    strategy = await db.get_strategy(resolved_strategy_id) if hasattr(db, "get_strategy") else None
    if not strategy:
        return {
            "uri": f"resource://strategy/{resolved_strategy_id}/review",
            "strategy_id": resolved_strategy_id,
            "found": False,
            "error": f"strategy not found: {resolved_strategy_id}",
        }

    latest_projection_snapshot = (
        await db.get_latest_strategy_projection_snapshot(resolved_strategy_id)
        if hasattr(db, "get_latest_strategy_projection_snapshot")
        else None
    )
    projection = dict((latest_projection_snapshot or {}).get("projection") or {})
    if not projection:
        try:
            projection = await get_strategy_domain_projection_service().project_strategy(
                db,
                resolved_strategy_id,
                limit=200,
            )
        except Exception:
            projection = {}

    latest_promotion_review = (
        await db.get_latest_strategy_promotion_review(resolved_strategy_id)
        if hasattr(db, "get_latest_strategy_promotion_review")
        else None
    )
    runtime_control = (
        await db.get_strategy_runtime_control(resolved_strategy_id)
        if hasattr(db, "get_strategy_runtime_control")
        else None
    )
    open_risks = (
        await db.list_strategy_runtime_risk_events(
            strategy_id=resolved_strategy_id,
            status="open",
            limit=20,
        )
        if hasattr(db, "list_strategy_runtime_risk_events")
        else []
    )
    task_runs = (
        await db.list_strategy_task_runs(strategy_id=resolved_strategy_id, limit=10)
        if hasattr(db, "list_strategy_task_runs")
        else []
    )

    return {
        "uri": f"resource://strategy/{resolved_strategy_id}/review",
        "strategy_id": resolved_strategy_id,
        "found": True,
        "strategy": strategy,
        "projection": projection,
        "latest_projection_snapshot": latest_projection_snapshot,
        "latest_promotion_review": latest_promotion_review,
        "runtime_control": runtime_control,
        "open_risks": open_risks,
        "recent_task_runs": task_runs,
        "summary": {
            "current_status": projection.get("current_status") or strategy.get("status"),
            "open_risk_count": len(open_risks or []),
            "runtime_control_mode": (runtime_control or {}).get("control_mode") or "active",
            "latest_promotion_status": (latest_promotion_review or {}).get("status"),
            "latest_promotion_recommendation": (latest_promotion_review or {}).get("recommendation"),
        },
    }


def register(mcp) -> None:
    """Register strategy review resources."""

    @mcp.resource(
        "resource://strategy/{id}/review",
        name="strategy_review_resource",
        title="Strategy Review Snapshot",
        description="Read-only lifecycle, projection and runtime review context for a strategy",
        mime_type="application/json",
    )
    async def strategy_review_resource(id: str) -> dict[str, Any]:
        return await build_strategy_review_payload(id)
