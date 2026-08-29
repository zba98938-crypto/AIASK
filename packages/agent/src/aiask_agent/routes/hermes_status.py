from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request


def create_hermes_status_router(
    *,
    require_api: Callable[[Request], None],
    hermes_status_payload: Callable[[], dict[str, Any]],
    hermes_readiness_payload: Callable[[], dict[str, Any]],
    financial_readiness_payload: Callable[[], Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/hermes/status")
    async def hermes_status(request: Request) -> dict[str, Any]:
        require_api(request)
        return hermes_status_payload()

    @router.get("/v1/hermes/readiness")
    async def hermes_readiness(request: Request) -> dict[str, Any]:
        require_api(request)
        return hermes_readiness_payload()

    @router.get("/v1/financial-system/readiness")
    async def financial_readiness(request: Request) -> dict[str, Any]:
        require_api(request)
        return await financial_readiness_payload()

    return router
