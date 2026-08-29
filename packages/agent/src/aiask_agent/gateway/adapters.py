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
from .http_client import *  # noqa: F401,F403
from . import http_client as _http

class WebhookAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        base = str(os.getenv("AIASK_GATEWAY_WEBHOOK_URL") or "").strip()
        url = target if target.startswith(("http://", "https://")) else base
        if not url:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["AIASK_GATEWAY_WEBHOOK_URL"]}
        secret = str(os.getenv("AIASK_GATEWAY_WEBHOOK_SECRET") or "").strip()
        payload = {"text": message, "target": target, "thread_id": thread_id, "platform": self.status.name}
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-AIASK-Gateway-Signature"] = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        result = await _http._json_request_async("POST", url, payload, headers=headers)
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}


class LocalAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        return {"ok": True, "status": "delivered", "configured": True, "target": target, "thread_id": thread_id, "local": True}


class ApiServerAdapter(BasePlatformAdapter):
    async def start(self) -> dict[str, Any]:
        return {"ok": True, "status": "managed_by_aiask_agent_server", "platform": self.status.name, "configured": True}

    async def stop(self) -> dict[str, Any]:
        return {"ok": True, "status": "managed_by_aiask_agent_server", "platform": self.status.name, "configured": True}

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "platform": self.status.name, "status": self.status.to_dict(), "runtime": "active"}

    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        return {"ok": True, "status": "delivered", "configured": True, "target": target, "thread_id": thread_id, "api_server": True}


class EmailAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        host = str(os.getenv("SMTP_HOST") or "").strip()
        username = str(os.getenv("SMTP_USERNAME") or "").strip()
        password = str(os.getenv("SMTP_PASSWORD") or "").strip()
        if not host or not username or not password:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        msg = EmailMessage()
        msg["Subject"] = str(os.getenv("AIASK_EMAIL_SUBJECT") or "AIASK Agent")
        msg["From"] = str(os.getenv("SMTP_FROM") or username)
        msg["To"] = target
        msg.set_content(message)
        port = int(os.getenv("SMTP_PORT", "587"))
        try:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if str(os.getenv("SMTP_STARTTLS", "1")).strip().lower() not in {"0", "false", "no"}:
                    smtp.starttls()
                smtp.login(username, password)
                smtp.send_message(msg)
            return {"ok": True, "status": "delivered", "configured": True}
        except Exception as exc:
            return {"ok": False, "status": "failed", "configured": True, "error": str(exc), "error_type": type(exc).__name__}


class FeishuAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        prefix = "LARK" if self.status.name == "lark" else "FEISHU"
        webhook = str(os.getenv(f"{prefix}_BOT_WEBHOOK") or "").strip()
        if webhook:
            result = await _http._json_request_async("POST", webhook, {"msg_type": "text", "content": {"text": message}})
            return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}
        token = await self._tenant_token(prefix)
        if not token.get("ok"):
            return {"ok": False, "status": "auth_failed", "configured": bool(token.get("configured")), "response": token}
        receive_type = str(os.getenv(f"{prefix}_RECEIVE_ID_TYPE") or "chat_id")
        payload = {"receive_id": target, "msg_type": "text", "content": json.dumps({"text": message}, ensure_ascii=False)}
        url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={urllib.parse.quote(receive_type)}"
        result = await _http._json_request_async("POST", url, payload, headers={"Authorization": f"Bearer {token['tenant_access_token']}"})
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        prefix = "LARK" if self.status.name == "lark" else "FEISHU"
        token = await self._tenant_token(prefix)
        if not token.get("ok"):
            return {"ok": False, "status": "auth_failed", "configured": bool(token.get("configured")), "response": token}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        content_type = media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream"
        upload = _http._multipart_request(
            "POST",
            "https://open.feishu.cn/open-apis/im/v1/files",
            fields={"file_type": "stream", "file_name": item.name},
            files={"file": (item.name, item.read_bytes(), content_type)},
            headers={"Authorization": f"Bearer {token['tenant_access_token']}"},
        )
        body = dict(upload.get("body") or {}) if isinstance(upload.get("body"), dict) else {}
        file_key = ((body.get("data") or {}) if isinstance(body.get("data"), dict) else {}).get("file_key")
        if not upload.get("ok") or not file_key:
            return {"ok": False, "status": "failed", "configured": True, "path": str(item), "response": upload}
        if target:
            receive_type = str(os.getenv(f"{prefix}_RECEIVE_ID_TYPE") or "chat_id")
            sent = await _http._json_request_async(
                "POST",
                f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={urllib.parse.quote(receive_type)}",
                {"receive_id": target, "msg_type": "file", "content": json.dumps({"file_key": file_key}, ensure_ascii=False)},
                headers={"Authorization": f"Bearer {token['tenant_access_token']}"},
            )
            return {"ok": bool(sent.get("ok")), "status": "delivered" if sent.get("ok") else "failed", "configured": True, "path": str(item), "file_key": file_key, "response": sent}
        return {"ok": True, "status": "uploaded", "configured": True, "path": str(item), "file_key": file_key, "response": upload}

    async def _tenant_token(self, prefix: str) -> dict[str, Any]:
        app_id = str(os.getenv(f"{prefix}_APP_ID") or "").strip()
        app_secret = str(os.getenv(f"{prefix}_APP_SECRET") or "").strip()
        if not app_id or not app_secret:
            return {"ok": False, "configured": False, "required_env": [f"{prefix}_APP_ID", f"{prefix}_APP_SECRET"]}
        result = await _http._json_request_async("POST", "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", {"app_id": app_id, "app_secret": app_secret})
        body = dict(result.get("body") or {}) if isinstance(result.get("body"), dict) else {}
        token = body.get("tenant_access_token")
        return {"ok": bool(token), "configured": True, "tenant_access_token": token, "response": result}

    def verify_signature(self, *, body: bytes, headers: dict[str, str]) -> bool:
        secret = str(os.getenv(f"{'LARK' if self.status.name == 'lark' else 'FEISHU'}_APP_SECRET") or os.getenv("FEISHU_APP_SECRET") or "").strip()
        signature = headers.get("x-lark-signature") or headers.get("x-feishu-signature") or headers.get("x-aiask-gateway-signature") or ""
        if not secret or not signature:
            return super().verify_signature(body=body, headers=headers)
        timestamp = headers.get("x-lark-request-timestamp") or headers.get("x-feishu-request-timestamp") or ""
        nonce = headers.get("x-lark-request-nonce") or headers.get("x-feishu-request-nonce") or ""
        candidates = [
            hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest(),
            hmac.new(secret.encode("utf-8"), f"{timestamp}{nonce}".encode("utf-8") + body, hashlib.sha256).hexdigest(),
        ]
        return any(hmac.compare_digest(signature, item) or hmac.compare_digest(signature, f"sha256={item}") for item in candidates)

    def normalize_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = dict(payload.get("event") or {}) if isinstance(payload.get("event"), dict) else payload
        message = dict(event.get("message") or {}) if isinstance(event.get("message"), dict) else {}
        sender = dict(event.get("sender") or {}) if isinstance(event.get("sender"), dict) else {}
        normalized = super().normalize_inbound(
            {
                **payload,
                "text": message.get("content") or payload.get("text") or payload.get("challenge") or "",
                "message_id": message.get("message_id") or event.get("event_id") or payload.get("uuid"),
                "chat_id": message.get("chat_id") or event.get("chat_id"),
                "user_id": sender.get("sender_id", {}).get("open_id") if isinstance(sender.get("sender_id"), dict) else sender.get("open_id"),
            }
        )
        if payload.get("challenge"):
            normalized["challenge"] = payload.get("challenge")
        return normalized


class DingTalkAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        webhook = str(os.getenv("DINGTALK_BOT_WEBHOOK") or "").strip()
        token = str(os.getenv("DINGTALK_BOT_TOKEN") or "").strip()
        if not webhook and token:
            webhook = f"https://oapi.dingtalk.com/robot/send?access_token={urllib.parse.quote(token)}"
        if not webhook:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["DINGTALK_BOT_WEBHOOK", "DINGTALK_BOT_TOKEN"]}
        secret = str(os.getenv("DINGTALK_BOT_SECRET") or "").strip()
        if secret:
            timestamp = str(round(time.time() * 1000))
            sign = urllib.parse.quote_plus(
                __import__("base64").b64encode(hmac.new(secret.encode("utf-8"), f"{timestamp}\n{secret}".encode("utf-8"), hashlib.sha256).digest()).decode("utf-8")
            )
            separator = "&" if "?" in webhook else "?"
            webhook = f"{webhook}{separator}timestamp={timestamp}&sign={sign}"
        result = await _http._json_request_async("POST", webhook, {"msgtype": "text", "text": {"content": message}})
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}

    def verify_signature(self, *, body: bytes, headers: dict[str, str]) -> bool:
        secret = str(os.getenv("DINGTALK_BOT_SECRET") or "").strip()
        signature = headers.get("sign") or headers.get("x-dingtalk-signature") or headers.get("x-aiask-gateway-signature") or ""
        timestamp = headers.get("timestamp") or headers.get("x-dingtalk-timestamp") or ""
        if not secret or not signature:
            return super().verify_signature(body=body, headers=headers)
        base = f"{timestamp}\n{secret}".encode("utf-8") if timestamp else body
        expected = __import__("base64").b64encode(hmac.new(secret.encode("utf-8"), base, hashlib.sha256).digest()).decode("utf-8")
        return hmac.compare_digest(urllib.parse.unquote_plus(signature), expected)


class WeComAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        token = await self._access_token()
        if not token.get("ok"):
            return {"ok": False, "status": "auth_failed", "configured": bool(token.get("configured")), "response": token}
        payload = {
            "touser": target or "@all",
            "msgtype": "text",
            "agentid": int(os.getenv("WECOM_AGENT_ID", "0")),
            "text": {"content": message},
            "safe": 0,
        }
        result = await _http._json_request_async("POST", f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={urllib.parse.quote(token['access_token'])}", payload)
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        token = await self._access_token()
        if not token.get("ok"):
            return {"ok": False, "status": "auth_failed", "configured": bool(token.get("configured")), "response": token}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        upload = _http._multipart_request(
            "POST",
            f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={urllib.parse.quote(token['access_token'])}&type=file",
            files={"media": (item.name, item.read_bytes(), media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream")},
        )
        body = dict(upload.get("body") or {}) if isinstance(upload.get("body"), dict) else {}
        media_id = body.get("media_id")
        if not upload.get("ok") or not media_id:
            return {"ok": False, "status": "failed", "configured": True, "path": str(item), "response": upload}
        sent = await _http._json_request_async(
            "POST",
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={urllib.parse.quote(token['access_token'])}",
            {"touser": target or "@all", "msgtype": "file", "agentid": int(os.getenv("WECOM_AGENT_ID", "0")), "file": {"media_id": media_id}, "safe": 0},
        )
        return {"ok": bool(sent.get("ok")), "status": "delivered" if sent.get("ok") else "failed", "configured": True, "path": str(item), "media_id": media_id, "response": sent}

    @staticmethod
    async def _access_token() -> dict[str, Any]:
        corp_id = str(os.getenv("WECOM_CORP_ID") or "").strip()
        secret = str(os.getenv("WECOM_SECRET") or "").strip()
        if not corp_id or not secret:
            return {"ok": False, "configured": False, "required_env": ["WECOM_CORP_ID", "WECOM_SECRET"]}
        url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={urllib.parse.quote(corp_id)}&corpsecret={urllib.parse.quote(secret)}"
        result = await _http._json_request_async("GET", url)
        body = dict(result.get("body") or {}) if isinstance(result.get("body"), dict) else {}
        return {"ok": bool(body.get("access_token")), "configured": True, "access_token": body.get("access_token"), "response": result}


class WeComCallbackAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        return {"ok": False, "status": "unsupported", "configured": self.status.configured, "error": "wecom_callback is inbound-only"}

    def verify_signature(self, *, body: bytes, headers: dict[str, str]) -> bool:
        token = str(os.getenv("WECOM_TOKEN") or "").strip()
        if not token:
            return super().verify_signature(body=body, headers=headers)
        signature = headers.get("msg_signature") or headers.get("x-wecom-signature") or headers.get("x-aiask-gateway-signature") or ""
        timestamp = headers.get("timestamp") or headers.get("x-wecom-timestamp") or ""
        nonce = headers.get("nonce") or headers.get("x-wecom-nonce") or ""
        encrypt = ""
        try:
            parsed = json.loads(body.decode("utf-8"))
            encrypt = str(parsed.get("Encrypt") or parsed.get("encrypt") or "")
        except Exception:
            encrypt = body.decode("utf-8", errors="ignore")
        expected = hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypt])).encode("utf-8")).hexdigest()
        return hmac.compare_digest(signature, expected) or hmac.compare_digest(signature, f"sha1={expected}")


class WeixinAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        app_id = str(os.getenv("WEIXIN_APP_ID") or "").strip()
        secret = str(os.getenv("WEIXIN_APP_SECRET") or "").strip()
        if not app_id or not secret:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        token_url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={urllib.parse.quote(app_id)}&secret={urllib.parse.quote(secret)}"
        token_result = await _http._json_request_async("GET", token_url)
        body = dict(token_result.get("body") or {}) if isinstance(token_result.get("body"), dict) else {}
        token = body.get("access_token")
        if not token:
            return {"ok": False, "status": "auth_failed", "configured": True, "response": token_result}
        payload = {"touser": target, "msgtype": "text", "text": {"content": message}}
        result = await _http._json_request_async("POST", f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={urllib.parse.quote(token)}", payload)
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        app_id = str(os.getenv("WEIXIN_APP_ID") or "").strip()
        secret = str(os.getenv("WEIXIN_APP_SECRET") or "").strip()
        if not app_id or not secret:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        token_result = await _http._json_request_async("GET", f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={urllib.parse.quote(app_id)}&secret={urllib.parse.quote(secret)}")
        body = dict(token_result.get("body") or {}) if isinstance(token_result.get("body"), dict) else {}
        token = body.get("access_token")
        if not token:
            return {"ok": False, "status": "auth_failed", "configured": True, "response": token_result}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        upload = _http._multipart_request(
            "POST",
            f"https://api.weixin.qq.com/cgi-bin/media/upload?access_token={urllib.parse.quote(token)}&type=file",
            files={"media": (item.name, item.read_bytes(), media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream")},
        )
        return {"ok": bool(upload.get("ok")), "status": "uploaded" if upload.get("ok") else "failed", "configured": True, "path": str(item), "response": upload}


class DiscordAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        token = str(os.getenv("DISCORD_BOT_TOKEN") or "").strip()
        if not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        channel = thread_id or target
        result = await _http._json_request_async("POST", f"https://discord.com/api/v10/channels/{urllib.parse.quote(channel)}/messages", {"content": message}, headers={"Authorization": f"Bot {token}"})
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        token = str(os.getenv("DISCORD_BOT_TOKEN") or "").strip()
        channel = str(thread_id or target or "").strip()
        if not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        if not channel:
            return {"ok": False, "status": "missing_target", "configured": True}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        content_type = media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream"
        result = _http._multipart_request(
            "POST",
            f"https://discord.com/api/v10/channels/{urllib.parse.quote(channel)}/messages",
            fields={"payload_json": json.dumps({"content": ""}, ensure_ascii=False)},
            files={"files[0]": (item.name, item.read_bytes(), content_type)},
            headers={"Authorization": f"Bot {token}"},
        )
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "path": str(item), "response": result}

    def verify_signature(self, *, body: bytes, headers: dict[str, str]) -> bool:
        public_key = str(os.getenv("DISCORD_PUBLIC_KEY") or "").strip()
        signature = headers.get("x-signature-ed25519") or ""
        timestamp = headers.get("x-signature-timestamp") or ""
        if not public_key or not signature or not timestamp:
            return super().verify_signature(body=body, headers=headers)
        try:
            from nacl.signing import VerifyKey  # type: ignore
            from nacl.exceptions import BadSignatureError  # type: ignore

            VerifyKey(bytes.fromhex(public_key)).verify(timestamp.encode("utf-8") + body, bytes.fromhex(signature))
            return True
        except (ImportError, ValueError, BadSignatureError):
            return False


class SlackAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        token = str(os.getenv("SLACK_BOT_TOKEN") or "").strip()
        result = await _http._json_request_async("POST", "https://slack.com/api/chat.postMessage", {"channel": target, "text": message, "thread_ts": thread_id}, headers={"Authorization": f"Bearer {token}"})
        return {"ok": bool(result.get("ok") and dict(result.get("body") or {}).get("ok", True)), "status": "delivered" if result.get("ok") else "failed", "configured": bool(token), "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        token = str(os.getenv("SLACK_BOT_TOKEN") or "").strip()
        if not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        fields = {"channels": str(target or ""), "thread_ts": str(thread_id or "")}
        result = _http._multipart_request(
            "POST",
            "https://slack.com/api/files.upload",
            fields={key: value for key, value in fields.items() if value},
            files={"file": (item.name, item.read_bytes(), media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream")},
            headers={"Authorization": f"Bearer {token}"},
        )
        body = dict(result.get("body") or {}) if isinstance(result.get("body"), dict) else {}
        ok = bool(result.get("ok") and body.get("ok", True))
        return {"ok": ok, "status": "delivered" if ok else "failed", "configured": True, "path": str(item), "response": result}


class TelegramAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        token = str(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        result = await _http._json_request_async("POST", f"https://api.telegram.org/bot{urllib.parse.quote(token)}/sendMessage", {"chat_id": target, "text": message, "reply_to_message_id": thread_id})
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        token = str(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = str(target or "").strip()
        if not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        if not chat_id:
            return {"ok": False, "status": "missing_target", "configured": True}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        fields: dict[str, Any] = {"chat_id": chat_id}
        if thread_id:
            fields["message_thread_id"] = thread_id
        content_type = media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream"
        result = _http._multipart_request(
            "POST",
            f"https://api.telegram.org/bot{urllib.parse.quote(token)}/sendDocument",
            fields=fields,
            files={"document": (item.name, item.read_bytes(), content_type)},
        )
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "path": str(item), "response": result}


class LineAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        token = str(os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
        if not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["LINE_CHANNEL_ACCESS_TOKEN"]}
        payload = {"to": target, "messages": [{"type": "text", "text": message}]}
        result = await _http._json_request_async(
            "POST",
            "https://api.line.me/v2/bot/message/push",
            payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}

    def verify_signature(self, *, body: bytes, headers: dict[str, str]) -> bool:
        secret = str(os.getenv("LINE_CHANNEL_SECRET") or "").strip()
        signature = headers.get("x-line-signature") or headers.get("x-aiask-gateway-signature") or ""
        if not secret or not signature:
            return super().verify_signature(body=body, headers=headers)
        expected = __import__("base64").b64encode(hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()).decode("ascii")
        return hmac.compare_digest(signature, expected)

    def normalize_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = {}
        events = payload.get("events") if isinstance(payload.get("events"), list) else []
        if events and isinstance(events[0], dict):
            event = dict(events[0])
        source = dict(event.get("source") or {}) if isinstance(event.get("source"), dict) else {}
        message = dict(event.get("message") or {}) if isinstance(event.get("message"), dict) else {}
        text = message.get("text") or payload.get("text") or ""
        target = source.get("groupId") or source.get("roomId") or source.get("userId") or ""
        return {
            **payload,
            "text": text,
            "target": target,
            "chat_id": target,
            "thread_id": event.get("replyToken") or "",
            "user_id": source.get("userId") or "",
            "message_id": message.get("id") or event.get("webhookEventId") or "",
        }


class TeamsAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        webhook = str(os.getenv("TEAMS_WEBHOOK_URL") or "").strip()
        if webhook:
            result = await _http._json_request_async("POST", webhook, {"text": message})
            return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}
        token = await self._graph_token()
        if not token.get("ok"):
            return {"ok": False, "status": "auth_failed", "configured": bool(token.get("configured")), "response": token}
        chat_id = str(target or os.getenv("MSGRAPH_CHAT_ID") or "").strip()
        if not chat_id:
            return {"ok": False, "status": "missing_target", "configured": True}
        result = await _http._json_request_async(
            "POST",
            f"https://graph.microsoft.com/v1.0/chats/{urllib.parse.quote(chat_id)}/messages",
            {"body": {"contentType": "text", "content": message}},
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}

    async def _graph_token(self) -> dict[str, Any]:
        tenant = str(os.getenv("MSGRAPH_TENANT_ID") or "").strip()
        client_id = str(os.getenv("MSGRAPH_CLIENT_ID") or "").strip()
        secret = str(os.getenv("MSGRAPH_CLIENT_SECRET") or "").strip()
        if not tenant or not client_id or not secret:
            return {"ok": False, "configured": False, "required_env": ["MSGRAPH_TENANT_ID", "MSGRAPH_CLIENT_ID", "MSGRAPH_CLIENT_SECRET"]}
        result = await _http._form_request_async(
            "POST",
            f"https://login.microsoftonline.com/{urllib.parse.quote(tenant)}/oauth2/v2.0/token",
            {
                "client_id": client_id,
                "client_secret": secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        body = dict(result.get("body") or {}) if isinstance(result.get("body"), dict) else {}
        token = body.get("access_token")
        return {"ok": bool(token), "configured": True, "access_token": token, "response": {key: value for key, value in result.items() if key != "body"}}


class HomeAssistantDeliveryAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        base = str(os.getenv("HASS_URL") or "").rstrip("/")
        token = str(os.getenv("HASS_TOKEN") or "").strip()
        if not base or not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        service = target or "notify"
        result = await _http._json_request_async("POST", f"{base}/api/services/notify/{urllib.parse.quote(service)}", {"message": message}, headers={"Authorization": f"Bearer {token}"})
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}


class MatrixAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        homeserver = str(os.getenv("MATRIX_HOMESERVER") or "").rstrip("/")
        token = str(os.getenv("MATRIX_ACCESS_TOKEN") or "").strip()
        txn = uuid4().hex
        result = await _http._json_request_async("PUT", f"{homeserver}/_matrix/client/v3/rooms/{urllib.parse.quote(target, safe='')}/send/m.room.message/{txn}", {"msgtype": "m.text", "body": message}, headers={"Authorization": f"Bearer {token}"})
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": bool(homeserver and token), "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        homeserver = str(os.getenv("MATRIX_HOMESERVER") or "").rstrip("/")
        token = str(os.getenv("MATRIX_ACCESS_TOKEN") or "").strip()
        if not homeserver or not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        upload = _http._multipart_request(
            "POST",
            f"{homeserver}/_matrix/media/v3/upload?filename={urllib.parse.quote(item.name)}",
            files={"file": (item.name, item.read_bytes(), media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream")},
            headers={"Authorization": f"Bearer {token}"},
        )
        body = dict(upload.get("body") or {}) if isinstance(upload.get("body"), dict) else {}
        content_uri = body.get("content_uri")
        if not upload.get("ok") or not content_uri:
            return {"ok": False, "status": "failed", "configured": True, "path": str(item), "response": upload}
        if target:
            txn = uuid4().hex
            sent = await _http._json_request_async(
                "PUT",
                f"{homeserver}/_matrix/client/v3/rooms/{urllib.parse.quote(target, safe='')}/send/m.room.message/{txn}",
                {"msgtype": "m.file", "body": item.name, "url": content_uri},
                headers={"Authorization": f"Bearer {token}"},
            )
            return {"ok": bool(sent.get("ok")), "status": "delivered" if sent.get("ok") else "failed", "configured": True, "path": str(item), "content_uri": content_uri, "response": sent}
        return {"ok": True, "status": "uploaded", "configured": True, "path": str(item), "content_uri": content_uri, "response": upload}


class MattermostAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        base = str(os.getenv("MATTERMOST_URL") or "").rstrip("/")
        token = str(os.getenv("MATTERMOST_TOKEN") or "").strip()
        result = await _http._json_request_async("POST", f"{base}/api/v4/posts", {"channel_id": target, "message": message, "root_id": thread_id}, headers={"Authorization": f"Bearer {token}"})
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": bool(base and token), "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        base = str(os.getenv("MATTERMOST_URL") or "").rstrip("/")
        token = str(os.getenv("MATTERMOST_TOKEN") or "").strip()
        if not base or not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        upload = _http._multipart_request(
            "POST",
            f"{base}/api/v4/files",
            fields={"channel_id": str(target or "")},
            files={"files": (item.name, item.read_bytes(), media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream")},
            headers={"Authorization": f"Bearer {token}"},
        )
        body = dict(upload.get("body") or {}) if isinstance(upload.get("body"), dict) else {}
        file_infos = body.get("file_infos") if isinstance(body.get("file_infos"), list) else []
        file_id = file_infos[0].get("id") if file_infos and isinstance(file_infos[0], dict) else None
        if not upload.get("ok") or not file_id:
            return {"ok": False, "status": "failed", "configured": True, "path": str(item), "response": upload}
        sent = await _http._json_request_async(
            "POST",
            f"{base}/api/v4/posts",
            {"channel_id": target, "message": "", "root_id": thread_id, "file_ids": [file_id]},
            headers={"Authorization": f"Bearer {token}"},
        )
        return {"ok": bool(sent.get("ok")), "status": "delivered" if sent.get("ok") else "failed", "configured": True, "path": str(item), "file_id": file_id, "response": sent}


class WhatsAppAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        token = str(os.getenv("WHATSAPP_TOKEN") or "").strip()
        phone_id = str(os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
        if not token or not phone_id:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"]}
        result = await _http._json_request_async("POST", f"https://graph.facebook.com/v19.0/{urllib.parse.quote(phone_id)}/messages", {"messaging_product": "whatsapp", "to": target, "type": "text", "text": {"body": message}}, headers={"Authorization": f"Bearer {token}"})
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        token = str(os.getenv("WHATSAPP_TOKEN") or "").strip()
        phone_id = str(os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
        if not token or not phone_id:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"]}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        upload = _http._multipart_request(
            "POST",
            f"https://graph.facebook.com/v19.0/{urllib.parse.quote(phone_id)}/media",
            fields={"messaging_product": "whatsapp", "type": media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream"},
            files={"file": (item.name, item.read_bytes(), media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream")},
            headers={"Authorization": f"Bearer {token}"},
        )
        body = dict(upload.get("body") or {}) if isinstance(upload.get("body"), dict) else {}
        media_id = body.get("id")
        if not upload.get("ok") or not media_id:
            return {"ok": False, "status": "failed", "configured": True, "path": str(item), "response": upload}
        if target:
            sent = await _http._json_request_async(
                "POST",
                f"https://graph.facebook.com/v19.0/{urllib.parse.quote(phone_id)}/messages",
                {"messaging_product": "whatsapp", "to": target, "type": "document", "document": {"id": media_id, "filename": item.name}},
                headers={"Authorization": f"Bearer {token}"},
            )
            return {"ok": bool(sent.get("ok")), "status": "delivered" if sent.get("ok") else "failed", "configured": True, "path": str(item), "media_id": media_id, "response": sent}
        return {"ok": True, "status": "uploaded", "configured": True, "path": str(item), "media_id": media_id, "response": upload}


class TwilioSMSAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        sid = str(os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
        token = str(os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
        sender = str(os.getenv("TWILIO_FROM") or "").strip()
        if not sid or not token or not sender:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"]}
        auth = __import__("base64").b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
        result = await _http._form_request_async("POST", f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json", {"From": sender, "To": target, "Body": message}, headers={"Authorization": f"Basic {auth}"})
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}


class BlueBubblesAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        base = str(os.getenv("BLUEBUBBLES_SERVER_URL") or "").rstrip("/")
        password = str(os.getenv("BLUEBUBBLES_PASSWORD") or "").strip()
        result = await _http._json_request_async("POST", f"{base}/api/v1/message/text?password={urllib.parse.quote(password)}", {"chatGuid": target, "text": message})
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": bool(base and password), "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        base = str(os.getenv("BLUEBUBBLES_SERVER_URL") or "").rstrip("/")
        password = str(os.getenv("BLUEBUBBLES_PASSWORD") or "").strip()
        if not base or not password:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": list(self.status.required_env)}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        result = _http._multipart_request(
            "POST",
            f"{base}/api/v1/message/attachment?password={urllib.parse.quote(password)}",
            fields={"chatGuid": str(target or "")},
            files={"attachment": (item.name, item.read_bytes(), media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream")},
        )
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "path": str(item), "response": result}


class SignalAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        cli = str(os.getenv("SIGNAL_CLI_PATH") or "").strip()
        executable = shutil.which(cli) if cli and os.path.sep not in cli else cli
        if not executable or not Path(executable).exists():
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["SIGNAL_CLI_PATH"]}
        account = str(os.getenv("SIGNAL_CLI_ACCOUNT") or "").strip()
        command = _executable_command(executable)
        if account:
            command.extend(["-u", account])
        command.extend(["send", "-m", message, target])
        try:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=60, check=False)
        except Exception as exc:
            return {"ok": False, "status": "failed", "configured": True, "error": str(exc), "error_type": type(exc).__name__}
        return {
            "ok": proc.returncode == 0,
            "status": "delivered" if proc.returncode == 0 else "failed",
            "configured": True,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        cli = str(os.getenv("SIGNAL_CLI_PATH") or "").strip()
        executable = shutil.which(cli) if cli and os.path.sep not in cli else cli
        if not executable or not Path(executable).exists():
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["SIGNAL_CLI_PATH"]}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        account = str(os.getenv("SIGNAL_CLI_ACCOUNT") or "").strip()
        command = _executable_command(executable)
        if account:
            command.extend(["-u", account])
        command.extend(["send", "-a", str(item), str(target or "")])
        try:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
        except Exception as exc:
            return {"ok": False, "status": "failed", "configured": True, "error": str(exc), "error_type": type(exc).__name__}
        return {
            "ok": proc.returncode == 0,
            "status": "delivered" if proc.returncode == 0 else "failed",
            "configured": True,
            "path": str(item),
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }


class SimpleXAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        cli = str(os.getenv("SIMPLEX_CLI_PATH") or "").strip()
        executable = shutil.which(cli) if cli and os.path.sep not in cli else cli
        if not executable or not Path(executable).exists():
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["SIMPLEX_CLI_PATH"]}
        command = _executable_command(executable)
        profile = str(os.getenv("SIMPLEX_PROFILE") or "").strip()
        if profile:
            command.extend(["--profile", profile])
        command.extend(["send", str(target), str(message)])
        if thread_id:
            command.extend(["--thread", str(thread_id)])
        try:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=60, check=False)
        except Exception as exc:
            return {"ok": False, "status": "failed", "configured": True, "error": str(exc), "error_type": type(exc).__name__}
        return {
            "ok": proc.returncode == 0,
            "status": "delivered" if proc.returncode == 0 else "failed",
            "configured": True,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        cli = str(os.getenv("SIMPLEX_CLI_PATH") or "").strip()
        executable = shutil.which(cli) if cli and os.path.sep not in cli else cli
        if not executable or not Path(executable).exists():
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["SIMPLEX_CLI_PATH"]}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        command = _executable_command(executable)
        command.extend(["send-file", str(target or ""), str(item)])
        if thread_id:
            command.extend(["--thread", str(thread_id)])
        try:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
        except Exception as exc:
            return {"ok": False, "status": "failed", "configured": True, "error": str(exc), "error_type": type(exc).__name__}
        return {
            "ok": proc.returncode == 0,
            "status": "delivered" if proc.returncode == 0 else "failed",
            "configured": True,
            "path": str(item),
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }


class QQBotAdapter(BasePlatformAdapter):
    async def send(self, *, target: str, message: str, thread_id: str | None = None) -> dict[str, Any]:
        app_id = str(os.getenv("QQBOT_APP_ID") or "").strip()
        token = str(os.getenv("QQBOT_TOKEN") or "").strip()
        if not app_id or not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["QQBOT_APP_ID", "QQBOT_TOKEN"]}
        base = str(os.getenv("QQBOT_API_BASE") or "https://api.sgroup.qq.com").rstrip("/")
        payload: dict[str, Any] = {"content": message}
        if thread_id:
            payload["msg_id"] = thread_id
        result = await _http._json_request_async(
            "POST",
            f"{base}/channels/{urllib.parse.quote(target)}/messages",
            payload,
            headers={"Authorization": f"Bot {app_id}.{token}"},
        )
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "response": result}

    async def upload_media(self, *, path: str, target: str | None = None, media_type: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        app_id = str(os.getenv("QQBOT_APP_ID") or "").strip()
        token = str(os.getenv("QQBOT_TOKEN") or "").strip()
        if not app_id or not token:
            return {"ok": False, "status": "unconfigured", "configured": False, "required_env": ["QQBOT_APP_ID", "QQBOT_TOKEN"]}
        item = Path(path).expanduser()
        if not item.exists() or not item.is_file():
            return {"ok": False, "status": "missing_file", "configured": True, "path": str(item)}
        base = str(os.getenv("QQBOT_API_BASE") or "https://api.sgroup.qq.com").rstrip("/")
        result = _http._multipart_request(
            "POST",
            f"{base}/channels/{urllib.parse.quote(str(target or ''))}/messages",
            fields={"msg_id": str(thread_id or "")},
            files={"file_image": (item.name, item.read_bytes(), media_type or mimetypes.guess_type(str(item))[0] or "application/octet-stream")},
            headers={"Authorization": f"Bot {app_id}.{token}"},
        )
        return {"ok": bool(result.get("ok")), "status": "delivered" if result.get("ok") else "failed", "configured": True, "path": str(item), "response": result}


ADAPTERS: dict[str, type[BasePlatformAdapter]] = {
    "local": LocalAdapter,
    "api_server": ApiServerAdapter,
    "webhook": WebhookAdapter,
    "email": EmailAdapter,
    "feishu": FeishuAdapter,
    "lark": FeishuAdapter,
    "dingtalk": DingTalkAdapter,
    "wecom": WeComAdapter,
    "wecom_callback": WeComCallbackAdapter,
    "weixin": WeixinAdapter,
    "discord": DiscordAdapter,
    "slack": SlackAdapter,
    "telegram": TelegramAdapter,
    "line": LineAdapter,
    "teams": TeamsAdapter,
    "homeassistant": HomeAssistantDeliveryAdapter,
    "matrix": MatrixAdapter,
    "mattermost": MattermostAdapter,
    "whatsapp": WhatsAppAdapter,
    "sms": TwilioSMSAdapter,
    "bluebubbles": BlueBubblesAdapter,
    "signal": SignalAdapter,
    "simplex": SimpleXAdapter,
    "qqbot": QQBotAdapter,
}


def adapter_for(status: GatewayPlatformStatus) -> BasePlatformAdapter:
    return ADAPTERS.get(status.name, BasePlatformAdapter)(status)
