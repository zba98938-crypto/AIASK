from __future__ import annotations

from datetime import date

import pytest

from aiask_quant_core.storage.sqlite.strategy_incubation_repos.writes import _IncWritesMixin


class _Repo(_IncWritesMixin):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def get_klines(self, code: str, *, start_date: str, end_date: str):
        self.calls.append((code, start_date, end_date))
        return [
            {"high": 11.0, "low": 9.8},
            {"high": 12.0, "low": 10.0},
        ]


@pytest.mark.asyncio
async def test_open_position_price_path_audit_uses_current_date_not_entry_time():
    repo = _Repo()
    position = {
        "code": "000001",
        "entry_ts": "2026-07-16T00:00:00+00:00",
        "entry_avg_price": 10.0,
        "status": "open",
        "last_trade_time": "2026-07-16T00:00:00+00:00",
    }

    enriched = await repo._enrich_trade_position_price_path(position)

    assert repo.calls == [("000001", "2026-07-16", date.today().isoformat())]
    assert enriched["price_path_audit_status"] == "audited_open_position"
    assert enriched["mfe"] == pytest.approx(0.2)
    assert enriched["mae"] == pytest.approx(-0.02)


@pytest.mark.asyncio
async def test_closed_position_price_path_audit_can_use_last_trade_time_when_exit_missing():
    repo = _Repo()
    position = {
        "code": "000001",
        "entry_ts": "2026-07-16T00:00:00+00:00",
        "entry_avg_price": 10.0,
        "status": "closed",
        "last_trade_time": "2026-07-20T00:00:00+00:00",
    }

    enriched = await repo._enrich_trade_position_price_path(position)

    assert repo.calls == [("000001", "2026-07-16", "2026-07-20")]
    assert enriched["price_path_audit_status"] == "audited_closed_position"
