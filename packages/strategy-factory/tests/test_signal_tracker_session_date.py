"""Without an explicit ``as_of`` the cycle is labelled with the provider's session.

A Saturday (2026-10-24) cycle fell back to ``date.today()`` and wrote signals
for a session absent from ``kline_1d``.  The orchestrator now asks the provider
for the newest synced session first.
"""

from __future__ import annotations

import asyncio
from datetime import date

from aiask_quant_core.storage import close_db, get_db
from strategy_factory.application.signal_tracker.orchestrator import SignalTrackerOrchestrator

FRIDAY = date(2026, 10, 23)


class _Provider:
    def __init__(self, db, *, session=None, resolver_error: bool = False) -> None:
        self._db = db
        self._session = session
        self._resolver_error = resolver_error
        self.seen_as_of: list[date] = []

    async def get_db(self):
        return self._db

    def get_default_universe(self) -> list:
        return [{"id": "s1", "name": "s1"}]

    def phase_timeout_seconds(self, phase_name: str) -> float:
        del phase_name
        return 30.0

    async def resolve_session_date(self, db):
        del db
        if self._resolver_error:
            raise RuntimeError("store unavailable")
        return {"date": self._session, "source": "kline_1d_synced_session"}

    async def generate_signals(self, db, strategy, *, as_of):
        del db, strategy
        self.seen_as_of.append(as_of)
        return {}

    def __getattr__(self, name: str):
        async def _phase(*_args, **_kwargs):
            return {}

        return _phase


def _cycle(tmp_path, monkeypatch, provider_factory, **kwargs):
    path = str(tmp_path / "session_date.sqlite3")
    monkeypatch.setenv("AIASK_SQLITE_PATH", path)
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", path)
    db = get_db()
    holder = {}

    async def _run():
        try:
            await db.initialize()
            provider = provider_factory(db)
            holder["provider"] = provider
            await SignalTrackerOrchestrator(provider).run_cycle(**kwargs)
        finally:
            await close_db()

    asyncio.run(_run())
    return holder["provider"]


def test_cycle_uses_resolved_session_not_wall_clock(tmp_path, monkeypatch):
    provider = _cycle(tmp_path, monkeypatch, lambda db: _Provider(db, session=FRIDAY))
    assert provider.seen_as_of == [FRIDAY]


def test_explicit_as_of_wins_over_resolver(tmp_path, monkeypatch):
    explicit = date(2026, 10, 16)
    provider = _cycle(tmp_path, monkeypatch, lambda db: _Provider(db, session=FRIDAY), as_of=explicit)
    assert provider.seen_as_of == [explicit]


def test_resolver_failure_falls_back_to_wall_clock(tmp_path, monkeypatch):
    provider = _cycle(tmp_path, monkeypatch, lambda db: _Provider(db, resolver_error=True))
    assert provider.seen_as_of == [date.today()]
