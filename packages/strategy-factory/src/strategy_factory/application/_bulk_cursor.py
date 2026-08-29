"""Shared helpers for strategy factory bulk stock matrix cursor state."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Optional

from ..domain.constants import STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT

_BULK_STOCK_CURSOR_KEYS = frozenset(
    {
        "bulk_stock_matrix_enabled",
        "bulk_stock_matrix_universe_limit",
        "bulk_stock_matrix_requested_universe_offset",
        "bulk_stock_matrix_effective_universe_offset",
        "bulk_stock_matrix_universe_offset_fallback",
        "bulk_stock_matrix_eligible_stock_count",
        "bulk_stock_matrix_next_universe_offset",
        "bulk_stock_matrix_cursor_wrapped",
        "bulk_stock_matrix_requested_task_offset",
        "bulk_stock_matrix_effective_task_offset",
        "bulk_stock_matrix_task_offset_fallback",
        "bulk_stock_matrix_next_task_offset",
        "bulk_stock_matrix_task_cursor_wrapped",
        "bulk_stock_matrix_planned_task_count",
    }
)


def coerce_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return max(0, int(default))


def extract_bulk_stock_cursor(
    summary: Optional[dict[str, Any]],
    *,
    source: str,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    payload = dict(summary or {})
    available = any(key in payload for key in _BULK_STOCK_CURSOR_KEYS)
    universe_limit = max(
        1,
        coerce_non_negative_int(
            payload.get("bulk_stock_matrix_universe_limit"),
            STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
        ),
    )
    enabled = bool(payload.get("bulk_stock_matrix_enabled"))
    requested_offset = coerce_non_negative_int(payload.get("bulk_stock_matrix_requested_universe_offset"))
    effective_offset = coerce_non_negative_int(payload.get("bulk_stock_matrix_effective_universe_offset"))
    offset_fallback = bool(payload.get("bulk_stock_matrix_universe_offset_fallback"))
    eligible_stock_count = coerce_non_negative_int(payload.get("bulk_stock_matrix_eligible_stock_count"))
    next_offset_raw = payload.get("bulk_stock_matrix_next_universe_offset")
    if next_offset_raw is None:
        if not enabled or eligible_stock_count <= 0:
            next_universe_offset = 0
            cursor_wrapped = False
        elif offset_fallback:
            next_universe_offset = universe_limit
            cursor_wrapped = True
        elif eligible_stock_count < universe_limit:
            next_universe_offset = 0
            cursor_wrapped = True
        else:
            next_universe_offset = effective_offset + universe_limit
            cursor_wrapped = False
    else:
        next_universe_offset = coerce_non_negative_int(next_offset_raw)
        if "bulk_stock_matrix_cursor_wrapped" in payload:
            cursor_wrapped = bool(payload.get("bulk_stock_matrix_cursor_wrapped"))
        else:
            cursor_wrapped = bool(
                enabled and eligible_stock_count > 0 and (offset_fallback or next_universe_offset == 0)
            )
    requested_task_offset = coerce_non_negative_int(
        payload.get("bulk_stock_matrix_requested_task_offset"),
        requested_offset,
    )
    effective_task_offset = coerce_non_negative_int(
        payload.get("bulk_stock_matrix_effective_task_offset"),
        effective_offset,
    )
    task_offset_fallback = bool(
        payload.get("bulk_stock_matrix_task_offset_fallback")
        if "bulk_stock_matrix_task_offset_fallback" in payload
        else offset_fallback
    )
    planned_task_count = coerce_non_negative_int(payload.get("bulk_stock_matrix_planned_task_count"))
    next_task_offset = coerce_non_negative_int(
        payload.get("bulk_stock_matrix_next_task_offset"),
        next_universe_offset,
    )
    task_cursor_wrapped = bool(
        payload.get("bulk_stock_matrix_task_cursor_wrapped")
        if "bulk_stock_matrix_task_cursor_wrapped" in payload
        else cursor_wrapped
    )
    return {
        "available": available,
        "source": str(source or "default"),
        "resume_from_run_id": str(run_id or "").strip() or None,
        "enabled": enabled,
        "universe_limit": universe_limit,
        "requested_universe_offset": requested_offset,
        "effective_universe_offset": effective_offset,
        "universe_offset_fallback": offset_fallback,
        "eligible_stock_count": eligible_stock_count,
        "next_universe_offset": next_universe_offset,
        "cursor_wrapped": cursor_wrapped,
        "cursor_mode": str(payload.get("bulk_stock_matrix_cursor_mode") or "task_offset").strip() or "task_offset",
        "requested_task_offset": requested_task_offset,
        "effective_task_offset": effective_task_offset,
        "task_offset_fallback": task_offset_fallback,
        "planned_task_count": planned_task_count,
        "next_task_offset": next_task_offset,
        "task_cursor_wrapped": task_cursor_wrapped,
    }


async def _call_optional_async(
    target: Any,
    method_name: str,
    *args,
    default=None,
    caller: Optional[Callable[..., Any]] = None,
    **kwargs,
):
    if callable(caller):
        result = caller(target, method_name, *args, default=default, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
    method = getattr(target, method_name, None)
    if not callable(method):
        return default
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def resolve_bulk_stock_matrix_cursor(
    *,
    last_result: Optional[dict[str, Any]],
    db,
    logger_: Optional[logging.Logger] = None,
    call_optional_async: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    payload = dict(last_result or {})
    last_cursor = extract_bulk_stock_cursor(
        (payload.get("summary") or {}),
        source="last_result",
        run_id=payload.get("run_id"),
    )
    if last_cursor.get("available"):
        return last_cursor

    try:
        latest_run = await _call_optional_async(
            db,
            "get_latest_strategy_factory_run",
            default=None,
            caller=call_optional_async,
        )
    except Exception as exc:
        if logger_ is not None:
            logger_.warning(
                "StrategyFactory: failed to resolve persisted bulk cursor, falling back to default: %s",
                exc,
            )
        latest_run = None
    latest_payload = dict(latest_run or {})
    latest_cursor = extract_bulk_stock_cursor(
        (latest_payload.get("summary") or {}),
        source="persisted_run",
        run_id=latest_payload.get("run_id"),
    )
    if latest_cursor.get("available"):
        return latest_cursor

    return extract_bulk_stock_cursor({}, source="default")
