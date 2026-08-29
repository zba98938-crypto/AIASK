from __future__ import annotations

import pytest

from akshare_mcp.services.matching_engine import MatchingEngine


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Conn:
    def __init__(self):
        self.executed = []

    async def execute(self, query: str, *args):
        self.executed.append((query, args))


class _Db:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


@pytest.mark.asyncio
async def test_marketable_limit_order_fills_at_current_quote(monkeypatch):
    conn = _Conn()
    engine = MatchingEngine()

    async def fake_fill_order(
        conn_arg,
        account_id,
        code,
        direction,
        shares,
        fill_price,
        **kwargs,
    ):
        assert conn_arg is conn
        assert account_id == "acc-1"
        assert code == "688981"
        assert direction == "sell"
        assert shares == 100
        assert 125.0 <= fill_price <= 126.0
        assert kwargs["strategy_id"] == "strategy-1"
        assert kwargs["source_order_id"] == "order-1"
        return "trade-1", 1.23

    async def fake_record_order_event(conn_arg, order_id, event_type, **kwargs):
        assert conn_arg is conn
        assert order_id == "order-1"
        assert event_type == "filled_by_engine"

    monkeypatch.setattr(engine, "_get_current_price", lambda code, db: _async_value(125.34))
    monkeypatch.setattr(engine, "_get_prev_close", lambda code, db: _async_value(None))
    monkeypatch.setattr(
        "akshare_mcp.tools.managers.paper_trading_manager._fill_order",
        fake_fill_order,
    )
    monkeypatch.setattr(
        "akshare_mcp.tools.managers.paper_trading_manager._record_order_event",
        fake_record_order_event,
    )

    await engine._try_match_order(
        _Db(conn),
        {
            "id": "order-1",
            "account_id": "acc-1",
            "strategy_id": "strategy-1",
            "code": "688981",
            "direction": "sell",
            "shares": 100,
            "price": 134.7,
            "order_type": "marketable_limit",
            "signal_id": "sig-1",
            "position_id": "pos-1",
        },
    )

    assert engine.matched_count == 1
    assert any("UPDATE paper_orders SET status='filled'" in query for query, _ in conn.executed)


async def _async_value(value):
    return value
