from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .action_intents import create_external_action_intent_payload
from ..session_store_utils import sanitize_for_audit, sanitize_public_response


FullToolCall = Callable[[Request, str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _public_mcp_payload(value: Any) -> Any:
    return sanitize_public_response(value, max_depth=10, max_items=500, max_text=4000)


def create_mcp_router(
    *,
    require_api: Callable[[Request], Any],
    require_full: Callable[[Request], Any],
    full_tool_call: FullToolCall,
    intent_store: Any,
    session_store: Any,
    mcp_aggregator_factory: Callable[[], Any],
    refresh_mcp_runtime: Callable[[], None],
    classify_mcp_error: Callable[[BaseException], tuple[str, str]],
    mcp_action_error_payload: Callable[..., dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/mcp/servers")
    async def mcp_servers(request: Request, all: bool = False) -> dict[str, Any]:
        require_api(request)
        rows = []
        for item in mcp_aggregator_factory().servers_summary(include_all=all):
            row = dict(item)
            row.setdefault("id", str(row.get("name") or ""))
            row.setdefault("enabled", bool(row.get("enabled", True)))
            rows.append(row)
        return {"object": "list", "data": _public_mcp_payload(rows), "secrets_redacted": True}

    @router.post("/v1/mcp/servers")
    async def mcp_server_create(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="mcp.server_upsert",
            params=dict(payload),
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "MCP server registration changes durable tool configuration and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "mcp.server_upsert.intent"],
            object_name="mcp.server_upsert_intent",
        )

    @router.patch("/v1/mcp/servers/{name}")
    async def mcp_server_update(request: Request, name: str) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="mcp.server_update",
            params={**payload, "name": name},
            request_payload={**payload, "name": name},
            headers=request.headers,
            rationale=str(payload.get("rationale") or "MCP server updates change durable tool configuration and require ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "mcp.server_update.intent"],
            object_name="mcp.server_update_intent",
        )

    @router.delete("/v1/mcp/servers/{name}")
    async def mcp_server_delete(request: Request, name: str) -> dict[str, Any]:
        require_full(request)
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="mcp.server_remove",
            params={"name": name},
            request_payload={"name": name},
            headers=request.headers,
            rationale="MCP server removal changes durable tool configuration and requires ActionIntent confirmation.",
            source_chain=["aiask_agent.server", "mcp.server_remove.intent"],
            object_name="mcp.server_remove_intent",
        )

    @router.get("/v1/mcp/tools")
    async def mcp_tools(request: Request, all: bool = False) -> dict[str, Any]:
        require_full(request)
        return {
            "object": "list",
            "data": _public_mcp_payload(mcp_aggregator_factory().tools_summary(include_all=all)),
            "secrets_redacted": True,
        }

    @router.get("/v1/mcp/resources")
    async def mcp_resources(request: Request, all: bool = False) -> dict[str, Any]:
        require_full(request)
        return {
            "object": "list",
            "data": _public_mcp_payload(mcp_aggregator_factory().resources_summary(include_all=all)),
            "secrets_redacted": True,
        }

    @router.get("/v1/mcp/prompts")
    async def mcp_prompts(request: Request, all: bool = False) -> dict[str, Any]:
        require_full(request)
        return {
            "object": "list",
            "data": _public_mcp_payload(mcp_aggregator_factory().prompts_summary(include_all=all)),
            "secrets_redacted": True,
        }

    @router.get("/v1/mcp/oauth_status")
    async def mcp_oauth_status(request: Request, all: bool = False) -> dict[str, Any]:
        require_full(request)
        return {
            "object": "list",
            "data": _public_mcp_payload(mcp_aggregator_factory().oauth_status(include_all=all)),
            "secrets_redacted": True,
        }

    @router.post("/v1/mcp/register-local")
    async def mcp_register_local(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="mcp.register_local",
            params=dict(payload),
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Local MCP registration changes durable tool configuration and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "mcp.register_local.intent"],
            object_name="mcp.register_local_intent",
        )

    @router.post("/v1/mcp/discover")
    async def mcp_discover(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        server_name = str(payload.get("server") or "").strip()
        if not server_name:
            raise HTTPException(400, detail="server is required")
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="mcp.discover_update",
            params={"server": server_name},
            request_payload={**payload, "server": server_name},
            headers=request.headers,
            rationale=str(payload.get("rationale") or "MCP discovery connects to an external server and writes discovered tool metadata after confirmation."),
            source_chain=["aiask_agent.server", "mcp.discover_update.intent"],
            object_name="mcp.discover_update_intent",
        )

    @router.post("/v1/mcp/oauth/start")
    async def mcp_oauth_start(request: Request) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        require_full(request)
        return {
            "object": "mcp.oauth_start",
            "data": _public_mcp_payload(mcp_aggregator_factory().oauth_start(str(payload.get("server") or ""))),
            "secrets_redacted": True,
        }

    @router.post("/v1/mcp/oauth/callback")
    async def mcp_oauth_callback(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="mcp.oauth_callback",
            params=dict(payload),
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "MCP OAuth callback can store credentials and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "mcp.oauth_callback.intent"],
            object_name="mcp.oauth_callback_intent",
        )

    @router.post("/v1/mcp/resources/read")
    async def mcp_resource_read(request: Request) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        require_full(request)
        server_name = str(payload.get("server") or "")
        try:
            return {
                "object": "mcp.resource",
                "success": True,
                "data": _public_mcp_payload(
                    await mcp_aggregator_factory().read_resource(server_name, str(payload.get("uri") or ""))
                ),
                "error": None,
                "secrets_redacted": True,
            }
        except Exception as exc:
            return mcp_action_error_payload(action="resource", server_name=server_name, exc=exc)

    @router.post("/v1/mcp/prompts/get")
    async def mcp_prompt_get(request: Request) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        require_full(request)
        server_name = str(payload.get("server") or "")
        try:
            return {
                "object": "mcp.prompt",
                "success": True,
                "data": _public_mcp_payload(
                    await mcp_aggregator_factory().get_prompt(
                        server_name,
                        str(payload.get("prompt") or payload.get("name") or ""),
                        dict(payload.get("arguments") or {}),
                    )
                ),
                "error": None,
                "secrets_redacted": True,
            }
        except Exception as exc:
            return mcp_action_error_payload(action="prompt", server_name=server_name, exc=exc)

    return router
