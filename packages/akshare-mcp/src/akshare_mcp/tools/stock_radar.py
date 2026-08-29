"""MCP tools for the AIASK stock radar."""

from __future__ import annotations

from typing import Any

from ..storage import get_db
from ..services.stock_radar import (
    push_stock_radar_digest,
    run_stock_radar,
    schedule_stock_radar_update,
    stock_radar_candidates,
    stock_radar_digest,
    stock_radar_status,
)


def _meta(name: str, *, side_effect: str = "read_only") -> dict[str, Any]:
    level = "stateful" if side_effect != "read_only" else "read_only"
    return {
        "contract_version": "ai_tool_contract_v1",
        "contract_source": "akshare_mcp.stock_radar",
        "side_effect": {
            "level": level,
            "confirmation_required": level != "read_only",
            "idempotent": level == "read_only",
            "target": name,
        },
        "source_policy": {
            "priority": ["market_documents", "market_events_normalized", "cninfo", "rsshub", "eastmoney"],
            "degraded_visible": True,
        },
    }


def register(mcp) -> None:
    @mcp.tool(
        title="Stock Radar Status",
        description="Read latest AIASK stock radar run status, candidates, degraded flags, and digest preview.",
        structured_output=True,
        meta=_meta("stock_radar_status"),
    )
    async def stock_radar_status_tool(run_id: str = "", limit: int = 20) -> dict[str, Any]:
        db = get_db()
        await db.initialize()
        return await stock_radar_status(db, {"run_id": run_id, "limit": limit})

    @mcp.tool(
        title="Stock Radar Candidates",
        description="List AIASK stock radar observation-pool candidates with scores and source chains.",
        structured_output=True,
        meta=_meta("stock_radar_candidates"),
    )
    async def stock_radar_candidates_tool(
        run_id: str = "",
        tier: str = "",
        symbol: str = "",
        min_score: float | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        db = get_db()
        await db.initialize()
        return await stock_radar_candidates(
            db,
            {
                "run_id": run_id,
                "tier": tier,
                "symbol": symbol,
                "min_score": min_score,
                "limit": limit,
            },
        )

    @mcp.tool(
        title="Stock Radar Digest",
        description="Build a no-trade-instruction stock radar digest preview for WeCom/Telegram delivery.",
        structured_output=True,
        meta=_meta("stock_radar_digest"),
    )
    async def stock_radar_digest_tool(run_id: str = "", limit: int = 20, channels: str = "wecom,telegram") -> dict[str, Any]:
        db = get_db()
        await db.initialize()
        return await stock_radar_digest(db, {"run_id": run_id, "limit": limit, "channels": channels})

    @mcp.tool(
        title="Run Stock Radar Once",
        description="Run one controlled stock radar pass. Agent/Desktop should call this through ActionIntent.",
        structured_output=True,
        meta=_meta("stock_radar_run_once", side_effect="stateful"),
    )
    async def stock_radar_run_once_tool(
        mode: str = "run_once",
        days: int = 3,
        limit: int = 80,
        allow_network: bool = False,
        allow_llm: bool = False,
        stock_codes: str = "",
    ) -> dict[str, Any]:
        db = get_db()
        await db.initialize()
        return await run_stock_radar(
            db,
            mode=mode,
            days=days,
            limit=limit,
            allow_network=allow_network,
            allow_llm=allow_llm,
            stock_codes=stock_codes,
        )

    @mcp.tool(
        title="Push Stock Radar Digest",
        description="Record or queue a stock radar digest delivery. Agent/Desktop should call this through ActionIntent.",
        structured_output=True,
        meta=_meta("stock_radar_push_digest", side_effect="stateful"),
    )
    async def stock_radar_push_digest_tool(
        run_id: str = "",
        channels: str = "wecom,telegram",
        target: str = "",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        db = get_db()
        await db.initialize()
        return await push_stock_radar_digest(
            db,
            {"run_id": run_id, "channels": channels, "target": target, "dry_run": dry_run},
        )

    @mcp.tool(
        title="Schedule Stock Radar Update",
        description="Preview/update a stock radar schedule. Agent/Desktop should call this through ActionIntent.",
        structured_output=True,
        meta=_meta("stock_radar_schedule_update", side_effect="stateful"),
    )
    async def stock_radar_schedule_update_tool(schedule: str = "manual", enabled: bool = False) -> dict[str, Any]:
        db = get_db()
        await db.initialize()
        return await schedule_stock_radar_update(db, {"schedule": schedule, "enabled": enabled})
