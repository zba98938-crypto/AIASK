from __future__ import annotations

import time
from typing import Any

_ATTACHMENT_TEXT_LIMIT = 12_000
_ATTACHMENT_PREVIEW_LIMIT = 4_000
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {
    "application/json",
    "application/csv",
    "application/xml",
    "application/x-ndjson",
}
_TEXT_EXTENSIONS = (".txt", ".md", ".markdown", ".json", ".csv", ".xml", ".log", ".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html")


def normalize_response_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("attachments")
    if not isinstance(raw, list):
        context = payload.get("context")
        if isinstance(context, dict):
            raw = context.get("uploaded_files")
    if not isinstance(raw, list):
        return []

    attachments: list[dict[str, Any]] = []
    remaining_text_budget = _ATTACHMENT_TEXT_LIMIT
    for index, item in enumerate(raw[:12]):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("filename") or item.get("id") or f"attachment_{index + 1}")
        mime_type = str(item.get("mime_type") or item.get("type") or "application/octet-stream")
        size = int(item.get("size") or 0)
        text_preview = item.get("text_preview")
        is_text_like = mime_type.startswith(_TEXT_MIME_PREFIXES) or mime_type in _TEXT_MIME_TYPES or name.lower().endswith(_TEXT_EXTENSIONS)
        parsed_text = ""
        parse_status = "uploaded_unparsed"
        if is_text_like and isinstance(text_preview, str) and remaining_text_budget > 0:
            parsed_text = text_preview[: min(_ATTACHMENT_PREVIEW_LIMIT, remaining_text_budget)]
            remaining_text_budget -= len(parsed_text)
            parse_status = "parsed_text_preview"
        elif is_text_like:
            parse_status = "text_content_unavailable"
        attachments.append(
            {
                "id": str(item.get("id") or item.get("file_id") or name),
                "name": name,
                "mime_type": mime_type,
                "size": size,
                "parse_status": str(item.get("parse_status") or parse_status),
                "text_preview": parsed_text,
            }
        )
    return attachments


def attachment_context_message(attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not attachments:
        return None
    lines = ["Desktop attachments supplied for this request:"]
    for item in attachments:
        lines.append(f"- {item['name']} ({item['mime_type']}, {item['size']} bytes): {item['parse_status']}")
        text_preview = str(item.get("text_preview") or "")
        if text_preview:
            lines.append("  text_preview:")
            lines.append(text_preview)
    return {
        "role": "system",
        "content": "\n".join(lines),
    }


def messages_from_responses_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    attachment_message = attachment_context_message(normalize_response_attachments(payload))
    if isinstance(payload.get("messages"), list):
        messages = [dict(item) for item in payload["messages"] if isinstance(item, dict)]
        return ([attachment_message] if attachment_message else []) + messages
    raw_input = payload.get("input", "")
    if isinstance(raw_input, str):
        messages = [{"role": "user", "content": raw_input}]
        return ([attachment_message] if attachment_message else []) + messages
    if isinstance(raw_input, list):
        messages: list[dict[str, Any]] = []
        for item in raw_input:
            if isinstance(item, dict) and item.get("role"):
                messages.append({"role": item.get("role"), "content": item.get("content", "")})
            elif isinstance(item, str):
                messages.append({"role": "user", "content": item})
        return ([attachment_message] if attachment_message else []) + messages
    return []


def chat_completion_payload(result: Any, *, model: str) -> dict[str, Any]:
    return {
        "id": result.response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": result.usage,
        "aiask": {
            "session_id": result.session_id,
            "run_id": result.run_id,
            "tool_calls": result.tool_calls,
            "audit_events": result.audit_events,
            "events": result.events,
            "context_summary_id": result.context_summary_id,
            "planner_steps": result.planner_steps,
            "subruns": result.subruns,
        },
    }


def responses_payload(result: Any, *, model: str) -> dict[str, Any]:
    return {
        "id": result.response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output_text": result.content,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": result.content}],
            }
        ],
        "usage": result.usage,
        "metadata": {
            "session_id": result.session_id,
            "run_id": result.run_id,
            "tool_calls": result.tool_calls,
            "audit_events": result.audit_events,
            "events": result.events,
            "context_summary_id": result.context_summary_id,
            "planner_steps": result.planner_steps,
            "subruns": result.subruns,
        },
    }
