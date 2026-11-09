"""Ambiguous daily history must not become measured backtest returns."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from aiask_quant_core.backtest import BacktestEngine
from aiask_quant_core.backtest.utils import align_klines_by_trade_date
from aiask_quant_core.storage.sqlite import SQLiteAdapter


def ambiguous_bars():
    rows = [{"date": f"2026-06-{day:02d}", "open": 10., "high": 11., "low": 9., "close": 10., "volume": 10000} for day in range(1, 22)]
    rows.insert(1, {**rows[0], "date": "2026-06-01T15:00:00+08:00", "close": 10.5})
    return rows


@pytest.mark.parametrize("strategy", ["buy_and_hold", "ma_cross", "momentum"])
def test_single_name_returns_unavailable_without_mutating_or_selecting_input(strategy):
    rows = ambiguous_bars()
    before = deepcopy(rows)
    result = BacktestEngine.run_backtest("600519", rows, strategy, {}, True)
    assert result["success"] is False
    assert result["error"] == "duplicate_trade_dates"
    assert result["duplicate_dates"] == ["20260601"]
    assert "data" not in result
    assert rows == before


def test_alignment_cannot_silently_keep_last_duplicate():
    with pytest.raises(ValueError, match="duplicate_trade_dates:600519:20260601"):
        align_klines_by_trade_date({"600519": ambiguous_bars()})


def test_portfolio_returns_failure_instead_of_deleting_duplicate_or_target():
    result = BacktestEngine.run_portfolio_backtest({"600519": ambiguous_bars(), "000001": ambiguous_bars()[2:]}, params={})
    assert result["success"] is False
    assert "duplicate_trade_dates:600519:20260601" in result["error"]
    assert "data" not in result


@pytest.mark.asyncio
async def test_real_sqlite_reader_rejects_ambiguous_calendar_day_and_preserves_raw_history(tmp_path):
    database = tmp_path / "ambiguous.sqlite3"
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE kline_1d(time TEXT,code TEXT,open REAL,high REAL,low REAL,close REAL,volume INTEGER,amount REAL,turnover REAL,change_pct REAL)")
        con.executemany("INSERT INTO kline_1d VALUES(?, '600519',10,11,9,10,10000,NULL,NULL,NULL)", [("2026-06-01",), ("2026-06-01T15:00:00+08:00",)])
    before = database.read_bytes()
    adapter = SQLiteAdapter(path=database, read_only=True)
    try:
        await adapter.initialize()
        with pytest.raises(ValueError, match="duplicate_trade_dates:600519:20260601"):
            await adapter.get_klines("600519", limit=100)
        # A row limit may otherwise cut away one member of the conflicting day.
        with pytest.raises(ValueError, match="duplicate_trade_dates:600519:20260601"):
            await adapter.get_klines("600519", limit=1)
    finally:
        await adapter.close()
    assert database.read_bytes() == before
