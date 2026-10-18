"""The built-in holiday fallback must agree with the market.

Neither optional calendar package is installed here, so `TradingCalendar` always
falls back to a hand-maintained holiday list — and `factory_scheduler` gates a whole
factory run on `is_trading_day`. A wrong answer is not cosmetic: the list had
`2026-10-08` as a holiday while the market traded 5,561 symbols that day, was missing
`2026-09-25` so a closed Friday read as open, and the fallback returned True for
调休 workdays, making four weekend days look like sessions.

Every expectation below is anchored to evidence the project already stores: a bar
exists only on a day the market traded, and the exchange published margin figures
for the dates quoted. `scripts/check_trading_calendar_agreement.py` re-derives the
whole comparison.
"""

from __future__ import annotations

import datetime

import pytest

from strategy_factory.domain.trading_calendar import TradingCalendar, get_trading_calendar


@pytest.fixture()
def calendar() -> TradingCalendar:
    """The built-in fallback specifically, not an optional package if one appears."""

    cal = TradingCalendar()
    cal._external_calendar = None
    return cal


class TestTheWorkdayOverride:
    """A 调休 workday is a working day for people, not a session for the exchange."""

    @pytest.mark.parametrize(
        "day",
        ["2026-02-14", "2026-02-28", "2026-09-27", "2026-10-10"],
    )
    def test_a_調休_weekend_is_not_a_trading_day(self, calendar, day):
        """All four are weekends the government designated as workdays.

        Measured: `kline_1d` holds no bar on any Saturday or Sunday in 2026, and the
        exchange's margin feed published nothing for 2026-10-10 or 2026-09-27.
        """

        assert calendar.is_trading_day(datetime.date.fromisoformat(day)) is False

    def test_an_ordinary_weekday_is_still_a_trading_day(self, calendar):
        assert calendar.is_trading_day(datetime.date(2026, 10, 16)) is True


class TestTheHolidayList:
    def test_a_listed_holiday_is_closed(self, calendar):
        assert calendar.is_trading_day(datetime.date(2026, 10, 1)) is False

    def test_the_first_day_back_after_national_day_is_open(self, calendar):
        """`2026-10-08` was listed as a holiday; the market traded.

        Evidence: 5,561 codes carry a bar that day, and the exchange's margin feed
        published figures for it. The National Day range runs to 10-07.
        """

        assert calendar.is_trading_day(datetime.date(2026, 10, 8)) is True

    def test_mid_autumn_2026_is_closed(self, calendar):
        """`2026-09-25` was absent from the list and read as open; no bar exists."""

        assert calendar.is_trading_day(datetime.date(2026, 9, 25)) is False


class TestNextTradingDay:
    def test_it_skips_a_weekend_and_lands_on_a_session(self, calendar):
        """2026-10-16 is a Friday, so the next session is Monday the 19th."""
        assert calendar.next_trading_day(datetime.date(2026, 10, 16)) == datetime.date(2026, 10, 19)

    def test_it_skips_a_调休_weekend_too(self, calendar):
        """2026-10-09 is a Friday; the 10th is a workday but not a session."""
        assert calendar.next_trading_day(datetime.date(2026, 10, 9)) == datetime.date(2026, 10, 12)


@pytest.mark.skip(reason="the cross-check lives in scripts/check_trading_calendar_agreement.py")
def test_cross_check_is_a_script_not_a_unit_test() -> None:
    """Deliberately not asserted here.

    Comparing the calendar against `kline_1d` needs the real database and is what
    `scripts/check_trading_calendar_agreement.py` does, with the arguments to run it
    on demand. A unit test that silently returns True on an empty database would be
    the "operational check disguised as a test" this project renamed eight files to
    avoid.
    """
