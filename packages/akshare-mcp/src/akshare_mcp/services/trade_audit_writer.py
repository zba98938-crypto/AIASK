"""Shared execution-audit writer for runtime and paper-trading fills."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _string(value: Any) -> str:
    return str(value or "").strip()


def _coerce_ts(value: Any) -> Any:
    if value is None or isinstance(value, (datetime, date)):
        return value
    text = _string(value)
    if not text:
        return None
    for parser in (
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")),
        lambda item: datetime.combine(date.fromisoformat(item[:10]), datetime.min.time()),
    ):
        try:
            return parser(text)
        except Exception:
            continue
    return value


def aggregate_trade_position(existing: Optional[dict], fills: list[dict]) -> dict:
    base = dict(existing or {})
    ordered = sorted(
        [dict(item or {}) for item in list(fills or [])],
        key=lambda item: (
            str(item.get("trade_time") or ""),
            str(item.get("created_at") or ""),
            str(item.get("fill_id") or ""),
        ),
    )
    entry_shares = 0
    exit_shares = 0
    remaining_shares = 0
    entry_amount = 0.0
    exit_amount = 0.0
    entry_commission = 0.0
    exit_commission = 0.0
    entry_trade_id = None
    exit_trade_id = None
    entry_order_id = None
    exit_order_id = None
    opened_at = None
    closed_at = None
    last_trade_time = None
    strategy_id = base.get("strategy_id")
    account_id = base.get("account_id")
    signal_id = base.get("signal_id")
    code = base.get("code")
    direction = base.get("direction") or "long"
    for fill in ordered:
        fill_side = str(fill.get("fill_side") or "").strip().lower()
        quantity = int(fill.get("quantity") or 0)
        if quantity <= 0:
            continue
        amount = float(fill.get("amount") or 0.0)
        commission = float(fill.get("commission") or 0.0)
        trade_time = fill.get("trade_time")
        if strategy_id is None:
            strategy_id = fill.get("strategy_id")
        if account_id is None:
            account_id = fill.get("account_id")
        if signal_id is None:
            signal_id = fill.get("signal_id")
        if code is None:
            code = fill.get("code")
        last_trade_time = trade_time or last_trade_time
        if fill_side == "buy":
            entry_shares += quantity
            remaining_shares += quantity
            entry_amount += amount
            entry_commission += commission
            entry_trade_id = entry_trade_id or fill.get("trade_id")
            entry_order_id = entry_order_id or fill.get("order_id")
            opened_at = opened_at or trade_time
        elif fill_side == "sell":
            exit_shares += quantity
            remaining_shares -= quantity
            exit_amount += amount
            exit_commission += commission
            exit_trade_id = fill.get("trade_id") or exit_trade_id
            exit_order_id = fill.get("order_id") or exit_order_id
            closed_at = trade_time or closed_at
    status = "pending_entry"
    if entry_shares > 0 and exit_shares > entry_shares:
        status = "over_closed"
    elif entry_shares > 0 and remaining_shares > 0:
        status = "open"
    elif entry_shares > 0 and exit_shares > 0 and remaining_shares <= 0:
        status = "closed"
    elif entry_shares <= 0 and exit_shares > 0:
        status = "orphaned_exit"
    audit_eligible = status == "closed" and entry_shares > 0 and exit_shares > 0
    entry_basis = entry_amount + entry_commission
    exit_proceeds = exit_amount - exit_commission
    realized_pnl = (exit_proceeds - entry_basis) if audit_eligible else None
    realized_return = (realized_pnl / entry_basis) if audit_eligible and entry_basis > 0 else None
    gross_pnl = (exit_amount - entry_amount) if audit_eligible else None
    gross_return = (gross_pnl / entry_amount) if audit_eligible and entry_amount > 0 else None
    execution_conversion_efficiency = (
        min(exit_shares, entry_shares) / entry_shares if entry_shares > 0 else 0.0
    )
    pnl_conversion_efficiency = realized_return if realized_return is not None else None
    trade_expectancy = realized_return if realized_return is not None else None
    entry_ts = opened_at
    exit_ts = closed_at if audit_eligible else None
    entry_avg_price = (entry_amount / entry_shares) if entry_shares > 0 else None
    exit_avg_price = (exit_amount / exit_shares) if exit_shares > 0 else None
    hold_days = None
    if isinstance(entry_ts, datetime) and isinstance(exit_ts, datetime):
        hold_days = (exit_ts - entry_ts).total_seconds() / 86400.0
    elif entry_ts and exit_ts:
        try:
            parsed_entry = _coerce_ts(entry_ts)
            parsed_exit = _coerce_ts(exit_ts)
            if isinstance(parsed_entry, datetime) and isinstance(parsed_exit, datetime):
                hold_days = (parsed_exit - parsed_entry).total_seconds() / 86400.0
        except Exception:
            hold_days = None
    return {
        **base,
        "position_id": base.get("position_id"),
        "strategy_id": strategy_id,
        "account_id": account_id,
        "signal_id": signal_id,
        "code": code,
        "direction": direction,
        "status": status,
        "entry_order_id": entry_order_id,
        "exit_order_id": exit_order_id,
        "entry_trade_id": entry_trade_id,
        "exit_trade_id": exit_trade_id,
        "entry_shares": entry_shares,
        "exit_shares": exit_shares,
        "remaining_shares": max(remaining_shares, 0),
        "entry_amount": round(entry_amount, 4),
        "exit_amount": round(exit_amount, 4),
        "entry_commission": round(entry_commission, 4),
        "exit_commission": round(exit_commission, 4),
        "realized_pnl": round(realized_pnl, 4) if realized_pnl is not None else None,
        "realized_return": round(realized_return, 6) if realized_return is not None else None,
        "pnl_conversion_efficiency": round(pnl_conversion_efficiency, 6)
        if pnl_conversion_efficiency is not None
        else None,
        "execution_conversion_efficiency": round(execution_conversion_efficiency, 6),
        "trade_expectancy": round(trade_expectancy, 6) if trade_expectancy is not None else None,
        "audit_eligible": audit_eligible,
        "opened_at": opened_at,
        "closed_at": closed_at if audit_eligible else None,
        "last_trade_time": last_trade_time,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_avg_price": round(entry_avg_price, 6) if entry_avg_price is not None else None,
        "exit_avg_price": round(exit_avg_price, 6) if exit_avg_price is not None else None,
        "gross_qty": max(entry_shares, exit_shares),
        "gross_return": round(gross_return, 6) if gross_return is not None else None,
        "net_return": round(realized_return, 6) if realized_return is not None else None,
        "gross_pnl": round(gross_pnl, 4) if gross_pnl is not None else None,
        "net_pnl": round(realized_pnl, 4) if realized_pnl is not None else None,
        "hold_days": round(hold_days, 4) if hold_days is not None else None,
        "exit_reason": (
            "filled_round_trip"
            if audit_eligible
            else "position_open"
            if status == "open"
            else base.get("exit_reason")
        ),
        "price_path_audit_status": (
            "pending_refresh"
            if audit_eligible
            else "open_position"
            if status == "open"
            else base.get("price_path_audit_status") or "unknown"
        ),
    }


def build_trade_fill_payload(
    order: Optional[dict],
    trade: Optional[dict],
    *,
    source: str,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    order_payload = dict(order or {})
    trade_payload = dict(trade or {})
    return {
        "fill_id": f"fill_{trade_payload.get('id')}" if trade_payload.get("id") else None,
        "position_id": _string(trade_payload.get("position_id") or order_payload.get("position_id")) or None,
        "trade_id": trade_payload.get("id"),
        "order_id": _string(order_payload.get("id") or trade_payload.get("source_order_id")) or None,
        "signal_id": trade_payload.get("signal_id") or order_payload.get("signal_id"),
        "strategy_id": trade_payload.get("strategy_id") or order_payload.get("strategy_id"),
        "account_id": trade_payload.get("account_id") or order_payload.get("account_id"),
        "code": trade_payload.get("stock_code") or order_payload.get("code"),
        "fill_side": trade_payload.get("trade_type") or order_payload.get("direction"),
        "quantity": _safe_int(trade_payload.get("quantity") or order_payload.get("shares"), 0),
        "price": _safe_float(trade_payload.get("price") or order_payload.get("price"), 0.0),
        "amount": _safe_float(trade_payload.get("amount"), 0.0),
        "commission": _safe_float(
            trade_payload.get("commission") or order_payload.get("commission"),
            0.0,
        ),
        "trade_time": trade_payload.get("trade_time"),
        "payload": {"source": source, **dict(payload or {})},
    }


def _get_async_db_method(target: Any, name: str):
    method = getattr(target, name, None)
    return method if callable(method) else None


async def _upsert_trade_position_snapshot(conn: Any, payload: dict) -> None:
    await conn.execute(
        """
        INSERT INTO strategy_trade_positions
            (position_id, strategy_id, account_id, signal_id, code, direction, status,
             entry_order_id, exit_order_id, entry_trade_id, exit_trade_id,
             entry_shares, exit_shares, remaining_shares,
             entry_amount, exit_amount, entry_commission, exit_commission,
             realized_pnl, realized_return, pnl_conversion_efficiency,
             execution_conversion_efficiency, trade_expectancy, audit_eligible,
             opened_at, closed_at, last_trade_time,
             entry_ts, exit_ts, entry_avg_price, exit_avg_price, gross_qty,
             gross_return, net_return, gross_pnl, net_pnl, hold_days, exit_reason,
             mfe, mae, price_path_audit_status, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7,
                $8, $9, $10, $11,
                $12, $13, $14,
                $15, $16, $17, $18,
                $19, $20, $21,
                $22, $23, $24,
                $25, $26, $27,
                $28, $29, $30, $31, $32,
                $33, $34, $35, $36, $37, $38,
                $39, $40, $41, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (position_id) DO UPDATE SET
            strategy_id = COALESCE(EXCLUDED.strategy_id, strategy_trade_positions.strategy_id),
            account_id = COALESCE(EXCLUDED.account_id, strategy_trade_positions.account_id),
            signal_id = COALESCE(EXCLUDED.signal_id, strategy_trade_positions.signal_id),
            code = COALESCE(EXCLUDED.code, strategy_trade_positions.code),
            direction = COALESCE(EXCLUDED.direction, strategy_trade_positions.direction),
            status = EXCLUDED.status,
            entry_order_id = EXCLUDED.entry_order_id,
            exit_order_id = EXCLUDED.exit_order_id,
            entry_trade_id = EXCLUDED.entry_trade_id,
            exit_trade_id = EXCLUDED.exit_trade_id,
            entry_shares = EXCLUDED.entry_shares,
            exit_shares = EXCLUDED.exit_shares,
            remaining_shares = EXCLUDED.remaining_shares,
            entry_amount = EXCLUDED.entry_amount,
            exit_amount = EXCLUDED.exit_amount,
            entry_commission = EXCLUDED.entry_commission,
            exit_commission = EXCLUDED.exit_commission,
            realized_pnl = EXCLUDED.realized_pnl,
            realized_return = EXCLUDED.realized_return,
            pnl_conversion_efficiency = EXCLUDED.pnl_conversion_efficiency,
            execution_conversion_efficiency = EXCLUDED.execution_conversion_efficiency,
            trade_expectancy = EXCLUDED.trade_expectancy,
            audit_eligible = EXCLUDED.audit_eligible,
            opened_at = EXCLUDED.opened_at,
            closed_at = EXCLUDED.closed_at,
            last_trade_time = EXCLUDED.last_trade_time,
            entry_ts = EXCLUDED.entry_ts,
            exit_ts = EXCLUDED.exit_ts,
            entry_avg_price = EXCLUDED.entry_avg_price,
            exit_avg_price = EXCLUDED.exit_avg_price,
            gross_qty = EXCLUDED.gross_qty,
            gross_return = EXCLUDED.gross_return,
            net_return = EXCLUDED.net_return,
            gross_pnl = EXCLUDED.gross_pnl,
            net_pnl = EXCLUDED.net_pnl,
            hold_days = EXCLUDED.hold_days,
            exit_reason = EXCLUDED.exit_reason,
            mfe = EXCLUDED.mfe,
            mae = EXCLUDED.mae,
            price_path_audit_status = EXCLUDED.price_path_audit_status,
            updated_at = CURRENT_TIMESTAMP
        """,
        _string(payload.get("position_id")),
        payload.get("strategy_id"),
        payload.get("account_id"),
        payload.get("signal_id"),
        payload.get("code"),
        payload.get("direction") or "long",
        payload.get("status") or "pending_entry",
        payload.get("entry_order_id"),
        payload.get("exit_order_id"),
        payload.get("entry_trade_id"),
        payload.get("exit_trade_id"),
        payload.get("entry_shares"),
        payload.get("exit_shares"),
        payload.get("remaining_shares"),
        payload.get("entry_amount"),
        payload.get("exit_amount"),
        payload.get("entry_commission"),
        payload.get("exit_commission"),
        payload.get("realized_pnl"),
        payload.get("realized_return"),
        payload.get("pnl_conversion_efficiency"),
        payload.get("execution_conversion_efficiency"),
        payload.get("trade_expectancy"),
        payload.get("audit_eligible"),
        _coerce_ts(payload.get("opened_at")),
        _coerce_ts(payload.get("closed_at")),
        _coerce_ts(payload.get("last_trade_time")),
        _coerce_ts(payload.get("entry_ts")),
        _coerce_ts(payload.get("exit_ts")),
        payload.get("entry_avg_price"),
        payload.get("exit_avg_price"),
        payload.get("gross_qty"),
        payload.get("gross_return"),
        payload.get("net_return"),
        payload.get("gross_pnl"),
        payload.get("net_pnl"),
        payload.get("hold_days"),
        payload.get("exit_reason"),
        payload.get("mfe"),
        payload.get("mae"),
        payload.get("price_path_audit_status"),
    )


async def _refresh_trade_position_from_conn(conn: Any, position_id: str) -> Optional[dict]:
    fills = await conn.fetch(
        """
        SELECT * FROM strategy_trade_position_fills
        WHERE position_id=$1
        ORDER BY trade_time ASC, created_at ASC, fill_id ASC
        """,
        _string(position_id),
    )
    if not fills:
        return None
    existing = await conn.fetchrow(
        "SELECT * FROM strategy_trade_positions WHERE position_id=$1",
        _string(position_id),
    )
    payload = aggregate_trade_position(dict(existing or {}), [dict(item) for item in fills])
    payload["position_id"] = _string(position_id)
    await _upsert_trade_position_snapshot(conn, payload)
    return payload


async def record_trade_position_fill(target: Any, fill: dict) -> Optional[dict]:
    payload = dict(fill or {})
    position_id = _string(payload.get("position_id"))
    if not position_id:
        return None
    if not payload.get("fill_id"):
        trade_id = _string(payload.get("trade_id"))
        payload["fill_id"] = f"fill_{trade_id}" if trade_id else None
    payload["payload"] = dict(payload.get("payload") or {})
    save_method = _get_async_db_method(target, "save_strategy_trade_position_fill")
    if save_method is not None:
        await save_method(payload)
        refresh_method = _get_async_db_method(target, "refresh_strategy_trade_position")
        if refresh_method is not None:
            return await refresh_method(position_id)
        return None
    if not callable(getattr(target, "execute", None)):
        return None
    await target.execute(
        """
        INSERT INTO strategy_trade_position_fills
            (fill_id, position_id, trade_id, order_id, signal_id, strategy_id, account_id, code,
             fill_side, quantity, price, amount, commission, trade_time, payload, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                $9, $10, $11, $12, $13, $14, $15, CURRENT_TIMESTAMP)
        ON CONFLICT (trade_id) DO UPDATE SET
            position_id = COALESCE(EXCLUDED.position_id, strategy_trade_position_fills.position_id),
            order_id = COALESCE(EXCLUDED.order_id, strategy_trade_position_fills.order_id),
            signal_id = COALESCE(EXCLUDED.signal_id, strategy_trade_position_fills.signal_id),
            strategy_id = COALESCE(EXCLUDED.strategy_id, strategy_trade_position_fills.strategy_id),
            account_id = COALESCE(EXCLUDED.account_id, strategy_trade_position_fills.account_id),
            code = COALESCE(EXCLUDED.code, strategy_trade_position_fills.code),
            fill_side = COALESCE(EXCLUDED.fill_side, strategy_trade_position_fills.fill_side),
            quantity = COALESCE(EXCLUDED.quantity, strategy_trade_position_fills.quantity),
            price = COALESCE(EXCLUDED.price, strategy_trade_position_fills.price),
            amount = COALESCE(EXCLUDED.amount, strategy_trade_position_fills.amount),
            commission = COALESCE(EXCLUDED.commission, strategy_trade_position_fills.commission),
            trade_time = COALESCE(EXCLUDED.trade_time, strategy_trade_position_fills.trade_time),
            payload = COALESCE(EXCLUDED.payload, strategy_trade_position_fills.payload)
        """,
        _string(payload.get("fill_id")),
        position_id,
        payload.get("trade_id"),
        payload.get("order_id"),
        payload.get("signal_id"),
        payload.get("strategy_id"),
        payload.get("account_id"),
        payload.get("code"),
        payload.get("fill_side"),
        _safe_int(payload.get("quantity"), 0),
        _safe_float(payload.get("price"), 0.0),
        _safe_float(payload.get("amount"), 0.0),
        _safe_float(payload.get("commission"), 0.0),
        _coerce_ts(payload.get("trade_time")),
        json.dumps(payload.get("payload") or {}, ensure_ascii=False, default=str),
    )
    return await _refresh_trade_position_from_conn(target, position_id)


async def record_trade_fill_from_order_and_trade(
    target: Any,
    order: Optional[dict],
    trade: Optional[dict],
    *,
    source: str,
    payload: Optional[dict[str, Any]] = None,
) -> Optional[dict]:
    return await record_trade_position_fill(
        target,
        build_trade_fill_payload(order, trade, source=source, payload=payload),
    )


__all__ = [
    "aggregate_trade_position",
    "build_trade_fill_payload",
    "record_trade_fill_from_order_and_trade",
    "record_trade_position_fill",
]
