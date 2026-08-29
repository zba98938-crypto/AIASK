"""Neutral user, activity and retention routes.

Replaces ``/v1/desktop/users/*``, ``/v1/desktop/events``,
``/v1/desktop/feedback``, ``/v1/desktop/analytics/summary`` and
``/v1/desktop/retention/sweep``.

Every user-scoped read and write goes through ``require_user_scope``, which
demands a control token before one user's id can be used to reach another's
data.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Query, Request

from ..runtime import AgentRuntime


def create_users_router(
    *,
    runtime: AgentRuntime,
    require_api: Callable[[Request], None],
    require_control: Callable[[Request], None],
    require_user_scope: Callable[[Request, str | None], str],
    local_profile_payload: Callable[[], dict[str, Any]],
    save_local_profile: Callable[[dict[str, Any]], dict[str, Any]],
    event_batch_from_payload: Callable[[dict[str, Any], Request], list[dict[str, Any]]],
    request_context_payload: Callable[..., dict[str, Any]],
    truthy: Callable[[Any], bool],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/users/local-profile")
    async def local_profile_get(request: Request) -> dict[str, Any]:
        require_api(request)
        return local_profile_payload()

    @router.post("/v1/users/local-profile")
    async def local_profile_post(request: Request) -> dict[str, Any]:
        require_control(request)
        return save_local_profile(dict(await request.json() or {}))

    @router.patch("/v1/users/local-profile")
    async def local_profile_patch(request: Request) -> dict[str, Any]:
        require_control(request)
        return save_local_profile(dict(await request.json() or {}))

    @router.post("/v1/users/events")
    async def user_events(request: Request) -> dict[str, Any]:
        require_api(request)
        payload = dict(await request.json() or {})
        events = runtime.session_store.record_activity_events(event_batch_from_payload(payload, request))
        return {"object": "list", "data": events, "count": len(events), "secrets_redacted": True}

    @router.post("/v1/users/feedback")
    async def user_feedback(request: Request) -> dict[str, Any]:
        require_api(request)
        payload = dict(await request.json() or {})
        context = request_context_payload(payload, headers=request.headers)
        feedback = runtime.session_store.record_feedback({**context, **payload})
        return {"object": "aiask.feedback", "data": feedback, "secrets_redacted": True}

    @router.get("/v1/users/analytics/summary")
    async def analytics_summary(
        request: Request,
        user_id: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = require_user_scope(request, user_id) if user_id else None
        if scoped_user_id is None:
            # A cross-user aggregate is not scoped to a caller, so it needs the
            # control token rather than plain API access.
            require_control(request)
        return runtime.session_store.analytics_summary(user_id=scoped_user_id, limit=limit)

    @router.post("/v1/users/retention/sweep")
    async def retention_sweep(request: Request) -> dict[str, Any]:
        require_control(request)
        payload = dict(await request.json() or {})
        user_id = str(payload.get("user_id") or "").strip() or None
        return runtime.session_store.apply_retention_policies(
            user_id=user_id,
            dry_run=payload.get("dry_run") is not False,
        )

    @router.get("/v1/users/{user_id}/activity")
    async def user_activity(
        request: Request,
        user_id: str,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = require_user_scope(request, user_id)
        return runtime.session_store.user_activity_summary(user_id=scoped_user_id, limit=limit)

    @router.get("/v1/users/{user_id}/export")
    async def user_data_export(
        request: Request,
        user_id: str,
        limit: int = Query(500, ge=1, le=5000),
    ) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = require_user_scope(request, user_id)
        return runtime.session_store.export_user_data(user_id=scoped_user_id, limit=limit)

    @router.post("/v1/users/{user_id}/delete")
    async def user_data_delete(request: Request, user_id: str) -> dict[str, Any]:
        require_control(request)
        scoped_user_id = require_user_scope(request, user_id)
        payload = dict(await request.json() or {})
        return runtime.session_store.delete_user_data(
            user_id=scoped_user_id,
            include_conversations=payload.get("include_conversations") is not False,
            include_audit=payload.get("include_audit") is not False,
            hard_delete=truthy(payload.get("hard_delete")),
            dry_run=payload.get("dry_run") is not False,
            reason=str(payload.get("reason") or "user_data_delete"),
            actor=str(payload.get("actor") or scoped_user_id),
        )

    @router.get("/v1/users/{user_id}/learning-dataset")
    async def user_learning_dataset(
        request: Request,
        user_id: str,
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = require_user_scope(request, user_id)
        return runtime.session_store.learning_dataset(user_id=scoped_user_id, limit=limit)

    @router.get("/v1/users/{user_id}/recommendations")
    async def user_recommendations(
        request: Request,
        user_id: str,
        limit: int = Query(5, ge=1, le=20),
    ) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = require_user_scope(request, user_id)
        return runtime.session_store.workflow_recommendations(user_id=scoped_user_id, limit=limit)

    @router.get("/v1/users/{user_id}/data-policy")
    async def user_data_policy(request: Request, user_id: str) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = require_user_scope(request, user_id)
        return {
            "object": "aiask.user_data_policy",
            "data": runtime.session_store.get_user_data_policy(scoped_user_id),
        }

    @router.patch("/v1/users/{user_id}/data-policy")
    async def user_data_policy_patch(request: Request, user_id: str) -> dict[str, Any]:
        require_control(request)
        scoped_user_id = require_user_scope(request, user_id)
        return {
            "object": "aiask.user_data_policy",
            "data": runtime.session_store.update_user_data_policy(
                scoped_user_id, dict(await request.json() or {})
            ),
        }

    return router


__all__ = ["create_users_router"]
