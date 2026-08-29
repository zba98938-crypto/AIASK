from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from .run_payloads import _normalize_run_event
from .server_http_utils import json_dumps


async def sse_events_stream(
    events: list[dict[str, Any]],
    *,
    normalize_run_event: Callable[[dict[str, Any]], dict[str, Any]] = _normalize_run_event,
) -> AsyncIterator[bytes]:
    for raw_event in events:
        event = normalize_run_event(raw_event)
        if event.get("id") is not None:
            yield f"id: {event['id']}\n".encode("utf-8")
        if event.get("event"):
            yield f"event: {event['event']}\n".encode("utf-8")
        yield b"data: "
        yield json_dumps(event)
        yield b"\n\n"


async def chat_completion_sse_stream(result: Any, *, model: str) -> AsyncIterator[bytes]:
    created = int(time.time())
    chunks = [
        {
            "id": result.response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        },
        {
            "id": result.response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": result.content}, "finish_reason": None}],
            "aiask": {"session_id": result.session_id, "run_id": result.run_id},
        },
        {
            "id": result.response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    for chunk in chunks:
        yield b"data: "
        yield json_dumps(chunk)
        yield b"\n\n"
    yield b"data: [DONE]\n\n"


async def response_sse_stream(result: Any, *, model: str) -> AsyncIterator[bytes]:
    events = [
        {"event": "response.created", "data": {"id": result.response_id, "status": "in_progress", "model": model}},
        {"event": "response.output_text.delta", "data": {"id": result.response_id, "delta": result.content}},
        {"event": "response.completed", "data": {"id": result.response_id, "status": result.status, "run_id": result.run_id}},
    ]
    for event in events:
        yield f"event: {event['event']}\n".encode("utf-8")
        yield b"data: "
        yield json_dumps(event["data"])
        yield b"\n\n"
    yield b"data: [DONE]\n\n"
