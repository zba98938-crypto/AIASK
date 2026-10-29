"""``as_of`` on the decision tools must filter, and its audit label must be true.

Two defects this pins:

1. ``should_i_sell`` accepted ``as_of`` and threw it away — the underlying
   implementation did not take the parameter at all — while the wrapper stamped
   ``meta.pit_guard.active=True`` and
   ``meta.time_precision="historical_eod_close_as_of"``. Callers got a decision
   computed from the latest bars carrying a point-in-time audit claim.
   ``should_i_buy`` filtered correctly, so the two disagreed.

2. ``_attach_pit_meta`` raised ``ValueError`` on an unparseable ``as_of``, which
   left the MCP layer to surface ``UnexpectedToolError`` instead of a structured
   failure — a bad date looked like a crash.

A false PIT label is worse than a missing one: it tells an auditor the run was
verified against history when nothing was filtered.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta

import pytest

from akshare_mcp.tools import decision


_CODE = "000001"
_LAST_BAR = date(2026, 6, 30)
_BARS = 100


def _weekdays_ending(last: date, count: int) -> list[date]:
    days: list[date] = []
    day = last
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


@pytest.fixture(autouse=True)
def _seeded_store(initialized_db):
    """A store holding 100 weekday bars of 000001 ending 2026-06-30.

    These tests used to read whatever the operator's live database held and
    skipped when it held nothing, so under an isolated HOME the PIT guard was
    never exercised. ``initialized_db`` builds the real schema in tmp_path
    and points the adapter at it; with fixed bars every assertion below runs.
    """
    conn = sqlite3.connect(initialized_db)
    conn.execute(
        "INSERT INTO stocks (stock_code, stock_name, market, industry) VALUES (?, ?, ?, ?)",
        (_CODE, "平安银行", "SZ", "银行"),
    )
    rows = []
    for i, day in enumerate(_weekdays_ending(_LAST_BAR, _BARS)):
        close = round(10.0 + 0.01 * i + 0.4 * math.sin(i / 6.0), 2)
        rows.append(
            (f"{day.isoformat()}T15:00:00+08:00", _CODE, close - 0.05, close + 0.1, close - 0.1, close, 1_000_000 + i * 1000, close * 1_000_000)
        )
    conn.executemany(
        "INSERT INTO kline_1d (time, code, open, high, low, close, volume, amount) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return initialized_db


def _guard(envelope: dict) -> dict:
    return dict((envelope.get("meta") or {}).get("pit_guard") or {})


def _precision(envelope: dict) -> str | None:
    return (envelope.get("meta") or {}).get("time_precision")


def _analysis_date(envelope: dict) -> str:
    return str((envelope.get("data") or {}).get("analysis_date") or "")


async def _require_success(fn, **kwargs) -> dict:
    result = await fn(**kwargs)
    assert result.get("success"), f"seeded store should serve {_CODE}: {result.get('error')}"
    return result


@pytest.mark.asyncio
class TestAsOfIsActuallyApplied:
    async def test_sell_as_of_moves_the_analysis_date_back(self) -> None:
        latest = await _require_success(decision.should_i_sell, code=_CODE, buy_price=10.0)
        latest_date = _analysis_date(latest)
        assert latest_date == _LAST_BAR.isoformat()

        earlier_day = f"{latest_date[:8]}01"
        historical = await decision.should_i_sell(
            code=_CODE, buy_price=10.0, as_of=earlier_day
        )
        assert historical.get("success"), historical.get("error")

        # The decision must be computed from bars at or before as_of.
        assert _analysis_date(historical) <= earlier_day
        assert _analysis_date(historical) != latest_date

    async def test_sell_reports_the_rows_it_dropped(self) -> None:
        latest = await _require_success(decision.should_i_sell, code=_CODE, buy_price=10.0)
        earlier_day = f"{_analysis_date(latest)[:8]}01"
        historical = await decision.should_i_sell(
            code=_CODE, buy_price=10.0, as_of=earlier_day
        )
        assert historical.get("success"), historical.get("error")

        guard = _guard(historical)
        assert guard.get("active") is True
        assert guard.get("applied_as_of") == earlier_day
        # June 2026 has 22 weekdays; every one after June 1 is in the future of as_of.
        assert guard.get("dropped_future_rows") == 21

    async def test_buy_and_sell_agree_on_the_guard_contract(self) -> None:
        latest = await _require_success(decision.should_i_sell, code=_CODE, buy_price=10.0)
        earlier_day = f"{_analysis_date(latest)[:8]}01"

        sell = await decision.should_i_sell(code=_CODE, buy_price=10.0, as_of=earlier_day)
        buy = await decision.should_i_buy(code=_CODE, as_of=earlier_day)
        assert sell.get("success"), sell.get("error")
        assert buy.get("success"), buy.get("error")

        for envelope in (sell, buy):
            guard = _guard(envelope)
            assert guard.get("active") is True
            assert guard.get("applied_as_of") == earlier_day
            assert _precision(envelope) == "historical_eod_close_as_of"


@pytest.mark.asyncio
class TestLabelMatchesReality:
    async def test_no_as_of_is_not_labelled_point_in_time(self) -> None:
        result = await _require_success(decision.should_i_sell, code=_CODE, buy_price=10.0)
        assert _guard(result).get("active") is False
        assert _precision(result) == "historical_eod_close"

    async def test_the_guard_is_never_upgraded_to_active_by_the_wrapper(self) -> None:
        # A body that reports no filtering must not come back labelled active
        # just because the caller passed a date string.
        envelope = decision._attach_pit_meta(
            {"success": True, "data": {}}, as_of="2026-07-10"
        )
        guard = _guard(envelope)
        assert guard.get("active") is False
        assert guard.get("unenforced") is True
        assert guard.get("reason") == "tool_did_not_report_as_of_filtering"
        assert _precision(envelope) == "historical_eod_close"

    async def test_a_body_reported_guard_is_preserved_verbatim(self) -> None:
        body_guard = {
            "active": True,
            "requested_as_of": "2026-07-10",
            "applied_as_of": "2026-07-10",
            "filtered_rows": 94,
            "dropped_future_rows": 6,
        }
        envelope = decision._attach_pit_meta(
            {"success": True, "data": {"pit_guard": body_guard}}, as_of="2026-07-10"
        )
        assert _guard(envelope)["dropped_future_rows"] == 6
        assert _precision(envelope) == "historical_eod_close_as_of"


@pytest.mark.asyncio
class TestUnparseableAsOf:
    @pytest.mark.parametrize("bad", ["not-a-date", "2026-13-45", "yesterday"])
    async def test_sell_returns_a_structured_failure(self, bad: str) -> None:
        result = await decision.should_i_sell(code=_CODE, buy_price=10.0, as_of=bad)
        assert result["success"] is False
        assert "as_of" in str(result.get("error"))

    @pytest.mark.parametrize("bad", ["not-a-date", "2026-13-45"])
    async def test_buy_returns_a_structured_failure(self, bad: str) -> None:
        result = await decision.should_i_buy(code=_CODE, as_of=bad)
        assert result["success"] is False
        assert "as_of" in str(result.get("error"))

    async def test_a_bad_date_never_raises_through_the_tool_boundary(self) -> None:
        # It used to raise ValueError, which the MCP layer turned into
        # UnexpectedToolError — indistinguishable from a real crash.
        for fn, kwargs in (
            (decision.should_i_sell, {"code": _CODE, "buy_price": 10.0}),
            (decision.should_i_buy, {"code": _CODE}),
        ):
            result = await fn(as_of="garbage", **kwargs)
            assert isinstance(result, dict)
            assert result["success"] is False
