from __future__ import annotations

import time
from typing import Any

from .desktop_payloads import local_profile_payload


def request_user_id_from_payload(payload: dict[str, Any] | None, *, headers: Any | None = None) -> str:
    body = dict(payload or {})
    header_value = ""
    if headers is not None:
        try:
            header_value = str(headers.get("X-AIASK-User-Id") or "")
        except Exception:
            header_value = ""
    return str(body.get("user_id") or header_value or local_profile_payload().get("user_id") or "local").strip() or "local"


def request_context_payload(payload: dict[str, Any] | None, *, headers: Any | None = None) -> dict[str, Any]:
    body = dict(payload or {})

    def header(name: str) -> str:
        if headers is None:
            return ""
        try:
            return str(headers.get(name) or "")
        except Exception:
            return ""

    return {
        "user_id": request_user_id_from_payload(body, headers=headers),
        "session_id": str(body.get("session_id") or header("X-AIASK-Session-Id") or "").strip() or None,
        "run_id": str(body.get("run_id") or header("X-AIASK-Run-Id") or "").strip() or None,
        "trace_id": str(body.get("trace_id") or header("X-AIASK-Trace-Id") or f"trace_{int(time.time() * 1000)}").strip(),
        "source": str(body.get("source") or "desktop").strip() or "desktop",
    }


def tool_payload_with_request_context(tool_payload: dict[str, Any], request_payload: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(tool_payload or {})
    outer = dict(request_payload or {})
    for key in ("user_id", "session_id", "run_id", "trace_id", "source"):
        value = outer.get(key)
        if value not in (None, "") and not merged.get(key):
            merged[key] = value
    return merged
