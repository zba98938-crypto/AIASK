from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..session_store_utils import redact_audit_text


def create_hermes_router(
    *,
    require_api: Callable[[Request], None],
    require_control: Callable[[Request], Any],
    require_full: Callable[[Request], Any],
    hermes_toolsets_payload: Callable[[], dict[str, Any]],
    tool_catalog_payload: Callable[..., dict[str, Any]],
    hermes_config_payload: Callable[[Any], dict[str, Any]],
    hermes_sessions_payload: Callable[..., dict[str, Any]],
    hermes_session_create_payload: Callable[[dict[str, Any]], dict[str, Any]],
    hermes_handoffs_payload: Callable[..., dict[str, Any]],
    hermes_resume_context_payload: Callable[[str], dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/hermes/toolsets")
    async def hermes_toolsets(request: Request) -> dict[str, Any]:
        require_api(request)
        return hermes_toolsets_payload()

    @router.get("/v1/hermes/tools")
    async def hermes_tools(request: Request) -> dict[str, Any]:
        return tool_catalog_payload(require_full(request), implementation="aiask_native")

    @router.get("/v1/hermes/config")
    async def hermes_config(request: Request) -> dict[str, Any]:
        return hermes_config_payload(require_full(request))

    @router.get("/v1/hermes/sessions")
    async def hermes_sessions(
        request: Request,
        user_id: str | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        require_api(request)
        return hermes_sessions_payload(user_id=user_id, limit=limit, include_archived=include_archived)

    @router.post("/v1/hermes/sessions")
    async def hermes_session_create(request: Request) -> dict[str, Any]:
        require_control(request)
        payload = dict(await request.json() or {})
        return hermes_session_create_payload(payload)

    @router.get("/v1/hermes/handoffs")
    async def hermes_handoffs(
        request: Request,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        include_completed: bool = False,
    ) -> dict[str, Any]:
        require_full(request)
        return hermes_handoffs_payload(
            user_id=user_id,
            session_id=session_id,
            status=status,
            limit=limit,
            include_completed=include_completed,
        )

    @router.get("/v1/hermes/sessions/{session_id}/resume-context")
    async def hermes_session_resume_context(request: Request, session_id: str) -> dict[str, Any]:
        require_full(request)
        try:
            return hermes_resume_context_payload(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, detail=redact_audit_text(exc)) from exc

    return router
