"""``resolve_session_date``: newest fully-synced kline_1d day, never a weekend guess."""

from __future__ import annotations

import asyncio
from datetime import date

from aiask_quant_core import market_session as ms


class _Db:
    """Minimal async store over an in-memory day -> row-count map."""

    def __init__(self, coverage: dict[str, int], *, broken: bool = False) -> None:
        self.coverage = coverage
        self.broken = broken

    async def fetch(self, sql: str, *args):
        if self.broken:
            raise RuntimeError("database is locked")
        if "MAX(time)" in sql:
            latest = max(self.coverage) if self.coverage else None
            return [{"latest": f"{latest}T15:00:00+08:00" if latest else None}]
        start = args[0]
        return [{"n": self.coverage.get(start, 0)}]


def _resolve(db):
    return asyncio.run(ms.resolve_session_date(db, use_cache=False))


def test_friday_full_session_on_a_saturday_run():
    result = _resolve(_Db({"2026-10-22": 5566, "2026-10-23": 5567}))
    assert result["date"] == date(2026, 10, 23)
    assert result["source"] == ms.SOURCE_SYNCED


def test_trailing_partial_days_are_skipped():
    result = _resolve(_Db({"2026-10-16": 5570, "2026-10-19": 53, "2026-10-23": 53}))
    assert result["date"] == date(2026, 10, 16)
    assert result["latest_bar_date"] == date(2026, 10, 23)


def test_unreadable_store_is_labelled_not_cached():
    ms.clear_cache()
    db = _Db({}, broken=True)
    result = asyncio.run(ms.resolve_session_date(db))
    assert result["source"] == ms.SOURCE_UNREADABLE
    assert "database is locked" in result["error"]
    db.broken = False
    db.coverage = {"2026-10-23": 5567}
    assert asyncio.run(ms.resolve_session_date(db))["date"] == date(2026, 10, 23)
    ms.clear_cache()
