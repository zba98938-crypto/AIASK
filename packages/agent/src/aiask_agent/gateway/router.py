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
from .stores import *  # noqa: F401,F403
from .models import _CONTROL_SLASH_COMMANDS, _MEDIA_EXTENSIONS, _MEDIA_RE, _PHONE_PLATFORMS, _configured_from_env, _executable_command, _safe_json
from .adapters import *  # noqa: F401,F403

class DeliveryRouter:
    def __init__(
        self,
        *,
        config: GatewayConfigStore | None = None,
        messages: GatewayMessageStore | None = None,
        directory: GatewayChannelDirectoryStore | None = None,
    ) -> None:
        self.config = config or GatewayConfigStore()
        self.messages = messages or GatewayMessageStore()
        self.directory = directory or GatewayChannelDirectoryStore(self.messages.path)

    async def send(
        self,
        *,
        platform: str,
        target: str,
        message: str,
        thread_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        media_paths: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        parsed = parse_delivery_target(platform=platform, target=target, thread_id=thread_id)
        name = normalize_platform(str(parsed["platform"]))
        status = self.config.platform_status(name)
        directory_resolution = None
        if parsed.get("target") and not parsed.get("explicit"):
            directory_resolution = self.directory.resolve(platform=name, name=str(parsed.get("target") or ""))
        if directory_resolution:
            resolved_target = str(directory_resolution.get("target") or "").strip()
            resolved_thread = parsed.get("thread_id") or directory_resolution.get("thread_id")
        else:
            resolved_target = str(parsed.get("target") or status.home_channel or "").strip()
            resolved_thread = parsed.get("thread_id")
        if not resolved_target:
            raise ValueError(f"target is required for {name}; configure a home_channel or pass platform:target")
        extracted_media, cleaned_message = extract_media(str(message or ""))
        explicit_media = [{"path": os.path.expanduser(str(item)), "voice": False} for item in list(media_paths or []) if str(item or "").strip()]
        media_files = [*extracted_media, *explicit_media]
        outbound_text = cleaned_message if media_files else str(message or "")
        if not str(outbound_text or "").strip() and not media_files:
            raise ValueError("message is required")
        adapter = adapter_for(status)
        if not status.enabled or not status.configured:
            adapter_result = await BasePlatformAdapter(status).send(target=target, message=message, thread_id=thread_id)
        else:
            if str(outbound_text or "").strip() or not media_files:
                adapter_result = await adapter.send(target=resolved_target, message=outbound_text, thread_id=resolved_thread)
            else:
                adapter_result = {"ok": True, "status": "media_pending", "configured": True}
        media_results: list[dict[str, Any]] = []
        if media_files and status.enabled and status.configured:
            for media in media_files:
                media_results.append(
                    await adapter.upload_media(
                        path=str(media.get("path") or ""),
                        target=resolved_target,
                        media_type=None,
                        thread_id=resolved_thread,
                    )
                )
            if media_results:
                media_ok = all(bool(item.get("ok")) for item in media_results)
                text_ok = bool(adapter_result.get("ok"))
                adapter_result = {
                    **adapter_result,
                    "ok": text_ok and media_ok,
                    "status": "delivered" if text_ok and media_ok else "failed",
                    "media": media_results,
                }
        message_record = self.messages.record(
            direction="outbound",
            platform=name,
            target=resolved_target,
            thread_id=resolved_thread,
            content=str(message or ""),
            status=str(adapter_result.get("status") or ("delivered" if adapter_result.get("ok") else "failed")),
            session_id=session_id,
            user_id=user_id,
            metadata={
                "adapter": adapter_result,
                "parsed_target": parsed,
                "channel_resolution": directory_resolution,
                "cleaned_content": outbound_text,
                "media": media_files,
                "media_results": media_results,
            },
        )
        return {"message": message_record, "platform": status.to_dict(), "adapter": adapter_result}

    async def retry(self, message_id: str) -> dict[str, Any]:
        item = self.messages.get(message_id)
        if item is None:
            raise FileNotFoundError(f"gateway message not found: {message_id}")
        data = await self.send(
            platform=str(item.get("platform") or "local"),
            target=str(item.get("target") or ""),
            message=str(item.get("content") or ""),
            thread_id=item.get("thread_id"),
            session_id=item.get("session_id"),
            user_id=item.get("user_id"),
        )
        self.messages.update_status(message_id, status="retried", metadata={"retry_message_id": data["message"]["message_id"]})
        return data

    def record_inbound(
        self,
        *,
        platform: str,
        payload: dict[str, Any],
        signature: str | None = None,
        verified: bool | None = None,
        adapter_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = normalize_platform(platform)
        secret = str(os.getenv(f"AIASK_GATEWAY_{name.upper()}_SECRET", "") or os.getenv("AIASK_GATEWAY_WEBHOOK_SECRET", "")).strip()
        signature_verified = False
        if verified is not None:
            signature_verified = bool(verified)
        elif secret and signature:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            signature_verified = hmac.compare_digest(signature, expected) or hmac.compare_digest(signature, f"sha256={expected}")
        elif not secret:
            signature_verified = True
        content = str(payload.get("text") or payload.get("message") or payload.get("content") or "")
        external_id = str(payload.get("message_id") or payload.get("event_id") or payload.get("id") or "").strip()
        duplicate = self.messages.find_by_external_id(platform=name, external_id=external_id) if external_id else None
        slash_command = None
        if content.strip().startswith("/"):
            parts = content.strip().split(maxsplit=1)
            slash_command = {"command": parts[0][1:], "arguments": parts[1] if len(parts) > 1 else ""}
        approval_callback = None
        if payload.get("approval_id") and str(payload.get("action") or "").lower() in {"approve", "deny"}:
            approval_callback = {"approval_id": payload.get("approval_id"), "action": str(payload.get("action")).lower()}
        if slash_command and slash_command["command"] in {"approve", "deny"} and slash_command.get("arguments"):
            approval_callback = {"approval_id": slash_command["arguments"].split()[0], "action": slash_command["command"]}
        control_action = None
        if slash_command and slash_command["command"] in _CONTROL_SLASH_COMMANDS:
            control_action = {
                "command": slash_command["command"],
                "arguments": slash_command.get("arguments") or "",
                "enqueue_agent": False,
            }
        if duplicate:
            updated = self.messages.update_status(
                str(duplicate["message_id"]),
                status="duplicate",
                metadata={"duplicate_seen_at": now_iso(), "last_payload": payload},
            )
            assert updated is not None
            return updated
        return self.messages.record(
            direction="inbound",
            platform=name,
            target=str(payload.get("chat_id") or payload.get("target") or ""),
            thread_id=payload.get("thread_id"),
            content=content,
            status="received" if signature_verified else "signature_failed",
            session_id=payload.get("session_id"),
            user_id=payload.get("user_id"),
            metadata={
                "payload": payload,
                "signature_verified": signature_verified,
                "adapter": adapter_result or {},
                "external_id": external_id or None,
                "slash_command": slash_command,
                "approval_callback": approval_callback,
                "control_action": control_action,
                "routing": {"enqueue_agent": not bool(control_action)},
                "media": payload.get("media") or payload.get("attachments") or [],
            },
        )
