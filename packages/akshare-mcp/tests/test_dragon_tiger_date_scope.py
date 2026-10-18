"""``get_dragon_tiger(date=...)`` must mean that date.

The DB fallback queried ``WHERE trade_date <= $1 ... LIMIT 200``, so asking for one
session returned that session plus every earlier one, capped at 200 rows — for
2026-10-16 that was 200 rows spanning three sessions where the table holds 66. The
rows also arrived without their own date, since the output mapping dropped the
column the query selected, so nothing in the response let a caller notice.

The fake connection below interprets the date predicate the way a database would,
which is what makes these assertions about behaviour rather than about SQL text.
"""

from __future__ import annotations

import pytest

from akshare_mcp.tools import fund_flow_market


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    """Applies the date predicate and the row limit, like a database would."""

    def __init__(self, rows: list[dict], default_date: str | None = None):
        self.rows = rows
        self.default_date = default_date
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *params):
        self.queries.append((sql, params))
        if "FROM dragon_tiger" not in sql:
            return []  # the secondary table is not populated in this fixture
        if self.default_date is not None and not params:
            return self.rows
        wanted = str(params[0]) if params else ""
        if "trade_date <= $1" in sql:
            matched = [r for r in self.rows if str(r["trade_date"]) <= wanted]
        else:
            matched = [r for r in self.rows if str(r["trade_date"]) == wanted]
        if "code = $2" in sql and len(params) > 1:
            matched = [r for r in matched if str(r["code"]) == str(params[1])]
        return matched


def _db(rows: list[dict], default_date: str | None = None):
    class Db:
        def __init__(self):
            self.conn = _FakeConn(rows, default_date)

        def acquire(self):
            return _FakeAcquire(self.conn)

    return Db()


ROWS = [
    {"code": "603259", "trade_date": "2026-10-16", "reason": "振幅达到15%", "buy_amount": 1.0, "sell_amount": 2.0, "net_amount": -1.0, "buyer_type": None},
    {"code": "600000", "trade_date": "2026-10-16", "reason": "换手率达到20%", "buy_amount": 3.0, "sell_amount": 1.0, "net_amount": 2.0, "buyer_type": None},
    {"code": "000001", "trade_date": "2026-10-15", "reason": "振幅达到15%", "buy_amount": 1.0, "sell_amount": 1.0, "net_amount": 0.0, "buyer_type": None},
    {"code": "000002", "trade_date": "2026-10-14", "reason": "振幅达到15%", "buy_amount": 1.0, "sell_amount": 1.0, "net_amount": 0.0, "buyer_type": None},
]


class TestExplicitDateIsExact:
    def test_only_the_requested_date_comes_back(self, monkeypatch):
        db = _db(ROWS)
        monkeypatch.setattr(fund_flow_market, "get_db", lambda: db)

        result = fund_flow_market.get_dragon_tiger(date="2026-10-16")
        codes = sorted(row["code"] for row in result["data"])

        assert codes == ["600000", "603259"], "an earlier session leaked into the result"

    def test_every_row_carries_its_own_date(self, monkeypatch):
        """A page of rows is unusable if a caller cannot tell the sessions apart."""

        monkeypatch.setattr(fund_flow_market, "get_db", lambda: _db(ROWS))

        result = fund_flow_market.get_dragon_tiger(date="2026-10-16")

        assert result["data"], "fixture returned nothing"
        assert {row["tradeDate"] for row in result["data"]} == {"2026-10-16"}

    def test_a_date_with_no_rows_returns_nothing_rather_than_an_earlier_day(self, monkeypatch):
        """The old window silently answered with 10-15 data for a 10-18 query."""

        monkeypatch.setattr(fund_flow_market, "get_db", lambda: _db(ROWS))

        result = fund_flow_market.get_dragon_tiger(date="2026-10-18")

        assert result["data"] == []

    def test_the_resolved_date_is_the_requested_one(self, monkeypatch):
        monkeypatch.setattr(fund_flow_market, "get_db", lambda: _db(ROWS))

        result = fund_flow_market.get_dragon_tiger(date="2026-10-15")

        assert result["resolved_date"] == "20261015"
        assert {row["code"] for row in result["data"]} == {"000001"}


class TestNoDateStillResolvesTheLatestSession:
    def test_an_empty_date_uses_the_latest_trading_day(self, monkeypatch):
        """"Give me the most recent page" must keep working."""

        monkeypatch.setattr(fund_flow_market, "get_db", lambda: _db(ROWS))
        monkeypatch.setattr(fund_flow_market, "get_latest_trading_date", lambda: "20261016")

        result = fund_flow_market.get_dragon_tiger(date="")

        assert sorted(row["code"] for row in result["data"]) == ["600000", "603259"]


class TestThePredicateItself:
    def test_the_query_does_not_ask_for_a_range(self, monkeypatch):
        """Direct assertion on the query, so the intent survives a refactor."""

        db = _db(ROWS)
        monkeypatch.setattr(fund_flow_market, "get_db", lambda: db)

        fund_flow_market.get_dragon_tiger(date="2026-10-16")

        statements = [sql for sql, _ in db.conn.queries]
        assert statements, "no query reached the database"
        assert not any("trade_date <= $1" in sql for sql in statements)
        assert any("trade_date = $1" in sql for sql in statements)

    def test_the_stock_filter_still_narrows_within_the_date(self, monkeypatch):
        db = _db(ROWS)
        monkeypatch.setattr(fund_flow_market, "get_db", lambda: db)

        result = fund_flow_market.get_dragon_tiger(date="2026-10-16", stock_code="603259")

        assert [row["code"] for row in result["data"]] == ["603259"]
