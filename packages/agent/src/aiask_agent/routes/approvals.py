from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..approvals import public_approval, public_approvals
from ..session_store_utils import redact_audit_text


def create_approvals_router(
    *,
    require_api: Callable[[Request], None],
    require_control: Callable[[Request], Any],
    approval_store_factory: Callable[[], Any],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/approvals")
    async def approvals(request: Request, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        require_api(request)
        return {
            "object": "list",
            "data": public_approvals(approval_store_factory().list(status=status, limit=limit)),
            "secrets_redacted": True,
        }

    @router.post("/v1/approvals/{approval_id}/{decision}")
    async def approval_decide(request: Request, approval_id: str, decision: str) -> dict[str, Any]:
        require_control(request)
        payload = await request.json()
        item = approval_store_factory().decide(
            approval_id,
            approved=decision == "approve",
            reason=payload.get("reason"),
        )
        if item is None:
            raise HTTPException(404, detail=redact_audit_text(f"approval not found: {approval_id}"))
        return {"object": "approval", **(public_approval(item) or {})}

    return router
