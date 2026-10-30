"""Paper fills use only prices that existed on the execution day.

A signal confirmed at the close of session S executes at the open of the first
tradable bar E with S < E <= T (the settlement session), the backtest's
next_open policy. On 2026-10-20 settlement used "the close on or before S,
else the order's own price": 241 000002 positions opened at 3.66, the
2026-10-16 close, a price no bar on the fill day contained.

Every test runs against a real SQLiteAdapter and real ``kline_1d`` rows so the
adapter's own date bounds are exercised.
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio

from akshare_mcp.services.incubation import (
    EXECUTION_WINDOW_MISSED,
    PENDING_MARKET_DATA,
    StrategyIncubationService,
)
from akshare_mcp.services.incubation_factory import session_date as sd

STRATEGY_ID = "strategy-paper-fill-price"


@pytest_asyncio.fixture
async def adapter(initialized_db):
    from aiask_quant_core.storage.sqlite import SQLiteAdapter
    from strategy_factory.infrastructure.factory_schema import register_factory_schema

    register_factory_schema()
    db = SQLiteAdapter(path=initialized_db)
    await db.initialize()
    sd.clear_cache()
    try:
        yield db
    finally:
        sd.clear_cache()
        await db.close()


async def _bars(db, code: str, rows: list[tuple]) -> None:
    """rows: (day, open, close[, volume]); stored as the adapter stores daily bars."""
    async with db.acquire() as conn:
        for row in rows:
            day, open_, close = row[0], row[1], row[2]
            volume = row[3] if len(row) > 3 else 100_000
            await conn.execute(
                "INSERT INTO kline_1d (time, code, open, high, low, close, volume, amount)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                f"{day}T15:00:00+08:00",
                code,
                open_,
                max(open_, close) + 0.1,
                min(open_, close) - 0.1,
                close,
                volume,
                close * volume,
            )


async def _strategy(db, service: StrategyIncubationService, code: str = "600000") -> tuple[dict, str]:
    strategy = {
        "id": STRATEGY_ID,
        "name": "paper fill price",
        "strategy_type": "custom_signal",
        "target_symbols": [code],
        "params": {"target_symbols": [code]},
    }
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO strategies (id, name, strategy_type, params, status, created_at, updated_at)"
            " VALUES ($1, $2, $3, '{}', 'incubating', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            strategy["id"],
            strategy["name"],
            strategy["strategy_type"],
        )
    account = (await service.ensure_account(db, strategy))["account"]
    return strategy, account["id"]


async def _order(db, account_id: str, *, code: str, signal_date: str, direction: str = "buy",
                 shares: int = 100, price: float = 50.0, reason: str | None = None) -> int:
    # `price` is the order's own limit, deliberately far from every bar: it
    # must never become the fill price.
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO paper_orders (account_id, strategy_id, signal_date, source, code, direction, shares,"
            " price, order_type, status, reason, signal_id, position_id, created_at, updated_at)"
            " VALUES ($1, $2, $3, 'strategy_signal', $4, $5, $6, $7, 'limit', 'pending', $8, $9, $10,"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING id",
            account_id, STRATEGY_ID, signal_date, code, direction, shares, price, reason,
            f"sig-{code}-{signal_date}-{direction}", f"pos-{code}",
        )
    return int(dict(row)["id"])


async def _position(db, account_id: str, *, code: str, quantity: int, cost: float) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO paper_positions (account_id, stock_code, stock_name, quantity, cost_price, current_price, market_value)"
            " VALUES ($1, $2, $2, $3, $4, $4, $5)",
            account_id, code, quantity, cost, cost * quantity,
        )


async def _order_row(db, order_id: int) -> dict:
    async with db.acquire() as conn:
        return dict(await conn.fetchrow("SELECT status, reason, price, filled_at FROM paper_orders WHERE id=$1", order_id))


async def _trades(db) -> list[dict]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT trade_type, price, quantity, trade_time, reason FROM paper_trades WHERE strategy_id=$1 ORDER BY trade_time",
            STRATEGY_ID,
        )
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_fills_at_the_next_session_open_and_marks_at_its_close(adapter):
    service = StrategyIncubationService()
    _, account_id = await _strategy(adapter, service)
    await _bars(adapter, "600000", [("2026-10-20", 10.0, 10.2), ("2026-10-21", 10.4, 10.6)])
    order_id = await _order(adapter, account_id, code="600000", signal_date="2026-10-20")

    result = await service.settle_orders(adapter, {"id": STRATEGY_ID}, date(2026, 10, 21))

    assert result["filled_count"] == 1
    assert result["deferred_counts"] == {}
    (trade,) = await _trades(adapter)
    assert float(trade["price"]) == 10.4  # S+1 open, not the S close (10.2) nor the order price (50.0)
    assert 10.3 <= float(trade["price"]) <= 10.7  # inside the execution day's [low, high]
    assert str(trade["trade_time"]).startswith("2026-10-21T01:30:00")  # 09:30 CST
    order = await _order_row(adapter, order_id)
    assert order["status"] == "filled"
    assert str(order["filled_at"]).startswith("2026-10-21T01:30:00")
    assert result["marks"]["600000"] == {"price": 10.6, "date": "2026-10-21", "source": "kline_1d"}
    assert result["nav_snapshot"]["nav_date"] == date(2026, 10, 21)
    assert result["nav_snapshot"]["market_value"] == pytest.approx(1060.0)


@pytest.mark.asyncio
async def test_zero_cash_is_not_replaced_by_initial_capital(adapter):
    service = StrategyIncubationService()
    strategy, account_id = await _strategy(adapter, service)
    async with adapter.acquire() as conn:
        account = dict(await conn.fetchrow("SELECT * FROM paper_accounts WHERE id=$1", account_id))
    account["current_capital"] = 0.0
    account["total_value"] = 0.0
    await service._save_strategy_account(adapter, account)
    await _bars(adapter, "600000", [("2026-10-20", 10.0, 10.0), ("2026-10-21", 10.0, 10.0)])
    order_id = await _order(adapter, account_id, code="600000", signal_date="2026-10-20")

    result = await service.settle_orders(adapter, strategy, date(2026, 10, 21))

    assert result["filled_count"] == 0
    assert result["rejected_count"] == 1
    assert (await _order_row(adapter, order_id))["reason"] == "insufficient_cash"
    assert await _trades(adapter) == []


@pytest.mark.asyncio
async def test_no_bar_after_the_signal_keeps_the_order_pending(adapter):
    service = StrategyIncubationService()
    _, account_id = await _strategy(adapter, service)
    await _bars(adapter, "600000", [("2026-10-16", 3.66, 3.66)])
    order_id = await _order(adapter, account_id, code="600000", signal_date="2026-10-16")

    result = await service.settle_orders(adapter, {"id": STRATEGY_ID}, date(2026, 10, 20))

    assert result["filled_count"] == 0
    assert result["rejected_count"] == 0
    assert result["deferred_counts"] == {PENDING_MARKET_DATA: 1}
    assert result["deferred_detail_counts"] == {"no_bar_after_signal": 1}
    assert await _trades(adapter) == []
    order = await _order_row(adapter, order_id)
    assert order["status"] == "pending"
    assert order["reason"] == PENDING_MARKET_DATA


@pytest.mark.asyncio
async def test_an_order_from_tonight_waits_for_the_next_session(adapter):
    service = StrategyIncubationService()
    _, account_id = await _strategy(adapter, service)
    await _bars(adapter, "600000", [("2026-10-21", 10.0, 10.2)])
    await _order(adapter, account_id, code="600000", signal_date="2026-10-21")

    result = await service.settle_orders(adapter, {"id": STRATEGY_ID}, date(2026, 10, 21))

    assert result["filled_count"] == 0
    assert result["deferred_detail_counts"] == {"awaiting_next_session": 1}


@pytest.mark.asyncio
async def test_a_friday_signal_fills_at_the_monday_open(adapter):
    service = StrategyIncubationService()
    _, account_id = await _strategy(adapter, service)
    await _bars(adapter, "600000", [("2026-10-16", 10.0, 10.1), ("2026-10-19", 10.3, 10.5)])
    await _order(adapter, account_id, code="600000", signal_date="2026-10-16")

    result = await service.settle_orders(adapter, {"id": STRATEGY_ID}, date(2026, 10, 19))

    assert result["filled_count"] == 1
    (trade,) = await _trades(adapter)
    assert float(trade["price"]) == 10.3
    assert str(trade["trade_time"]).startswith("2026-10-19T01:30:00")


@pytest.mark.asyncio
async def test_an_order_trailing_the_settlement_by_more_than_one_session_expires(adapter):
    service = StrategyIncubationService()
    _, account_id = await _strategy(adapter, service)
    await _bars(adapter, "600000", [
        ("2026-10-20", 10.0, 10.0), ("2026-10-21", 10.1, 10.1),
        ("2026-10-22", 10.2, 10.2), ("2026-10-23", 10.3, 10.3),
    ])
    order_id = await _order(adapter, account_id, code="600000", signal_date="2026-10-20")

    result = await service.settle_orders(adapter, {"id": STRATEGY_ID}, date(2026, 10, 23))

    assert result["filled_count"] == 0
    assert result["expired_count"] == 1
    assert await _trades(adapter) == []
    order = await _order_row(adapter, order_id)
    assert order["status"] == "expired"
    assert order["reason"] == EXECUTION_WINDOW_MISSED


@pytest.mark.asyncio
async def test_one_missed_run_still_fills_at_the_missed_session_open(adapter):
    # Healthy control for the expiry rule: one session of lag is inside the window.
    service = StrategyIncubationService()
    _, account_id = await _strategy(adapter, service)
    await _bars(adapter, "600000", [("2026-10-20", 10.0, 10.0), ("2026-10-21", 10.1, 10.15), ("2026-10-22", 10.2, 10.25)])
    await _order(adapter, account_id, code="600000", signal_date="2026-10-20")

    result = await service.settle_orders(adapter, {"id": STRATEGY_ID}, date(2026, 10, 22))

    assert result["filled_count"] == 1
    assert result["expired_count"] == 0
    (trade,) = await _trades(adapter)
    assert float(trade["price"]) == 10.1
    assert result["marks"]["600000"]["date"] == "2026-10-22"


@pytest.mark.asyncio
async def test_a_zero_volume_day_is_not_an_execution_bar(adapter):
    service = StrategyIncubationService()
    _, account_id = await _strategy(adapter, service)
    await _bars(adapter, "600000", [
        ("2026-10-20", 10.0, 10.0), ("2026-10-21", 10.0, 10.0, 0), ("2026-10-22", 10.5, 10.6),
    ])
    await _order(adapter, account_id, code="600000", signal_date="2026-10-20")

    result = await service.settle_orders(adapter, {"id": STRATEGY_ID}, date(2026, 10, 22))

    assert result["filled_count"] == 1
    (trade,) = await _trades(adapter)
    assert float(trade["price"]) == 10.5
    assert str(trade["trade_time"]).startswith("2026-10-22")


@pytest.mark.asyncio
async def test_a_bar_after_the_settlement_session_is_never_used(adapter):
    # A replay settling 10-21 must not see the 10-23 bar, for the fill or the mark.
    service = StrategyIncubationService()
    _, account_id = await _strategy(adapter, service)
    await _bars(adapter, "600000", [("2026-10-20", 10.0, 10.0), ("2026-10-23", 99.0, 99.0)])
    await _bars(adapter, "600001", [("2026-10-20", 20.0, 20.0), ("2026-10-23", 88.0, 88.0)])
    await _position(adapter, account_id, code="600001", quantity=100, cost=20.0)
    await _order(adapter, account_id, code="600000", signal_date="2026-10-20")

    result = await service.settle_orders(adapter, {"id": STRATEGY_ID}, date(2026, 10, 21))

    assert result["filled_count"] == 0
    assert result["deferred_counts"] == {PENDING_MARKET_DATA: 1}
    assert result["marks"]["600001"] == {"price": 20.0, "date": "2026-10-20", "source": "kline_1d"}
    assert result["stale_mark_codes"] == ["600001"]
    assert result["nav_snapshot"] is None
    assert result["nav_status"] == "unmeasurable"
    assert result["nav_reason_code"] == "position_mark_not_current"
    assert result["market_value"] is None
    async with adapter.acquire() as conn:
        nav_rows = await conn.fetch(
            "SELECT * FROM paper_nav WHERE account_id=$1 AND nav_date=$2",
            account_id,
            date(2026, 10, 21),
        )
    assert nav_rows == []


@pytest.mark.asyncio
async def test_a_still_pending_order_is_not_duplicated_by_the_next_run(adapter):
    service = StrategyIncubationService()
    strategy, account_id = await _strategy(adapter, service)
    strategy["target_symbols"] = ["600000", "600002"]
    strategy["params"] = {"target_symbols": ["600000", "600002"]}
    await _bars(adapter, "600000", [("2026-10-20", 10.0, 10.0), ("2026-10-21", 10.1, 10.1)])
    await _bars(adapter, "600002", [("2026-10-20", 10.0, 10.0), ("2026-10-21", 10.1, 10.1)])
    await _order(adapter, account_id, code="600000", signal_date="2026-10-20", price=10.0)
    await adapter.save_signals(
        STRATEGY_ID,
        date(2026, 10, 21),
        [{"code": "600000", "signal": 1, "score": 1.0}, {"code": "600002", "signal": 1, "score": 1.0}],
    )

    result = await service.sync_signals_to_orders(adapter, strategy, date(2026, 10, 21))

    assert result["skip_reason_counts"].get("order_already_pending") == 1
    async with adapter.acquire() as conn:
        rows = await conn.fetch(
            "SELECT code FROM paper_orders WHERE strategy_id=$1 AND signal_date=$2 ORDER BY code",
            STRATEGY_ID, "2026-10-21",
        )
    assert [dict(row)["code"] for row in rows] == ["600002"]


@pytest.mark.asyncio
async def test_without_a_date_the_settlement_session_comes_from_the_data(adapter):
    service = StrategyIncubationService()
    _, account_id = await _strategy(adapter, service)
    await _bars(adapter, "600000", [("2026-10-20", 10.0, 10.0), ("2026-10-21", 10.4, 10.6)])
    await _order(adapter, account_id, code="600000", signal_date="2026-10-20")

    result = await service.settle_orders(adapter, {"id": STRATEGY_ID})

    assert result["settle_date"] == "2026-10-21"
    assert result["session_source"] == sd.SOURCE_PARTIAL  # one code is not a synced session
    assert result["nav_snapshot"]["nav_date"] == date(2026, 10, 21)
    batch = await service.process_strategies(adapter, [])
    assert batch["signal_date"] == "2026-10-21"
    assert batch["session_source"] == sd.SOURCE_PARTIAL


@pytest.mark.asyncio
async def test_a_decision_never_sees_a_bar_after_its_date(adapter):
    service = StrategyIncubationService()
    await _bars(adapter, "600000", [("2026-10-23", 99.0, 99.0)])
    assert await service._price_on_or_before(adapter, "600000", date(2026, 10, 20)) is None
    assert await service._price_on_or_before(adapter, "600000", None) == 99.0


def test_the_execution_timestamp_never_falls_back_to_the_wall_clock():
    with pytest.raises(ValueError):
        StrategyIncubationService._execution_timestamp(None)
    stamp = StrategyIncubationService._execution_timestamp("2026-10-21")
    assert stamp.isoformat() == "2026-10-21T00:00:00+00:00"
