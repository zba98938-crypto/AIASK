from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request


def create_full_controls_router(
    *,
    require_full: Callable[[Request], Any],
    process_list: Callable[..., list[dict[str, Any]]],
    list_terminal_backends: Callable[[], list[dict[str, Any]]],
    terminal_sessions: Callable[..., list[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/processes")
    async def processes(request: Request, session_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        require_full(request)
        return {"object": "list", "data": process_list(session_id=session_id, limit=limit)}

    @router.get("/v1/terminal/backends")
    async def terminal_backends_api(request: Request) -> dict[str, Any]:
        require_full(request)
        return {"object": "list", "data": list_terminal_backends()}

    @router.get("/v1/terminal/sessions")
    async def terminal_sessions_api(request: Request, limit: int = 200) -> dict[str, Any]:
        require_full(request)
        return {"object": "list", "data": terminal_sessions(limit=limit)}

    @router.get("/v1/terminal/backends/{name}/sessions")
    async def terminal_backend_sessions_api(request: Request, name: str, limit: int = 200) -> dict[str, Any]:
        require_full(request)
        backend = str(name or "local").strip().lower()
        data = [
            item
            for item in terminal_sessions(limit=limit)
            if str(item.get("backend") or "local") == backend
        ]
        return {"object": "list", "backend": backend, "data": data}

    @router.get("/v1/browser/sessions")
    async def browser_sessions(request: Request) -> dict[str, Any]:
        require_full(request)
        return {"object": "list", "data": [{"name": "default", "provider": "playwright", "persistent": True}]}

    return router
