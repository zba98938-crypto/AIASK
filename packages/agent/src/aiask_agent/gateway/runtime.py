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
from .stores import *  # noqa: F401,F403
from .http_client import *  # noqa: F401,F403
from .http_client import _form_request, _form_request_async, _json_request, _json_request_async, _multipart_request
from .adapters import *  # noqa: F401,F403
from .router import *  # noqa: F401,F403

class GatewayRuntime:
    def __init__(self, *, config: GatewayConfigStore | None = None, messages: GatewayMessageStore | None = None, directory: GatewayChannelDirectoryStore | None = None) -> None:
        self.config = config or GatewayConfigStore()
        self.messages = messages or GatewayMessageStore()
        self.directory = directory or GatewayChannelDirectoryStore(self.messages.path)
        self.router = DeliveryRouter(config=self.config, messages=self.messages, directory=self.directory)
        self.lock_path = aiask_agent_home() / "gateway-runtime.lock"
        self.status_path = aiask_agent_home() / "gateway-runtime.json"

    def status(self) -> dict[str, Any]:
        payload = self.config.status()
        payload["runtime"] = self.runtime_status()
        payload["directory"] = {"count": len(self.directory.list(limit=1000))}
        return payload

    def list_platforms(self) -> list[dict[str, Any]]:
        return self.config.platforms()

    def runtime_status(self) -> dict[str, Any]:
        lock = _safe_json(self.lock_path.read_text(encoding="utf-8") if self.lock_path.exists() else "", {}) if self.lock_path.exists() else {}
        status = _safe_json(self.status_path.read_text(encoding="utf-8") if self.status_path.exists() else "", {}) if self.status_path.exists() else {}
        return {
            "lock_path": str(self.lock_path),
            "status_path": str(self.status_path),
            "locked": self.lock_path.exists(),
            "lock": lock,
            "status": status,
        }

    def write_runtime_status(self, *, state: str, error: str | None = None) -> dict[str, Any]:
        payload = {"state": state, "pid": os.getpid(), "updated_at": now_iso(), "error": error}
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        if state in {"running", "starting"}:
            self.lock_path.write_text(json.dumps({"pid": os.getpid(), "created_at": now_iso()}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        elif state in {"stopped", "failed"} and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except OSError:
                pass
        return payload
