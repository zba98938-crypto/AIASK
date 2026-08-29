"""Live broker gateway adapters with safe default read-only mode."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..env_loader import load_mcp_env

_TERMINAL_EVENT_STATUSES = {"filled", "cancelled", "rejected"}

def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default

def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None

def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None

def _safe_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None

def _dig(payload: Any, *path: str) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current

def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item or {}) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [dict(value)]
    return []

def _normalize_symbol(value: Any) -> str | None:
    text = _safe_text(value)
    return text.upper() if text else None

def _normalize_event_time(payload: dict[str, Any], *extra: Any) -> str | None:
    value = _safe_text(
        payload.get("occurred_at"),
        payload.get("transaction_time"),
        payload.get("activity_date"),
        payload.get("date"),
        payload.get("filled_at"),
        payload.get("last_fill_at"),
        payload.get("submitted_at"),
        payload.get("created_at"),
        payload.get("transaction_date"),
        payload.get("updated_at"),
        *extra,
    )
    return value

def _build_live_order_ticket(order: dict[str, Any], *, status: str | None = None, account_id: str | None = None) -> dict[str, Any]:
    normalized_status = _safe_text(status, order.get("status")) or "unknown"
    qty = _safe_int(order.get("qty"))
    filled_qty = _safe_int(order.get("filled_qty"))
    if filled_qty is None:
        filled_qty = qty if normalized_status == "filled" else 0
    remaining_qty = None
    if qty is not None and filled_qty is not None:
        remaining_qty = max(int(qty) - int(filled_qty), 0)
    return {
        "schema_version": "v1",
        "ticket_type": "live_order_ticket",
        "provider": order.get("provider"),
        "order_id": _safe_text(order.get("order_id")) or "",
        "account_id": account_id or order.get("account_id"),
        "code": _normalize_symbol(order.get("symbol")),
        "status": normalized_status,
        "terminal": normalized_status in _TERMINAL_EVENT_STATUSES,
        "created_at": _safe_text(order.get("created_at"), order.get("submitted_at")),
        "updated_at": _safe_text(order.get("updated_at"), order.get("filled_at"), order.get("submitted_at")),
        "source": _safe_text(order.get("source"), order.get("provider")) or "live_broker",
        "order": {
            "order_type": order.get("order_type"),
            "direction": order.get("side"),
            "shares": qty,
            "filled_shares": filled_qty,
            "remaining_shares": remaining_qty,
            "price": _safe_float(order.get("limit_price")) if str(order.get("order_type") or "").strip().lower() == "limit" else _safe_float(order.get("filled_avg_price")),
            "stop_price": _safe_float(order.get("stop_price")),
            "amount": _safe_float(order.get("notional")),
            "commission": _safe_float(order.get("commission")),
        },
    }

def _normalize_broker_fill(
    provider: str,
    payload: dict[str, Any],
    *,
    account_id: str | None = None,
    order_id: str | None = None,
    symbol: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    raw = dict(payload or {})
    resolved_order_id = _safe_text(raw.get("order_id"), raw.get("id"), order_id)
    qty = _safe_float(raw.get("qty") or raw.get("quantity") or raw.get("filled_qty") or raw.get("exec_quantity") or raw.get("last_fill_qty"))
    price = _safe_float(raw.get("price") or raw.get("filled_avg_price") or raw.get("avg_fill_price") or raw.get("last_fill_price"))
    amount = _safe_float(raw.get("amount") or raw.get("gross_amount") or raw.get("net_amount"))
    if amount is None and qty is not None and price is not None:
        amount = float(qty) * float(price)
    fill_time = _normalize_event_time(raw)
    fill_id = _safe_text(raw.get("fill_id"), raw.get("activity_id"), raw.get("trade_id"), raw.get("id"))
    if fill_id is None:
        fill_id = f"{provider}:{resolved_order_id or _safe_text(symbol, raw.get('symbol')) or 'unknown'}:{fill_time or 'na'}"
    resolved_symbol = _normalize_symbol(symbol or raw.get("symbol"))
    return {
        "schema_version": "v1",
        "provider": provider,
        "fill_id": fill_id,
        "order_id": resolved_order_id,
        "account_id": account_id or _safe_text(raw.get("account_id")),
        "symbol": resolved_symbol,
        "side": _safe_text(raw.get("side"), raw.get("transaction"), raw.get("direction")),
        "fill_type": _safe_text(raw.get("type"), raw.get("activity_type")) or "fill",
        "occurred_at": fill_time,
        "price": price,
        "qty": qty,
        "shares": _safe_int(qty),
        "amount": amount,
        "commission": _safe_float(raw.get("commission") or raw.get("fee")),
        "venue": _safe_text(raw.get("exchange"), raw.get("venue")),
        "source": source or f"{provider}.fills",
        "raw": raw,
    }

def _build_broker_receipt(
    provider: str,
    order: dict[str, Any],
    *,
    account_id: str | None = None,
    raw_receipt: dict[str, Any] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    raw = dict(raw_receipt or order or {})
    status = _safe_text(order.get("status"), raw.get("status")) or "unknown"
    message_type = "brokerage_rejected" if status in {"rejected", "error"} else "brokerage_ack"
    message_id = _safe_text(raw.get("message_id"), raw.get("id"), raw.get("order_id"))
    message = _safe_text(raw.get("message"), raw.get("reason"), raw.get("error"), raw.get("description"), status)
    severity = "high" if status in {"rejected", "error"} else "low"
    order_id = _safe_text(order.get("order_id"), raw.get("order_id"))
    symbol = _normalize_symbol(order.get("symbol") or raw.get("symbol"))
    received_at = _safe_text(order.get("submitted_at"), order.get("created_at"), raw.get("created_at"))
    acknowledged_at = _safe_text(order.get("updated_at"), raw.get("updated_at"), raw.get("transaction_date"), received_at)
    return {
        "schema_version": "v1",
        "provider": provider,
        "message_id": message_id or f"{provider}:{order_id or symbol or 'unknown'}:receipt",
        "receipt_id": message_id or f"{provider}:{order_id or symbol or 'unknown'}:receipt",
        "order_id": order_id,
        "account_id": account_id or order.get("account_id"),
        "symbol": symbol,
        "message_type": message_type,
        "occurred_at": acknowledged_at,
        "status": status,
        "severity": severity,
        "reason": message,
        "broker_status": status,
        "source": source or f"{provider}.receipt",
        "retryable": False,
        "raw": raw,
    }

def _build_live_order_event(
    *,
    order: dict[str, Any],
    event_type: str,
    event_status: str,
    occurred_at: str | None,
    raw_payload: dict[str, Any],
    fill_event: dict[str, Any] | None = None,
    brokerage_event: dict[str, Any] | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
) -> dict[str, Any]:
    order_ticket = _build_live_order_ticket(order, status=event_status, account_id=order.get("account_id"))
    event_category = "execution" if fill_event is not None else "brokerage" if brokerage_event is not None else "order_lifecycle"
    object_kind = "fill_event" if fill_event is not None else "brokerage_event" if brokerage_event is not None else "order_event"
    return {
        "schema_version": "v1",
        "provider": order.get("provider"),
        "order_id": _safe_text(order.get("order_id")) or "",
        "account_id": order.get("account_id"),
        "code": _normalize_symbol(order.get("symbol")),
        "symbol": _normalize_symbol(order.get("symbol")),
        "event_type": event_type,
        "event_category": event_category,
        "object_kind": object_kind,
        "event_status": event_status,
        "status": event_status,
        "occurred_at": occurred_at,
        "order_ticket": order_ticket,
        "fill_event": fill_event,
        "brokerage_event": brokerage_event,
        "state_transition": {
            "from_status": from_status,
            "to_status": to_status or event_status,
        },
        "raw_payload": raw_payload,
    }

def build_live_order_events(
    order: dict[str, Any],
    *,
    fills: list[dict[str, Any]] | None = None,
    receipt: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized_order = dict(order or {})
    order_status = _safe_text(normalized_order.get("status")) or "unknown"
    events: list[dict[str, Any]] = []

    submitted_at = _safe_text(normalized_order.get("submitted_at"), normalized_order.get("created_at"))
    if submitted_at:
        events.append(
            _build_live_order_event(
                order=normalized_order,
                event_type="submitted",
                event_status="submitted",
                occurred_at=submitted_at,
                raw_payload={"source": normalized_order.get("provider"), "order": normalized_order},
                from_status=None,
                to_status="submitted",
            )
        )

    if receipt:
        receipt_status = _safe_text(receipt.get("status")) or order_status
        events.append(
            _build_live_order_event(
                order=normalized_order,
                event_type="brokerage_rejected" if receipt_status in {"rejected", "error"} else "brokerage_ack",
                event_status="rejected" if receipt_status in {"rejected", "error"} else "accepted",
                occurred_at=_safe_text(receipt.get("occurred_at"), receipt.get("acknowledged_at"), submitted_at),
                raw_payload=dict(receipt.get("raw") or receipt),
                brokerage_event=receipt,
                from_status="submitted",
                to_status="rejected" if receipt_status in {"rejected", "error"} else "accepted",
            )
        )

    fill_items = sorted(
        [dict(item or {}) for item in list(fills or []) if isinstance(item, dict)],
        key=lambda item: _safe_text(item.get("occurred_at")) or "",
    )
    if fill_items:
        total_fills = len(fill_items)
        for index, fill in enumerate(fill_items):
            fill_status = "filled" if order_status == "filled" and index == total_fills - 1 else "partially_filled"
            if order_status == "filled" and total_fills == 1:
                fill_status = "filled"
            events.append(
                _build_live_order_event(
                    order=normalized_order,
                    event_type=fill_status,
                    event_status=fill_status,
                    occurred_at=_safe_text(fill.get("occurred_at"), normalized_order.get("filled_at")),
                    raw_payload=dict(fill.get("raw") or fill),
                    fill_event=fill,
                    from_status="accepted" if receipt else "submitted",
                    to_status=fill_status,
                )
            )
    elif order_status == "partially_filled":
        partial_fill = _normalize_broker_fill(
            str(normalized_order.get("provider") or "live_broker"),
            {
                "order_id": normalized_order.get("order_id"),
                "symbol": normalized_order.get("symbol"),
                "price": normalized_order.get("filled_avg_price"),
                "qty": normalized_order.get("filled_qty"),
                "filled_at": normalized_order.get("filled_at") or normalized_order.get("updated_at"),
            },
            account_id=_safe_text(normalized_order.get("account_id")),
            order_id=_safe_text(normalized_order.get("order_id")),
            symbol=_safe_text(normalized_order.get("symbol")),
            source=f"{normalized_order.get('provider')}.synthetic_fill",
        )
        events.append(
            _build_live_order_event(
                order=normalized_order,
                event_type="partially_filled",
                event_status="partially_filled",
                occurred_at=_safe_text(partial_fill.get("occurred_at"), normalized_order.get("updated_at")),
                raw_payload=dict(partial_fill.get("raw") or partial_fill),
                fill_event=partial_fill,
                from_status="accepted" if receipt else "submitted",
                to_status="partially_filled",
            )
        )

    if order_status == "cancelled":
        events.append(
            _build_live_order_event(
                order=normalized_order,
                event_type="cancelled",
                event_status="cancelled",
                occurred_at=_safe_text(normalized_order.get("updated_at"), normalized_order.get("created_at")),
                raw_payload={"source": normalized_order.get("provider"), "order": normalized_order},
                from_status="accepted" if receipt else "submitted",
                to_status="cancelled",
            )
        )
    elif order_status == "rejected" and not receipt:
        synthetic_receipt = _build_broker_receipt(
            str(normalized_order.get("provider") or "live_broker"),
            normalized_order,
            account_id=_safe_text(normalized_order.get("account_id")),
            source=f"{normalized_order.get('provider')}.synthetic_receipt",
        )
        events.append(
            _build_live_order_event(
                order=normalized_order,
                event_type="brokerage_rejected",
                event_status="rejected",
                occurred_at=_safe_text(synthetic_receipt.get("occurred_at"), normalized_order.get("updated_at")),
                raw_payload=dict(synthetic_receipt.get("raw") or synthetic_receipt),
                brokerage_event=synthetic_receipt,
                from_status="submitted",
                to_status="rejected",
            )
        )
    elif order_status == "filled" and not fill_items and _safe_float(normalized_order.get("filled_qty")):
        synthetic_fill = _normalize_broker_fill(
            str(normalized_order.get("provider") or "live_broker"),
            {
                "order_id": normalized_order.get("order_id"),
                "symbol": normalized_order.get("symbol"),
                "price": normalized_order.get("filled_avg_price"),
                "qty": normalized_order.get("filled_qty"),
                "filled_at": normalized_order.get("filled_at") or normalized_order.get("updated_at"),
            },
            account_id=_safe_text(normalized_order.get("account_id")),
            order_id=_safe_text(normalized_order.get("order_id")),
            symbol=_safe_text(normalized_order.get("symbol")),
            source=f"{normalized_order.get('provider')}.synthetic_fill",
        )
        events.append(
            _build_live_order_event(
                order=normalized_order,
                event_type="filled",
                event_status="filled",
                occurred_at=_safe_text(synthetic_fill.get("occurred_at"), normalized_order.get("filled_at")),
                raw_payload=dict(synthetic_fill.get("raw") or synthetic_fill),
                fill_event=synthetic_fill,
                from_status="accepted" if receipt else "submitted",
                to_status="filled",
            )
        )

    events.sort(key=lambda item: (_safe_text(item.get("occurred_at")) or "", _safe_text(item.get("event_type")) or ""))
    return events

def summarize_live_order_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_object_kind: dict[str, int] = {}
    for event in list(events or []):
        event_type = _safe_text(event.get("event_type"))
        event_category = _safe_text(event.get("event_category"))
        event_status = _safe_text(event.get("event_status"))
        object_kind = _safe_text(event.get("object_kind"))
        if event_type:
            by_type[event_type] = by_type.get(event_type, 0) + 1
        if event_category:
            by_category[event_category] = by_category.get(event_category, 0) + 1
        if event_status:
            by_status[event_status] = by_status.get(event_status, 0) + 1
        if object_kind:
            by_object_kind[object_kind] = by_object_kind.get(object_kind, 0) + 1
    return {
        "schema_version": "v1",
        "by_type": by_type,
        "by_category": by_category,
        "by_status": by_status,
        "by_object_kind": by_object_kind,
    }

def build_live_order_event_collection(events: list[dict[str, Any]], *, order_id: str | None = None, account_id: str | None = None) -> dict[str, Any]:
    normalized_events = [dict(item or {}) for item in list(events or []) if isinstance(item, dict)]
    latest_ticket = None
    if normalized_events:
        latest_ticket = next(
            (dict(item.get("order_ticket") or {}) for item in reversed(normalized_events) if isinstance(item.get("order_ticket"), dict)),
            None,
        )
    fill_events = [dict(item.get("fill_event") or {}) for item in normalized_events if isinstance(item.get("fill_event"), dict)]
    brokerage_events = [dict(item.get("brokerage_event") or {}) for item in normalized_events if isinstance(item.get("brokerage_event"), dict)]
    statuses = [_safe_text(item.get("event_status")) for item in normalized_events if _safe_text(item.get("event_status"))]
    transitions = [
        {
            "event_type": item.get("event_type"),
            "from_status": _dig(item, "state_transition", "from_status"),
            "to_status": _dig(item, "state_transition", "to_status"),
            "occurred_at": item.get("occurred_at"),
        }
        for item in normalized_events
    ]
    current_status = statuses[-1] if statuses else _safe_text(_dig(latest_ticket or {}, "status")) or "unknown"
    return {
        "order_id": order_id or _safe_text(_dig(latest_ticket or {}, "order_id")) or None,
        "account_id": account_id or _safe_text(_dig(latest_ticket or {}, "account_id")) or None,
        "events": normalized_events,
        "summary": summarize_live_order_events(normalized_events),
        "count": len(normalized_events),
        "order_ticket": latest_ticket,
        "fill_events": fill_events,
        "brokerage_events": brokerage_events,
        "state_machine": {
            "schema_version": "v1",
            "current_status": current_status,
            "state_path": statuses,
            "transition_count": len(transitions),
            "transitions": transitions,
            "terminal": current_status in _TERMINAL_EVENT_STATUSES,
            "valid": True,
        },
    }
