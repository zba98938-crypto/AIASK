from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.error
import urllib.request
from typing import Any

from .session_store_utils import sanitize_public_response


def _public_platform_payload(value: Any) -> Any:
    return sanitize_public_response(value, max_depth=8, max_items=200, max_text=4000)


def _json_request(method: str, url: str, payload: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json", **dict(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read(10 * 1024 * 1024).decode("utf-8", errors="replace")
            parsed = json.loads(text) if text else {}
            return {"ok": 200 <= getattr(response, "status", 200) < 300, "status_code": getattr(response, "status", None), "body": parsed}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            parsed: Any = json.loads(text)
        except Exception:
            parsed = text
        return {"ok": False, "status_code": exc.code, "error": exc.reason, "body": parsed}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


def _query(params: dict[str, Any]) -> str:
    return urllib.parse.urlencode({key: value for key, value in params.items() if value not in {None, ""}})


class FeishuClient:
    def __init__(self, *, domain: str = "feishu") -> None:
        self.domain = domain if domain in {"feishu", "lark"} else "feishu"
        self.prefix = "LARK" if self.domain == "lark" else "FEISHU"
        self.base_url = "https://open.larksuite.com" if self.domain == "lark" else "https://open.feishu.cn"

    def configured(self) -> bool:
        return bool(os.getenv(f"{self.prefix}_APP_ID") and os.getenv(f"{self.prefix}_APP_SECRET"))

    def _tenant_token_raw(self) -> dict[str, Any]:
        app_id = str(os.getenv(f"{self.prefix}_APP_ID") or "").strip()
        app_secret = str(os.getenv(f"{self.prefix}_APP_SECRET") or "").strip()
        if not app_id or not app_secret:
            return {"ok": False, "configured": False, "required_env": [f"{self.prefix}_APP_ID", f"{self.prefix}_APP_SECRET"]}
        result = _json_request("POST", f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal", {"app_id": app_id, "app_secret": app_secret})
        body = dict(result.get("body") or {}) if isinstance(result.get("body"), dict) else {}
        token = body.get("tenant_access_token")
        return {"ok": bool(token), "configured": True, "tenant_access_token": token, "response": result}

    def tenant_token(self) -> dict[str, Any]:
        return _public_platform_payload(self._tenant_token_raw())

    def _headers(self) -> dict[str, str]:
        token = self._tenant_token_raw()
        if not token.get("ok"):
            raise RuntimeError(json.dumps(_public_platform_payload(token), ensure_ascii=False))
        return {"Authorization": f"Bearer {token['tenant_access_token']}"}

    def read_doc(self, *, document_id: str | None = None, url: str | None = None) -> dict[str, Any]:
        token = self._extract_doc_token(document_id=document_id, url=url)
        if not token:
            raise ValueError("document_id or url is required")
        if not self.configured():
            return {"configured": False, "required_env": [f"{self.prefix}_APP_ID", f"{self.prefix}_APP_SECRET"], "document_id": token}
        headers = self._headers()
        attempts = [
            f"{self.base_url}/open-apis/docx/v1/documents/{urllib.parse.quote(token)}/raw_content",
            f"{self.base_url}/open-apis/doc/v2/{urllib.parse.quote(token)}/content",
        ]
        responses: list[dict[str, Any]] = []
        for endpoint in attempts:
            result = _json_request("GET", endpoint, headers=headers)
            responses.append({"endpoint": endpoint, "result": result})
            body = result.get("body")
            if result.get("ok") and isinstance(body, dict):
                data = body.get("data") if isinstance(body.get("data"), dict) else body
                content = data.get("content") or data.get("raw_content") or data.get("text") or data
                return {"configured": True, "document_id": token, "content": content, "response": result}
        return {"configured": True, "document_id": token, "content": None, "responses": responses, "error": "Feishu document read failed"}

    def list_comments(self, *, file_token: str, page_token: str | None = None, page_size: int = 50, file_type: str = "docx") -> dict[str, Any]:
        if not self.configured():
            return {"configured": False, "required_env": [f"{self.prefix}_APP_ID", f"{self.prefix}_APP_SECRET"], "file_token": file_token}
        query = _query(
            {
                "file_type": file_type or "docx",
                "user_id_type": "open_id",
                "page_size": max(1, min(int(page_size or 50), 100)),
                "page_token": page_token,
            }
        )
        result = _json_request("GET", f"{self.base_url}/open-apis/drive/v1/files/{urllib.parse.quote(file_token)}/comments?{query}", headers=self._headers())
        return {"configured": True, "file_token": file_token, "response": result, "comments": dict(result.get("body") or {}).get("data", {}) if isinstance(result.get("body"), dict) else {}}

    def list_comment_replies(
        self,
        *,
        file_token: str,
        comment_id: str,
        file_type: str = "docx",
        page_size: int = 100,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        if not self.configured():
            return {
                "configured": False,
                "required_env": [f"{self.prefix}_APP_ID", f"{self.prefix}_APP_SECRET"],
                "file_token": file_token,
                "comment_id": comment_id,
            }
        query = _query(
            {
                "file_type": file_type or "docx",
                "user_id_type": "open_id",
                "page_size": max(1, min(int(page_size or 100), 100)),
                "page_token": page_token,
            }
        )
        result = _json_request(
            "GET",
            f"{self.base_url}/open-apis/drive/v1/files/{urllib.parse.quote(file_token)}/comments/{urllib.parse.quote(comment_id)}/replies?{query}",
            headers=self._headers(),
        )
        body = dict(result.get("body") or {}) if isinstance(result.get("body"), dict) else {}
        return {
            "configured": True,
            "file_token": file_token,
            "comment_id": comment_id,
            "response": result,
            "replies": body.get("data", {}),
        }

    def add_comment(self, *, file_token: str, message: str) -> dict[str, Any]:
        if not self.configured():
            return {"configured": False, "required_env": [f"{self.prefix}_APP_ID", f"{self.prefix}_APP_SECRET"], "file_token": file_token}
        payload = {"reply_list": [{"content": message}]}
        result = _json_request("POST", f"{self.base_url}/open-apis/drive/v1/files/{urllib.parse.quote(file_token)}/comments", payload, headers=self._headers())
        return {"configured": True, "file_token": file_token, "response": result}

    def reply_comment(self, *, file_token: str, comment_id: str, message: str) -> dict[str, Any]:
        if not self.configured():
            return {"configured": False, "required_env": [f"{self.prefix}_APP_ID", f"{self.prefix}_APP_SECRET"], "file_token": file_token}
        payload = {"content": message}
        result = _json_request(
            "POST",
            f"{self.base_url}/open-apis/drive/v1/files/{urllib.parse.quote(file_token)}/comments/{urllib.parse.quote(comment_id)}/reply",
            payload,
            headers=self._headers(),
        )
        return {"configured": True, "file_token": file_token, "comment_id": comment_id, "response": result}


class DiscordServerClient:
    def __init__(self, *, token: str | None = None) -> None:
        self.token = str(token or os.getenv("DISCORD_BOT_TOKEN") or "").strip()

    def configured(self) -> bool:
        return bool(self.token)

    def call(self, *, action: str, **kwargs: Any) -> dict[str, Any]:
        if not self.token:
            return {"configured": False, "required_env": ["DISCORD_BOT_TOKEN"], "action": action}
        actions = {
            "list_guilds": self._list_guilds,
            "server_info": self._server_info,
            "list_channels": self._list_channels,
            "channel_info": self._channel_info,
            "list_roles": self._list_roles,
            "member_info": self._member_info,
            "search_members": self._search_members,
            "fetch_messages": self._fetch_messages,
            "list_pins": self._list_pins,
            "pin_message": self._pin_message,
            "unpin_message": self._unpin_message,
            "create_thread": self._create_thread,
            "add_role": self._add_role,
            "remove_role": self._remove_role,
        }
        fn = actions.get(str(action or "").strip())
        if fn is None:
            return {"configured": True, "ok": False, "error": f"Unknown action: {action}", "available_actions": sorted(actions)}
        missing = [name for name in self.required_params(str(action)) if not str(kwargs.get(name) or "").strip()]
        if missing:
            return {"configured": True, "ok": False, "error": f"Missing required parameters for {action}: {', '.join(missing)}", "missing": missing}
        return {"configured": True, "action": action, **fn(**kwargs)}

    @staticmethod
    def required_params(action: str) -> list[str]:
        return {
            "server_info": ["guild_id"],
            "list_channels": ["guild_id"],
            "channel_info": ["channel_id"],
            "list_roles": ["guild_id"],
            "member_info": ["guild_id", "user_id"],
            "search_members": ["guild_id", "query"],
            "fetch_messages": ["channel_id"],
            "list_pins": ["channel_id"],
            "pin_message": ["channel_id", "message_id"],
            "unpin_message": ["channel_id", "message_id"],
            "create_thread": ["channel_id", "name"],
            "add_role": ["guild_id", "user_id", "role_id"],
            "remove_role": ["guild_id", "user_id", "role_id"],
        }.get(action, [])

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = f"?{_query(dict(params or {}))}" if params else ""
        result = _json_request(
            method,
            f"https://discord.com/api/v10{path}{query}",
            body,
            headers={"Authorization": f"Bot {self.token}"},
        )
        if not result.get("ok"):
            return {"ok": False, "response": result, "error": result.get("error") or "Discord API call failed"}
        return {"ok": True, "response": result, "data": result.get("body")}

    def _list_guilds(self, **_: Any) -> dict[str, Any]:
        result = self._request("GET", "/users/@me/guilds")
        guilds = result.get("data") if isinstance(result.get("data"), list) else []
        return {**result, "guilds": guilds, "count": len(guilds)}

    def _server_info(self, *, guild_id: str, **_: Any) -> dict[str, Any]:
        return self._request("GET", f"/guilds/{urllib.parse.quote(guild_id)}", params={"with_counts": "true"})

    def _list_channels(self, *, guild_id: str, **_: Any) -> dict[str, Any]:
        result = self._request("GET", f"/guilds/{urllib.parse.quote(guild_id)}/channels")
        channels = result.get("data") if isinstance(result.get("data"), list) else []
        return {**result, "channels": channels, "count": len(channels)}

    def _channel_info(self, *, channel_id: str, **_: Any) -> dict[str, Any]:
        return self._request("GET", f"/channels/{urllib.parse.quote(channel_id)}")

    def _list_roles(self, *, guild_id: str, **_: Any) -> dict[str, Any]:
        result = self._request("GET", f"/guilds/{urllib.parse.quote(guild_id)}/roles")
        roles = result.get("data") if isinstance(result.get("data"), list) else []
        return {**result, "roles": roles, "count": len(roles)}

    def _member_info(self, *, guild_id: str, user_id: str, **_: Any) -> dict[str, Any]:
        return self._request("GET", f"/guilds/{urllib.parse.quote(guild_id)}/members/{urllib.parse.quote(user_id)}")

    def _search_members(self, *, guild_id: str, query: str, limit: int = 20, **_: Any) -> dict[str, Any]:
        result = self._request("GET", f"/guilds/{urllib.parse.quote(guild_id)}/members/search", params={"query": query, "limit": max(1, min(int(limit or 20), 100))})
        members = result.get("data") if isinstance(result.get("data"), list) else []
        return {**result, "members": members, "count": len(members)}

    def _fetch_messages(self, *, channel_id: str, limit: int = 50, before: str = "", after: str = "", **_: Any) -> dict[str, Any]:
        result = self._request(
            "GET",
            f"/channels/{urllib.parse.quote(channel_id)}/messages",
            params={"limit": max(1, min(int(limit or 50), 100)), "before": before, "after": after},
        )
        messages = result.get("data") if isinstance(result.get("data"), list) else []
        return {**result, "messages": messages, "count": len(messages)}

    def _list_pins(self, *, channel_id: str, **_: Any) -> dict[str, Any]:
        result = self._request("GET", f"/channels/{urllib.parse.quote(channel_id)}/pins")
        messages = result.get("data") if isinstance(result.get("data"), list) else []
        return {**result, "pinned_messages": messages, "count": len(messages)}

    def _pin_message(self, *, channel_id: str, message_id: str, **_: Any) -> dict[str, Any]:
        return self._request("PUT", f"/channels/{urllib.parse.quote(channel_id)}/pins/{urllib.parse.quote(message_id)}")

    def _unpin_message(self, *, channel_id: str, message_id: str, **_: Any) -> dict[str, Any]:
        return self._request("DELETE", f"/channels/{urllib.parse.quote(channel_id)}/pins/{urllib.parse.quote(message_id)}")

    def _create_thread(self, *, channel_id: str, name: str, message_id: str = "", auto_archive_duration: int = 1440, **_: Any) -> dict[str, Any]:
        body = {"name": name, "auto_archive_duration": auto_archive_duration}
        if message_id:
            path = f"/channels/{urllib.parse.quote(channel_id)}/messages/{urllib.parse.quote(message_id)}/threads"
        else:
            path = f"/channels/{urllib.parse.quote(channel_id)}/threads"
            body["type"] = 11
        return self._request("POST", path, body=body)

    def _add_role(self, *, guild_id: str, user_id: str, role_id: str, **_: Any) -> dict[str, Any]:
        return self._request("PUT", f"/guilds/{urllib.parse.quote(guild_id)}/members/{urllib.parse.quote(user_id)}/roles/{urllib.parse.quote(role_id)}")

    def _remove_role(self, *, guild_id: str, user_id: str, role_id: str, **_: Any) -> dict[str, Any]:
        return self._request("DELETE", f"/guilds/{urllib.parse.quote(guild_id)}/members/{urllib.parse.quote(user_id)}/roles/{urllib.parse.quote(role_id)}")

    @staticmethod
    def _extract_doc_token(*, document_id: str | None = None, url: str | None = None) -> str:
        if document_id:
            return str(document_id).strip()
        raw = str(url or "").strip()
        for pattern in (r"/docx?/([A-Za-z0-9_-]+)", r"/wiki/([A-Za-z0-9_-]+)", r"token=([A-Za-z0-9_-]+)"):
            match = re.search(pattern, raw)
            if match:
                return match.group(1)
        return raw
