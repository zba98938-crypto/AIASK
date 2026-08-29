from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import smtplib
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..paths import aiask_agent_home, default_state_db_path
from ..session_store import now_iso

from .models import *  # noqa: F401,F403
from .models import _CONTROL_SLASH_COMMANDS, _MEDIA_EXTENSIONS, _MEDIA_RE, _PHONE_PLATFORMS, _configured_from_env, _executable_command, _safe_json

class BasePlatformAdapter:
    def __init__(self, status: GatewayPlatformStatus) -> None:
        self.status = status

    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        if not self.status.enabled:
            return {"ok": False, "status": "disabled", "configured": self.status.configured}
        if not self.status.configured:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        if self.status.name in {"local", "api_server"}:
            return {"ok": True, "status": "delivered", "configured": True}
        return {"ok": False, "status": "unsupported", "configured": True, "error": f"no live adapter for platform: {self.status.name}"}

    async def start(self) -> dict[str, Any]:
        return {"ok": True, "status": "started", "platform": self.status.name, "configured": self.status.configured}

    async def stop(self) -> dict[str, Any]:
        return {"ok": True, "status": "stopped", "platform": self.status.name}

    async def health(self) -> dict[str, Any]:
        return {"ok": self.status.enabled and self.status.configured, "platform": self.status.name, "status": self.status.to_dict()}

    def verify_signature(self, *, body: bytes, headers: dict[str, str]) -> bool:
        secret = str(os.getenv(f"AIASK_GATEWAY_{self.status.name.upper()}_SECRET", "") or os.getenv("AIASK_GATEWAY_WEBHOOK_SECRET", "")).strip()
        if not secret:
            return True
        signature = headers.get("x-aiask-gateway-signature") or headers.get("x-hub-signature-256") or headers.get("x-signature") or ""
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected) or hmac.compare_digest(signature, f"sha256={expected}")

    async def handle_inbound(self, *, payload: dict[str, Any], headers: dict[str, str] | None = None, body: bytes | None = None) -> dict[str, Any]:
        normalized_headers = {k.lower(): v for k, v in dict(headers or {}).items()}
        raw_body = body if body is not None else json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return {"verified": self.verify_signature(body=raw_body, headers=normalized_headers), "payload": self.normalize_inbound(payload)}

    def normalize_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = (
            payload.get("text")
            or payload.get("message")
            or payload.get("content")
            or payload.get("msg")
            or payload.get("body")
            or ""
        )
        sender = payload.get("user_id") or payload.get("sender") or payload.get("from") or payload.get("open_id")
        target = payload.get("chat_id") or payload.get("channel_id") or payload.get("target") or payload.get("conversation_id")
        external_id = payload.get("message_id") or payload.get("event_id") or payload.get("id") or payload.get("msg_id")
        normalized = dict(payload)
        normalized.setdefault("text", content)
        normalized.setdefault("target", target or "")
        normalized.setdefault("chat_id", target or "")
        normalized.setdefault("thread_id", payload.get("thread_id") or payload.get("topic_id") or payload.get("message_thread_id") or "")
        normalized.setdefault("user_id", sender or "")
        normalized.setdefault("media", payload.get("media") or payload.get("attachments") or [])
        if external_id:
            normalized.setdefault("message_id", external_id)
        return normalized

    async def download_media(self, *, url: str, max_bytes: int = 25 * 1024 * 1024) -> dict[str, Any]:
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read(max(1, min(int(max_bytes), 50 * 1024 * 1024)))
        media_dir = aiask_agent_home() / "gateway-media"
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / f"media-{uuid4().hex}"
        path.write_bytes(raw)
        return {"path": str(path), "bytes": len(raw), "content_type": response.headers.get("Content-Type")}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        if not self.status.enabled:
            return {"ok": False, "status": "disabled", "configured": self.status.configured}
        if not self.status.configured:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        return {
            "ok": False,
            "status": "unsupported",
            "configured": True,
            "platform": self.status.name,
            "path": str(item),
            "target": target,
            "media_type": media_type,
            "thread_id": thread_id,
        }


def _json_request(method: str, url: str, payload: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **dict(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
            text = raw.decode("utf-8", errors="replace")
            parsed = json.loads(text) if text else {}
            return {"ok": 200 <= getattr(response, "status", 200) < 300, "status_code": getattr(response, "status", None), "body": parsed}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status_code": exc.code, "error": exc.reason, "body": _safe_json(body, body)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


async def _json_request_async(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Run _json_request in a worker thread to keep async handlers non-blocking.

    The synchronous helpers (_json_request, _form_request, _multipart_request)
    use urllib.request.urlopen, which blocks the event loop when called from
    inside async platform adapters. Wrapping with asyncio.to_thread releases
    the loop while the outbound HTTP I/O is in flight.
    """
    return await asyncio.to_thread(
        _json_request, method, url, payload, headers=headers, timeout=timeout
    )


def _form_request(method: str, url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/x-www-form-urlencoded", **dict(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read(1024 * 1024).decode("utf-8", errors="replace")
            return {"ok": 200 <= getattr(response, "status", 200) < 300, "status_code": getattr(response, "status", None), "body": _safe_json(text, text)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status_code": exc.code, "error": exc.reason, "body": exc.read().decode("utf-8", errors="replace")}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


async def _form_request_async(
    method: str,
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _form_request, method, url, payload, headers=headers, timeout=timeout
    )


def _multipart_request(
    method: str,
    url: str,
    *,
    fields: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | list[tuple[str, tuple[str, bytes, str]]] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    boundary = f"----aiask{uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in dict(fields or {}).items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    if isinstance(files, dict):
        file_items = list(files.items())
    else:
        file_items = list(files or [])
    for key, (filename, data, content_type) in file_items:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode("utf-8"),
                f"Content-Type: {content_type or 'application/octet-stream'}\r\n\r\n".encode("utf-8"),
                data,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", **dict(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read(10 * 1024 * 1024).decode("utf-8", errors="replace")
            return {"ok": 200 <= getattr(response, "status", 200) < 300, "status_code": getattr(response, "status", None), "body": _safe_json(text, text)}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status_code": exc.code, "error": exc.reason, "body": _safe_json(text, text)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
