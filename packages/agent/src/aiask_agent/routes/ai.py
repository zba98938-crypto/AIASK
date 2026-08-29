from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..session_store_utils import redact_audit_text


def create_ai_router(
    *,
    require_api: Callable[[Request], None],
    require_control: Callable[[Request], None],
    ai_status_payload: Callable[[], dict[str, Any]],
    ai_config_payload: Callable[[], dict[str, Any]],
    ai_config_save_payload: Callable[[dict[str, Any] | None], Awaitable[dict[str, Any]]],
    ai_smoke_payload: Callable[[dict[str, Any] | None], Awaitable[dict[str, Any]]],
    ai_models_payload: Callable[[], Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/ai/status")
    async def ai_status(request: Request) -> dict[str, Any]:
        require_api(request)
        return ai_status_payload()

    @router.get("/v1/ai/config")
    async def ai_config(request: Request) -> dict[str, Any]:
        require_api(request)
        return ai_config_payload()

    @router.patch("/v1/ai/config")
    async def ai_config_save(request: Request) -> dict[str, Any]:
        require_control(request)
        try:
            payload = dict(await request.json() or {})
            return await ai_config_save_payload(payload)
        except ValueError as exc:
            raise HTTPException(400, detail=redact_audit_text(exc)) from exc

    @router.post("/v1/ai/smoke")
    async def ai_smoke(request: Request) -> dict[str, Any]:
        require_api(request)
        return await ai_smoke_payload(dict(await request.json() or {}))

    @router.get("/v1/ai/models")
    async def ai_models(request: Request) -> dict[str, Any]:
        require_api(request)
        return await ai_models_payload()

    return router
