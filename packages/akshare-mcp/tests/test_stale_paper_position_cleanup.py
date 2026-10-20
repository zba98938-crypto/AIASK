from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from akshare_mcp.services.incubation_factory.runner import IncubationFactoryRunner


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"


class _FakeAcquire:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeService:
    async def force_close_open_positions(self, db, strategy, signal_date, **kwargs):
        return {"created_count": 0, "skipped_count": 0}


@pytest.mark.asyncio
async def test_stale_cleanup_orphans_rows_without_a_paper_position(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_GRACE_DAYS", "0")
    runner = IncubationFactoryRunner(dry_run=False)
    conn = _FakeConn()
    db = MagicMock()
    db.acquire = lambda: _FakeAcquire(conn)
    db.list_strategy_trade_positions = AsyncMock(
        side_effect=[
            [
                {
                    "strategy_id": "stale-1",
                    "position_id": "pos-stale",
                    "code": "000001",
                    "status": "open",
                    "opened_at": "2026-01-01T00:00:00+00:00",
                }
            ],
            [
                {
                    "strategy_id": "stale-1",
                    "position_id": "pos-stale",
                    "code": "000001",
                    "status": "open",
                }
            ],
        ]
    )
    monkeypatch.setattr(
        "akshare_mcp.services.incubation.get_strategy_incubation_service",
        lambda: _FakeService(),
    )

    result = await runner._run_stale_paper_position_closure(
        db,
        strategies=[
            {
                "id": "stale-1",
                "status": "submitted",
                "params": {"hold_days": 1},
                "runtime_playbook": {},
            }
        ],
        as_of=date(2026, 10, 20),
    )

    assert result["status"] == "ok"
    assert result["stale_candidate_count"] == 1
    assert result["orphaned_positions"] == 1
    assert result["items"][0]["orphaned_positions"] == 1
    assert len(conn.calls) == 1
    assert "status='orphaned_exit'" in conn.calls[0][0]
    assert conn.calls[0][1] == ("pos-stale",)
