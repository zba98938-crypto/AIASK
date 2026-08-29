from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .action_intents import create_external_action_intent_payload
from ..session_store_utils import sanitize_public_response


FullToolCall = Callable[[Request, str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _public_webhook_payload(value: Any) -> Any:
    return sanitize_public_response(value, max_depth=8, max_items=200, max_text=4000)


def create_webhooks_router(
    *,
    require_full: Callable[[Request], Any],
    full_tool_call: FullToolCall,
    intent_store: Any,
    session_store: Any,
    webhook_store_factory: Callable[[], Any],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/webhooks")
    async def webhooks(request: Request) -> dict[str, Any]:
        require_full(request)
        return {
            "object": "list",
            "data": _public_webhook_payload(webhook_store_factory().list()),
            "secrets_redacted": True,
        }

    @router.post("/v1/webhooks")
    async def webhook_subscribe(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        deliver_mode = str(payload.get("deliver_mode") or "").strip()
        deliver_value = str(payload.get("deliver") or "desktop_inbox")
        if deliver_mode == "direct_platform":
            deliver_value = json.dumps(
                {
                    "mode": "direct_platform",
                    "platform": str(payload.get("platform") or "local"),
                    "target": str(payload.get("target") or ""),
                    "thread_id": payload.get("thread_id"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="webhook.subscribe",
            params={
                "name": str(payload.get("name") or ""),
                "events": [str(item) for item in list(payload.get("events") or [])],
                "prompt": str(payload.get("prompt") or ""),
                "deliver": deliver_value,
                "secret": payload.get("secret"),
                "user_id": payload.get("user_id"),
                "session_id": payload.get("session_id"),
            },
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Webhook subscriptions persist future external triggers and require ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "webhook.subscribe.intent"],
            object_name="webhook.subscribe_intent",
        )

    @router.delete("/v1/webhooks/{webhook_id}")
    async def webhook_delete(request: Request, webhook_id: str) -> dict[str, Any]:
        require_full(request)
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="webhook delete payload must be a JSON object")
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="webhook.remove",
            params={
                "webhook_id": str(webhook_id or ""),
                "user_id": payload.get("user_id"),
                "session_id": payload.get("session_id"),
            },
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Webhook removal changes durable trigger configuration and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "webhook.remove.intent"],
            object_name="webhook.remove_intent",
        )

    @router.post("/v1/webhooks/{webhook_id}/trigger")
    async def webhook_trigger(request: Request, webhook_id: str) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        params = {"webhook_id": webhook_id, **payload}
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="webhook.trigger",
            params=params,
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or f"Webhook trigger {webhook_id} requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "webhook.trigger.intent"],
            object_name="webhook.trigger_intent",
        )

    return router
