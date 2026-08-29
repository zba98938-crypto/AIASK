"""Stock profile and watchlist snapshot resources."""

from __future__ import annotations

import math
from typing import Any

from ..services.market_data_access import FALLBACK_DB_ONLY, get_quote_snapshot_response
from ..services.stock_profile_pipeline import build_stock_profile_payload
from ..storage import get_db
from ..tools.finance import get_stock_info
from ..utils import normalize_code

DEFAULT_GROUP_ID = "default"
DEFAULT_GROUP_NAME = "我的自选"
DEFAULT_GROUP_COLOR = "#6366f1"


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return parsed
    except Exception:
        return None


def _stock_info_from_tool_response(response: dict[str, Any] | None, code: str) -> dict[str, Any]:
    data = dict((response or {}).get("data") or {})
    return {
        "code": code,
        "name": data.get("name") or "",
        "industry": data.get("industry") or "",
        "list_date": data.get("listDate") or "",
        "total_shares": data.get("totalShares") or "",
        "float_shares": data.get("floatShares") or "",
        "total_market_cap": data.get("totalMarketCap") or "",
        "float_market_cap": data.get("floatMarketCap") or "",
        "raw": dict(data.get("raw") or {}),
    }


async def build_stock_profile_resource_payload(code: str) -> dict[str, Any]:
    normalized_code = normalize_code(code)
    db = get_db()
    stock_info = dict(await db.get_stock_info(normalized_code) or {}) if hasattr(db, "get_stock_info") else {}
    if not stock_info:
        info_response = get_stock_info(normalized_code)
        stock_info = _stock_info_from_tool_response(info_response, normalized_code)

    quote_response = await get_quote_snapshot_response(normalized_code, fallback_mode=FALLBACK_DB_ONLY)
    quote_data = dict(quote_response.get("data") or {}) if isinstance(quote_response, dict) else {}

    profile_payload = await build_stock_profile_payload(
        db,
        normalized_code,
        profile_type="both",
        kline_limit=90,
        version="resource_v1",
    )

    profile: dict[str, Any] | None = None
    if isinstance(profile_payload, dict):
        metadata = dict(profile_payload.get("metadata") or {})
        profile = {
            "profile_type": profile_payload.get("profile_type"),
            "version": profile_payload.get("version"),
            "vector_dim": profile_payload.get("vector_dim"),
            "summary": metadata.get("summary_text") or "",
            "feature_coverage": list(metadata.get("feature_coverage") or []),
            "raw_features": dict(metadata.get("raw_features") or {}),
        }

    return {
        "uri": f"resource://stock/{normalized_code}/profile",
        "code": normalized_code,
        "found": bool(stock_info or profile),
        "stock": {
            "code": stock_info.get("code") or normalized_code,
            "name": stock_info.get("name") or "",
            "industry": stock_info.get("industry") or stock_info.get("sector") or "",
            "market": stock_info.get("market"),
            "list_date": stock_info.get("list_date"),
            "market_cap": _safe_float(
                stock_info.get("market_cap")
                if stock_info.get("market_cap") is not None
                else stock_info.get("total_market_cap")
            ),
            "pe_ratio": _safe_float(stock_info.get("pe_ratio")),
            "pb_ratio": _safe_float(stock_info.get("pb_ratio")),
        },
        "realtime_quote": {
            "price": _safe_float(quote_data.get("price")),
            "change_pct": _safe_float(quote_data.get("pct_chg") or quote_data.get("change_pct")),
            "volume": _safe_float(quote_data.get("volume")),
            "amount": _safe_float(quote_data.get("amount")),
            "asof": quote_data.get("timestamp") or quote_data.get("time"),
        },
        "profile": profile,
    }


async def build_watchlist_snapshot_payload(user_id: str) -> dict[str, Any]:
    resolved_user_id = str(user_id or "default").strip() or "default"
    db = get_db()
    async with db.acquire() as conn:
        group_rows = await conn.fetch(
            """
            SELECT id, name, user_id, color, sort_order, created_at
            FROM watchlist_groups
            WHERE COALESCE(user_id, 'default') = $1
               OR (COALESCE(user_id, 'default') = 'default' AND id = $2)
            ORDER BY sort_order ASC, created_at ASC
            """,
            resolved_user_id,
            DEFAULT_GROUP_ID,
        )
        item_rows = await conn.fetch(
            """
            SELECT id, user_id, code, name, group_id, sort_order, note, added_at
            FROM watchlist
            WHERE user_id = $1
            ORDER BY group_id ASC, sort_order ASC, added_at DESC
            """,
            resolved_user_id,
        )

    groups: dict[str, dict[str, Any]] = {}
    for row in group_rows:
        payload = dict(row)
        group_id = str(payload.get("id") or DEFAULT_GROUP_ID).strip() or DEFAULT_GROUP_ID
        groups[group_id] = {
            "id": group_id,
            "name": str(payload.get("name") or (DEFAULT_GROUP_NAME if group_id == DEFAULT_GROUP_ID else group_id)),
            "color": str(payload.get("color") or DEFAULT_GROUP_COLOR),
            "sort_order": int(payload.get("sort_order") or 0),
            "created_at": payload.get("created_at"),
            "items": [],
        }

    if DEFAULT_GROUP_ID not in groups:
        groups[DEFAULT_GROUP_ID] = {
            "id": DEFAULT_GROUP_ID,
            "name": DEFAULT_GROUP_NAME,
            "color": DEFAULT_GROUP_COLOR,
            "sort_order": 0,
            "created_at": None,
            "items": [],
        }

    flattened_items: list[dict[str, Any]] = []
    for row in item_rows:
        payload = dict(row)
        group_id = str(payload.get("group_id") or DEFAULT_GROUP_ID).strip() or DEFAULT_GROUP_ID
        groups.setdefault(
            group_id,
            {
                "id": group_id,
                "name": group_id,
                "color": DEFAULT_GROUP_COLOR,
                "sort_order": 999,
                "created_at": payload.get("added_at"),
                "items": [],
            },
        )
        item = {
            "code": str(payload.get("code") or ""),
            "name": str(payload.get("name") or payload.get("code") or ""),
            "group_id": group_id,
            "added_at": payload.get("added_at"),
            "sort_order": int(payload.get("sort_order") or 0),
            "note": payload.get("note"),
        }
        groups[group_id]["items"].append(item)
        flattened_items.append(item)

    ordered_groups = sorted(
        groups.values(),
        key=lambda item: (int(item.get("sort_order") or 0), str(item.get("name") or "")),
    )
    for group in ordered_groups:
        group["items"] = sorted(
            list(group.get("items") or []),
            key=lambda item: (int(item.get("sort_order") or 0), str(item.get("code") or "")),
        )

    return {
        "uri": f"resource://watchlist/{resolved_user_id}/snapshot",
        "user_id": resolved_user_id,
        "summary": {
            "group_count": len(ordered_groups),
            "item_count": len(flattened_items),
        },
        "groups": ordered_groups,
        "items": flattened_items,
    }


def register(mcp) -> None:
    """Register stock and watchlist resources."""

    @mcp.resource(
        "resource://stock/{code}/profile",
        name="stock_profile",
        title="Stock Profile",
        description="Read-only stock profile snapshot with valuation, factor coverage and quote context",
        mime_type="application/json",
    )
    async def stock_profile(code: str) -> dict[str, Any]:
        return await build_stock_profile_resource_payload(code)

    @mcp.resource(
        "resource://watchlist/{user_id}/snapshot",
        name="watchlist_snapshot",
        title="Watchlist Snapshot",
        description="Read-only grouped watchlist snapshot for a user",
        mime_type="application/json",
    )
    async def watchlist_snapshot(user_id: str) -> dict[str, Any]:
        return await build_watchlist_snapshot_payload(user_id)
