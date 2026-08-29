"""Minimal read-only MCP server for the AIASK four-factory control plane."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from .mcp_control import (
    factory_readiness,
    factory_run_detail,
    factory_runs,
    factory_status,
    list_factories,
)


logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("mcp.server").setLevel(logging.WARNING)

mcp = FastMCP("aiask-factories", json_response=True)

READ_ONLY_META = {
    "contract_version": "aiask.factory_run.v1",
    "side_effect": {
        "level": "read_only",
        "confirmation_required": False,
        "idempotent": True,
    },
}


@mcp.tool(description="List the four AIASK factories and the SignalTracker sidecar.", structured_output=True, meta=READ_ONLY_META)
async def factory_list() -> dict[str, Any]:
    return await list_factories()


@mcp.tool(description="Read one factory's latest, active, and queued run status.", structured_output=True, meta=READ_ONLY_META)
async def factory_status_read(factory: str) -> dict[str, Any]:
    return await factory_status(factory)


@mcp.tool(description="List persisted factory runs with optional factory and status filters.", structured_output=True, meta=READ_ONLY_META)
async def factory_runs_read(factory: str = "", status: str = "", limit: int = 20) -> dict[str, Any]:
    return await factory_runs(factory=factory or None, status=status or None, limit=limit)


@mcp.tool(description="Read one persisted FactoryRunContract by run id.", structured_output=True, meta=READ_ONLY_META)
async def factory_run_read(run_id: str) -> dict[str, Any]:
    return await factory_run_detail(run_id)


@mcp.tool(description="Read database, runner, and optional sidecar readiness for the factories.", structured_output=True, meta=READ_ONLY_META)
async def factory_readiness_read(factory: str = "") -> dict[str, Any]:
    return await factory_readiness(factory or None)


@mcp.resource("resource://factories/catalog", mime_type="application/json")
async def factory_catalog_resource() -> dict[str, Any]:
    return await list_factories()


@mcp.resource("resource://factories/readiness", mime_type="application/json")
async def factory_readiness_resource() -> dict[str, Any]:
    return await factory_readiness()


@mcp.resource("resource://factories/{factory}/status", mime_type="application/json")
async def factory_status_resource(factory: str) -> dict[str, Any]:
    return await factory_status(factory)


@mcp.resource("resource://factory-runs/{run_id}", mime_type="application/json")
async def factory_run_resource(run_id: str) -> dict[str, Any]:
    return await factory_run_detail(run_id)


async def _run() -> None:
    await mcp.run_stdio_async()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
