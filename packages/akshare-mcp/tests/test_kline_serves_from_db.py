"""Two defects in the daily read path: a gate that could never open, and a derivation.

`get_kline` served the database only when the rows carried `turnover`. No kline
channel publishes turnover — Sina and Tencent both return six fields, date through
volume — so the condition was never true and **every** daily read abandoned the 1.7M
stored rows for a live fetch. A column being unavailable says nothing about the rest
of the bar, so it is now reported rather than required.

`change_pct` was then derived from the series, which surfaced a second defect: 99
rows store a bare date where the column is a timestamp, and under
`PRIMARY KEY (time, code)` a date and its timestamped form are two different keys, so
both survive for the same day. Ordering on the raw column interleaves them, and the
derived changes came out near ±100%.

The derivation is exercised against a real SQLite table built to contain that trap,
because the failure it guards is a query-order bug and only running the query shows it.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture(scope="module")
def sync_module():
    spec = importlib.util.spec_from_file_location(
        "sync_market_fundamentals_under_test", REPO_ROOT / "scripts" / "sync_market_fundamentals.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCHEMA = """
CREATE TABLE kline_1d (
  time TEXT NOT NULL, code TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume INTEGER NOT NULL, amount REAL, turnover REAL, change_pct REAL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (time, code));
CREATE TABLE stocks (stock_code TEXT, stock_name TEXT);
"""


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO kline_1d (time, code, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn


def _at(code, day, close, volume=1000):
    """A canonical bar."""
    return (f"{day}T15:00:00+08:00", code, close, close, close, close, volume)


class TestBoardLimits:
    def test_the_limit_is_board_specific(self, sync_module):
        assert sync_module._price_limit_percent("600519", "贵州茅台") == 10.0
        assert sync_module._price_limit_percent("300750", "宁德时代") == 20.0
        assert sync_module._price_limit_percent("688981", "中芯国际") == 20.0
        assert sync_module._price_limit_percent("920000", "某北交所") == 30.0
        assert sync_module._price_limit_percent("600519", "ST茅台") == 5.0


class TestDerivation:
    def test_a_plain_series_derives_the_obvious_change(self, sync_module):
        conn = _db([_at("X", "2026-01-05", 100.0), _at("X", "2026-01-06", 110.0)])
        result = sync_module.backfill_kline_change_pct(conn, apply=True)
        got = conn.execute("SELECT change_pct FROM kline_1d WHERE time LIKE '2026-01-06%'").fetchone()[0]
        assert got == pytest.approx(10.0)
        assert result["null_first_bar"] == 1, "the first bar has no predecessor"

    def test_the_first_bar_is_null_not_zero(self, sync_module):
        """0.0 would read as 'this bar did not move', which is a different fact."""
        conn = _db([_at("X", "2026-01-05", 100.0), _at("X", "2026-01-06", 110.0)])
        sync_module.backfill_kline_change_pct(conn, apply=True)
        first = conn.execute("SELECT change_pct FROM kline_1d WHERE time LIKE '2026-01-05%'").fetchone()[0]
        assert first is None

    def test_a_bare_date_does_not_interleave_two_series(self, sync_module):
        """The defect that produced ±100% changes.

        The same day carries both `2026-06-15` and `2026-06-15T15:00:00+08:00`. Sorted
        on the raw column the bare date comes first, so the two prices alternate and
        every step looks like a halving.
        """
        conn = _db([
            _at("X", "2026-06-12", 150.0),
            _at("X", "2026-06-15", 151.0),
            ("2026-06-15", "X", 75.0, 75.0, 75.0, 75.0, 900000),   # bare date, same day
            _at("X", "2026-06-16", 152.0),
        ])
        sync_module.backfill_kline_change_pct(conn, apply=True)
        rows = dict(conn.execute(
            "SELECT time, change_pct FROM kline_1d WHERE code='X' ORDER BY time"))
        assert rows["2026-06-15T15:00:00+08:00"] == pytest.approx((151.0 - 150.0) / 150.0 * 100)
        assert rows["2026-06-16T15:00:00+08:00"] == pytest.approx((152.0 - 151.0) / 151.0 * 100)
        assert rows["2026-06-15"] is None, "the duplicate date is excluded, not interleaved"

    def test_a_row_excluded_from_the_derivation_loses_any_stale_value(self, sync_module):
        """A value computed from an interleaved order must not survive the fix."""
        conn = _db([
            _at("X", "2026-06-12", 150.0),
            _at("X", "2026-06-15", 151.0),
            ("2026-06-15", "X", 75.0, 75.0, 75.0, 75.0, 900000),
        ])
        conn.execute("UPDATE kline_1d SET change_pct = -50.0 WHERE length(time) = 10")
        conn.commit()
        sync_module.backfill_kline_change_pct(conn, apply=True)
        assert conn.execute("SELECT change_pct FROM kline_1d WHERE length(time)=10").fetchone()[0] is None

    def test_a_beyond_limit_move_is_counted_not_corrected(self, sync_module):
        """On unadjusted prices an ex-dividend gap exceeds the board limit.

        The number is the real change of the stored series; reporting it is the point.
        """
        conn = _db([_at("X", "2026-01-05", 100.0), _at("X", "2026-01-06", 60.0)])
        result = sync_module.backfill_kline_change_pct(conn, apply=True)
        assert result["beyond_daily_limit"] == 1
        assert conn.execute("SELECT change_pct FROM kline_1d WHERE time LIKE '2026-01-06%'").fetchone()[0] == pytest.approx(-40.0)

    def test_the_basis_is_declared_unadjusted(self, sync_module):
        conn = _db([_at("X", "2026-01-05", 100.0), _at("X", "2026-01-06", 101.0)])
        result = sync_module.backfill_kline_change_pct(conn, apply=True)
        assert result["basis"] == "unadjusted"
        assert "beyond_daily_limit_note" in result


class TestTheKlineGate:
    def test_the_open_path_does_not_mention_turnover(self):
        """The gate was one boolean expression; that is what regressed.

        Kept as a source assertion deliberately narrow in scope: the expression must
        not require a column that no channel publishes. A live-database test would
        verify the same thing while also depending on the runtime database.
        """
        text = (REPO_ROOT / "packages/akshare-mcp/src/akshare_mcp/tools/market/kline/__init__.py").read_text()
        assert "db_is_fresh and _kline_rows_usable(validated_results)" in text
        assert "db_is_fresh and (has_turnover" not in text
        assert "not_published_by_any_kline_channel" in text
