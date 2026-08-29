from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse


def create_responses_router(
    *,
    require_api: Callable[[Request], None],
    require_control: Callable[[Request], None],
    select_runtime: Callable[[dict[str, Any], Request], tuple[Any, str]],
    messages_from_responses_payload: Callable[[dict[str, Any]], list[dict[str, Any]]],
    responses_payload: Callable[..., dict[str, Any]],
    response_sse: Callable[..., Any],
    chat_completion_payload: Callable[..., dict[str, Any]],
    chat_completion_sse: Callable[..., Any],
    get_response: Callable[[str], dict[str, Any] | None],
    delete_response: Callable[[str], bool],
    search_payload: Callable[..., dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    def _attachments(body: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            from aiask_agent.response_payloads import normalize_response_attachments

            return normalize_response_attachments(body)
        except Exception:
            return []

    @router.post("/v1/responses")
    async def responses(request: Request) -> Any:
        require_api(request)
        payload = await request.json()
        body = dict(payload or {})
        selected, mode = select_runtime(body, request)
        result = await selected.run(
            messages_from_responses_payload(body),
            session_id=body.get("session_id") or request.headers.get("X-AIASK-Session-Id"),
            user_id=body.get("user_id") or request.headers.get("X-AIASK-User-Id"),
            stream=bool(body.get("stream", False)),
        )
        model = str(body.get("model") or selected.model)
        response = responses_payload(result, model=model)
        response["metadata"]["mode"] = mode
        response["metadata"]["attachments"] = _attachments(body)
        response["metadata"]["attachment_count"] = len(response["metadata"]["attachments"])
        if bool(body.get("stream", False)):
            return StreamingResponse(response_sse(result, model=model), media_type="text/event-stream")
        return response

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        require_api(request)
        payload = await request.json()
        body = dict(payload or {})
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise HTTPException(400, detail="messages must be an array")
        selected, mode = select_runtime(body, request)
        result = await selected.run(
            [dict(item) for item in messages if isinstance(item, dict)],
            session_id=body.get("session_id") or request.headers.get("X-AIASK-Session-Id"),
            user_id=body.get("user_id") or request.headers.get("X-AIASK-User-Id"),
            stream=bool(body.get("stream", False)),
        )
        model = str(body.get("model") or selected.model)
        response = chat_completion_payload(result, model=model)
        response["aiask"]["mode"] = mode
        if bool(body.get("stream", False)):
            return StreamingResponse(chat_completion_sse(result, model=model), media_type="text/event-stream")
        return response

    @router.get("/v1/responses/{response_id}")
    async def response_get(request: Request, response_id: str) -> dict[str, Any]:
        require_api(request)
        payload = get_response(response_id)
        if payload is None:
            raise HTTPException(404, detail=f"response not found: {response_id}")
        return {"object": "response", **payload}

    @router.delete("/v1/responses/{response_id}")
    async def response_delete(request: Request, response_id: str) -> dict[str, Any]:
        require_control(request)
        return {"id": response_id, "object": "response.deleted", "deleted": delete_response(response_id)}

    @router.get("/v1/search")
    async def search(
        request: Request,
        query: str = "",
        q: str = "",
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 20,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        require_api(request)
        return search_payload(
            query=query,
            q=q,
            session_id=session_id,
            user_id=user_id,
            limit=limit,
            include_archived=include_archived,
        )

    return router
