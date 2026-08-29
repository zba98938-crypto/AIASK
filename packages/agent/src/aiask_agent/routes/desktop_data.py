from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..session_store_utils import redact_audit_text
from ..stock_data_sources import list_stock_data_sources, save_stock_data_source, test_stock_data_source


def create_desktop_data_router(
    *,
    require_api: Callable[[Request], None],
    control_authorized: Callable[[Request], tuple[bool, str | None]],
    desktop_settings_status_payload: Callable[[Request | None], dict[str, Any]],
    desktop_data_status_payload: Callable[[dict[str, Any] | None], Awaitable[dict[str, Any]]],
    desktop_data_sync_plan_payload: Callable[[dict[str, Any] | None], Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/desktop/settings/status")
    async def desktop_settings_status(request: Request) -> dict[str, Any]:
        require_api(request)
        return desktop_settings_status_payload(request)

    @router.get("/v1/desktop/data/status")
    async def desktop_data_status(request: Request, codes: str = "", max_stale_days: int = 5) -> dict[str, Any]:
        require_api(request)
        code_list = [item.strip() for item in str(codes or "").replace("\n", ",").split(",") if item.strip()]
        return await desktop_data_status_payload({"codes": code_list, "max_stale_days": max_stale_days})

    @router.post("/v1/desktop/data/sync-plan")
    async def desktop_data_sync_plan(request: Request) -> dict[str, Any]:
        require_api(request)
        return await desktop_data_sync_plan_payload(dict(await request.json() or {}))

    @router.get("/v1/desktop/stock-data-sources")
    async def desktop_stock_data_sources(request: Request) -> dict[str, Any]:
        require_api(request)
        return list_stock_data_sources()

    @router.post("/v1/desktop/stock-data-sources")
    async def desktop_stock_data_source_save(request: Request) -> dict[str, Any]:
        ok, reason = control_authorized(request)
        if not ok:
            raise HTTPException(503 if reason == "control token is not configured" else 401, detail=reason or "unauthorized")
        try:
            return save_stock_data_source(dict(await request.json() or {}))
        except ValueError as exc:
            raise HTTPException(400, detail=redact_audit_text(exc)) from exc

    @router.post("/v1/desktop/stock-data-sources/test")
    async def desktop_stock_data_source_test(request: Request) -> dict[str, Any]:
        ok, reason = control_authorized(request)
        if not ok:
            raise HTTPException(503 if reason == "control token is not configured" else 401, detail=reason or "unauthorized")
        return test_stock_data_source(dict(await request.json() or {}))

    return router
