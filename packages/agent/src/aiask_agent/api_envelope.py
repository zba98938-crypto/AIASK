"""Shared response envelope for the neutral domain APIs.

Every neutral endpoint answers with the same shape so a caller can branch on
``success`` / ``error_code`` without knowing which domain it asked. ``meta``
carries the honesty fields: ``source`` says where the data came from,
``freshness`` when it was produced, and ``degraded`` whether this is a complete
answer or a partial one.
"""

from __future__ import annotations

import time
from typing import Any

# Paths under this prefix were the Desktop-facing facade. The frontend was
# removed, so they are gone rather than deprecated.
REMOVED_PREFIX = "/v1/desktop"

# Old family -> the neutral family that replaced it. Used to give a caller an
# actionable 404 instead of a bare "Not Found".
#
# Capabilities with no replacement are listed separately below: they proxied a
# separate Desktop asset service (AIASK_DESKTOP_API_BASE_URL, 127.0.0.1:8001)
# that is not part of a backend-only tree. Naming a path that does not exist
# would be worse than admitting the capability is gone, so those resolve to None.
REMOVED_WITHOUT_REPLACEMENT: tuple[str, ...] = (
    "/v1/desktop/files",
    "/v1/desktop/users/{user_id}/strategies",
    "/v1/desktop/users/{user_id}/stock-pools",
)

REMOVED_ROUTE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("/v1/desktop/settings", "/v1/settings"),
    ("/v1/desktop/data", "/v1/data"),
    ("/v1/desktop/stock-data-sources", "/v1/data/stock-data-sources"),
    ("/v1/desktop/users", "/v1/users"),
    ("/v1/desktop/events", "/v1/users/events"),
    ("/v1/desktop/feedback", "/v1/users/feedback"),
    ("/v1/desktop/analytics", "/v1/users/analytics"),
    ("/v1/desktop/retention", "/v1/users/retention"),
    ("/v1/desktop/quant", "/v1/finance/quant"),
    ("/v1/desktop/financial-manager", "/v1/finance/manager"),
    ("/v1/desktop/broker-readiness", "/v1/finance/broker/readiness"),
    ("/v1/desktop/broker", "/v1/finance/broker"),
    ("/v1/desktop/factor-factory", "/v1/research/factor-factory"),
    ("/v1/desktop/stock-radar", "/v1/research/stock-radar"),
    ("/v1/desktop/trade-predictions", "/v1/research/trade-predictions"),
    ("/v1/desktop/workbench", "/v1/workbench"),
    ("/v1/desktop/runs", "/v1/runs"),
    ("/v1/desktop/capabilities", "/v1/capabilities"),
)


def _matches_removed_asset_route(path: str) -> bool:
    """True for the asset-proxy paths that have no replacement at all."""
    segments = path.strip("/").split("/")
    if path == "/v1/desktop/files" or path.startswith("/v1/desktop/files/"):
        return True
    # /v1/desktop/users/{user_id}/strategies|stock-pools[/...]
    if len(segments) >= 5 and segments[:3] == ["v1", "desktop", "users"]:
        return segments[4] in {"strategies", "stock-pools"}
    return False


def replacement_for(path: str) -> str | None:
    """Return the neutral path family that replaced a removed Desktop path."""
    candidate = str(path or "")
    if not candidate.startswith("/"):
        candidate = f"/{candidate}"
    if not candidate.startswith(REMOVED_PREFIX):
        return None
    if _matches_removed_asset_route(candidate):
        return None
    # Longest prefix first so /broker-readiness is not shadowed by /broker.
    for old, new in sorted(REMOVED_ROUTE_REPLACEMENTS, key=lambda item: len(item[0]), reverse=True):
        if candidate == old or candidate.startswith(f"{old}/"):
            return new
    return None


def removed_route_payload(path: str) -> dict[str, Any]:
    """Structured 404 body for a route that no longer exists."""
    replacement = replacement_for(path)
    detail = (
        f"{REMOVED_PREFIX}/* was removed with the Desktop frontend."
        if replacement is None
        else f"{REMOVED_PREFIX}/* was removed with the Desktop frontend; use {replacement} instead."
    )
    return envelope(
        False,
        object_name="aiask.removed_route",
        error=detail,
        error_code="ROUTE_REMOVED",
        source="aiask_agent.routes",
        meta={"requested_path": path, "replacement": replacement},
    )


def envelope(
    success: bool,
    *,
    object_name: str,
    data: Any = None,
    error: str | None = None,
    error_code: str | None = None,
    source: str | None = None,
    freshness: Any = None,
    degraded: bool = False,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard neutral-API response body."""
    merged_meta = dict(meta or {})
    merged_meta.setdefault("degraded", bool(degraded))
    return {
        "object": object_name,
        "success": bool(success),
        "data": data,
        "error": error,
        "error_code": error_code,
        "source": source,
        "freshness": freshness,
        "degraded": bool(degraded),
        "meta": merged_meta,
        "secrets_redacted": True,
        "generated_at": int(time.time()),
    }


def wrap_domain_payload(
    payload: dict[str, Any],
    *,
    object_name: str,
    source: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adapt an existing domain payload to the neutral envelope.

    Existing payload builders already return their own success/error fields; keep
    them authoritative and only add the envelope fields they lack, so hardening
    the transport does not rewrite domain semantics.
    """
    body = dict(payload or {})
    success = bool(body.get("success", True))
    result = envelope(
        success,
        object_name=str(body.get("object") or object_name),
        data=body.get("data", body if "data" not in body else None),
        error=body.get("error"),
        error_code=body.get("error_code"),
        source=str(body.get("source") or source),
        freshness=body.get("freshness") or body.get("generated_at"),
        degraded=bool(body.get("degraded", False)),
        meta={**dict(body.get("meta") or {}), **dict(meta or {})},
    )
    # Preserve domain-specific keys the envelope does not model (read_only,
    # live_trading_enabled, source_chain, ...) so callers keep their guarantees.
    for key, value in body.items():
        if key not in result:
            result[key] = value
    return result


def ensure_envelope(
    payload: Any,
    *,
    status_code: int = 200,
    path: str = "",
    source: str = "aiask_agent.routes",
) -> dict[str, Any]:
    """Fill the neutral contract fields without rewriting a domain payload.

    Domain handlers pre-date the neutral API and often return a useful object
    with ``data`` plus extra keys but no explicit success metadata.  The HTTP
    adapter uses this function at the transport boundary so every JSON response
    under a neutral route has the same observable contract while preserving all
    domain-specific fields.
    """
    if isinstance(payload, dict):
        body = dict(payload)
    else:
        body = {"data": payload}
    ok = status_code < 400 and body.get("success", True) is not False
    body.setdefault("object", "aiask.domain.response")
    body.setdefault("success", bool(ok))
    body.setdefault("data", None)
    if not ok and body.get("error") is None:
        detail = body.get("detail")
        body["error"] = detail if isinstance(detail, str) else ("request failed" if status_code >= 400 else None)
    else:
        body.setdefault("error", None)
    if not ok and body.get("error_code") is None:
        body["error_code"] = f"HTTP_{status_code}"
    else:
        body.setdefault("error_code", None)
    body.setdefault("source", source)
    body.setdefault("freshness", body.get("generated_at"))
    degraded = bool(body.get("degraded", False) or status_code >= 500)
    body["degraded"] = degraded
    meta = body.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("degraded", degraded)
    if path:
        meta.setdefault("path", path)
    body["meta"] = meta
    body.setdefault("secrets_redacted", True)
    return body


__all__ = [
    "REMOVED_PREFIX",
    "REMOVED_ROUTE_REPLACEMENTS",
    "envelope",
    "removed_route_payload",
    "replacement_for",
    "ensure_envelope",
    "wrap_domain_payload",
]
