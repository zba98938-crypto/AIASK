"""模拟交易管理器 — P3 订单生命周期 + 佣金 + 风控"""

from typing import Any
import json
import uuid
import logging
from datetime import datetime, timezone
from ...storage import get_db
from ...utils import ok, fail
from ...services.cost_model import build_cost_model
from ...services.trade_audit_writer import (
    aggregate_trade_position as _aggregate_trade_position_shared,
    record_trade_position_fill as _record_trade_position_fill_shared,
)
from ..manager_protocol import normalize_manager_payload

logger = logging.getLogger(__name__)

# 默认风控规则
DEFAULT_RISK_RULES = {
    "max_position_pct": 30.0,   # 单股最大仓位占比 %
    "max_drawdown_pct": 20.0,   # 最大回撤阈值 %
    "stop_loss_pct": 10.0,      # 个股止损线 %
}

def _normalize_kwargs(kwargs: dict) -> dict:
    """统一解析 kwargs 参数（兼容 JSON 字符串和 dict）"""
    params = kwargs.get("params")
    if isinstance(params, dict):
        kwargs = {**kwargs, **params}
    raw = kwargs.get("kwargs")
    if isinstance(raw, dict):
        kwargs = {**kwargs, **raw}
    elif isinstance(raw, str):
        try:
            extra = json.loads(raw or "{}")
            if isinstance(extra, dict):
                kwargs = {**kwargs, **extra}
        except Exception:
            pass
    if "code" not in kwargs:
        kwargs["code"] = kwargs.get("stock_code") or kwargs.get("symbol")
    return kwargs

def _safe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None

def _db_supports_acquire(db) -> bool:
    acquire = getattr(db, "acquire", None)
    return callable(acquire)

def _safe_int(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None

def _canonical_stock_code(code: str | None) -> str:
    text = str(code or '').strip()
    digits = ''.join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else text

def _event_timestamp(value=None) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value or "").strip()
    if text:
        return text
    return datetime.now(timezone.utc).isoformat()

def _coerce_event_payload(payload) -> dict:
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload or "{}")
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}

def _event_type_profile(event_type: str) -> dict:
    profile_map = {
        "created": {"event_category": "order_lifecycle", "status": "pending"},
        "filled": {"event_category": "execution", "status": "filled"},
        "cancelled": {"event_category": "order_lifecycle", "status": "cancelled"},
        "risk_rejected": {"event_category": "risk", "status": "rejected"},
    }
    return profile_map.get(str(event_type or "").strip(), {"event_category": "misc", "status": "unknown"})

def _build_order_event_object(
    *,
    order_id: str,
    event_type: str,
    account_id: str | None = None,
    code: str | None = None,
    payload: dict | None = None,
    created_at=None,
) -> dict:
    raw_payload = _coerce_event_payload(payload)
    if raw_payload.get("schema_version") == "v1" and raw_payload.get("event_type") == event_type:
        event_object = dict(raw_payload)
    else:
        profile = _event_type_profile(event_type)
        normalized_code = _canonical_stock_code(code or raw_payload.get("code"))
        order_block = {
            "order_type": raw_payload.get("order_type"),
            "direction": raw_payload.get("direction"),
            "shares": _safe_int(raw_payload.get("shares") or raw_payload.get("quantity")),
            "price": _safe_float(raw_payload.get("price")),
            "stop_price": _safe_float(raw_payload.get("stop_price")),
            "amount": _safe_float(raw_payload.get("amount")),
            "commission": _safe_float(raw_payload.get("commission")),
        }
        event_object = {
            "schema_version": "v1",
            "order_id": str(order_id),
            "account_id": account_id,
            "code": normalized_code or (code or raw_payload.get("code")),
            "event_type": str(event_type),
            "event_category": profile["event_category"],
            "status": profile["status"],
            "occurred_at": _event_timestamp(raw_payload.get("occurred_at") or created_at),
            "order": order_block,
            "risk": {
                "reason": str(raw_payload.get("reason") or "").strip() or None,
            },
            "transition": {
                "from_status": raw_payload.get("from_status"),
                "to_status": profile["status"],
            },
            "raw_payload": raw_payload,
        }

    event_object.setdefault("schema_version", "v1")
    event_object.setdefault("order_id", str(order_id))
    event_object.setdefault("account_id", account_id)
    event_object.setdefault("code", _canonical_stock_code(code or event_object.get("code")))
    event_object.setdefault("event_type", str(event_type))
    profile = _event_type_profile(str(event_object.get("event_type") or event_type))
    event_object.setdefault("event_category", profile["event_category"])
    event_object.setdefault("status", profile["status"])
    event_object["occurred_at"] = _event_timestamp(event_object.get("occurred_at") or created_at)
    event_object.setdefault("order", {})
    event_object.setdefault("risk", {"reason": None})
    event_object.setdefault("transition", {"from_status": None, "to_status": event_object.get("status")})
    event_object.setdefault("raw_payload", raw_payload)
    return event_object

def _serialize_order_event_row(row) -> dict:
    item = dict(row) if isinstance(row, dict) else dict(row or {})
    payload_raw = _coerce_event_payload(item.get("payload"))
    event_object = _build_order_event_object(
        order_id=str(item.get("order_id") or ""),
        event_type=str(item.get("event_type") or ""),
        account_id=item.get("account_id"),
        code=item.get("code"),
        payload=payload_raw,
        created_at=item.get("created_at"),
    )
    return {
        **item,
        "payload_raw": payload_raw,
        "payload": event_object,
        "event_object": event_object,
        "event_schema_version": event_object.get("schema_version"),
        "event_category": event_object.get("event_category"),
        "event_status": event_object.get("status"),
        "occurred_at": event_object.get("occurred_at"),
    }

def _summarize_order_events(events: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("event_type") or "")
        event_category = str(event.get("event_category") or "")
        event_status = str(event.get("event_status") or "")
        if event_type:
            by_type[event_type] = by_type.get(event_type, 0) + 1
        if event_category:
            by_category[event_category] = by_category.get(event_category, 0) + 1
        if event_status:
            by_status[event_status] = by_status.get(event_status, 0) + 1
    return {
        "schema_version": "v1",
        "by_type": by_type,
        "by_category": by_category,
        "by_status": by_status,
    }

def _normalize_risk_pct(value, default: float) -> float:
    numeric = _safe_float(value)
    if numeric is None:
        return float(default)
    if 0 < numeric <= 1:
        numeric *= 100.0
    return float(max(numeric, 0.0))

def _price_limit_pct(code: str, stock_name: str | None = None) -> float:
    normalized = _canonical_stock_code(code)
    name = str(stock_name or '').upper()
    if 'ST' in name:
        return 0.05
    if normalized.startswith('688'):
        return 0.20
    if normalized.startswith('300'):
        return 0.20
    if normalized.startswith(('8', '4')):
        return 0.30
    return 0.10

async def _get_quote_snapshot(code: str, db=None) -> dict:
    normalized_code = _canonical_stock_code(code)
    if db is not None:
        try:
            getter = getattr(db, "get_latest_quote", None)
            quote = await getter(normalized_code) if callable(getter) else None
            if isinstance(quote, dict) and quote:
                quote_code = _canonical_stock_code(quote.get("code") or quote.get("stock_code") or normalized_code)
                if quote_code != normalized_code:
                    return {}
                return quote
        except Exception as e:
            logger.debug('[PaperTrading] DB 行情快照读取失败: %s', e)
    return {}

async def _get_previous_close(code: str, db, quote: dict | None = None) -> float | None:
    quote = quote or {}
    for key in ('preClose', 'pre_close', 'prev_close'):
        prev_close = _safe_float(quote.get(key))
        if prev_close is not None and prev_close > 0:
            return prev_close
    try:
        klines = await db.get_klines(code, limit=2)
        if klines:
            if len(klines) >= 2:
                prev_close = _safe_float(klines[-2].get('close'))
            else:
                prev_close = _safe_float(klines[-1].get('close'))
            if prev_close is not None and prev_close > 0:
                return prev_close
    except Exception:
        pass
    return None

async def _validate_price_limit(code: str, price: float | None, db) -> str | None:
    if price is None:
        return None
    quote = await _get_quote_snapshot(code, db)
    prev_close = await _get_previous_close(code, db, quote)
    if prev_close is None or prev_close <= 0:
        return None
    limit_pct = _price_limit_pct(code, quote.get('name'))
    lower = round(prev_close * (1 - limit_pct), 2)
    upper = round(prev_close * (1 + limit_pct), 2)
    normalized_price = round(float(price), 2)
    if normalized_price < lower - 0.01 or normalized_price > upper + 0.01:
        return f'涨跌停限制：价格 {normalized_price:.2f} 超出允许范围 [{lower:.2f}, {upper:.2f}]'
    return None

async def _get_sellable_quantity(conn, account_id: str, code: str) -> int:
    sellable = await conn.fetchval(
        """SELECT COALESCE(SUM(
               CASE WHEN trade_type='buy' AND DATE(trade_time) < CURRENT_DATE THEN quantity
                    WHEN trade_type='sell' THEN -quantity
                    ELSE 0 END
           ), 0)
           FROM paper_trades
           WHERE account_id=$1 AND stock_code=$2""",
        account_id, code
    )
    try:
        return max(int(sellable or 0), 0)
    except Exception:
        return 0

async def _validate_sell_request(conn, account_id: str, code: str, shares: int) -> str | None:
    existing_pos = await conn.fetchrow(
        "SELECT * FROM paper_positions WHERE account_id = $1 AND stock_code = $2",
        account_id, code
    )
    position_qty = int(existing_pos.get('quantity') or 0) if existing_pos else 0
    if position_qty < shares:
        return '持仓不足，无法卖出'
    sellable = await _get_sellable_quantity(conn, account_id, code)
    if shares > sellable:
        return f'T+1限制：可卖 {sellable} 股，请求卖出 {shares} 股'
    return None

async def _refresh_account_prices(db, account_id: str) -> list[dict]:
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM paper_positions WHERE account_id=$1", account_id)
        account = await conn.fetchrow("SELECT * FROM paper_accounts WHERE id = $1", account_id)

    positions = [dict(row) for row in rows]
    if not positions:
        if account:
            async with db.acquire() as conn:
                await _sync_account_from_ledger(conn, account_id)
        return []

    quotes = []
    for row in positions:
        stock_code = row.get('stock_code')
        if not stock_code:
            continue
        quote = await _get_quote_snapshot(stock_code, db)
        if quote:
            quotes.append(quote)

    quote_map = {
        _canonical_stock_code(item.get('code')): item
        for item in quotes
        if isinstance(item, dict) and item.get('code')
    }

    market_value_sum = 0.0
    refreshed_positions: list[dict] = []
    # P3-5.15 fix: 记录 updated_count / unchanged_count(诊断报告 §5.15)
    # 历史问题:update_prices 不区分"成功更新"和"行情失败保留旧值",AI 看不到刷新成功率
    updated_count = 0
    unchanged_count = 0
    async with db.acquire() as conn:
        for row in positions:
            price = None
            quote = quote_map.get(_canonical_stock_code(row.get('stock_code')))
            if quote:
                price = _safe_float(quote.get('price'))

            if price is None or price <= 0:
                row_dict = dict(row)
                row_dict['_refresh_status'] = 'unchanged_no_quote'
                refreshed_positions.append(row_dict)
                market_value_sum += float(row.get('market_value') or 0)
                unchanged_count += 1
                continue

            quantity = int(row.get('quantity') or 0)
            cost_price = float(row.get('cost_price') or 0)
            market_value = price * quantity
            profit_rate = ((price - cost_price) / cost_price) if cost_price else 0.0
            await conn.execute(
                """UPDATE paper_positions
                   SET quantity=$1, current_price=$2, market_value=$3, profit_rate=$4, updated_at=CURRENT_TIMESTAMP
                   WHERE account_id=$5 AND stock_code=$6""",
                quantity, price, market_value, profit_rate, account_id, row.get('stock_code')
            )
            refreshed = {
                **row,
                'current_price': price,
                'market_value': market_value,
                'profit_rate': profit_rate,
                '_refresh_status': 'updated',
            }
            refreshed_positions.append(refreshed)
            market_value_sum += market_value
            updated_count += 1

        await _sync_account_from_ledger(conn, account_id)

    # P3-5.15: 把统计挂到列表 attribute 上,供上层使用
    refreshed_positions_list = list(refreshed_positions)
    try:
        # list 不能加 attribute, 通过 trailing dict sentinel 传出
        # 改用旁路: 设置 _refresh_status 字段后,上层用聚合
        pass
    except Exception:
        pass

    return refreshed_positions_list

async def _ensure_account(user_id: str, db) -> str:
    """确保用户有默认账户，没有则自动创建"""
    async with db.acquire() as conn:
        account = await conn.fetchrow(
            "SELECT id FROM paper_accounts WHERE user_id = $1 AND COALESCE(status, 'active') <> 'archived' ORDER BY created_at LIMIT 1",
            user_id
        )
        if account:
            return account['id']
        account_id = str(uuid.uuid4())[:8]
        await conn.execute(
            """INSERT INTO paper_accounts (id, user_id, name, initial_capital, current_capital, total_value, created_at)
               VALUES ($1, $2, $3, $4, $4, $4, CURRENT_TIMESTAMP)""",
            account_id, user_id, f'默认账户_{user_id}', 100000
        )
        return account_id


async def _sync_account_from_ledger(conn, account_id: str) -> dict:
    """以 paper_trades + paper_positions 为唯一事实源回算账户现金与总资产。"""
    account = await conn.fetchrow("SELECT * FROM paper_accounts WHERE id = $1", account_id)
    if not account:
        raise ValueError("账户不存在")

    initial_capital = float(account.get('initial_capital') or 0.0)
    cash_delta = await conn.fetchval(
        """
        SELECT COALESCE(SUM(
            CASE
                WHEN trade_type = 'buy' THEN -(amount + COALESCE(commission, 0))
                WHEN trade_type = 'sell' THEN (amount - COALESCE(commission, 0))
                ELSE 0
            END
        ), 0)
        FROM paper_trades
        WHERE account_id = $1
        """,
        account_id,
    )
    market_value = await conn.fetchval(
        "SELECT COALESCE(SUM(market_value), 0) FROM paper_positions WHERE account_id = $1",
        account_id,
    )
    current_capital = initial_capital + float(cash_delta or 0.0)
    total_value = current_capital + float(market_value or 0.0)
    await conn.execute(
        "UPDATE paper_accounts SET current_capital=$1, total_value=$2, updated_at=CURRENT_TIMESTAMP WHERE id=$3",
        current_capital,
        total_value,
        account_id,
    )
    return {
        'account_id': account_id,
        'initial_capital': initial_capital,
        'current_capital': current_capital,
        'market_value': float(market_value or 0.0),
        'total_value': total_value,
    }


def _position_signature(row: dict) -> tuple[int, float, float, float]:
    return (
        int(row.get('quantity') or 0),
        round(float(row.get('cost_price') or 0.0), 6),
        round(float(row.get('current_price') or 0.0), 6),
        round(float(row.get('market_value') or 0.0), 6),
    )


def _collect_position_reconcile_reasons(
    existing_rows: list[dict],
    rebuilt_rows: list[dict],
    *,
    compare_market_values: bool,
) -> list[str]:
    reasons: list[str] = []
    existing_map = {
        str(row.get('stock_code') or '').strip(): dict(row)
        for row in existing_rows
        if str(row.get('stock_code') or '').strip()
    }
    rebuilt_map = {
        str(row.get('stock_code') or '').strip(): dict(row)
        for row in rebuilt_rows
        if str(row.get('stock_code') or '').strip()
    }
    existing_codes = set(existing_map.keys())
    rebuilt_codes = set(rebuilt_map.keys())
    if existing_codes != rebuilt_codes:
        reasons.append('positions_symbol_set_mismatch')
    for code in sorted(existing_codes & rebuilt_codes):
        existing = existing_map[code]
        rebuilt = rebuilt_map[code]
        if int(existing.get('quantity') or 0) != int(rebuilt.get('quantity') or 0):
            reasons.append(f'position_quantity_mismatch:{code}')
        if round(float(existing.get('cost_price') or 0.0), 6) != round(float(rebuilt.get('cost_price') or 0.0), 6):
            reasons.append(f'position_cost_mismatch:{code}')
        if compare_market_values and _position_signature(existing) != _position_signature(rebuilt):
            reasons.append(f'position_valuation_mismatch:{code}')
    return reasons


async def _build_reconciled_positions(conn, db, account_id: str, *, refresh_prices: bool) -> tuple[list[dict], list[dict]]:
    existing_rows = [
        dict(row)
        for row in await conn.fetch(
            "SELECT * FROM paper_positions WHERE account_id=$1 ORDER BY stock_code",
            account_id,
        )
    ]
    trades = await conn.fetch(
        "SELECT * FROM paper_trades WHERE account_id=$1 ORDER BY trade_time, created_at, id",
        account_id,
    )
    if not trades:
        return existing_rows, []

    existing_map = {
        str(row.get('stock_code') or '').strip(): dict(row)
        for row in existing_rows
        if str(row.get('stock_code') or '').strip()
    }
    rebuilt: dict[str, dict] = {}
    for trade in trades:
        item = dict(trade)
        code = str(item.get('stock_code') or '').strip()
        if not code:
            continue
        quantity = int(item.get('quantity') or 0)
        if quantity <= 0:
            continue
        trade_type = str(item.get('trade_type') or '').strip().lower()
        price = float(item.get('price') or 0.0)
        current = rebuilt.get(code) or {
            'stock_code': code,
            'stock_name': item.get('stock_name') or existing_map.get(code, {}).get('stock_name') or code,
            'quantity': 0,
            'cost_price': 0.0,
        }

        old_qty = int(current.get('quantity') or 0)
        old_cost = float(current.get('cost_price') or 0.0)
        if trade_type == 'buy':
            new_qty = old_qty + quantity
            new_cost = ((old_cost * old_qty) + (price * quantity)) / new_qty if new_qty > 0 else price
            current['quantity'] = new_qty
            current['cost_price'] = new_cost
            rebuilt[code] = current
        elif trade_type == 'sell':
            new_qty = max(old_qty - quantity, 0)
            if new_qty <= 0:
                rebuilt.pop(code, None)
            else:
                current['quantity'] = new_qty
                rebuilt[code] = current

    rebuilt_rows: list[dict] = []
    for code in sorted(rebuilt.keys()):
        current = dict(rebuilt[code])
        quantity = int(current.get('quantity') or 0)
        if quantity <= 0:
            continue
        cost_price = float(current.get('cost_price') or 0.0)
        existing = existing_map.get(code, {})
        current_price = _safe_float(existing.get('current_price'))
        if refresh_prices:
            fetched_price = await _get_price(code, db)
            if fetched_price is not None and float(fetched_price) > 0:
                current_price = float(fetched_price)
        if current_price is None or current_price <= 0:
            current_price = _safe_float(existing.get('current_price')) or cost_price
        market_value = float(current_price or 0.0) * quantity
        profit_rate = ((float(current_price or 0.0) - cost_price) / cost_price) if cost_price else 0.0
        rebuilt_rows.append({
            'account_id': account_id,
            'stock_code': code,
            'stock_name': current.get('stock_name') or existing.get('stock_name') or code,
            'quantity': quantity,
            'cost_price': cost_price,
            'current_price': float(current_price or 0.0),
            'market_value': market_value,
            'profit_rate': profit_rate,
        })
    return existing_rows, rebuilt_rows


async def _persist_reconciled_positions(conn, account_id: str, rows: list[dict]) -> None:
    await conn.execute("DELETE FROM paper_positions WHERE account_id=$1", account_id)
    for row in rows:
        await conn.execute(
            """INSERT INTO paper_positions
               (account_id, stock_code, stock_name, quantity, cost_price, current_price, market_value, profit_rate, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
               ON CONFLICT (account_id, stock_code) DO UPDATE SET
                   stock_name=EXCLUDED.stock_name,
                   quantity=EXCLUDED.quantity,
                   cost_price=EXCLUDED.cost_price,
                   current_price=EXCLUDED.current_price,
                   market_value=EXCLUDED.market_value,
                   profit_rate=EXCLUDED.profit_rate,
                   updated_at=CURRENT_TIMESTAMP""",
            account_id,
            row.get('stock_code'),
            row.get('stock_name'),
            int(row.get('quantity') or 0),
            float(row.get('cost_price') or 0.0),
            float(row.get('current_price') or 0.0),
            float(row.get('market_value') or 0.0),
            float(row.get('profit_rate') or 0.0),
        )


async def _reconcile_account_state(db, account_id: str, *, refresh_prices: bool = True, force: bool = False) -> dict:
    async with db.acquire() as conn:
        account = await conn.fetchrow("SELECT * FROM paper_accounts WHERE id=$1", account_id)
        if not account:
            raise ValueError("账户不存在")
        account_snapshot = dict(account)
        existing_rows, rebuilt_rows = await _build_reconciled_positions(
            conn,
            db,
            account_id,
            refresh_prices=refresh_prices,
        )
        reasons = _collect_position_reconcile_reasons(
            existing_rows,
            rebuilt_rows,
            compare_market_values=refresh_prices,
        )
        before_cash = float(account_snapshot.get('current_capital') or 0.0)
        before_total_value = float(account_snapshot.get('total_value') or 0.0)
        if force or reasons:
            await _persist_reconciled_positions(conn, account_id, rebuilt_rows)
        ledger_snapshot = await _sync_account_from_ledger(conn, account_id)
        after_cash = float(ledger_snapshot.get('current_capital') or 0.0)
        after_total_value = float(ledger_snapshot.get('total_value') or 0.0)
        if abs(before_cash - after_cash) > 0.01:
            reasons.append('account_cash_mismatch')
        if abs(before_total_value - after_total_value) > 0.01:
            reasons.append('account_total_value_mismatch')

    return {
        'account_id': account_id,
        'drift_detected': bool(reasons),
        'reconciled': bool(force or reasons),
        'refresh_prices': refresh_prices,
        'reasons': list(dict.fromkeys(reasons)),
        'positions_before_count': len(existing_rows),
        'positions_after_count': len(rebuilt_rows),
        'cash_before': before_cash,
        'cash_after': after_cash,
        'total_value_before': before_total_value,
        'total_value_after': after_total_value,
        'positions': rebuilt_rows if (force or reasons) else existing_rows,
    }


def _aggregate_trade_position(existing: dict | None, fills: list[dict]) -> dict:
    return _aggregate_trade_position_shared(existing, fills)


async def _upsert_trade_position_snapshot(conn, payload: dict) -> None:
    try:
        await conn.execute(
            """
            INSERT INTO strategy_trade_positions
                (position_id, strategy_id, account_id, signal_id, code, direction, status,
                 entry_order_id, exit_order_id, entry_trade_id, exit_trade_id,
                 entry_shares, exit_shares, remaining_shares,
                 entry_amount, exit_amount, entry_commission, exit_commission,
                 realized_pnl, realized_return, pnl_conversion_efficiency,
                 execution_conversion_efficiency, trade_expectancy, audit_eligible,
                 opened_at, closed_at, last_trade_time, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7,
                    $8, $9, $10, $11,
                    $12, $13, $14,
                    $15, $16, $17, $18,
                    $19, $20, $21,
                    $22, $23, $24,
                    $25, $26, $27, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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
                updated_at = CURRENT_TIMESTAMP
            """,
            str(payload.get("position_id") or ""),
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
            payload.get("opened_at"),
            payload.get("closed_at"),
            payload.get("last_trade_time"),
        )
    except Exception:
        return


async def _refresh_trade_position_from_fills(conn, position_id: str) -> None:
    if not position_id:
        return
    try:
        fills = await conn.fetch(
            """
            SELECT * FROM strategy_trade_position_fills
            WHERE position_id=$1
            ORDER BY trade_time ASC, created_at ASC, fill_id ASC
            """,
            str(position_id),
        )
        if not fills:
            return
        existing = await conn.fetchrow(
            "SELECT * FROM strategy_trade_positions WHERE position_id=$1",
            str(position_id),
        )
        payload = _aggregate_trade_position(dict(existing or {}), [dict(item) for item in fills])
        payload["position_id"] = str(position_id)
        await _upsert_trade_position_snapshot(conn, payload)
    except Exception:
        return


async def _record_trade_position_fill(
    conn,
    *,
    position_id: str | None,
    trade_id: str | None,
    order_id: str | None,
    signal_id: str | None,
    strategy_id: str | None,
    account_id: str | None,
    code: str | None,
    fill_side: str | None,
    quantity: int,
    price: float,
    amount: float,
    commission: float,
    trade_time,
    source: str,
) -> None:
    try:
        await _record_trade_position_fill_shared(
            conn,
            {
                "fill_id": f"fill_{trade_id}" if trade_id else f"fill_{uuid.uuid4().hex[:10]}",
                "position_id": position_id,
                "trade_id": trade_id,
                "order_id": order_id,
                "signal_id": signal_id,
                "strategy_id": strategy_id,
                "account_id": account_id,
                "code": code,
                "fill_side": fill_side,
                "quantity": int(quantity or 0),
                "price": float(price or 0.0),
                "amount": float(amount or 0.0),
                "commission": float(commission or 0.0),
                "trade_time": trade_time,
                "payload": {"source": source},
            },
        )
    except Exception:
        return

async def _fill_order(conn, account_id: str, code: str, trade_type: str,
                      shares: int, price: float, order_id: str = None,
                      strategy_id: str | None = None, source_order_id: str | None = None,
                      signal_id: str | None = None, position_id: str | None = None):
    """统一成交记账：写 trade → 更新 position → 更新 account。返回 (trade_id, commission)。"""
    amount = price * shares
    cost = build_cost_model({}, notional=amount, default_mode="execution")
    commission = cost["estimated"]["commission"]

    trade_id = order_id or str(uuid.uuid4())[:8]
    linked_position_id = str(position_id or "").strip() or trade_id

    account = await conn.fetchrow("SELECT * FROM paper_accounts WHERE id = $1", account_id)
    if not account:
        raise ValueError("账户不存在")

    if trade_type == 'sell':
        reject = await _validate_sell_request(conn, account_id, code, shares)
        if reject:
            raise ValueError(reject)

    existing_pos = await conn.fetchrow(
        "SELECT * FROM paper_positions WHERE account_id = $1 AND stock_code = $2",
        account_id, code
    )

    await conn.execute(
        """INSERT INTO paper_trades
           (id, account_id, stock_code, stock_name, trade_type, price, quantity, amount, commission,
            strategy_id, source_order_id, signal_id, position_id, trade_time, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        trade_id, account_id, code, code, trade_type, price, shares, amount, commission, strategy_id, source_order_id, signal_id, linked_position_id
    )

    if trade_type == 'buy':
        if existing_pos:
            old_qty = int(existing_pos.get('quantity') or 0)
            old_cost = float(existing_pos.get('cost_price') or 0)
            new_qty = old_qty + shares
            new_cost = ((old_cost * old_qty) + amount) / new_qty if new_qty > 0 else price
            market_value = price * new_qty
            profit_rate = ((price - new_cost) / new_cost) if new_cost else 0.0
            await conn.execute(
                """UPDATE paper_positions
                   SET quantity=$1, cost_price=$2, current_price=$3,
                       market_value=$4, profit_rate=$5, updated_at=CURRENT_TIMESTAMP
                   WHERE account_id=$6 AND stock_code=$7""",
                new_qty, new_cost, price, market_value, profit_rate, account_id, code
            )
        else:
            await conn.execute(
                """INSERT INTO paper_positions
                   (account_id, stock_code, stock_name, quantity, cost_price, current_price, market_value, profit_rate, created_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                account_id, code, code, shares, price, price, amount, 0.0
            )
        capital_delta = -(amount + commission)
    else:
        old_qty = int(existing_pos.get('quantity') or 0)
        old_cost = float(existing_pos.get('cost_price') or 0)
        new_qty = old_qty - shares
        if new_qty > 0:
            market_value = price * new_qty
            profit_rate = ((price - old_cost) / old_cost) if old_cost else 0.0
            await conn.execute(
                """UPDATE paper_positions
                   SET quantity=$1, current_price=$2, market_value=$3, profit_rate=$4, updated_at=CURRENT_TIMESTAMP
                   WHERE account_id=$5 AND stock_code=$6""",
                new_qty, price, market_value, profit_rate, account_id, code
            )
        else:
            await conn.execute(
                "DELETE FROM paper_positions WHERE account_id=$1 AND stock_code=$2",
                account_id, code
            )
        capital_delta = amount - commission

    await _sync_account_from_ledger(conn, account_id)
    await _record_trade_position_fill(
        conn,
        position_id=linked_position_id,
        trade_id=str(trade_id),
        order_id=str(source_order_id or order_id or ""),
        signal_id=signal_id,
        strategy_id=strategy_id,
        account_id=account_id,
        code=code,
        fill_side=trade_type,
        quantity=shares,
        price=price,
        amount=amount,
        commission=commission,
        trade_time=datetime.now(timezone.utc),
        source="paper_trading_fill",
    )
    return trade_id, commission

async def _ensure_positions_consistency(db, account_id: str) -> list[dict]:
    """基于 paper_trades 账本核对 paper_positions；发现漂移则自动校准。"""
    result = await _reconcile_account_state(db, account_id, refresh_prices=False, force=False)
    return list(result.get('positions') or [])

async def _check_risk_before_buy(conn, account_id: str, code: str, amount: float) -> str | None:
    """买入前风控检查，返回拒绝原因或 None（通过）。"""
    account = await conn.fetchrow("SELECT * FROM paper_accounts WHERE id = $1", account_id)
    if not account:
        return "账户不存在"
    rules = account.get('risk_rules') or {}
    if isinstance(rules, str):
        try:
            rules = json.loads(rules)
        except Exception:
            rules = {}
    max_pos_pct = float(rules.get('max_position_pct', DEFAULT_RISK_RULES['max_position_pct']))
    max_dd_pct = float(rules.get('max_drawdown_pct', DEFAULT_RISK_RULES['max_drawdown_pct']))

    total_value = float(account.get('total_value') or 0)
    if total_value > 0 and (amount / total_value * 100) > max_pos_pct:
        return f"单股仓位超限: 买入金额占总资产 {amount/total_value*100:.1f}% > {max_pos_pct}%"

    initial = float(account.get('initial_capital') or 0)
    if initial > 0:
        drawdown = (initial - total_value) / initial * 100
        if drawdown > max_dd_pct:
            return f"账户回撤超限: {drawdown:.1f}% > {max_dd_pct}%，禁止新买入"
    return None

async def _get_price(code: str, db):
    """获取股票当前价格"""
    snapshot = await _get_quote_snapshot(code, db)
    price = _safe_float(snapshot.get('price')) if snapshot else None
    if price is not None and price > 0:
        return price
    try:
        klines = await db.get_klines(code, limit=1)
        if klines:
            return klines[0].get('close')
    except Exception as e:
        logger.warning("[PaperTrading] 获取实时价格失败: %s", e)
        pass
    return None

async def _record_order_event(conn, order_id: str, event_type: str,
                              account_id: str | None = None, code: str | None = None,
                              payload: dict | None = None):
    """订单事件审计（容错写入，不影响主流程）"""
    try:
        event_object = _build_order_event_object(
            order_id=str(order_id),
            event_type=str(event_type),
            account_id=account_id,
            code=code,
            payload=payload,
        )
        await conn.execute(
            """INSERT INTO order_events (order_id, account_id, code, event_type, payload, created_at)
               VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)""",
            str(order_id), account_id, code, event_type, json.dumps(event_object)
        )
    except Exception as e:
        logger.debug("[PaperTrading] 记录 order_events 失败: %s", e)
