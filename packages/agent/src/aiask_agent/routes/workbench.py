"""Neutral workbench summary and session batch routes.

Replaces ``/v1/desktop/workbench/summary``. ``/v1/desktop/runs`` is not replaced
here: the neutral ``/v1/runs`` family already covers run history, so the old
alias simply goes away.

``/v1/sessions/batch/archive`` was already on a neutral path; it moved here from
the removed desktop_assets router unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ..session_store_utils import redact_audit_text


def create_workbench_router(
    *,
    require_api: Callable[[Request], None],
    require_control: Callable[[Request], None],
    session_store: Any,
    workbench_summary_payload: Callable[..., dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/workbench/summary")
    async def workbench_summary(
        request: Request,
        user_id: str | None = None,
        session_limit: int = Query(8, ge=1, le=100),
        run_limit: int = Query(8, ge=1, le=100),
    ) -> dict[str, Any]:
        require_api(request)
        return workbench_summary_payload(
            user_id=user_id,
            session_limit=session_limit,
            run_limit=run_limit,
        )

    @router.post("/v1/sessions/batch/archive")
    async def sessions_batch_archive(request: Request) -> dict[str, Any]:
        require_control(request)
        payload = dict(await request.json() or {})
        session_ids = [str(item).strip() for item in list(payload.get("session_ids") or []) if str(item).strip()]
        if not session_ids:
            raise HTTPException(400, detail="session_ids is required")

        results = []
        for session_id in session_ids:
            try:
                archived = session_store.set_session_archived(
                    session_id,
                    archived=True,
                    reason=str(payload.get("reason") or "batch archive"),
                    actor=str(payload.get("actor") or payload.get("user_id") or "control_token"),
                )
                results.append({"session_id": session_id, "success": True, "data": archived})
            except FileNotFoundError:
                results.append(
                    {
                        "session_id": session_id,
                        "success": False,
                        "error": "session_not_found",
                        "error_code": "SESSION_NOT_FOUND",
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "session_id": session_id,
                        "success": False,
                        "error": redact_audit_text(exc),
                        "error_code": "SESSION_ARCHIVE_FAILED",
                    }
                )

        return {
            "object": "aiask.session_archive_batch",
            "success": True,
            "data": {
                "results": results,
                "archived_count": sum(1 for item in results if item.get("success")),
                "failed_count": sum(1 for item in results if not item.get("success")),
            },
            "error": None,
            "error_code": None,
            "secrets_redacted": True,
        }

    return router


__all__ = ["create_workbench_router"]
