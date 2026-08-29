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
from ..session_store_utils import sanitize_public_response

from .models import *  # noqa: F401,F403
from .models import _CONTROL_SLASH_COMMANDS, _MEDIA_EXTENSIONS, _MEDIA_RE, _PHONE_PLATFORMS, _configured_from_env, _executable_command, _safe_json


def _safe_gateway_metadata(value: Any) -> Any:
    return sanitize_public_response(value, max_depth=8, max_items=200, max_text=4000)


class GatewayConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or aiask_agent_home() / "gateway_config.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"platforms": {}, "session_policy": {"mode": "both", "idle_minutes": 1440}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {"platforms": {}, "session_policy": {"mode": "both", "idle_minutes": 1440}}

    def save_platform(self, platform: str, payload: dict[str, Any]) -> dict[str, Any]:
        name = normalize_platform(platform)
        data = self.load()
        platforms = dict(data.get("platforms") or {})
        current = dict(platforms.get(name) or {})
        allowed = {key: value for key, value in dict(payload or {}).items() if key in {"enabled", "home_channel", "reply_to_mode", "extra"}}
        current.update(allowed)
        platforms[name] = current
        data["platforms"] = platforms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return self.platform_status(name).to_dict()

    def platform_status(self, platform: str) -> GatewayPlatformStatus:
        name = normalize_platform(platform)
        config = dict((self.load().get("platforms") or {}).get(name) or {})
        enabled = bool(config.get("enabled", name in {"local", "api_server"}))
        priority = "domestic" if name in DOMESTIC_PRIORITY_PLATFORMS else "global"
        return GatewayPlatformStatus(
            name=name,
            enabled=enabled,
            configured=_configured_from_env(name),
            priority=priority,
            required_env=PLATFORM_ENV_KEYS.get(name, ()),
            home_channel=config.get("home_channel"),
        )

    def platforms(self) -> list[dict[str, Any]]:
        seen = list(dict.fromkeys((*HERMES_PLATFORM_MATRIX, *list((self.load().get("platforms") or {}).keys()))))
        return [self.platform_status(name).to_dict() for name in seen]

    def status(self) -> dict[str, Any]:
        platforms = self.platforms()
        return {
            "object": "aiask.gateway_status",
            "implementation": "aiask_native",
            "domestic_priority": list(DOMESTIC_PRIORITY_PLATFORMS),
            "platform_count": len(platforms),
            "enabled_count": sum(1 for item in platforms if item["enabled"]),
            "configured_count": sum(1 for item in platforms if item["configured"]),
            "platforms": platforms,
            "secrets_redacted": True,
        }


class GatewayChannelDirectoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gateway_directory (
                directory_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                target TEXT NOT NULL,
                thread_id TEXT,
                metadata_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_directory_lookup ON gateway_directory(platform, kind, name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gateway_directory_target ON gateway_directory(platform, target)")
        conn.commit()
        return conn

    def upsert(
        self,
        *,
        platform: str,
        name: str,
        target: str,
        kind: str = "channel",
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(name or "").strip():
            raise ValueError("directory name is required")
        if not str(target or "").strip():
            raise ValueError("directory target is required")
        directory_id = f"gwdir_{uuid4().hex}"
        ts = now_iso()
        with closing(self._connect()) as conn:
            existing = conn.execute(
                "SELECT directory_id FROM gateway_directory WHERE platform = ? AND kind = ? AND name = ?",
                (normalize_platform(platform), str(kind or "channel"), str(name).strip()),
            ).fetchone()
            if existing:
                directory_id = str(existing["directory_id"])
            conn.execute(
                """
                INSERT OR REPLACE INTO gateway_directory
                    (directory_id, platform, kind, name, target, thread_id, metadata_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    directory_id,
                    normalize_platform(platform),
                    str(kind or "channel").strip() or "channel",
                    str(name).strip(),
                    str(target).strip(),
                    thread_id,
                    json.dumps(_safe_gateway_metadata(dict(metadata or {})), ensure_ascii=False, sort_keys=True),
                    ts,
                ),
            )
            conn.commit()
        item = self.get(directory_id)
        assert item is not None
        return item

    def get(self, directory_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM gateway_directory WHERE directory_id = ?", (str(directory_id or "").strip(),)).fetchone()
        return self._row(row)

    def list(self, *, platform: str | None = None, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if platform:
            clauses.append("platform = ?")
            values.append(normalize_platform(platform))
        if kind:
            clauses.append("kind = ?")
            values.append(str(kind))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(limit or 200), 1000)))
        with closing(self._connect()) as conn:
            rows = conn.execute(f"SELECT * FROM gateway_directory {where} ORDER BY updated_at DESC LIMIT ?", tuple(values)).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def resolve(self, *, platform: str | None, name: str, kind: str | None = None) -> dict[str, Any] | None:
        token = str(name or "").strip()
        if not token:
            return None
        clauses = ["(name = ? OR target = ?)"]
        values: list[Any] = [token, token]
        if platform:
            clauses.append("platform = ?")
            values.append(normalize_platform(platform))
        if kind:
            clauses.append("kind = ?")
            values.append(str(kind))
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT * FROM gateway_directory WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT 1",
                tuple(values),
            ).fetchone()
        return self._row(row)

    def refresh(self, *, config: GatewayConfigStore | None = None) -> dict[str, Any]:
        store = config or GatewayConfigStore()
        refreshed: list[dict[str, Any]] = []
        for status in store.platforms():
            platform = str(status.get("name") or "")
            home = str(status.get("home_channel") or "").strip()
            if home:
                refreshed.append(
                    self.upsert(
                        platform=platform,
                        kind="channel",
                        name="home",
                        target=home,
                        metadata={"source": "gateway_config"},
                    )
                )
        raw = str(os.getenv("AIASK_GATEWAY_DIRECTORY_JSON") or "").strip()
        if raw:
            payload = _safe_json(raw, [])
            entries = payload.values() if isinstance(payload, dict) else payload
            for entry in list(entries or []):
                if not isinstance(entry, dict):
                    continue
                refreshed.append(
                    self.upsert(
                        platform=str(entry.get("platform") or "local"),
                        kind=str(entry.get("kind") or "channel"),
                        name=str(entry.get("name") or entry.get("alias") or entry.get("target") or ""),
                        target=str(entry.get("target") or entry.get("id") or ""),
                        thread_id=entry.get("thread_id"),
                        metadata={"source": "env", **dict(entry.get("metadata") or {})},
                    )
                )
        return {"refreshed_count": len(refreshed), "items": refreshed}

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = _safe_json(item.pop("metadata_json", None), {})
        return item


class GatewayMessageStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gateway_messages (
                message_id TEXT PRIMARY KEY,
                direction TEXT NOT NULL,
                platform TEXT NOT NULL,
                target TEXT,
                thread_id TEXT,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                session_id TEXT,
                user_id TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gateway_messages_platform ON gateway_messages(platform, created_at)")
        conn.commit()
        return conn

    def record(
        self,
        *,
        direction: str,
        platform: str,
        target: str | None,
        content: str,
        status: str,
        thread_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message_id = f"gwmsg_{uuid4().hex}"
        ts = now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO gateway_messages
                    (message_id, direction, platform, target, thread_id, content, status, session_id, user_id, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    direction,
                    normalize_platform(platform),
                    target,
                    thread_id,
                    content,
                    status,
                    session_id,
                    user_id,
                    json.dumps(_safe_gateway_metadata(dict(metadata or {})), ensure_ascii=False, sort_keys=True),
                    ts,
                ),
            )
            conn.commit()
        item = self.get(message_id)
        assert item is not None
        return item

    def get(self, message_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM gateway_messages WHERE message_id = ?", (str(message_id or "").strip(),)).fetchone()
        return self._row(row)

    def update_status(self, message_id: str, *, status: str, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        current = self.get(message_id)
        if current is None:
            return None
        next_metadata = dict(current.get("metadata") or {})
        if metadata:
            next_metadata.update(_safe_gateway_metadata(dict(metadata)))
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE gateway_messages SET status = ?, metadata_json = ? WHERE message_id = ?",
                (status, json.dumps(next_metadata, ensure_ascii=False, sort_keys=True), message_id),
            )
            conn.commit()
        return self.get(message_id)

    def list(self, *, platform: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        values: list[Any] = []
        where = ""
        if platform:
            where = "WHERE platform = ?"
            values.append(normalize_platform(platform))
        values.append(max(1, min(int(limit or 100), 1000)))
        with closing(self._connect()) as conn:
            rows = conn.execute(f"SELECT * FROM gateway_messages {where} ORDER BY created_at DESC LIMIT ?", tuple(values)).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def find_by_external_id(self, *, platform: str, external_id: str) -> dict[str, Any] | None:
        token = str(external_id or "").strip()
        if not token:
            return None
        for item in self.list(platform=platform, limit=1000):
            metadata = dict(item.get("metadata") or {})
            if metadata.get("external_id") == token:
                return item
        return None

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = _safe_json(item.pop("metadata_json", None), {})
        return item
