from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request


def create_desktop_runs_router(
    *,
    require_api: Callable[[Request], None],
    desktop_runs_payload: Callable[..., dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/desktop/runs")
    async def desktop_runs(
        request: Request,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        require_api(request)
        return desktop_runs_payload(session_id=session_id, status=status, limit=limit)

    return router
