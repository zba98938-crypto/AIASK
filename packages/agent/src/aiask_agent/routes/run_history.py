from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..session_store_utils import redact_audit_text


def create_run_history_router(
    *,
    require_api: Callable[[Request], None],
    session_store: Any,
    sse_events: Callable[..., Any],
    normalize_run_event: Callable[[dict[str, Any]], dict[str, Any]],
    run_trace_eval_payload: Callable[[str], dict[str, Any]],
    artifact_content_payload: Callable[..., dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str, after: int = 0) -> StreamingResponse:
        require_api(request)
        events = session_store.list_run_events(run_id, after_event_id=after)
        normalized = [normalize_run_event(event) for event in events]
        return StreamingResponse(sse_events(normalized), media_type="text/event-stream")

    @router.get("/v1/runs/{run_id}/events/stream")
    async def run_events_stream(request: Request, run_id: str, after: int = 0) -> StreamingResponse:
        return await run_events(request, run_id, after=after)

    @router.get("/v1/runs/{run_id}/artifacts")
    async def run_artifacts(request: Request, run_id: str, kind: str | None = None, limit: int = 100) -> dict[str, Any]:
        require_api(request)
        return {
            "object": "list",
            "run_id": run_id,
            "data": session_store.list_artifacts(run_id=run_id, kind=kind, limit=limit),
        }

    @router.get("/v1/runs/{run_id}/sources")
    async def run_sources(request: Request, run_id: str, source_type: str | None = None, limit: int = 100) -> dict[str, Any]:
        require_api(request)
        return {
            "object": "list",
            "run_id": run_id,
            "data": session_store.list_sources(run_id=run_id, source_type=source_type, limit=limit),
        }

    @router.get("/v1/runs/{run_id}/trace-eval")
    async def run_trace_eval(request: Request, run_id: str) -> dict[str, Any]:
        require_api(request)
        try:
            return run_trace_eval_payload(run_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, detail=redact_audit_text(exc)) from exc

    @router.get("/v1/runs/{run_id}/tool-invocations")
    async def run_tool_invocations(request: Request, run_id: str, limit: int = 100) -> dict[str, Any]:
        require_api(request)
        return {
            "object": "list",
            "run_id": run_id,
            "data": session_store.list_tool_invocations(run_id=run_id, limit=limit),
        }

    @router.get("/v1/runs/{run_id}")
    async def run_get(request: Request, run_id: str) -> dict[str, Any]:
        require_api(request)
        item = session_store.get_run(run_id)
        if item is None:
            raise HTTPException(404, detail=f"run not found: {run_id}")
        return {"object": "run", **item}

    @router.get("/v1/sessions/{session_id}/artifacts")
    async def session_artifacts(request: Request, session_id: str, kind: str | None = None, limit: int = 100) -> dict[str, Any]:
        require_api(request)
        return {
            "object": "list",
            "session_id": session_id,
            "data": session_store.list_artifacts(session_id=session_id, kind=kind, limit=limit),
        }

    @router.get("/v1/sessions/{session_id}/sources")
    async def session_sources(request: Request, session_id: str, source_type: str | None = None, limit: int = 100) -> dict[str, Any]:
        require_api(request)
        return {
            "object": "list",
            "session_id": session_id,
            "data": session_store.list_sources(session_id=session_id, source_type=source_type, limit=limit),
        }

    @router.get("/v1/sessions/{session_id}/messages")
    async def session_messages(request: Request, session_id: str, limit: int = 200) -> dict[str, Any]:
        require_api(request)
        return {
            "object": "list",
            "session_id": session_id,
            "data": session_store.list_session_messages(session_id, limit=limit),
        }

    @router.get("/v1/artifacts/{artifact_id}/content")
    async def artifact_content(request: Request, artifact_id: str, max_bytes: int = 262144) -> dict[str, Any]:
        require_api(request)
        try:
            return artifact_content_payload(artifact_id, max_bytes=max_bytes)
        except FileNotFoundError as exc:
            raise HTTPException(404, detail=redact_audit_text(exc)) from exc

    @router.get("/v1/artifacts/{artifact_id}")
    async def artifact_get(request: Request, artifact_id: str) -> dict[str, Any]:
        require_api(request)
        item = session_store.get_artifact(artifact_id)
        if item is None:
            raise HTTPException(404, detail=f"artifact not found: {artifact_id}")
        return {"object": "artifact", **item}

    @router.get("/v1/sources/{source_id}")
    async def source_get(request: Request, source_id: str) -> dict[str, Any]:
        require_api(request)
        item = session_store.get_source(source_id)
        if item is None:
            raise HTTPException(404, detail=f"source not found: {source_id}")
        return {"object": "source", **item}

    @router.get("/v1/tool-invocations/{invocation_id}")
    async def tool_invocation_get(request: Request, invocation_id: str) -> dict[str, Any]:
        require_api(request)
        item = session_store.get_tool_invocation(invocation_id)
        if item is None:
            raise HTTPException(404, detail=f"tool invocation not found: {invocation_id}")
        return {"object": "tool_invocation", **item}

    return router
