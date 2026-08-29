"""Neutral settings and market-data routes.

Replaces ``/v1/desktop/settings/*``, ``/v1/desktop/data/*`` and
``/v1/desktop/stock-data-sources``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ..session_store_utils import redact_audit_text
from ..stock_data_sources import list_stock_data_sources, save_stock_data_source, test_stock_data_source


def _split_codes(raw: str | None) -> list[str]:
    return [item.strip() for item in str(raw or "").replace("\n", ",").split(",") if item.strip()]


def create_data_settings_router(
    *,
    require_api: Callable[[Request], None],
    control_authorized: Callable[[Request], tuple[bool, str | None]],
    settings_status_payload: Callable[[Request | None], dict[str, Any]],
    data_status_payload: Callable[[dict[str, Any] | None], Awaitable[dict[str, Any]]],
    data_sync_plan_payload: Callable[[dict[str, Any] | None], Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    def _require_control(request: Request) -> None:
        ok, reason = control_authorized(request)
        if not ok:
            raise HTTPException(
                503 if reason == "control token is not configured" else 401,
                detail=reason or "unauthorized",
            )

    @router.get("/v1/settings/status")
    async def settings_status(request: Request) -> dict[str, Any]:
        require_api(request)
        return settings_status_payload(request)

    @router.get("/v1/data/status")
    async def data_status(
        request: Request,
        codes: str = "",
        max_stale_days: int = Query(5, ge=1, le=365),
    ) -> dict[str, Any]:
        require_api(request)
        return await data_status_payload({"codes": _split_codes(codes), "max_stale_days": max_stale_days})

    @router.post("/v1/data/sync-plan")
    async def data_sync_plan(request: Request) -> dict[str, Any]:
        require_api(request)
        return await data_sync_plan_payload(dict(await request.json() or {}))

    @router.get("/v1/data/stock-data-sources")
    async def stock_data_sources(request: Request) -> dict[str, Any]:
        require_api(request)
        return list_stock_data_sources()

    @router.post("/v1/data/stock-data-sources")
    async def stock_data_source_save(request: Request) -> dict[str, Any]:
        _require_control(request)
        try:
            return save_stock_data_source(dict(await request.json() or {}))
        except ValueError as exc:
            raise HTTPException(400, detail=redact_audit_text(exc)) from exc

    @router.post("/v1/data/stock-data-sources/test")
    async def stock_data_source_test(request: Request) -> dict[str, Any]:
        _require_control(request)
        return test_stock_data_source(dict(await request.json() or {}))

    return router


__all__ = ["create_data_settings_router"]
