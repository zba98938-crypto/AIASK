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


class _TradePositionsMixin:
    async def backfill_trade_position_links(self, strategy_id: Optional[str] = None) -> dict:
        strategy_filter = str(strategy_id or "").strip() or None
        unfilled_order_result = await self._backfill_unfilled_order_position_links(strategy_filter)
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE paper_trades
                SET signal_id = COALESCE(paper_trades.signal_id, paper_orders.signal_id),
                    position_id = COALESCE(paper_trades.position_id, paper_orders.position_id),
                    strategy_id = COALESCE(paper_trades.strategy_id, paper_orders.strategy_id)
                FROM paper_orders
                WHERE paper_trades.source_order_id = paper_orders.id
                  AND paper_orders.position_id IS NOT NULL
                  AND ($1 IS NULL OR COALESCE(paper_trades.strategy_id, paper_orders.strategy_id) = $1)
                """,
                strategy_filter,
            )
        positions_touched: set[str] = set()
        trades = await self.list_strategy_paper_trades(strategy_filter, limit=5000) if strategy_filter else []
        if not strategy_filter:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM paper_trades ORDER BY trade_time DESC, created_at DESC LIMIT 5000"
                )
            trades = [dict(row) for row in rows]
        for trade in trades:
            position_id_value = str(trade.get("position_id") or "").strip()
            if not position_id_value:
                continue
            fill_id = str(trade.get("id") or "").strip()
            await self.save_strategy_trade_position_fill(
                {
                    "fill_id": f"fill_{fill_id}" if fill_id else "",
                    "position_id": position_id_value,
                    "trade_id": trade.get("id"),
                    "order_id": trade.get("source_order_id"),
                    "signal_id": trade.get("signal_id"),
                    "strategy_id": trade.get("strategy_id"),
                    "account_id": trade.get("account_id"),
                    "code": trade.get("stock_code"),
                    "fill_side": trade.get("trade_type"),
                    "quantity": int(trade.get("quantity") or 0),
                    "price": float(trade.get("price") or 0.0),
                    "amount": float(trade.get("amount") or 0.0),
                    "commission": float(trade.get("commission") or 0.0),
                    "trade_time": trade.get("trade_time"),
                    "payload": {"source": "paper_trades_backfill"},
                }
            )
            positions_touched.add(position_id_value)
        for position_id_value in positions_touched:
            await self.refresh_strategy_trade_position(position_id_value)
        legacy_entry_result = await self._backfill_legacy_orphan_entry_links(strategy_filter)
        open_entry_result = await self._backfill_unlinked_filled_buy_positions(strategy_filter)
        orphan_sell_result = await self._backfill_unlinked_sell_positions(strategy_filter)
        orphan_result = await self._backfill_orphan_exit_trade_links(strategy_filter)
        return {
            "strategy_id": strategy_filter,
            "position_count": len(positions_touched),
            "fill_count": len(
                [
                    trade
                    for trade in trades
                    if str(trade.get("position_id") or "").strip()
                ]
            ),
            "unfilled_order_links": unfilled_order_result,
            "legacy_orphan_entry_links": legacy_entry_result,
            "open_entry_links": open_entry_result,
            "orphan_sell_links": orphan_sell_result,
            "orphan_exit_links": orphan_result,
        }

    async def _backfill_unfilled_order_position_links(self, strategy_id: Optional[str] = None) -> dict:
        strategy_filter = str(strategy_id or "").strip() or None

        def _affected_count(value: Any) -> int:
            try:
                return int(str(value or "").strip().split()[-1])
            except Exception:
                return 0

        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE paper_orders
                SET position_id = 'ordpos_' || CAST(id AS TEXT),
                    status = CASE
                        WHEN LOWER(COALESCE(status, '')) IN ('', 'pending', 'open', 'created', 'submitted')
                            THEN 'expired'
                        ELSE status
                    END,
                    reason = COALESCE(NULLIF(reason, ''), 'stale_unfilled_order_position_backfill')
                WHERE ($1 IS NULL OR strategy_id = $1)
                  AND NULLIF(TRIM(COALESCE(position_id, '')), '') IS NULL
                  AND LOWER(COALESCE(status, '')) NOT IN ('filled')
                  AND datetime(COALESCE(created_at, filled_at, CURRENT_TIMESTAMP)) <= datetime('now', '-1 day')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM paper_trades
                      WHERE CAST(paper_trades.source_order_id AS TEXT) = CAST(paper_orders.id AS TEXT)
                  )
                """,
                strategy_filter,
            )
        return {
            "strategy_id": strategy_filter,
            "linked_order_count": _affected_count(result),
        }

    async def _backfill_legacy_orphan_entry_links(self, strategy_id: Optional[str] = None) -> dict:
        strategy_filter = str(strategy_id or "").strip() or None
        async with self.acquire() as conn:
            orphan_rows = await conn.fetch(
                """
                SELECT
                    p.position_id,
                    p.strategy_id,
                    p.account_id,
                    p.signal_id,
                    p.code,
                    p.exit_trade_id,
                    p.exit_order_id,
                    p.exit_shares,
                    p.closed_at,
                    p.last_trade_time,
                    t.trade_time AS exit_trade_time,
                    t.quantity AS exit_quantity
                FROM strategy_trade_positions p
                JOIN paper_trades t
                  ON t.id = p.exit_trade_id
                WHERE COALESCE(p.status, '') = 'orphaned_exit'
                  AND p.entry_trade_id IS NULL
                  AND LOWER(COALESCE(t.trade_type, '')) = 'sell'
                  AND ($1 IS NULL OR p.strategy_id = $1)
                ORDER BY COALESCE(t.trade_time, p.closed_at, p.last_trade_time), p.position_id
                LIMIT 200
                """,
                strategy_filter,
            )

        linked_count = 0
        positions_touched: set[str] = set()
        for row in orphan_rows:
            item = dict(row or {})
            position_id = _string(item.get("position_id"))
            target_strategy_id = _string(item.get("strategy_id"))
            account_id = _string(item.get("account_id"))
            code = _string(item.get("code"))
            exit_quantity = _safe_int(item.get("exit_quantity") or item.get("exit_shares"))
            exit_time = item.get("exit_trade_time") or item.get("closed_at") or item.get("last_trade_time")
            if not position_id or not target_strategy_id or not account_id or not code or exit_quantity <= 0:
                continue
            async with self.acquire() as conn:
                candidate_rows = await conn.fetch(
                    """
                    SELECT
                        t.id AS trade_id,
                        t.source_order_id AS order_id,
                        COALESCE(NULLIF(t.signal_id, ''), NULLIF(o.signal_id, '')) AS signal_id,
                        COALESCE(NULLIF(t.strategy_id, ''), NULLIF(o.strategy_id, '')) AS strategy_id,
                        t.account_id,
                        t.stock_code AS code,
                        t.quantity,
                        t.price,
                        t.amount,
                        t.commission,
                        t.trade_time
                    FROM paper_trades t
                    LEFT JOIN paper_orders o
                      ON CAST(t.source_order_id AS TEXT) = CAST(o.id AS TEXT)
                    WHERE COALESCE(NULLIF(t.strategy_id, ''), NULLIF(o.strategy_id, '')) = $1
                      AND t.account_id = $2
                      AND t.stock_code = $3
                      AND LOWER(COALESCE(t.trade_type, '')) = 'buy'
                      AND datetime(COALESCE(t.trade_time, t.created_at)) <= datetime($4)
                      AND (NULLIF(TRIM(COALESCE(t.position_id, '')), '') IS NULL OR t.position_id = $5)
                      AND (NULLIF(TRIM(COALESCE(o.position_id, '')), '') IS NULL OR o.position_id = $5)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM strategy_trade_position_fills f
                          WHERE f.trade_id = t.id
                            AND f.position_id <> $5
                      )
                    ORDER BY
                        CASE WHEN COALESCE(t.quantity, 0) = $6 THEN 0 ELSE 1 END,
                        datetime(COALESCE(t.trade_time, t.created_at)) DESC,
                        t.id DESC
                    LIMIT 50
                    """,
                    target_strategy_id,
                    account_id,
                    code,
                    exit_time,
                    position_id,
                    exit_quantity,
                )
            if not candidate_rows:
                continue
            candidates = [dict(candidate or {}) for candidate in candidate_rows]
            selected: list[dict] = [
                candidate for candidate in candidates if _safe_int(candidate.get("quantity")) == exit_quantity
            ][:1]
            if not selected:
                ordered = sorted(
                    candidates,
                    key=lambda candidate: (
                        str(candidate.get("trade_time") or ""),
                        str(candidate.get("trade_id") or ""),
                    ),
                )
                running_quantity = 0
                cumulative: list[dict] = []
                for candidate in ordered:
                    running_quantity += _safe_int(candidate.get("quantity"))
                    cumulative.append(candidate)
                    if running_quantity >= exit_quantity:
                        break
                if running_quantity == exit_quantity:
                    selected = cumulative
            if not selected:
                continue
            async with self.acquire() as conn:
                for candidate in selected:
                    trade_id = _string(candidate.get("trade_id"))
                    order_id = _string(candidate.get("order_id"))
                    if not trade_id:
                        continue
                    await conn.execute(
                        """
                        UPDATE paper_trades
                        SET position_id = $2,
                            strategy_id = COALESCE(NULLIF(strategy_id, ''), $3)
                        WHERE id = $1
                        """,
                        trade_id,
                        position_id,
                        target_strategy_id,
                    )
                    if order_id:
                        await conn.execute(
                            """
                            UPDATE paper_orders
                            SET position_id = COALESCE(NULLIF(position_id, ''), $2),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE CAST(id AS TEXT) = CAST($1 AS TEXT)
                            """,
                            order_id,
                            position_id,
                        )
            for candidate in selected:
                trade_id = _string(candidate.get("trade_id"))
                if not trade_id:
                    continue
                await self.save_strategy_trade_position_fill(
                    {
                        "fill_id": f"fill_{trade_id}",
                        "position_id": position_id,
                        "trade_id": trade_id,
                        "order_id": candidate.get("order_id"),
                        "signal_id": candidate.get("signal_id") or item.get("signal_id"),
                        "strategy_id": target_strategy_id,
                        "account_id": account_id,
                        "code": code,
                        "fill_side": "buy",
                        "quantity": _safe_int(candidate.get("quantity")),
                        "price": _safe_float(candidate.get("price"), 0.0),
                        "amount": _safe_float(candidate.get("amount"), 0.0),
                        "commission": _safe_float(candidate.get("commission"), 0.0),
                        "trade_time": candidate.get("trade_time"),
                        "payload": {"source": "legacy_orphan_exit_entry_link_backfill"},
                    }
                )
                linked_count += 1
            positions_touched.add(position_id)
        for position_id in positions_touched:
            await self.refresh_strategy_trade_position(position_id)
        return {
            "strategy_id": strategy_filter,
            "linked_entry_trade_count": linked_count,
            "position_count": len(positions_touched),
        }

    async def _backfill_unlinked_filled_buy_positions(self, strategy_id: Optional[str] = None) -> dict:
        strategy_filter = str(strategy_id or "").strip() or None
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    t.id AS trade_id,
                    t.source_order_id AS order_id,
                    COALESCE(NULLIF(t.signal_id, ''), NULLIF(o.signal_id, '')) AS signal_id,
                    COALESCE(NULLIF(t.strategy_id, ''), NULLIF(o.strategy_id, '')) AS strategy_id,
                    t.account_id,
                    t.stock_code AS code,
                    t.quantity,
                    t.price,
                    t.amount,
                    t.commission,
                    t.trade_time
                FROM paper_trades t
                LEFT JOIN paper_orders o
                  ON CAST(t.source_order_id AS TEXT) = CAST(o.id AS TEXT)
                WHERE ($1 IS NULL OR COALESCE(NULLIF(t.strategy_id, ''), NULLIF(o.strategy_id, '')) = $1)
                  AND LOWER(COALESCE(t.trade_type, '')) = 'buy'
                  AND NULLIF(TRIM(COALESCE(t.position_id, '')), '') IS NULL
                  AND NULLIF(TRIM(COALESCE(o.position_id, '')), '') IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM strategy_trade_position_fills f
                      WHERE f.trade_id = t.id
                  )
                ORDER BY COALESCE(t.trade_time, t.created_at), t.id
                LIMIT 1000
                """,
                strategy_filter,
            )
        linked_count = 0
        positions_touched: set[str] = set()
        for row in rows:
            item = dict(row or {})
            trade_id = _string(item.get("trade_id"))
            strategy_id_value = _string(item.get("strategy_id"))
            account_id = _string(item.get("account_id"))
            code = _string(item.get("code"))
            quantity = _safe_int(item.get("quantity"))
            if not trade_id or not strategy_id_value or not account_id or not code or quantity <= 0:
                continue
            order_id = _string(item.get("order_id"))
            position_id = f"ordpos_{order_id}" if order_id else f"tradepos_{trade_id}"
            async with self.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE paper_trades
                    SET position_id = $2,
                        strategy_id = COALESCE(NULLIF(strategy_id, ''), $3)
                    WHERE id = $1
                    """,
                    trade_id,
                    position_id,
                    strategy_id_value,
                )
                if order_id:
                    await conn.execute(
                        """
                        UPDATE paper_orders
                        SET position_id = COALESCE(NULLIF(position_id, ''), $2),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE CAST(id AS TEXT) = CAST($1 AS TEXT)
                        """,
                        order_id,
                        position_id,
                    )
            await self.save_strategy_trade_position_fill(
                {
                    "fill_id": f"fill_{trade_id}",
                    "position_id": position_id,
                    "trade_id": trade_id,
                    "order_id": order_id or None,
                    "signal_id": item.get("signal_id"),
                    "strategy_id": strategy_id_value,
                    "account_id": account_id,
                    "code": code,
                    "fill_side": "buy",
                    "quantity": quantity,
                    "price": _safe_float(item.get("price"), 0.0),
                    "amount": _safe_float(item.get("amount"), 0.0),
                    "commission": _safe_float(item.get("commission"), 0.0),
                    "trade_time": item.get("trade_time"),
                    "payload": {"source": "legacy_unlinked_buy_open_position_backfill"},
                }
            )
            positions_touched.add(position_id)
            linked_count += 1
        for position_id in positions_touched:
            await self.refresh_strategy_trade_position(position_id)
        return {
            "strategy_id": strategy_filter,
            "linked_open_entry_trade_count": linked_count,
            "position_count": len(positions_touched),
        }

    async def _backfill_unlinked_sell_positions(self, strategy_id: Optional[str] = None) -> dict:
        strategy_filter = str(strategy_id or "").strip() or None
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    t.id AS trade_id,
                    t.source_order_id AS order_id,
                    COALESCE(NULLIF(t.signal_id, ''), NULLIF(o.signal_id, '')) AS signal_id,
                    COALESCE(NULLIF(t.strategy_id, ''), NULLIF(o.strategy_id, '')) AS strategy_id,
                    t.account_id,
                    t.stock_code AS code,
                    t.quantity,
                    t.price,
                    t.amount,
                    t.commission,
                    t.trade_time
                FROM paper_trades t
                LEFT JOIN paper_orders o
                  ON CAST(t.source_order_id AS TEXT) = CAST(o.id AS TEXT)
                WHERE ($1 IS NULL OR COALESCE(NULLIF(t.strategy_id, ''), NULLIF(o.strategy_id, '')) = $1)
                  AND LOWER(COALESCE(t.trade_type, '')) = 'sell'
                  AND NULLIF(TRIM(COALESCE(t.position_id, '')), '') IS NULL
                  AND (o.id IS NULL OR NULLIF(TRIM(COALESCE(o.position_id, '')), '') IS NULL)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM strategy_trade_position_fills f
                      WHERE f.trade_id = t.id
                  )
                ORDER BY COALESCE(t.trade_time, t.created_at), t.id
                LIMIT 1000
                """,
                strategy_filter,
            )
        linked_count = 0
        positions_touched: set[str] = set()
        for row in rows:
            item = dict(row or {})
            trade_id = _string(item.get("trade_id"))
            strategy_id_value = _string(item.get("strategy_id"))
            account_id = _string(item.get("account_id"))
            code = _string(item.get("code"))
            quantity = _safe_int(item.get("quantity"))
            if not trade_id or not strategy_id_value or not account_id or not code or quantity <= 0:
                continue
            order_id = _string(item.get("order_id"))
            position_id = f"orphanexit_{trade_id}"
            async with self.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE paper_trades
                    SET position_id = $2,
                        strategy_id = COALESCE(NULLIF(strategy_id, ''), $3)
                    WHERE id = $1
                    """,
                    trade_id,
                    position_id,
                    strategy_id_value,
                )
                if order_id:
                    await conn.execute(
                        """
                        UPDATE paper_orders
                        SET position_id = COALESCE(NULLIF(position_id, ''), $2),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE CAST(id AS TEXT) = CAST($1 AS TEXT)
                        """,
                        order_id,
                        position_id,
                    )
            await self.save_strategy_trade_position_fill(
                {
                    "fill_id": f"fill_{trade_id}",
                    "position_id": position_id,
                    "trade_id": trade_id,
                    "order_id": order_id or None,
                    "signal_id": item.get("signal_id"),
                    "strategy_id": strategy_id_value,
                    "account_id": account_id,
                    "code": code,
                    "fill_side": "sell",
                    "quantity": quantity,
                    "price": _safe_float(item.get("price"), 0.0),
                    "amount": _safe_float(item.get("amount"), 0.0),
                    "commission": _safe_float(item.get("commission"), 0.0),
                    "trade_time": item.get("trade_time"),
                    "payload": {"source": "legacy_unlinked_sell_orphan_exit_backfill"},
                }
            )
            positions_touched.add(position_id)
            linked_count += 1
        for position_id in positions_touched:
            await self.refresh_strategy_trade_position(position_id)
        return {
            "strategy_id": strategy_filter,
            "linked_orphan_sell_trade_count": linked_count,
            "position_count": len(positions_touched),
        }

    async def _backfill_orphan_exit_trade_links(self, strategy_id: Optional[str] = None) -> dict:
        strategy_filter = str(strategy_id or "").strip() or None
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    p.position_id AS target_position_id,
                    p.strategy_id AS target_strategy_id,
                    p.account_id AS target_account_id,
                    p.signal_id AS target_signal_id,
                    p.code AS target_code,
                    p.remaining_shares AS target_remaining_shares,
                    p.opened_at AS target_opened_at,
                    t.id AS trade_id,
                    t.source_order_id AS trade_order_id,
                    t.signal_id AS trade_signal_id,
                    t.strategy_id AS trade_strategy_id,
                    t.position_id AS trade_position_id,
                    t.quantity AS trade_quantity,
                    t.price AS trade_price,
                    t.amount AS trade_amount,
                    t.commission AS trade_commission,
                    t.trade_time AS trade_time,
                    orphan.position_id AS orphan_position_id
                FROM strategy_trade_positions p
                JOIN paper_trades t
                  ON t.account_id = p.account_id
                 AND t.stock_code = p.code
                 AND LOWER(COALESCE(t.trade_type, '')) = 'sell'
                 AND datetime(COALESCE(t.trade_time, t.created_at)) >= datetime(COALESCE(p.opened_at, p.created_at))
                LEFT JOIN strategy_trade_positions linked
                  ON linked.exit_trade_id = t.id
                 AND linked.position_id <> p.position_id
                 AND COALESCE(linked.status, '') = 'closed'
                LEFT JOIN strategy_trade_positions orphan
                  ON orphan.position_id = t.position_id
                 AND COALESCE(orphan.status, '') = 'orphaned_exit'
                 AND orphan.entry_trade_id IS NULL
                 AND orphan.exit_trade_id = t.id
                WHERE COALESCE(p.status, '') IN ('open', 'pending_exit')
                  AND p.exit_trade_id IS NULL
                  AND ($1 IS NULL OR p.strategy_id = $1)
                  AND linked.position_id IS NULL
                  AND (
                    t.position_id IS NULL
                    OR t.position_id = ''
                    OR t.position_id = p.position_id
                    OR orphan.position_id IS NOT NULL
                  )
                  AND (
                    t.strategy_id IS NULL
                    OR t.strategy_id = ''
                    OR t.strategy_id = p.strategy_id
                  )
                  AND COALESCE(t.quantity, 0) <= CASE
                    WHEN COALESCE(p.remaining_shares, 0) > 0
                      THEN COALESCE(p.remaining_shares, 0)
                    WHEN COALESCE(p.entry_shares, 0) > COALESCE(p.exit_shares, 0)
                      THEN COALESCE(p.entry_shares, 0) - COALESCE(p.exit_shares, 0)
                    ELSE COALESCE(t.quantity, 0)
                  END
                ORDER BY p.opened_at, t.trade_time, t.created_at, t.id
                LIMIT 500
                """,
                strategy_filter,
            )

        linked_count = 0
        orphan_removed_count = 0
        positions_touched: set[str] = set()
        trades_seen: set[str] = set()
        for row in rows:
            item = dict(row or {})
            trade_id = _string(item.get("trade_id"))
            position_id = _string(item.get("target_position_id"))
            if not trade_id or not position_id or trade_id in trades_seen:
                continue
            trades_seen.add(trade_id)
            quantity = _safe_int(item.get("trade_quantity"))
            if quantity <= 0:
                continue
            remaining_shares = _safe_int(item.get("target_remaining_shares"))
            if remaining_shares > 0 and quantity > remaining_shares:
                continue
            async with self.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE paper_trades
                    SET strategy_id = COALESCE(NULLIF(strategy_id, ''), $2),
                        signal_id = COALESCE(NULLIF(signal_id, ''), $3),
                        position_id = $4
                    WHERE id = $1
                    """,
                    trade_id,
                    _string(item.get("target_strategy_id")) or None,
                    _string(item.get("trade_signal_id")) or _string(item.get("target_signal_id")) or None,
                    position_id,
                )
                orphan_position_id = _string(item.get("orphan_position_id"))
                if orphan_position_id and orphan_position_id != position_id:
                    await conn.execute(
                        """
                        DELETE FROM strategy_trade_position_fills
                        WHERE position_id = $1 AND trade_id = $2
                        """,
                        orphan_position_id,
                        trade_id,
                    )
                    await conn.execute(
                        """
                        DELETE FROM strategy_trade_positions
                        WHERE position_id = $1
                          AND status = 'orphaned_exit'
                          AND entry_trade_id IS NULL
                          AND exit_trade_id = $2
                        """,
                        orphan_position_id,
                        trade_id,
                    )
                    orphan_removed_count += 1
            await self.save_strategy_trade_position_fill(
                {
                    "fill_id": f"fill_{trade_id}",
                    "position_id": position_id,
                    "trade_id": trade_id,
                    "order_id": item.get("trade_order_id"),
                    "signal_id": item.get("trade_signal_id") or item.get("target_signal_id"),
                    "strategy_id": item.get("target_strategy_id"),
                    "account_id": item.get("target_account_id"),
                    "code": item.get("target_code"),
                    "fill_side": "sell",
                    "quantity": quantity,
                    "price": _safe_float(item.get("trade_price")),
                    "amount": _safe_float(item.get("trade_amount")),
                    "commission": _safe_float(item.get("trade_commission")),
                    "trade_time": item.get("trade_time"),
                    "payload": {"source": "orphan_exit_trade_link_backfill"},
                }
            )
            positions_touched.add(position_id)
            linked_count += 1
        for position_id in positions_touched:
            await self.refresh_strategy_trade_position(position_id)
        return {
            "strategy_id": strategy_filter,
            "linked_exit_trade_count": linked_count,
            "position_count": len(positions_touched),
            "orphan_removed_count": orphan_removed_count,
        }
