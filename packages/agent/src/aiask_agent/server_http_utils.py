from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler
from typing import Any

from .json_utils import dumps_json_bytes
from .route_auth import extract_bearer_token


def json_dumps(payload: Any) -> bytes:
    return dumps_json_bytes(payload, ensure_ascii=False, sort_keys=True)


def query_bool(query: dict[str, list[str]], key: str, *, default: bool = False) -> bool:
    values = query.get(key)
    if not values:
        return default
    value = str(values[-1] or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "archived"}
    return bool(value)


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid JSON request body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON request body must be an object")
    return parsed


def header_token(handler: BaseHTTPRequestHandler, *names: str) -> str | None:
    for name in names:
        value = extract_bearer_token(handler.headers.get(name))
        if value:
            return value
    return extract_bearer_token(handler.headers.get("Authorization"))


def cors_origins() -> set[str]:
    configured = str(os.getenv("AIASK_AGENT_CORS_ORIGINS", "")).strip()
    defaults = {
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "tauri://localhost",
    }
    values = defaults if not configured else set()
    for item in configured.split(","):
        origin = item.strip().rstrip("/")
        if origin and origin != "*":
            values.add(origin)
    return values
