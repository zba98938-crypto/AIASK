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

DOMESTIC_PRIORITY_PLATFORMS = (
    "feishu",
    "lark",
    "dingtalk",
    "wecom",
    "wecom_callback",
    "weixin",
    "email",
    "webhook",
    "api_server",
    "local",
)

HERMES_PLATFORM_MATRIX = (
    *DOMESTIC_PRIORITY_PLATFORMS,
    "telegram",
    "discord",
    "slack",
    "line",
    "teams",
    "whatsapp",
    "signal",
    "simplex",
    "matrix",
    "mattermost",
    "sms",
    "qqbot",
    "bluebubbles",
    "homeassistant",
)

PLATFORM_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "feishu": ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_BOT_TOKEN"),
    "lark": ("LARK_APP_ID", "LARK_APP_SECRET", "LARK_BOT_TOKEN"),
    "dingtalk": ("DINGTALK_APP_KEY", "DINGTALK_APP_SECRET", "DINGTALK_BOT_TOKEN"),
    "wecom": ("WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET"),
    "wecom_callback": ("WECOM_CORP_ID", "WECOM_TOKEN", "WECOM_ENCODING_AES_KEY"),
    "weixin": ("WEIXIN_APP_ID", "WEIXIN_APP_SECRET"),
    "email": ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"),
    "webhook": ("AIASK_GATEWAY_WEBHOOK_URL",),
    "telegram": ("TELEGRAM_BOT_TOKEN",),
    "discord": ("DISCORD_BOT_TOKEN",),
    "slack": ("SLACK_BOT_TOKEN",),
    "line": ("LINE_CHANNEL_ACCESS_TOKEN",),
    "teams": ("MSGRAPH_TENANT_ID", "MSGRAPH_CLIENT_ID", "MSGRAPH_CLIENT_SECRET"),
    "whatsapp": ("WHATSAPP_TOKEN",),
    "signal": ("SIGNAL_CLI_PATH",),
    "simplex": ("SIMPLEX_CLI_PATH",),
    "matrix": ("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN"),
    "mattermost": ("MATTERMOST_URL", "MATTERMOST_TOKEN"),
    "sms": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
    "qqbot": ("QQBOT_APP_ID", "QQBOT_TOKEN"),
    "bluebubbles": ("BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD"),
    "homeassistant": ("HASS_URL", "HASS_TOKEN"),
}

OPTIONAL_PLATFORM_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "feishu": ("FEISHU_BOT_WEBHOOK",),
    "lark": ("LARK_BOT_WEBHOOK",),
    "dingtalk": ("DINGTALK_BOT_WEBHOOK", "DINGTALK_BOT_SECRET"),
    "webhook": ("AIASK_GATEWAY_WEBHOOK_URL",),
    "whatsapp": ("WHATSAPP_PHONE_NUMBER_ID",),
    "line": ("LINE_CHANNEL_SECRET",),
    "teams": ("MSGRAPH_CHAT_ID", "TEAMS_WEBHOOK_URL"),
    "sms": ("TWILIO_FROM",),
}

_PHONE_PLATFORMS = {"signal", "sms", "whatsapp"}
_MEDIA_EXTENSIONS = (
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "mp4",
    "mov",
    "avi",
    "mkv",
    "webm",
    "ogg",
    "opus",
    "mp3",
    "wav",
    "m4a",
    "epub",
    "pdf",
    "zip",
    "rar",
    "7z",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "txt",
    "csv",
)
_MEDIA_RE = re.compile(
    r"""[`"']?MEDIA:\s*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|(?:~/|/)\S+(?:[^\S\n]+\S+)*?\.(?:"""
    + "|".join(_MEDIA_EXTENSIONS)
    + r""")(?=[\s`"',;:)\]}]|$)|\S+)[`"']?""",
    re.IGNORECASE,
)
_CONTROL_SLASH_COMMANDS = {"approve", "deny", "stop", "new", "reset", "help"}


def normalize_platform(value: str | None) -> str:
    token = str(value or "local").strip().lower().replace("-", "_")
    return token or "local"


def extract_media(content: str) -> tuple[list[dict[str, Any]], str]:
    text = str(content or "")
    is_voice = "[[audio_as_voice]]" in text
    cleaned = text.replace("[[audio_as_voice]]", "")
    media: list[dict[str, Any]] = []
    for match in _MEDIA_RE.finditer(text):
        raw = match.group("path").strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "`\"'":
            raw = raw[1:-1].strip()
        path = os.path.expanduser(raw.lstrip("`\"'").rstrip("`\"',.;:)}]"))
        if path:
            media.append({"path": path, "voice": is_voice})
    if media:
        cleaned = _MEDIA_RE.sub("", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return media, cleaned


def parse_delivery_target(*, platform: str | None = None, target: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
    raw_platform = normalize_platform(platform)
    raw_target = str(target or "").strip()
    parsed_thread = str(thread_id or "").strip() or None
    if raw_target and ":" in raw_target:
        maybe_platform, remainder = raw_target.split(":", 1)
        normalized_maybe = normalize_platform(maybe_platform)
        if not platform or normalized_maybe in HERMES_PLATFORM_MATRIX or normalized_maybe == raw_platform:
            raw_platform = normalized_maybe
            raw_target = remainder.strip()
    explicit = False
    if raw_target:
        if raw_platform in {"telegram", "discord"}:
            match = re.fullmatch(r"\s*(-?\d+)(?::(\d+))?\s*", raw_target)
            if match:
                raw_target = match.group(1)
                parsed_thread = parsed_thread or match.group(2)
                explicit = True
        elif raw_platform in {"feishu", "lark"}:
            match = re.fullmatch(r"\s*((?:oc|ou|on|chat|open)_[-A-Za-z0-9]+)(?::([-A-Za-z0-9_]+))?\s*", raw_target)
            if match:
                raw_target = match.group(1)
                parsed_thread = parsed_thread or match.group(2)
                explicit = True
        elif raw_platform == "weixin":
            if re.fullmatch(r"\s*((?:wxid|gh|v\d+|wm|wb)_[A-Za-z0-9_-]+|[A-Za-z0-9._-]+@chatroom|filehelper)\s*", raw_target):
                explicit = True
        elif raw_platform in _PHONE_PLATFORMS:
            if re.fullmatch(r"\s*\+\d{7,15}\s*", raw_target):
                raw_target = raw_target.strip()
                explicit = True
        elif raw_platform == "matrix" and raw_target.startswith(("!", "@")):
            explicit = True
        elif raw_target.lstrip("-").isdigit():
            explicit = True
    return {"platform": raw_platform, "target": raw_target, "thread_id": parsed_thread, "explicit": explicit}


def _configured_from_env(platform: str) -> bool:
    if platform in {"local", "api_server"}:
        return True
    if platform in {"feishu", "lark"}:
        return bool(
            (os.getenv(f"{platform.upper()}_APP_ID") and os.getenv(f"{platform.upper()}_APP_SECRET"))
            or os.getenv(f"{platform.upper()}_BOT_WEBHOOK")
            or os.getenv(f"{platform.upper()}_BOT_TOKEN")
        )
    if platform == "dingtalk":
        return bool(os.getenv("DINGTALK_BOT_WEBHOOK") or os.getenv("DINGTALK_BOT_TOKEN") or (os.getenv("DINGTALK_APP_KEY") and os.getenv("DINGTALK_APP_SECRET")))
    if platform == "weixin":
        return bool(
            (os.getenv("WEIXIN_APP_ID") and os.getenv("WEIXIN_APP_SECRET"))
            or (os.getenv("WEIXIN_ILINK_APP_ID") and os.getenv("WEIXIN_ILINK_APP_SECRET"))
        )
    if platform == "webhook":
        return bool(os.getenv("AIASK_GATEWAY_WEBHOOK_URL") or os.getenv("AIASK_GATEWAY_WEBHOOK_SECRET"))
    if platform == "line":
        return bool(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
    if platform == "teams":
        return bool(
            os.getenv("TEAMS_WEBHOOK_URL")
            or (
                os.getenv("MSGRAPH_TENANT_ID")
                and os.getenv("MSGRAPH_CLIENT_ID")
                and os.getenv("MSGRAPH_CLIENT_SECRET")
            )
        )
    if platform == "simplex":
        cli = str(os.getenv("SIMPLEX_CLI_PATH") or "").strip()
        executable = shutil.which(cli) if cli and os.path.sep not in cli else cli
        return bool(executable and Path(executable).exists())
    keys = PLATFORM_ENV_KEYS.get(platform, ())
    if not keys:
        return False
    return all(str(os.getenv(key, "")).strip() for key in keys)


def _safe_json(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _executable_command(path: str) -> list[str]:
    item = Path(path)
    if os.name == "nt" and item.exists() and item.is_file():
        try:
            prefix = item.read_text(encoding="utf-8", errors="ignore")[:128].lower()
        except OSError:
            prefix = ""
        if item.suffix.lower() == ".py" or prefix.startswith("#!") and "python" in prefix:
            return [sys.executable, str(item)]
    return [str(path)]


@dataclass(frozen=True)
class GatewayPlatformStatus:
    name: str
    enabled: bool
    configured: bool
    priority: str
    required_env: tuple[str, ...]
    home_channel: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "configured": self.configured,
            "priority": self.priority,
            "required_env": list(self.required_env),
            "home_channel": self.home_channel,
        }
