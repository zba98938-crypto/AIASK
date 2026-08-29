from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request


def create_desktop_workbench_router(
    *,
    require_api: Callable[[Request], None],
    workbench_summary_payload: Callable[..., dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/desktop/workbench/summary")
    async def desktop_workbench_summary(
        request: Request,
        user_id: str | None = None,
        session_limit: int = 8,
        run_limit: int = 8,
    ) -> dict[str, Any]:
        require_api(request)
        return workbench_summary_payload(user_id=user_id, session_limit=session_limit, run_limit=run_limit)

    return router
