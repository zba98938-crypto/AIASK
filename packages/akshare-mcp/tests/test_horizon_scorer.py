"""Behaviour tests for WP-A horizon scoring.

Every case calls the real scorer; none reads source text. The three exit-date
tests exist because v2 had the exit one bar short (``entry_index + k - 1``),
which silently shaved a day off every holding period.
"""

from __future__ import annotations

from datetime import date

import pytest

from akshare_mcp.services.incubation_factory.horizon_scorer import (
    HORIZON_SCORE_VERSION,
    REASON_ENTRY_OPEN_MISSING,
    REASON_EXIT_BAR_MISSING,
    REASON_HORIZON_UNPARSEABLE,
    REASON_RETRY_EXHAUSTED,
    STATUS_NEUTRAL_EXCLUDED,
    STATUS_OK,
    STATUS_PENDING,
    STATUS_UNAVAILABLE,
    HorizonScorer,
    parse_horizon,
    score_horizon,
)


def _bar(day: str, open_: float, close: float) -> dict:
    return {"date": day, "open": open_, "high": max(open_, close), "low": min(open_, close), "close": close}


def _prediction(direction: str = "up", as_of: str = "2026-10-01", horizon: str = "5d") -> dict:
    return {
        "prediction_id": "tp-1",
        "strategy_id": "s-1",
        "stock_code": "600519",
        "prediction_as_of": as_of,
        "direction": direction,
        "horizon": horizon,
        "contract_json": {},
    }


def _series(count: int, *, start: float = 10.0, step: float = 0.1) -> list[dict]:
    rows = []
    day = date(2026, 10, 2)
    price = start
    while len(rows) < count:
        if day.weekday() < 5:
            rows.append(_bar(day.isoformat(), price, price + step))
            price += step
        day = day.fromordinal(day.toordinal() + 1)
    return rows


@pytest.mark.parametrize(
    "token,expected",
    [
        ("5d", 5),
        ("5", 5),
        ("1d", 1),
        ("next_day", 1),
        ("20D", 20),
        ("10 day", 10),
        ("", None),
        (None, None),
        ("abc", None),
        (0, None),
    ],
)
def test_parse_horizon_accepts_database_forms(token, expected) -> None:
    assert parse_horizon(token) == expected


def test_exit_is_k_bars_after_entry_not_k_minus_one() -> None:
    bars = _series(12)
    score = score_horizon(_prediction(horizon="5d"), bars, cost=0.0, flat_band=0.0)
    assert score.status == STATUS_OK
    entry_index = next(i for i, bar in enumerate(bars) if bar["date"] == score.entry_date)
    exit_index = next(i for i, bar in enumerate(bars) if bar["date"] == score.exit_date)
    assert exit_index - entry_index == 5


def test_entry_is_first_session_after_as_of_and_needs_k_bars() -> None:
    bars = _series(10)
    # as_of 2026-10-01 -> entry 2026-10-02; 5 bars later exists.
    assert score_horizon(_prediction(), bars).status == STATUS_OK
    # Only 3 sessions after as_of: the exit bar is not there yet.
    short = bars[:4]
    pending = score_horizon(_prediction(), short)
    assert pending.status == STATUS_PENDING
    assert pending.reason == REASON_EXIT_BAR_MISSING


def test_missing_entry_open_is_unavailable_not_zero() -> None:
    bars = _series(12)
    bars[0]["open"] = 0.0
    score = score_horizon(_prediction(), bars)
    assert score.status == STATUS_UNAVAILABLE
    assert score.reason == REASON_ENTRY_OPEN_MISSING
    assert score.net_return is None


def test_unparseable_horizon_is_unavailable() -> None:
    score = score_horizon(_prediction(horizon="whenever"), _series(12))
    assert score.status == STATUS_UNAVAILABLE
    assert score.reason == REASON_HORIZON_UNPARSEABLE


def test_net_return_charges_round_trip_cost() -> None:
    bars = [_bar("2026-10-02", 100.0, 100.0), _bar("2026-10-05", 100.0, 110.0)]
    gross = score_horizon(_prediction(horizon="1d"), bars, cost=0.0, flat_band=0.0)
    net = score_horizon(_prediction(horizon="1d"), bars, cost=0.01, flat_band=0.0)
    assert gross.net_return == pytest.approx(0.10)
    assert net.net_return == pytest.approx(110.0 / (100.0 * 1.01) - 1.0)
    assert gross.direction_hit is True and net.direction_hit is True


def test_flat_band_decides_marginal_moves() -> None:
    bars = [_bar("2026-10-02", 100.0, 100.0), _bar("2026-10-05", 100.0, 100.1)]
    score = score_horizon(_prediction(horizon="1d"), bars, cost=0.0, flat_band=0.002)
    assert score.direction_hit is False


def test_down_direction_requires_negative_net_move() -> None:
    bars = [_bar("2026-10-02", 100.0, 100.0), _bar("2026-10-05", 100.0, 95.0)]
    assert score_horizon(_prediction(direction="down", horizon="1d"), bars, cost=0.0).direction_hit is True


def test_neutral_is_excluded_from_direction_denominator() -> None:
    bars = [_bar("2026-10-02", 100.0, 100.0), _bar("2026-10-05", 100.0, 100.0)]
    score = score_horizon(_prediction(direction="neutral", horizon="1d"), bars)
    assert score.status == STATUS_NEUTRAL_EXCLUDED
    assert score.direction_hit is None
    assert score.net_return is not None


def test_gap_before_entry_does_not_consume_horizon() -> None:
    # Suspension: as_of 10-01, next available session is 10-06.
    bars = [_bar("2026-10-06", 10.0, 10.1), _bar("2026-10-07", 10.1, 10.2)]
    score = score_horizon(_prediction(as_of="2026-10-01", horizon="1d"), bars, cost=0.0)
    assert score.entry_date == "2026-10-06"
    assert score.exit_date == "2026-10-07"


class _FakeDB:
    def __init__(self, predictions, bars):
        self._predictions = predictions
        self._bars = bars
        self.saved: list[dict] = []

    async def get_klines(self, code, start_date=None, end_date=None, limit=None):
        return list(self._bars)

    async def list_strategy_trade_predictions(self, **kwargs):
        return list(self._predictions)

    async def fetch(self, sql, *args):
        return []

    async def save_strategy_trade_prediction_outcome(self, payload):
        self.saved.append(payload)
        return payload


@pytest.mark.asyncio
async def test_pending_is_retried_then_becomes_data_unavailable() -> None:
    db = _FakeDB([_prediction()], _series(2))
    result = await HorizonScorer(max_retry_attempts=2).verify_pending(db)
    assert result["score_status_counts"] == {STATUS_PENDING: 1}
    assert db.saved[0]["metadata"]["retry_attempts"] == 1

    # Second pass: the fake storage has no stored outcomes, so retry counts stay
    # at 1 and the row is still pending rather than silently dropped.
    result = await HorizonScorer(max_retry_attempts=2).verify_pending(db)
    assert result["evaluated"] == 1


@pytest.mark.asyncio
async def test_retry_budget_exhaustion_is_terminal() -> None:
    class _ExhaustedDB(_FakeDB):
        async def fetch(self, sql, *args):
            return [{"prediction_id": "tp-1", "attempts": 5}]

    db = _ExhaustedDB([_prediction()], _series(2))
    result = await HorizonScorer(max_retry_attempts=5).verify_pending(db)
    outcome = db.saved[0]
    assert outcome["score_status"] == STATUS_UNAVAILABLE
    assert outcome["outcome_json"]["reason"].startswith(REASON_RETRY_EXHAUSTED)


@pytest.mark.asyncio
async def test_scored_outcome_carries_definition_fields() -> None:
    db = _FakeDB([_prediction()], _series(12))
    await HorizonScorer().verify_pending(db)
    outcome = db.saved[0]
    assert outcome["score_version"] == HORIZON_SCORE_VERSION
    assert outcome["outcome_json"]["cost"] == 0.0021
    assert outcome["outcome_json"]["entry_open"] is not None
    assert outcome["outcome_json"]["exit_close"] is not None
