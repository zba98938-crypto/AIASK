from __future__ import annotations

import asyncio
from datetime import date

from strategy_factory.runtime.provider_adapters import LegacyIncubationProvider


class _Db:
    def __init__(self) -> None:
        self.saved: list[dict] = []

    async def get_signals(self, _strategy_id, start_date=None, end_date=None, limit=100):
        # The provider reported a wall-clock Saturday (10-24), while the
        # persisted ledger's latest closed signal is 10-21.
        if start_date and start_date > date(2026, 10, 21):
            return []
        return [{"code": "000001", "signal": 1, "signal_date": "2026-10-21"}]

    async def get_klines(self, _code, limit=400):
        return [
            {"date": "2026-10-20", "open": 10, "high": 11, "low": 9, "close": 10},
            {"date": "2026-10-21", "open": 10, "high": 11, "low": 9, "close": 10.5},
            # This bar must not become the prediction's as-of date.
            {"date": "2026-10-23", "open": 11, "high": 12, "low": 10, "close": 11.5},
        ]

    async def save_strategy_trade_prediction(self, payload):
        self.saved.append(payload)

    async def fetch(self, *_args):
        return []


def test_writer_recovers_closed_signal_date_and_bounds_future_bars() -> None:
    provider = LegacyIncubationProvider(object())
    db = _Db()
    result = asyncio.run(
        provider.write_daily_predictions(
            db,
            {"id": "strategy-1", "params": {}},
            {"signal_date": "2026-10-24"},
        )
    )

    assert result["status"] == "ok"
    assert result["signal_date"] == "2026-10-21"
    assert result["written"] == 1
    assert db.saved[0]["contract_json"]["prediction_as_of"] == "2026-10-21"

