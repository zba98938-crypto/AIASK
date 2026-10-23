"""Behaviour tests for WP-0.9 stuck-outcome triage.

The measured failure mode: 600 outcomes carried ``pending_market_data`` for
sessions whose bar simply had not been synced, and nothing distinguished that
from a bar that will never arrive. These tests pin the distinction.
"""

from __future__ import annotations

from datetime import date

import pytest

from akshare_mcp.services.incubation_factory.outcome_retry import (
    REASON_BAR_MISSING_FINAL,
    REASON_SYNC_STALLED,
    StuckOutcome,
    apply_classification,
    classify,
    latest_bar_date,
    list_stuck_outcomes,
)


def _stuck(target: str | None, attempts: int = 0) -> StuckOutcome:
    return StuckOutcome(
        outcome_id="o1",
        prediction_id="p1",
        strategy_id="s1",
        stock_code="600519",
        target_trading_date=date.fromisoformat(target) if target else None,
        attempts=attempts,
    )


def test_target_beyond_latest_bar_is_late_data_not_missing() -> None:
    action, reason = classify(_stuck("2026-10-20"), latest_bar=date(2026, 10, 16))
    assert action == "keep"
    assert reason == REASON_SYNC_STALLED


def test_a_handful_of_rows_is_not_a_synced_session() -> None:
    """Measured: 2026-10-20 has 4 index rows; the market sync stopped on 10-16."""
    action, reason = classify(
        _stuck("2026-10-20"),
        latest_bar=date(2026, 10, 20),
        market_codes_on_target=4,
        last_synced_session=date(2026, 10, 16),
    )
    assert action == "keep"
    assert reason == REASON_SYNC_STALLED


def test_code_series_stopping_early_is_a_code_gap_not_a_market_gap() -> None:
    action, reason = classify(
        _stuck("2026-10-15"),
        latest_bar=date(2026, 10, 16),
        code_latest_bar=date(2026, 9, 1),
        market_codes_on_target=5500,
        last_synced_session=date(2026, 10, 16),
    )
    assert action == "keep"
    assert reason == "code_series_stale"


def test_target_inside_stored_range_without_a_bar_is_final_after_retries() -> None:
    action, reason = classify(
        _stuck("2026-10-15", attempts=4),
        latest_bar=date(2026, 10, 16),
        code_latest_bar=date(2026, 10, 16),
        market_codes_on_target=5500,
        max_attempts=5,
    )
    assert action == "finalize"
    assert reason == REASON_BAR_MISSING_FINAL


def test_target_inside_range_but_retries_left_stays_retryable() -> None:
    action, reason = classify(
        _stuck("2026-10-15", attempts=1),
        latest_bar=date(2026, 10, 16),
        code_latest_bar=date(2026, 10, 16),
        market_codes_on_target=5500,
        max_attempts=5,
    )
    assert action == "keep"
    assert reason == "retry_scheduled"


def test_missing_target_date_is_final_immediately() -> None:
    action, reason = classify(_stuck(None), latest_bar=date(2026, 10, 16))
    assert action == "finalize"
    assert reason == "target_trading_date_missing"


class _DB:
    def __init__(self, latest="2026-10-16", rows=None):
        self.latest = latest
        self.rows = rows or []
        self.updates: list[tuple] = []

    async def fetch(self, sql, *args):
        if "MAX(substr(time" in sql:
            return [{"latest": self.latest}]
        if "FROM strategy_trade_prediction_outcomes" in sql and "pending_market_data" in sql:
            return list(self.rows)
        if "FROM strategy_trade_predictions" in sql:
            return [{"prediction_id": "p1", "target_trading_date": "2026-10-20"}]
        return []

    async def execute(self, sql, *args):
        self.updates.append(args)
        return 1


@pytest.mark.asyncio
async def test_latest_bar_date_reads_the_store() -> None:
    assert await latest_bar_date(_DB()) == date(2026, 10, 16)
    assert await latest_bar_date(object()) is None


@pytest.mark.asyncio
async def test_apply_classification_keeps_late_rows_pending() -> None:
    db = _DB(rows=[{"outcome_id": "o1", "prediction_id": "p1", "strategy_id": "s1", "stock_code": "600519", "score_status": "pending_market_data", "metadata": "{}"}])
    stuck = await list_stuck_outcomes(db)
    assert stuck[0].target_trading_date == date(2026, 10, 20)
    result = await apply_classification(db, latest_bar=date(2026, 10, 16))
    assert result["sync_stalled"] == 1
    assert result["finalized"] == 0
    assert result["latest_bar"] == "2026-10-16"


@pytest.mark.asyncio
async def test_apply_classification_finalizes_missing_bars() -> None:
    db = _DB(
        latest="2026-10-16",
        rows=[{"outcome_id": "o1", "prediction_id": "p1", "strategy_id": "s1", "stock_code": "600519", "score_status": "pending_market_data", "metadata": '{"retry_attempts": 4}'}],
    )
    class _LateDB(_DB):
        async def fetch(self, sql, *args):
            if "FROM strategy_trade_predictions" in sql:
                return [{"prediction_id": "p1", "target_trading_date": "2026-10-15"}]
            return await super().fetch(sql, *args)

    result = await apply_classification(_LateDB(latest="2026-10-16", rows=db.rows), latest_bar=date(2026, 10, 16))
    assert result["finalized"] == 1
    assert result["reason_counts"][REASON_BAR_MISSING_FINAL] == 1
