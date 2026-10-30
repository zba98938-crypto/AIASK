"""Intraday matching, stop-loss, risk reduction and manual market orders need a fresh quote.

On 2026-10-21 09:30:01 the matching engine read 000002's ``stock_quotes`` row
from 2026-07-15 (3.13). The snapshot said ``stale=True`` and nothing checked:
16 stop-loss sells, 55 order fills near 3.1316 and 24 risk reductions at stale
marks followed. This machine has no realtime source, so after the fix none of
these paths may trade, and each refusal is counted.

Quotes go through the real ``market_data_access`` snapshot (only its ``get_db``
is replaced), so the freshness rule under test is the production one.
"""

from __future__ import annotations

import sqlite3
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from akshare_mcp.services import market_data_access
from akshare_mcp.services.matching_engine import MatchingEngine
from akshare_mcp.services.risk_executor import RiskExecutor

# Imported here, before any fixture patches akshare_mcp.storage.get_db: the
# manager binds `from ...storage import get_db` at import, and RiskExecutor
# imports it lazily. Imported first inside a patched test, it would keep the
# fake for the rest of the session.
from akshare_mcp.tools.managers import _paper_trading_manager_support as paper_support  # noqa: E402
from akshare_mcp.tools.managers import paper_trading_manager  # noqa: E402

STALE = "2026-07-15T15:00:00+08:00"


def _fresh() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()


def test_naive_quote_timestamps_are_utc_not_host_local_time():
    if not hasattr(time, "tzset"):
        pytest.skip("host timezone cannot be changed in this process")
    previous_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Asia/Shanghai"
        time.tzset()
        expected = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)
        assert market_data_access._parse_asof("2026-10-21 01:00:00") == expected
        assert market_data_access._parse_asof(datetime(2026, 10, 21, 1, 0)) == expected
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


def test_future_dated_quote_is_not_a_current_execution_price():
    future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    assert market_data_access.quote_row_verdict(_quote(future)) == (
        False,
        "quote_timestamp_in_future",
    )
    assert market_data_access.execution_quote_verdict(
        {
            "success": True,
            "data": {"price": 10.0},
            "data_freshness_seconds": -60.0,
            "stale": False,
        }
    ) == (False, "quote_timestamp_in_future")


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_exc):
        return False


class _Conn:
    def __init__(self, store):
        self.store = store
        self.executed: list[tuple] = []

    async def fetch(self, query, *args):
        if "FROM paper_accounts" in query:
            return [dict(row) for row in self.store.accounts]
        if "FROM paper_positions" in query:
            return [dict(row) for row in self.store.positions if row["account_id"] == args[0]]
        return []

    async def fetchrow(self, query, *args):
        if "FROM paper_accounts" in query:
            return next((dict(row) for row in self.store.accounts if row["id"] == args[0]), None)
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))


class _Store:
    """Quote rows plus the few paper tables these paths read."""

    def __init__(self, quotes=None, accounts=None, positions=None):
        self.quotes = dict(quotes or {})
        self.accounts = list(accounts or [])
        self.positions = list(positions or [])
        self.conn = _Conn(self)

    async def get_latest_quote(self, code):
        row = self.quotes.get(code)
        return dict(row) if row else None

    def acquire(self):
        return _Acquire(self.conn)


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AKSHARE_MCP_QUOTE_MAX_STALE_SECONDS", "30")
    holder = {"store": _Store()}
    monkeypatch.setattr(market_data_access, "get_db", lambda: holder["store"])
    monkeypatch.setattr("akshare_mcp.storage.get_db", lambda: holder["store"])

    def install(**kwargs):
        holder["store"] = _Store(**kwargs)
        return holder["store"]

    return install


@pytest.fixture
def fills(monkeypatch):
    recorded: list[dict] = []

    async def fake_fill_order(conn, account_id, code, trade_type, shares, price, **kwargs):
        recorded.append({"account_id": account_id, "code": code, "trade_type": trade_type, "shares": shares, "price": price})
        return f"trade-{len(recorded)}", 0.0

    async def fake_record_order_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr("akshare_mcp.tools.managers.paper_trading_manager._fill_order", fake_fill_order)
    monkeypatch.setattr("akshare_mcp.tools.managers.paper_trading_manager._record_order_event", fake_record_order_event)
    return recorded


def _quote(time, price=10.0, prev_close=10.0):
    return {"code": "600000", "name": "浦发银行", "price": price, "prev_close": prev_close, "time": time}


def _order():
    return {"id": "order-1", "account_id": "acc-1", "strategy_id": "s-1", "code": "600000",
            "direction": "buy", "order_type": "marketable_limit", "price": 10.5, "shares": 100}


# --- MatchingEngine: pending orders ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("time, reason", [(STALE, "stale_quote"), (None, "quote_age_unknown")])
async def test_a_stale_or_undated_quote_does_not_fill_a_pending_order(store, fills, time, reason):
    db = store(quotes={"600000": _quote(time)})
    engine = MatchingEngine()

    await engine._try_match_order(db, _order())

    assert fills == []
    assert engine.skipped_stale_quote == 1
    assert engine.status()["skipped_quote_by_reason"] == {reason: 1}
    assert engine.status()["skipped_quote_by_path"] == {"order_match": 1}


@pytest.mark.asyncio
async def test_a_fresh_quote_still_fills(store, fills):
    # Healthy control: the guard must not stop trading on a real quote.
    db = store(quotes={"600000": _quote(_fresh())})
    engine = MatchingEngine()

    await engine._try_match_order(db, _order())

    assert len(fills) == 1
    assert 10.0 <= fills[0]["price"] <= 10.01  # quote plus slippage
    assert engine.skipped_stale_quote == 0


# --- MatchingEngine: stop-loss -----------------------------------------------


def _stop_loss_store(store, quote_time, quote_price):
    return store(
        quotes={"600000": _quote(quote_time, price=quote_price)},
        accounts=[{"id": "acc-1", "risk_rules": '{"stop_loss_pct": 8}'}],
        # The stored mark says -14%: it is the stale price that triggered 10-21.
        positions=[{"account_id": "acc-1", "stock_code": "600000", "quantity": 1000, "cost_price": 10.0, "current_price": 8.6}],
    )


@pytest.mark.asyncio
async def test_a_stale_quote_does_not_trigger_a_stop_loss(store, fills):
    db = _stop_loss_store(store, STALE, 8.6)
    engine = MatchingEngine()

    await engine._check_stop_loss(db)

    assert fills == []
    assert engine.status()["skipped_quote_by_path"] == {"stop_loss": 1}


@pytest.mark.asyncio
async def test_the_stop_loss_reads_the_fresh_quote_not_the_stored_mark(store, fills):
    engine = MatchingEngine()
    # Fresh quote at -1%: the stored -14% mark must not sell.
    await engine._check_stop_loss(_stop_loss_store(store, _fresh(), 9.9))
    assert fills == []
    # Fresh quote at -14%: sell, at the quote.
    await engine._check_stop_loss(_stop_loss_store(store, _fresh(), 8.6))
    assert fills == [{"account_id": "acc-1", "code": "600000", "trade_type": "sell", "shares": 1000, "price": 8.6}]


# --- RiskExecutor ------------------------------------------------------------


def _risk_store(store, quote_time):
    # 5,000 x 10.0 = 50% of 100,000 > 30% cap: keep 3,000, reduce 2,000.
    return store(
        quotes={"600000": _quote(quote_time, price=9.8)},
        accounts=[{"id": "acc-1", "total_value": 100000.0, "initial_capital": 100000.0,
                   "risk_rules": '{"max_position_pct": 30, "max_drawdown_pct": 20}'}],
        positions=[{"account_id": "acc-1", "stock_code": "600000", "quantity": 5000, "cost_price": 10.0, "current_price": 10.0}],
    )


@pytest.mark.asyncio
async def test_risk_reduction_is_skipped_without_a_fresh_quote(store, fills):
    _risk_store(store, STALE)
    executor = RiskExecutor()

    actions = await executor.enforce("acc-1")

    assert actions == []
    assert fills == []
    assert executor.status()["skipped_stale_quote"] == 1
    assert executor.status()["skipped_quote_by_reason"] == {"stale_quote": 1}


@pytest.mark.asyncio
async def test_risk_reduction_executes_at_the_fresh_quote(store, fills):
    # Healthy control: the trigger still fires, and the sell uses the quote.
    _risk_store(store, _fresh())
    executor = RiskExecutor()

    actions = await executor.enforce("acc-1")

    assert [action.action_type for action in actions] == ["reduce_position"]
    assert fills == [{"account_id": "acc-1", "code": "600000", "trade_type": "sell", "shares": 2000, "price": 9.8}]
    assert executor.status()["skipped_stale_quote"] == 0


# --- manual market orders ----------------------------------------------------


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def _register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _register


def _seed(path, *, quote_time, price=10.0):
    con = sqlite3.connect(path)
    con.execute("INSERT INTO stocks (stock_code, stock_name, market) VALUES ('600000', '浦发银行', 'SH')")
    con.execute(
        "INSERT INTO stock_quotes (time, code, name, price, prev_close) VALUES (?, '600000', '浦发银行', ?, ?)",
        (quote_time, price, price),
    )
    con.execute(
        "INSERT INTO kline_1d (time, code, open, high, low, close, volume) VALUES"
        " ('2026-07-15T15:00:00+08:00', '600000', 10, 10, 10, 10, 1000)"
    )
    con.commit()
    con.close()


@pytest_asyncio.fixture
async def fresh_store(initialized_db):
    # get_db() caches one adapter per event loop; without a reset this test
    # would read the previous test's temporary database.
    from akshare_mcp.storage import close_db

    await close_db()
    try:
        yield initialized_db
    finally:
        await close_db()


async def _place_market_buy(tool):
    created = await tool(action="create_account", params={"user_id": "local", "name": "t", "initial_capital": 100000})
    assert created["success"], created
    account_id = created["data"]["account_id"]
    result = await tool(action="place_order", params={"account_id": account_id, "code": "600000",
                                                     "direction": "buy", "shares": 100, "order_type": "market"})
    return account_id, result


@pytest.mark.asyncio
async def test_a_market_order_on_a_stale_quote_fails_instead_of_filling(fresh_store):
    _seed(fresh_store, quote_time=STALE)
    mcp = _FakeMcp()
    paper_trading_manager.register_paper_trading_manager(mcp)

    account_id, result = await _place_market_buy(mcp.tools["paper_trading_manager"])

    assert result["success"] is False
    assert result["error_code"] == "stale_quote"
    con = sqlite3.connect(fresh_store)
    assert con.execute("SELECT COUNT(*) FROM paper_trades WHERE account_id=?", (account_id,)).fetchone()[0] == 0
    con.close()


@pytest.mark.asyncio
async def test_a_market_order_on_a_fresh_quote_fills_at_it(fresh_store):
    _seed(fresh_store, quote_time=_fresh(), price=10.0)
    mcp = _FakeMcp()
    paper_trading_manager.register_paper_trading_manager(mcp)

    account_id, result = await _place_market_buy(mcp.tools["paper_trading_manager"])

    assert result["success"] is True, result
    con = sqlite3.connect(fresh_store)
    prices = [row[0] for row in con.execute("SELECT price FROM paper_trades WHERE account_id=?", (account_id,))]
    con.close()
    assert prices == [10.0]


@pytest.mark.asyncio
async def test_valuation_ignores_a_stale_quote(store):
    support = paper_support
    db = store(quotes={"600000": _quote(STALE, price=3.13)})
    assert await support._get_quote_snapshot("600000", db) == {}
    assert await support._get_fill_price("600000", db) == (None, "stale_quote")
    fresh_db = store(quotes={"600000": _quote(_fresh(), price=10.0)})
    assert (await support._get_quote_snapshot("600000", fresh_db))["price"] == 10.0
    assert await support._get_fill_price("600000", fresh_db) == (10.0, None)
def test_data_sync_scheduler_defaults_follow_startup_profile(monkeypatch):
    from akshare_mcp import server

    monkeypatch.delenv("DATA_SYNC_SCHEDULER_ENABLED", raising=False)
    assert server._data_sync_scheduler_enabled("tool-only") is False
    assert server._data_sync_scheduler_enabled("full") is True
    monkeypatch.setenv("DATA_SYNC_SCHEDULER_ENABLED", "0")
    assert server._data_sync_scheduler_enabled("full") is False
    monkeypatch.setenv("DATA_SYNC_SCHEDULER_ENABLED", "true")
    assert server._data_sync_scheduler_enabled("tool-only") is True

