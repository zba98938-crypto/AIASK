"""``factor_date`` must be a session that exists, not the day the job ran.

It was ``datetime.now().date()``. Run on a weekend or a holiday that writes a date
no bar carries, so any join from factors to bars over the trading calendar matches
nothing — 2026-10-18 is a Sunday and will never appear in ``kline_1d``.

Deriving it from the newest bar also makes a re-run idempotent: the same
``(stock_code, factor_date, factor_name)`` keys are upserted until a new session
actually produces bars.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from akshare_mcp.tools.managers.quant_mgr_classic import _factor_date_from_bars


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *_args, **_kwargs):
        return self.rows


class _Db:
    def __init__(self, rows=(), raises: Exception | None = None):
        self.rows = list(rows)
        self.raises = raises

    def acquire(self):
        if self.raises is not None:
            raise self.raises
        return _Acquire(_Conn(self.rows))


def _resolve(db) -> datetime.date:
    return asyncio.run(_factor_date_from_bars(db))


TODAY = datetime.datetime.now().date()


class TestUsesTheBars:
    def test_a_weekend_run_uses_the_last_session(self):
        """The case that motivated this: today is 2026-10-18, a Sunday.

        The newest bar is Friday's, so that is the date the factors describe.
        """

        assert _resolve(_Db([{"latest": "2026-10-16"}])) == datetime.date(2026, 10, 16)

    def test_a_monday_pre_open_run_still_uses_friday(self):
        """Idempotent: until a session produces bars, the date does not move."""

        assert _resolve(_Db([{"latest": "2026-10-16"}])) == datetime.date(2026, 10, 16)

    def test_the_row_shape_does_not_matter(self):
        """A tuple row is read the same way as a mapping."""

        assert _resolve(_Db([("2026-10-16",)])) == datetime.date(2026, 10, 16)

    def test_a_timestamp_with_a_time_part_is_accepted(self):
        assert _resolve(_Db([{"latest": "2026-10-16T15:00:00"}])) == datetime.date(2026, 10, 16)


class TestFallsBackOnlyWhenThereIsNothingBetter:
    @pytest.mark.parametrize(
        "label, rows",
        [
            ("no bars at all", []),
            ("a null max", [{"latest": None}]),
            ("an unparsable value", [{"latest": "not-a-date"}]),
        ],
    )
    def test_falls_back_to_the_wall_clock_date(self, label, rows):
        """Only when there is no session to point at — never silently."""

        assert _resolve(_Db(rows)) == TODAY, label

    def test_an_unreadable_database_falls_back_rather_than_failing_the_batch(self):
        """Losing the date is not worth discarding a whole factor run."""

        assert _resolve(_Db(raises=RuntimeError("db down"))) == TODAY


class TestTheBatchHandlerUsesIt:
    def test_a_persisted_batch_carries_the_bar_date_not_the_clock_date(self, monkeypatch):
        """End to end through the handler, with the clock deliberately elsewhere.

        The stub reports bars through 2026-10-16 while the wall clock says whatever
        it says today, so the two can only agree by accident on one day a week.
        """

        from akshare_mcp.tools.managers import quant_mgr_classic

        saved: dict[str, list] = {"batch": [], "single": []}

        class HandlerDb(_Db):
            def __init__(self):
                super().__init__([{"latest": "2026-10-16"}])

            async def get_klines(self, code, limit=252):
                # 80 bars: the momentum factor needs about 60 before it will report
                # anything, and `unmeasurable_counts` would otherwise record
                # `insufficient_history` and persist nothing — leaving this test
                # passing without ever reaching the date.
                return [
                    {"date": f"2026-01-{day:02d}", "close": 10.0 + day * 0.1}
                    for day in range(1, 29)
                ] + [
                    {"date": f"2026-02-{day:02d}", "close": 12.0 + day * 0.1}
                    for day in range(1, 29)
                ] + [
                    {"date": f"2026-03-{day:02d}", "close": 14.0 + day * 0.1}
                    for day in range(1, 25)
                ]

            async def get_financials(self, code, limit=8):
                return []

            async def save_factor_values_batch(self, buffer):
                saved["batch"].extend(buffer)

            async def save_factor_values(self, code, factor_date, values):
                saved["single"].append((code, factor_date, values))

        monkeypatch.setattr(quant_mgr_classic, "_load_valuation_snapshot", lambda db, code: _noop())

        result = asyncio.run(
            quant_mgr_classic.handle_batch_compute_factors(
                kw={
                    "codes": ["600519"],
                    "factors": ["momentum"],
                    "persist": True,
                    "compute_ic": False,
                    "max_codes": 1,
                },
                ok=lambda data, **kwargs: {"success": True, "data": data, **kwargs},
                fail=lambda message: {"success": False, "error": message},
                get_db_fn=HandlerDb,
            )
        )

        assert result.get("success") is True, result
        rows = saved["batch"] or [
            {"stock_code": code, "factor_date": date, "values": values}
            for code, date, values in saved["single"]
        ]
        assert rows, "nothing was persisted, so the date was never exercised"
        assert {str(row["factor_date"]) for row in rows} == {"2026-10-16"}


async def _noop():
    return None
