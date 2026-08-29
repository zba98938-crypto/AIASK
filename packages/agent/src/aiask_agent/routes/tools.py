from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

def create_tools_router(
    *,
    runtime: Any,
    require_api: Callable[[Request], None],
    require_full: Callable[[Request], Any],
    audited_tool_call: Callable[..., Awaitable[dict[str, Any]]],
    metadata_allows_read_only_call: Callable[[dict[str, Any], str], bool],
    is_read_only_tool: Callable[[str], bool],
    read_only_result_guard: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/tools/{tool_name}")
    async def tool_call_read_only(request: Request, tool_name: str) -> dict[str, Any]:
        require_api(request)
        payload = await request.json()
        tool = runtime.tool_registry.get(tool_name)
        metadata = dict(getattr(tool, "metadata", {}) or {}) if tool else {}
        if not metadata_allows_read_only_call(metadata, tool_name) and not is_read_only_tool(tool_name):
            raise HTTPException(403, detail=f"tool is not available through the read-only desktop API: {tool_name}")
        return await audited_tool_call(
            runtime,
            tool_name,
            dict(payload or {}),
            request=request,
            metadata=metadata,
            source_chain=["aiask_agent.server", "desktop.read_only_tool"],
            result_guard=read_only_result_guard,
        )

    @router.post("/v1/hermes/admin/tools/{tool_name}")
    async def hermes_tool_call(request: Request, tool_name: str) -> dict[str, Any]:
        full = require_full(request)
        payload = dict(await request.json() or {})
        tool = full.tool_registry.get(tool_name)
        metadata = dict(getattr(tool, "metadata", {}) or {}) if tool else {}
        return await audited_tool_call(
            full,
            tool_name,
            payload,
            request=request,
            metadata=metadata,
            source_chain=["aiask_agent.server", "hermes.admin_tool"],
        )

    return router
