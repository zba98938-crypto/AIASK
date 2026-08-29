from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from aiask_quant_core.storage.sqlite import SQLiteAdapter

from akshare_mcp.services.signal_tracker import SignalTracker


class _ForwardReturnDb:
    def __init__(self) -> None:
        self.get_kline_calls: list[tuple[str, dict]] = []
        self.saved_klines: list[dict] = []
        self.saved_forward_returns: list[dict] = []

    async def get_klines(self, code: str, **kwargs):
        self.get_kline_calls.append((code, dict(kwargs)))
        return []

    async def save_klines(self, code: str, rows: list[dict]):
        self.saved_klines.extend(dict(row) for row in rows)
        return {"accepted_count": len(rows), "rejected_count": 0, "accept_ratio": 1.0}

    async def save_forward_returns_batch(self, rows: list[dict]) -> int:
        self.saved_forward_returns.extend(dict(row) for row in rows)
        return len(rows)


@pytest.mark.asyncio
async def test_forward_backfill_uses_latest_bar_date_metadata_and_refreshes_provider(monkeypatch):
    tracker = SignalTracker()
    db = _ForwardReturnDb()
    provider_rows = [
        {
            "date": "2026-06-03",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.0,
            "volume": 1000,
        },
        {
            "date": "2026-06-04",
            "open": 10.0,
            "high": 11.2,
            "low": 9.9,
            "close": 11.0,
            "volume": 1000,
        },
    ]
    fetch_provider = AsyncMock(return_value=provider_rows)
    monkeypatch.setattr(tracker, "_fetch_provider_klines", fetch_provider)

    saved = await tracker._compute_forward_returns_batch(
        db,
        [
            {
                "id": 7,
                "strategy_id": "strategy-1",
                "signal_date": date(2026, 6, 15),
                "code": "601288",
                "signal": 1,
                "signal_metadata": {"latest_bar_date": "2026-06-03"},
            }
        ],
        forward_days=1,
    )

    assert saved == 1
    assert db.get_kline_calls[0] == ("601288", {"start_date": "2026-06-03"})
    assert db.saved_klines[0]["code"] == "601288"
    assert db.saved_forward_returns == [
        {"signal_id": 7, "forward_days": 1, "actual_return": pytest.approx(0.1)}
    ]
    fetch_provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_forward_backfill_aligns_non_trading_signal_date_to_previous_bar(monkeypatch):
    tracker = SignalTracker()
    db = _ForwardReturnDb()
    provider_rows = [
        {
            "date": "2026-06-05",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.0,
            "volume": 1000,
        },
        {
            "date": "2026-06-08",
            "open": 10.0,
            "high": 11.2,
            "low": 9.9,
            "close": 11.0,
            "volume": 1000,
        },
    ]
    fetch_provider = AsyncMock(return_value=provider_rows)
    monkeypatch.setattr(tracker, "_fetch_provider_klines", fetch_provider)

    saved = await tracker._compute_forward_returns_batch(
        db,
        [
            {
                "id": 8,
                "strategy_id": "strategy-1",
                "signal_date": date(2026, 6, 6),
                "code": "601288",
                "signal": 1,
            }
        ],
        forward_days=1,
    )

    assert saved == 1
    assert db.saved_forward_returns == [
        {"signal_id": 8, "forward_days": 1, "actual_return": pytest.approx(0.1)}
    ]


def test_forward_base_index_never_uses_future_bar():
    tracker = SignalTracker()

    resolved = tracker._resolve_forward_base_index(
        [(date(2026, 6, 8), 11.0)],
        date(2026, 6, 6),
    )

    assert resolved is None


def test_signal_record_date_prefers_latest_bar_date_metadata():
    tracker = SignalTracker()

    resolved = tracker._resolve_signal_record_date(
        {"signal_metadata": {"latest_bar_date": "2026-06-03"}, "signal_date": "2026-06-15"}
    )

    assert resolved == date(2026, 6, 3)


@pytest.mark.asyncio
async def test_signal_stats_excludes_exit_actions_from_predictive_hit_rate(initialized_db):
    adapter = SQLiteAdapter(path=initialized_db)
    await adapter.initialize()
    try:
        await adapter.save_signals(
            "strategy-long-only",
            date(2026, 1, 1),
            [
                {"code": "000001", "signal": 1, "score": 1.0, "event_action": "enter"},
                {"code": "000002", "signal": -1, "score": -1.0, "event_action": "exit"},
            ],
        )
        await adapter.save_signals(
            "strategy-long-only",
            date(2026, 1, 2),
            [
                {"code": "000003", "signal": 1, "score": 1.0, "event_action": "enter"},
                {"code": "000004", "signal": -1, "score": -1.0, "event_action": "exit"},
            ],
        )
        async with adapter.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, event_action FROM strategy_signals WHERE strategy_id=$1 ORDER BY id",
                "strategy-long-only",
            )
        for row in rows:
            await adapter.save_forward_returns(
                int(row["id"]),
                5,
                0.02,
            )

        stats = await adapter.get_signal_stats("strategy-long-only")
    finally:
        await adapter.close()

    assert stats["total_signals"] == 2
    assert stats["sample_count"][5] == 2
    assert stats["hit_count"][5] == 2
    assert stats["miss_count"][5] == 0
