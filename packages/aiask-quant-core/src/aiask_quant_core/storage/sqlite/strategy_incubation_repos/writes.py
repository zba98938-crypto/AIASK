from __future__ import annotations

from ._base import *  # noqa: F401,F403
from ._base import (
    _coerce_ts,
    _fallback_execution_audit_gate,
    _safe_float,
    _safe_int,
    _safe_rules_dict,
    _string,
)


class _IncWritesMixin:
    async def save_paper_order(self, order: dict) -> dict:
        payload = dict(order or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO paper_orders
                    (account_id, strategy_id, signal_date, source, code, direction, shares, price,
                     order_type, stop_price, status, commission, reason, filled_at, signal_id, position_id,
                     created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                        $9, $10, $11, $12, $13, $14, $15, $16, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING *
                """,
                payload.get('account_id'),
                payload.get('strategy_id'),
                payload.get('signal_date'),
                payload.get('source') or 'manual',
                payload.get('code'),
                payload.get('direction'),
                int(payload.get('shares') or 0),
                payload.get('price'),
                payload.get('order_type') or 'market',
                payload.get('stop_price'),
                payload.get('status') or 'pending',
                float(payload.get('commission') or 0.0),
                payload.get('reason'),
                payload.get('filled_at'),
                payload.get('signal_id'),
                payload.get('position_id'),
            )
        return dict(row)

    async def update_paper_order(self, order_id: int, updates: dict) -> Optional[dict]:
        payload = dict(updates or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE paper_orders
                SET price = COALESCE($2, price),
                    shares = COALESCE($3, shares),
                    status = COALESCE($4, status),
                    commission = COALESCE($5, commission),
                    reason = COALESCE($6, reason),
                    filled_at = COALESCE($7, filled_at),
                    signal_id = COALESCE($8, signal_id),
                    position_id = COALESCE($9, position_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                RETURNING *
                """,
                int(order_id),
                payload.get('price'),
                payload.get('shares'),
                payload.get('status'),
                payload.get('commission'),
                payload.get('reason'),
                payload.get('filled_at'),
                payload.get('signal_id'),
                payload.get('position_id'),
            )
        return dict(row) if row else None

    async def list_paper_positions(self, account_id: str) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM paper_positions WHERE account_id = $1 ORDER BY stock_code",
                account_id,
            )
        return [dict(row) for row in rows]

    async def save_paper_position(self, position: dict) -> dict:
        payload = dict(position or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO paper_positions
                    (account_id, stock_code, stock_name, quantity, cost_price, current_price, market_value, profit_rate, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (account_id, stock_code) DO UPDATE SET
                    stock_name = EXCLUDED.stock_name,
                    quantity = EXCLUDED.quantity,
                    cost_price = EXCLUDED.cost_price,
                    current_price = EXCLUDED.current_price,
                    market_value = EXCLUDED.market_value,
                    profit_rate = EXCLUDED.profit_rate,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                payload.get('account_id'),
                payload.get('stock_code'),
                payload.get('stock_name') or payload.get('stock_code') or '',
                int(payload.get('quantity') or 0),
                float(payload.get('cost_price') or 0.0),
                payload.get('current_price'),
                payload.get('market_value'),
                payload.get('profit_rate'),
            )
        return dict(row)

    async def save_paper_trade(self, trade: dict) -> dict:
        payload = dict(trade or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO paper_trades
                    (id, account_id, stock_code, stock_name, trade_type, price, quantity, amount, commission,
                     trade_time, reason, strategy_id, source_order_id, signal_id, position_id, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, CURRENT_TIMESTAMP)
                RETURNING *
                """,
                str(payload.get('id') or ''),
                payload.get('account_id'),
                payload.get('stock_code'),
                payload.get('stock_name') or payload.get('stock_code') or '',
                payload.get('trade_type'),
                float(payload.get('price') or 0.0),
                int(payload.get('quantity') or 0),
                float(payload.get('amount') or 0.0),
                float(payload.get('commission') or 0.0),
                payload.get('trade_time'),
                payload.get('reason'),
                payload.get('strategy_id'),
                payload.get('source_order_id'),
                payload.get('signal_id'),
                payload.get('position_id'),
            )
        return dict(row)

    async def update_paper_trade_linkage(self, trade_id: str, updates: dict) -> Optional[dict]:
        payload = dict(updates or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE paper_trades
                SET strategy_id = COALESCE($2, strategy_id),
                    source_order_id = COALESCE($3, source_order_id),
                    signal_id = COALESCE($4, signal_id),
                    position_id = COALESCE($5, position_id)
                WHERE id = $1
                RETURNING *
                """,
                str(trade_id),
                payload.get("strategy_id"),
                payload.get("source_order_id"),
                payload.get("signal_id"),
                payload.get("position_id"),
            )
        return dict(row) if row else None

    async def get_strategy_trade_position(self, position_id: str) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM strategy_trade_positions WHERE position_id = $1",
                str(position_id),
            )
        return dict(row) if row else None

    async def list_strategy_trade_positions(
        self,
        strategy_id: Optional[str] = None,
        account_id: Optional[str] = None,
        code: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_trade_positions WHERE 1=1"
            params: list[Any] = []
            idx = 1
            if strategy_id:
                sql += f" AND strategy_id = ${idx}"
                params.append(strategy_id)
                idx += 1
            if account_id:
                sql += f" AND account_id = ${idx}"
                params.append(account_id)
                idx += 1
            if code:
                sql += f" AND code = ${idx}"
                params.append(code)
                idx += 1
            if status:
                sql += f" AND status = ${idx}"
                params.append(status)
                idx += 1
            sql += f" ORDER BY COALESCE(closed_at, opened_at, created_at) DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 200), 5000)))
            rows = await conn.fetch(sql, *params)
        return [dict(row) for row in rows]

    async def save_strategy_trade_position(self, position: dict) -> dict:
        payload = dict(position or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
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
                RETURNING *
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
        return dict(row)

    async def list_strategy_trade_position_fills(
        self,
        *,
        position_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_trade_position_fills WHERE 1=1"
            params: list[Any] = []
            idx = 1
            if position_id:
                sql += f" AND position_id = ${idx}"
                params.append(position_id)
                idx += 1
            if strategy_id:
                sql += f" AND strategy_id = ${idx}"
                params.append(strategy_id)
                idx += 1
            sql += f" ORDER BY trade_time ASC, created_at ASC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 500), 5000)))
            rows = await conn.fetch(sql, *params)
        return [dict(row) for row in rows]

    async def save_strategy_trade_position_fill(self, fill: dict) -> dict:
        payload = dict(fill or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
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
                RETURNING *
                """,
                str(payload.get("fill_id") or ""),
                payload.get("position_id"),
                payload.get("trade_id"),
                payload.get("order_id"),
                payload.get("signal_id"),
                payload.get("strategy_id"),
                payload.get("account_id"),
                payload.get("code"),
                payload.get("fill_side"),
                payload.get("quantity"),
                payload.get("price"),
                payload.get("amount"),
                payload.get("commission"),
                payload.get("trade_time"),
                json.dumps(payload.get("payload") or {}, ensure_ascii=False, default=str),
            )
        return dict(row)

    async def save_paper_nav(self, nav: dict) -> dict:
        payload = dict(nav or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO paper_nav
                    (account_id, nav_date, total_value, cash, market_value, daily_return, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP)
                ON CONFLICT (account_id, nav_date) DO UPDATE SET
                    total_value = EXCLUDED.total_value,
                    cash = EXCLUDED.cash,
                    market_value = EXCLUDED.market_value,
                    daily_return = EXCLUDED.daily_return
                RETURNING *
                """,
                payload.get('account_id'),
                payload.get('nav_date'),
                float(payload.get('total_value') or 0.0),
                float(payload.get('cash') or 0.0),
                float(payload.get('market_value') or 0.0),
                payload.get('daily_return'),
            )
        return dict(row)

    async def get_paper_nav_rows(self, account_id: str, limit: int = 60) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM paper_nav WHERE account_id = $1 ORDER BY nav_date DESC LIMIT $2",
                account_id,
                max(1, min(int(limit or 60), 365)),
            )
        return [dict(row) for row in rows]

    async def get_paper_order_summary(self, account_id: str) -> dict:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COALESCE((SELECT COUNT(*) FROM paper_orders WHERE account_id = $1), 0) AS total_orders,
                    COALESCE((SELECT COUNT(*) FROM paper_orders WHERE account_id = $1 AND status = 'filled'), 0) AS filled_orders,
                    COALESCE((SELECT COUNT(*) FROM paper_trades WHERE account_id = $1), 0) AS total_trades,
                    COALESCE((SELECT SUM(amount) FROM paper_trades WHERE account_id = $1), 0) AS trade_amount
                """,
                account_id,
            )
        return {
            'total_orders': int((row or {}).get('total_orders') or 0),
            'filled_orders': int((row or {}).get('filled_orders') or 0),
            'total_trades': int((row or {}).get('total_trades') or 0),
            'trade_amount': float((row or {}).get('trade_amount') or 0.0),
        }

    @staticmethod
    def _aggregate_trade_position(existing: Optional[dict], fills: list[dict]) -> dict:
        return aggregate_trade_position(existing, fills)

    async def _enrich_trade_position_price_path(self, position: Optional[dict]) -> dict:
        payload = dict(position or {})
        entry_ts = _coerce_ts(payload.get("entry_ts") or payload.get("opened_at"))
        exit_ts = _coerce_ts(payload.get("exit_ts") or payload.get("closed_at") or payload.get("last_trade_time"))
        entry_avg_price = _safe_float(payload.get("entry_avg_price"))
        code = _string(payload.get("code"))
        direction = _string(payload.get("direction")).lower() or "long"
        if not code or entry_avg_price is None or entry_avg_price <= 0 or entry_ts is None:
            payload["price_path_audit_status"] = "missing_entry_context"
            return payload
        if not hasattr(self, "get_klines"):
            payload["price_path_audit_status"] = "missing_kline_source"
            return payload
        start_date = entry_ts.date().isoformat() if isinstance(entry_ts, datetime) else str(entry_ts)
        resolved_end_ts = exit_ts or datetime.utcnow()
        end_date = (
            resolved_end_ts.date().isoformat()
            if isinstance(resolved_end_ts, datetime)
            else str(resolved_end_ts)
        )
        try:
            klines = await self.get_klines(code, start_date=start_date, end_date=end_date)
        except Exception:
            payload["price_path_audit_status"] = "missing_kline"
            return payload
        if not klines:
            payload["price_path_audit_status"] = "missing_kline"
            return payload
        favorable_moves: list[float] = []
        adverse_moves: list[float] = []
        for item in klines:
            high = _safe_float(dict(item).get("high"))
            low = _safe_float(dict(item).get("low"))
            if high is None or low is None:
                continue
            if direction == "short":
                favorable_moves.append((entry_avg_price - low) / entry_avg_price)
                adverse_moves.append((entry_avg_price - high) / entry_avg_price)
            else:
                favorable_moves.append((high - entry_avg_price) / entry_avg_price)
                adverse_moves.append((low - entry_avg_price) / entry_avg_price)
        payload["mfe"] = round(max(favorable_moves), 6) if favorable_moves else None
        payload["mae"] = round(min(adverse_moves), 6) if adverse_moves else None
        payload["price_path_audit_status"] = (
            "audited_closed_position"
            if str(payload.get("status") or "") == "closed"
            else "audited_open_position"
        )
        return payload

    async def refresh_strategy_trade_position(self, position_id: str) -> Optional[dict]:
        fills = await self.list_strategy_trade_position_fills(position_id=str(position_id), limit=2000)
        if not fills:
            return await self.get_strategy_trade_position(position_id)
        existing = await self.get_strategy_trade_position(position_id)
        aggregate = self._aggregate_trade_position(existing, fills)
        aggregate["position_id"] = str(position_id)
        aggregate = await self._enrich_trade_position_price_path(aggregate)
        return await self.save_strategy_trade_position(aggregate)
