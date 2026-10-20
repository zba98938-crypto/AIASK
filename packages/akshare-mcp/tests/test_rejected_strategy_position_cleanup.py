from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from akshare_mcp.services.incubation_factory.runner import IncubationFactoryRunner


class _FakeIncubationService:
    def __init__(self) -> None:
        self.close_calls: list[tuple[str, dict]] = []
        self.settle_calls: list[str] = []

    async def force_close_open_positions(self, db, strategy, signal_date, **kwargs):
        self.close_calls.append((str(strategy.get("id")), dict(kwargs)))
        return {"created_count": 1, "skipped_count": 0}

    async def settle_orders(self, db, strategy, signal_date):
        self.settle_calls.append(str(strategy.get("id")))
        return {"filled_count": 1, "rejected_count": 0}


@pytest.mark.asyncio
async def test_rejected_strategy_cleanup_closes_only_rejected_positions(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_REJECTED_POSITION_CLEANUP_ENABLED", "1")
    runner = IncubationFactoryRunner(dry_run=False)
    db = MagicMock()
    db.acquire = None
    db.list_strategy_trade_positions = AsyncMock(
        return_value=[
            {"strategy_id": "rejected-1", "code": "000001", "status": "open"},
            {"strategy_id": "submitted-1", "code": "000002", "status": "open"},
        ]
    )

    async def _get_strategy(strategy_id: str):
        return {
            "id": strategy_id,
            "status": "rejected" if strategy_id == "rejected-1" else "submitted",
            "params": "{}",
        }

    db.get_strategy = AsyncMock(side_effect=_get_strategy)
    service = _FakeIncubationService()
    monkeypatch.setattr(
        "akshare_mcp.services.incubation.get_strategy_incubation_service",
        lambda: service,
    )

    result = await runner._run_rejected_strategy_position_cleanup(
        db, as_of=date(2026, 10, 20)
    )

    assert result["status"] == "ok"
    assert result["candidate_strategy_count"] == 1
    assert result["evaluated"] == 1
    assert result["orders_created"] == 1
    assert result["orders_filled"] == 1
    assert [call[0] for call in service.close_calls] == ["rejected-1"]
    assert service.close_calls[0][1]["reason"] == "strategy_rejected_cleanup"
    assert service.close_calls[0][1]["source"] == "incubation_factory_rejected_cleanup"
    assert service.close_calls[0][1]["codes"] == ["000001"]
    assert service.settle_calls == ["rejected-1"]


@pytest.mark.asyncio
async def test_rejected_strategy_cleanup_can_be_disabled(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_REJECTED_POSITION_CLEANUP_ENABLED", "0")
    runner = IncubationFactoryRunner(dry_run=False)
    db = MagicMock()
    db.list_strategy_trade_positions = AsyncMock()

    result = await runner._run_rejected_strategy_position_cleanup(db)

    assert result["status"] == "skipped"
    assert result["reason"] == "disabled"
    db.list_strategy_trade_positions.assert_not_called()


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


@pytest.mark.asyncio
async def test_rejected_strategy_cleanup_orphans_duplicate_rows_without_paper_position(
    monkeypatch,
):
    monkeypatch.setenv("INCUBATION_FACTORY_REJECTED_POSITION_CLEANUP_ENABLED", "1")
    runner = IncubationFactoryRunner(dry_run=False)
    conn = _FakeConn()
    db = MagicMock()
    db.acquire = lambda: _FakeAcquire(conn)
    db.list_strategy_trade_positions = AsyncMock(
        side_effect=[
            [{"strategy_id": "rejected-1", "code": "000001", "status": "open"}],
            [
                {
                    "strategy_id": "rejected-1",
                    "position_id": "pos-duplicate",
                    "code": "000001",
                    "status": "open",
                }
            ],
        ]
    )

    async def _get_strategy(strategy_id: str):
        return {"id": strategy_id, "status": "rejected", "params": "{}"}

    db.get_strategy = AsyncMock(side_effect=_get_strategy)
    service = _FakeIncubationService()
    service.force_close_open_positions = AsyncMock(return_value={"created_count": 0})
    monkeypatch.setattr(
        "akshare_mcp.services.incubation.get_strategy_incubation_service",
        lambda: service,
    )

    result = await runner._run_rejected_strategy_position_cleanup(
        db, as_of=date(2026, 10, 20)
    )

    assert result["status"] == "ok"
    assert result["orphaned_positions"] == 1
    assert result["items"][0]["orphaned_positions"] == 1
    assert len(conn.calls) == 1
    assert "status='orphaned_exit'" in conn.calls[0][0]
    assert conn.calls[0][1] == ("pos-duplicate",)


@pytest.mark.asyncio
async def test_rejected_strategy_cleanup_skips_when_no_open_positions(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_REJECTED_POSITION_CLEANUP_ENABLED", "1")
    runner = IncubationFactoryRunner(dry_run=False)
    db = MagicMock()
    db.list_strategy_trade_positions = AsyncMock(return_value=[])

    result = await runner._run_rejected_strategy_position_cleanup(db)

    assert result["status"] == "skipped"
    assert result["reason"] == "no_open_positions"
