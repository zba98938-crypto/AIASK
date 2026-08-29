"""Public-response redaction helpers for finance MCP servers."""

from __future__ import annotations

import re
from typing import Any


_SECRET_TEXT_KEY = (
    r"[A-Za-z0-9_.-]*(?:api[_-]?key|apikey|broker[_-]?token|"
    r"cookie|credential|password|refresh[_-]?token|secret|token(?!s\b))[A-Za-z0-9_.-]*"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"\b(?P<key>{_SECRET_TEXT_KEY})\s*(?P<sep>[:=])\s*(?P<quote>['\"]?)[^\s,;&'\"]+(?P=quote)",
    re.IGNORECASE,
)
_SECRET_QUERY_PARAM_RE = re.compile(
    rf"(?P<prefix>[?&;](?:{_SECRET_TEXT_KEY})=)(?P<value>[^&;\s]+)",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"\b(?P<key>Authorization)\s*:\s*(?P<scheme>Bearer|Bot|Basic)\s+[^\s,;&'\"]+",
    re.IGNORECASE,
)
_AUTH_QUERY_PARAM_RE = re.compile(r"(?P<prefix>[?&;]authorization=)(?P<value>[^&;\s]+)", re.IGNORECASE)
_BEARER_TOKEN_RE = re.compile(r"\b(?P<scheme>Bearer|Bot)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)

_PUBLIC_SAFE_TOKEN_KEYS = {
    "count",
    "token_count",
    "total_tokens",
}
_PUBLIC_EXACT_SECRET_KEYS = {
    "api_key",
    "apikey",
    "app_secret",
    "auth_token",
    "authorization",
    "bearer",
    "bearer_token",
    "broker_token",
    "client_secret",
    "cookie",
    "credential",
    "id_token",
    "password",
    "refresh_token",
    "secret",
    "subscription_token",
    "tenant_access_token",
    "token",
    "x-api-key",
}


def _bounded_text(value: Any, *, limit: int = 2000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"


def redact_public_text(value: Any, *, limit: int = 2000) -> str:
    """Bound and redact free-form text returned by MCP tools."""

    text = _bounded_text(value, limit=limit)
    text = _AUTH_HEADER_RE.sub(lambda match: f"{match.group('key')}: {match.group('scheme')} [redacted]", text)
    text = _BEARER_TOKEN_RE.sub(lambda match: f"{match.group('scheme')} [redacted]", text)
    text = _AUTH_QUERY_PARAM_RE.sub(lambda match: f"{match.group('prefix')}[redacted]", text)
    text = _SECRET_QUERY_PARAM_RE.sub(lambda match: f"{match.group('prefix')}[redacted]", text)
    return _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('key')}{match.group('sep')}[redacted]",
        text,
    )


def _is_public_secret_key(key: str) -> bool:
    lowered = str(key or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    if lowered in _PUBLIC_SAFE_TOKEN_KEYS:
        return False
    if lowered in _PUBLIC_EXACT_SECRET_KEYS:
        return True
    if any(marker in lowered for marker in ("api_key", "apikey", "password", "credential", "authorization", "bearer", "cookie")):
        return True
    if any(marker in lowered for marker in ("access_token", "auth_token", "broker_token", "client_secret", "id_token", "refresh_token")):
        return True
    if any(marker in compact for marker in ("apikey", "password", "credential", "authorization", "bearer", "cookie")):
        return True
    if any(marker in compact for marker in ("accesstoken", "authtoken", "brokertoken", "clientsecret", "idtoken", "refreshtoken")):
        return True
    if lowered.endswith("_token") or lowered.endswith("-token") or lowered.endswith("_secret") or lowered.endswith("-secret"):
        return True
    if compact.endswith("token") or compact.endswith("secret"):
        return True
    return False


def sanitize_public_payload(value: Any, *, max_depth: int = 8, max_items: int = 200, max_text: int = 4000) -> Any:
    """Return a bounded, secret-redacted copy for MCP-visible payloads."""

    if max_depth <= 0:
        if isinstance(value, (dict, list, tuple)):
            return {"truncated": True, "type": value.__class__.__name__}
        return redact_public_text(value, limit=max_text) if isinstance(value, str) else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_public_text(value, limit=max_text)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                result["_truncated_items"] = max(0, len(value) - max_items)
                break
            text_key = str(key)
            if _is_public_secret_key(text_key):
                result[text_key] = "[redacted]"
            else:
                result[text_key] = sanitize_public_payload(item, max_depth=max_depth - 1, max_items=max_items, max_text=max_text)
        return result
    if isinstance(value, (list, tuple)):
        items = [
            sanitize_public_payload(item, max_depth=max_depth - 1, max_items=max_items, max_text=max_text)
            for item in list(value)[:max_items]
        ]
        if len(value) > max_items:
            items.append({"_truncated_items": len(value) - max_items})
        return items
    return _bounded_text(value, limit=max_text)
