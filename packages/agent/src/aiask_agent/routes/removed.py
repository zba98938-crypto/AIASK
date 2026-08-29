"""Catch-all for the removed Desktop facade.

``/v1/desktop/*`` existed only to serve the Electron/React workbench, which is
no longer part of this tree. Rather than deleting 40-odd route registrations and
letting the framework answer a bare ``{"detail":"Not Found"}``, one interceptor
answers every removed path with a structured 404 that names its replacement.

That distinction matters: a caller can tell "this moved to /v1/finance/broker"
apart from "you typo'd the URL", and the behaviour is testable in one place
instead of being an absence of code.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..api_envelope import removed_route_payload


def create_removed_routes_router() -> APIRouter:
    router = APIRouter()

    @router.api_route(
        "/v1/desktop/{removed_path:path}",
        methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        include_in_schema=False,
    )
    async def removed_desktop_route(request: Request, removed_path: str) -> JSONResponse:
        return JSONResponse(status_code=404, content=removed_route_payload(request.url.path))

    return router


__all__ = ["create_removed_routes_router"]
