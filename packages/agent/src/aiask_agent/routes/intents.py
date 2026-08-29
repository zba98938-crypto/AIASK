from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..intents import public_intents
from ..session_store_utils import redact_audit_text


def create_intents_router(
    *,
    require_api: Callable[[Request], None],
    control_authorized: Callable[[Request], tuple[bool, str | None]],
    intent_store: Any,
    intent_executor: Any,
    audited_domain_tool_call: Callable[..., Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    def require_control_token(request: Request) -> None:
        ok, reason = control_authorized(request)
        if not ok:
            raise HTTPException(503 if reason == "control token is not configured" else 401, detail=reason)

    @router.get("/intents")
    async def intent_list(request: Request, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        require_api(request)
        return {
            "object": "list",
            "data": public_intents(intent_store.list(status=status, limit=limit)),
            "secrets_redacted": True,
        }

    @router.get("/intents/{intent_id}")
    async def intent_get(request: Request, intent_id: str) -> dict[str, Any]:
        require_api(request)
        result = await audited_domain_tool_call(
            "agent_action_intent_get",
            {"intent_id": intent_id},
            request=request,
            source_chain=["aiask_agent.server", "intent.api"],
        )
        if not result.get("success"):
            raise HTTPException(404, detail=redact_audit_text(result.get("error") or "intent not found"))
        return result

    @router.post("/intents")
    async def intent_create(request: Request) -> dict[str, Any]:
        require_control_token(request)
        payload = dict(await request.json() or {})
        result = await audited_domain_tool_call(
            "agent_action_intent_create",
            payload,
            request=request,
            source_chain=["aiask_agent.server", "intent.api"],
        )
        if not result.get("success"):
            raise HTTPException(400, detail=redact_audit_text(result.get("error") or "intent create failed"))
        return result

    @router.post("/intents/{intent_id}/confirm")
    async def intent_confirm(request: Request, intent_id: str) -> dict[str, Any]:
        require_control_token(request)
        return await intent_executor.confirm(intent_id)

    @router.post("/intents/{intent_id}/deny")
    async def intent_deny(request: Request, intent_id: str) -> dict[str, Any]:
        require_control_token(request)
        payload = await request.json()
        return await intent_executor.deny(intent_id, reason=payload.get("reason"))

    return router
