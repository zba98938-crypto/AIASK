from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .action_intents import create_external_action_intent_payload
from ..session_store_utils import redact_audit_text, sanitize_for_audit, sanitize_public_response


FullToolCall = Callable[[Request, str, dict[str, Any]], Any]


def _public_gateway_payload(value: Any) -> Any:
    return sanitize_public_response(value, max_depth=8, max_items=200, max_text=4000)


def create_gateway_router(
    *,
    require_api: Callable[[Request], Any],
    require_full: Callable[[Request], Any],
    full_tool_call: FullToolCall,
    intent_store: Any,
    session_store: Any,
    gateway_runtime_factory: Callable[[], Any],
    message_store_factory: Callable[[], Any],
    directory_store_factory: Callable[[], Any],
    config_store_factory: Callable[[], Any],
    delivery_router_factory: Callable[..., Any],
    adapter_for: Callable[[Any], Any],
    normalize_platform: Callable[[str | None], str],
    gateway_daemon_status_payload: Callable[[], dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/gateway/status")
    async def gateway_status_api(request: Request) -> dict[str, Any]:
        require_full(request)
        return _public_gateway_payload(gateway_runtime_factory().status())

    @router.get("/v1/gateway/platforms")
    async def gateway_platforms_api(request: Request) -> dict[str, Any]:
        require_full(request)
        return {"object": "list", "data": _public_gateway_payload(gateway_runtime_factory().list_platforms()), "secrets_redacted": True}

    @router.get("/v1/gateway/pairing")
    async def gateway_pairing_status_api(
        request: Request,
        platform: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        result = await full_tool_call(
            request,
            "agent_gateway_pairing",
            {
                "action": "status",
                "platform": platform,
                "user_id": user_id,
                "session_id": session_id,
            },
        )
        return {
            "object": "gateway.pairing",
            "success": bool(result.get("success", True)),
            "data": sanitize_for_audit(result.get("data"), max_depth=6, max_items=120, max_text=2000),
            "error": redact_audit_text(result.get("error")) if result.get("error") is not None else None,
            "error_code": result.get("error_code"),
            "meta": sanitize_for_audit(result.get("meta"), max_depth=6, max_items=120, max_text=2000),
            "secrets_redacted": True,
        }

    @router.post("/v1/gateway/pairing")
    async def gateway_pairing_create_api(request: Request) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        require_full(request)
        action = str(payload.get("action") or "create").strip().lower() or "create"
        if action != "create":
            raise HTTPException(400, detail=f"unsupported gateway pairing action: {action}")
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="gateway.pairing_create",
            params={
                "action": "create",
                "platform": payload.get("platform"),
                "user_id": payload.get("user_id"),
                "session_id": payload.get("session_id"),
            },
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Gateway pairing changes platform/session routing state and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "gateway.pairing.intent"],
            object_name="gateway.pairing_intent",
        )

    @router.get("/v1/gateway/messages")
    async def gateway_messages_api(request: Request, platform: str | None = None, limit: int = 100) -> dict[str, Any]:
        require_full(request)
        return {"object": "list", "data": _public_gateway_payload(message_store_factory().list(platform=platform, limit=limit)), "secrets_redacted": True}

    @router.get("/v1/gateway/directory")
    async def gateway_directory_api(request: Request, platform: str | None = None, kind: str | None = None, limit: int = 200) -> dict[str, Any]:
        require_full(request)
        return {"object": "list", "data": _public_gateway_payload(directory_store_factory().list(platform=platform, kind=kind, limit=limit)), "secrets_redacted": True}

    @router.post("/v1/gateway/directory/refresh")
    async def gateway_directory_refresh_api(request: Request) -> dict[str, Any]:
        require_full(request)
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="directory refresh payload must be a JSON object")
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="gateway.directory_refresh",
            params={"action": "refresh"},
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Refreshing the gateway directory changes platform routing aliases and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "gateway.directory_refresh.intent"],
            object_name="gateway.directory_refresh_intent",
        )

    @router.post("/v1/gateway/send")
    async def gateway_send_api(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="gateway.send_message",
            params={
                "platform": str(payload.get("platform") or "local"),
                "target": str(payload.get("target") or ""),
                "message": str(payload.get("message") or ""),
                "thread_id": payload.get("thread_id"),
                "session_id": payload.get("session_id"),
                "user_id": payload.get("user_id"),
                "media_paths": [str(item) for item in list(payload.get("media_paths") or [])],
            },
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Gateway HTTP send requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "gateway.send.intent"],
            object_name="gateway.message_intent",
        )

    @router.post("/v1/gateway/direct-deliver")
    async def gateway_direct_deliver_api(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="gateway.direct_deliver",
            params={
                "platform": str(payload.get("platform") or "local"),
                "target": str(payload.get("target") or ""),
                "message": str(payload.get("message") or ""),
                "thread_id": payload.get("thread_id"),
                "session_id": payload.get("session_id"),
                "user_id": payload.get("user_id"),
                "media_paths": [str(item) for item in list(payload.get("media_paths") or [])],
            },
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Gateway direct delivery requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "gateway.direct_deliver.intent"],
            object_name="gateway.direct_delivery_intent",
        )

    @router.post("/v1/gateway/messages/{message_id}/retry")
    async def gateway_retry_api(request: Request, message_id: str) -> dict[str, Any]:
        require_full(request)
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="retry payload must be a JSON object")
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="gateway.retry",
            params={
                "message_id": str(message_id or ""),
                "session_id": payload.get("session_id"),
                "user_id": payload.get("user_id"),
            },
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Gateway retry requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "gateway.retry.intent"],
            object_name="gateway.retry_intent",
        )

    @router.post("/v1/gateway/platforms/{platform}/start")
    async def gateway_platform_start_api(request: Request, platform: str) -> dict[str, Any]:
        require_full(request)
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="platform start payload must be a JSON object")
        normalized = normalize_platform(platform)
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="gateway.platform_start",
            params={"platform": normalized},
            request_payload={**payload, "platform": normalized},
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Gateway platform start controls an external integration runtime and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "gateway.platform_start.intent"],
            object_name="gateway.platform_start_intent",
        )

    @router.post("/v1/gateway/platforms/{platform}/stop")
    async def gateway_platform_stop_api(request: Request, platform: str) -> dict[str, Any]:
        require_full(request)
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="platform stop payload must be a JSON object")
        normalized = normalize_platform(platform)
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="gateway.platform_stop",
            params={"platform": normalized},
            request_payload={**payload, "platform": normalized},
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Gateway platform stop controls an external integration runtime and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "gateway.platform_stop.intent"],
            object_name="gateway.platform_stop_intent",
        )

    @router.get("/v1/gateway/platforms/{platform}/health")
    async def gateway_platform_health_api(request: Request, platform: str) -> dict[str, Any]:
        require_full(request)
        status = config_store_factory().platform_status(normalize_platform(platform))
        data = await adapter_for(status).health()
        data["runtime"] = gateway_runtime_factory().runtime_status()
        recent = message_store_factory().list(platform=platform, limit=20)
        data["last_inbound"] = next((item for item in recent if item.get("direction") == "inbound"), None)
        data["last_outbound"] = next((item for item in recent if item.get("direction") == "outbound"), None)
        return {"object": "gateway.platform_health", "data": _public_gateway_payload(data), "secrets_redacted": True}

    @router.post("/v1/gateway/webhooks/{platform}")
    async def gateway_webhook_api(request: Request, platform: str) -> dict[str, Any]:
        require_api(request)
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="webhook payload must be a JSON object")
        signature = request.headers.get("X-AIASK-Gateway-Signature") or request.headers.get("X-Hub-Signature-256")
        status = config_store_factory().platform_status(normalize_platform(platform))
        adapter_result = await adapter_for(status).handle_inbound(payload=payload, headers=dict(request.headers), body=raw_body)
        item = delivery_router_factory(messages=message_store_factory()).record_inbound(
            platform=platform,
            payload=dict(adapter_result.get("payload") or payload),
            signature=signature,
            verified=bool(adapter_result.get("verified")),
            adapter_result=adapter_result,
        )
        return {"object": "gateway.inbound", "data": _public_gateway_payload(item), "secrets_redacted": True}

    @router.get("/v1/gateway/daemon/status")
    async def gateway_daemon_status_api(request: Request) -> dict[str, Any]:
        require_full(request)
        return _public_gateway_payload(gateway_daemon_status_payload())

    return router
