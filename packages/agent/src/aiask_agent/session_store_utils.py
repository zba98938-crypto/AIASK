from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any


SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "broker_token",
    "cookie",
    "credential",
    "password",
    "refresh_token",
    "secret",
    "token",
)

PUBLIC_SAFE_TOKEN_KEYS = {
    "cached_tokens",
    "completion_tokens",
    "estimated_tokens",
    "estimated_tokens_after",
    "estimated_tokens_before",
    "input_tokens",
    "max_reference_tokens",
    "max_tokens",
    "output_tokens",
    "prompt_tokens",
    "reasoning_tokens",
    "token_count",
    "token_estimate",
    "token_estimate_after",
    "token_estimate_before",
    "token_env_presence",
    "token_usage",
    "total_tokens",
}
PUBLIC_SAFE_SECRET_POLICY_KEYS = {
    "auth_url",
    "authorization_url",
    "authorize_url",
    "broker_token_env_configured",
    "broker_token_policy",
    "broker_token_required",
    "explicit_token_required",
    "explicit_token_required_for_live_mcp",
    "redirect_uri",
    "token_endpoint",
    "token_url",
}
PUBLIC_EXACT_SECRET_KEYS = {
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
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(0, int(days or 0)))).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_text(value: Any, *, limit: int = 2000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"


def redact_audit_text(value: Any, *, limit: int = 2000) -> str:
    """Bound and redact free-form audit text that may contain inline secrets."""

    text = _bounded_text(value, limit=limit)
    text = _AUTH_HEADER_RE.sub(lambda match: f"{match.group('key')}: {match.group('scheme')} [redacted]", text)
    text = _BEARER_TOKEN_RE.sub(lambda match: f"{match.group('scheme')} [redacted]", text)
    text = _AUTH_QUERY_PARAM_RE.sub(lambda match: f"{match.group('prefix')}[redacted]", text)
    text = _SECRET_QUERY_PARAM_RE.sub(lambda match: f"{match.group('prefix')}[redacted]", text)
    return _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('key')}{match.group('sep')}[redacted]",
        text,
    )


def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").strip().lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def _is_public_secret_key(key: str) -> bool:
    lowered = str(key or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    safe_compact = {
        re.sub(r"[^a-z0-9]", "", item)
        for item in (*PUBLIC_SAFE_TOKEN_KEYS, *PUBLIC_SAFE_SECRET_POLICY_KEYS)
    }
    if lowered in PUBLIC_SAFE_TOKEN_KEYS or lowered in PUBLIC_SAFE_SECRET_POLICY_KEYS or compact in safe_compact:
        return False
    if lowered in PUBLIC_EXACT_SECRET_KEYS:
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


def _looks_like_json_schema(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return any(
        key in value
        for key in (
            "$defs",
            "additionalProperties",
            "allOf",
            "anyOf",
            "items",
            "oneOf",
            "properties",
            "required",
            "type",
        )
    )


def _clean_optional(value: Any, *, limit: int = 500) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:limit]


def _int_or_none(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _side_effect_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()[:200] or None
    if isinstance(value, dict):
        for key in ("level", "side_effect", "type"):
            if value.get(key):
                return str(value.get(key)).strip()[:200]
        return _dumps(sanitize_for_audit(value))[:500]
    return str(value).strip()[:200] or None


def sanitize_for_audit(value: Any, *, max_depth: int = 4, max_items: int = 40, max_text: int = 2000) -> Any:
    """Return a bounded, secret-redacted copy that is safe for local audit rows."""
    if max_depth <= 0:
        if isinstance(value, (dict, list, tuple)):
            return {"truncated": True, "type": value.__class__.__name__}
        return _bounded_text(value, limit=max_text) if isinstance(value, str) else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_audit_text(value, limit=max_text)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                result["_truncated_items"] = max(0, len(value) - max_items)
                break
            text_key = str(key)
            if _is_secret_key(text_key):
                result[text_key] = "[redacted]"
            else:
                result[text_key] = sanitize_for_audit(item, max_depth=max_depth - 1, max_items=max_items, max_text=max_text)
        return result
    if isinstance(value, (list, tuple)):
        items = [
            sanitize_for_audit(item, max_depth=max_depth - 1, max_items=max_items, max_text=max_text)
            for item in list(value)[:max_items]
        ]
        if len(value) > max_items:
            items.append({"_truncated_items": len(value) - max_items})
        return items
    return _bounded_text(value, limit=max_text)


def sanitize_public_response(
    value: Any,
    *,
    max_depth: int = 6,
    max_items: int = 120,
    max_text: int = 4000,
    _schema_properties: bool = False,
    _env_var_names: bool = False,
) -> Any:
    """Return a bounded, secret-redacted copy for model/HTTP-visible payloads.

    This is intentionally narrower than audit redaction so harmless accounting
    fields such as total_tokens and token_estimate_before remain useful.
    """

    if max_depth <= 0:
        if isinstance(value, (dict, list, tuple)):
            return {"truncated": True, "type": value.__class__.__name__}
        return redact_audit_text(value, limit=max_text) if isinstance(value, str) else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _env_var_names:
            text = value.strip()
            return text if _ENV_VAR_NAME_RE.match(text) else "[redacted]"
        return redact_audit_text(value, limit=max_text)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                result["_truncated_items"] = max(0, len(value) - max_items)
                break
            text_key = str(key)
            if _env_var_names:
                result[text_key] = sanitize_public_response(
                    item,
                    max_depth=max_depth - 1,
                    max_items=max_items,
                    max_text=max_text,
                    _env_var_names=True,
                )
            elif _schema_properties:
                result[text_key] = sanitize_public_response(item, max_depth=max_depth - 1, max_items=max_items, max_text=max_text)
            elif text_key == "properties" and isinstance(item, dict) and _looks_like_json_schema(value):
                result[text_key] = sanitize_public_response(
                    item,
                    max_depth=max_depth - 1,
                    max_items=max_items,
                    max_text=max_text,
                    _schema_properties=True,
                )
            elif text_key == "headers_from_env" and isinstance(item, dict):
                result[text_key] = sanitize_public_response(
                    item,
                    max_depth=max_depth - 1,
                    max_items=max_items,
                    max_text=max_text,
                    _env_var_names=True,
                )
            elif _is_public_secret_key(text_key):
                result[text_key] = "[redacted]"
            else:
                result[text_key] = sanitize_public_response(item, max_depth=max_depth - 1, max_items=max_items, max_text=max_text)
        return result
    if isinstance(value, (list, tuple)):
        items = [
            sanitize_public_response(item, max_depth=max_depth - 1, max_items=max_items, max_text=max_text)
            for item in list(value)[:max_items]
        ]
        if len(value) > max_items:
            items.append({"_truncated_items": len(value) - max_items})
        return items
    return _bounded_text(value, limit=max_text)


def _metadata_archived(metadata: dict[str, Any] | None) -> bool:
    value = dict(metadata or {}).get("archived")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "archived"}
    return bool(value)
