"""Shared paginated stock-universe loading helpers."""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _coerce_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return max(0, int(default))


async def load_stock_universe_rows(
    db,
    *,
    limit: int,
    page_size: int,
    start_offset: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load stock-universe rows page by page until exhausted or the requested cap is reached.

    On page-level failure, the exception is recorded into ``meta`` (``last_error``/
    ``last_error_type``/``last_error_offset``) and pagination stops; callers can decide
    whether to escalate. Silent failures are not allowed: a non-empty ``last_error``
    plus ``loaded_count == 0`` should surface as ``skip_reason="universe_load_failed"``
    in the factory run summary.
    """

    requested_limit = max(1, _coerce_non_negative_int(limit, 1))
    effective_page_size = max(1, min(_coerce_non_negative_int(page_size, requested_limit), requested_limit))
    current_offset = max(0, _coerce_non_negative_int(start_offset, 0))
    list_stock_universe = getattr(db, "list_stock_universe", None)

    meta: dict[str, Any] = {
        "available": callable(list_stock_universe),
        "requested_limit": requested_limit,
        "page_size": effective_page_size,
        "start_offset": current_offset,
        "pages_loaded": 0,
        "loaded_count": 0,
        "raw_loaded_count": 0,
        "next_offset": current_offset,
        "exhausted": False,
        "truncated": False,
        "complete": False,
        "last_error": None,
        "last_error_type": None,
        "last_error_offset": None,
    }
    if not callable(list_stock_universe):
        return [], meta

    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    while meta["raw_loaded_count"] < requested_limit:
        fetch_limit = min(effective_page_size, requested_limit - int(meta["raw_loaded_count"]))
        try:
            result = list_stock_universe(limit=fetch_limit, offset=current_offset)
            if inspect.isawaitable(result):
                result = await result
            page_rows = [dict(item or {}) for item in list(result or []) if isinstance(item, dict)]
        except Exception as exc:
            logger.warning(
                "load_stock_universe_rows: page fetch failed at offset=%s limit=%s: %s",
                current_offset,
                fetch_limit,
                exc,
            )
            meta["last_error"] = str(exc)
            meta["last_error_type"] = type(exc).__name__
            meta["last_error_offset"] = current_offset
            meta["pages_loaded"] = int(meta["pages_loaded"]) + 1
            break

        meta["pages_loaded"] = int(meta["pages_loaded"]) + 1
        page_count = len(page_rows)
        if page_count <= 0:
            meta["exhausted"] = True
            break

        meta["raw_loaded_count"] = int(meta["raw_loaded_count"]) + page_count
        for index, row in enumerate(page_rows):
            code = str((row or {}).get("code") or "").strip()
            dedupe_key = code or f"row:{current_offset + index}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            rows.append(row)

        current_offset += page_count
        meta["next_offset"] = current_offset
        if page_count < fetch_limit:
            meta["exhausted"] = True
            break

    meta["loaded_count"] = len(rows)
    meta["truncated"] = bool(meta["raw_loaded_count"] >= requested_limit and not meta["exhausted"])
    meta["complete"] = bool(meta["exhausted"]) and meta["last_error"] is None
    return rows, meta


def filter_stock_universe_rows_by_codes(
    rows: list[dict[str, Any]],
    codes: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    target_codes: list[str] = []
    raw_codes = codes
    if isinstance(raw_codes, str):
        raw_codes = [item.strip() for item in raw_codes.split(",") if item.strip()]
    for item in list(raw_codes or []):
        code = str(item or "").strip()
        if code and code not in target_codes:
            target_codes.append(code)
    if not target_codes:
        return [dict(item or {}) for item in list(rows or [])], {
            "target_code_filter_applied": False,
            "requested_target_codes": [],
            "target_missing_codes": [],
        }

    rows_by_code: dict[str, dict[str, Any]] = {}
    for item in list(rows or []):
        row = dict(item or {})
        code = str(row.get("code") or "").strip()
        if code and code not in rows_by_code:
            rows_by_code[code] = row

    filtered_rows: list[dict[str, Any]] = []
    missing_codes: list[str] = []
    for code in target_codes:
        row = dict(rows_by_code.get(code) or {})
        if row:
            filtered_rows.append(row)
        else:
            missing_codes.append(code)
            filtered_rows.append({"code": code, "name": code})

    return filtered_rows, {
        "target_code_filter_applied": True,
        "requested_target_codes": target_codes,
        "target_missing_codes": missing_codes,
        "target_universe_row_count": len(filtered_rows),
        "target_universe_found_count": len(filtered_rows) - len(missing_codes),
    }
