from __future__ import annotations

import asyncio

from akshare_mcp.tools import data_sync as data_sync_tools
from akshare_mcp.tools import market_temperature as market_temperature_tools
from akshare_mcp.tools.tool_catalog import get_tool_contract


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def _register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _register


def test_market_temperature_cache_sync_tool_refreshes_via_data_sync(monkeypatch):
    captured = {}

    async def fake_refresh(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "data": {
                "as_of": kwargs["as_of"],
                "cache": {"status": "written"},
                "quality": {"status": "healthy"},
            },
            "error": None,
            "source": "akshare",
            "cached": False,
            "timestamp": "2026-06-09T10:00:00",
            "source_chain": ["db.stocks", "db.kline_1d", "market_temperature.service"],
            "meta": {
                "side_effect": {
                    "level": "local_state",
                    "target": "market_temperature_snapshots",
                    "confirmation_required": False,
                },
                "source_chain": ["db.stocks", "db.kline_1d", "market_temperature.service"],
            },
        }

    monkeypatch.setattr(
        market_temperature_tools,
        "refresh_market_temperature_snapshot_cache",
        fake_refresh,
    )

    mcp = FakeMcp()
    data_sync_tools.register(mcp)

    result = asyncio.run(
        mcp.tools["sync_market_temperature_snapshot_cache"](
            limit=12,
            top_n=3,
            as_of="20260608",
            min_bars=21,
        )
    )

    assert result["success"] is True
    assert captured == {"limit": 12, "top_n": 3, "as_of": "2026-06-08", "min_bars": 21}
    assert result["source"] == "data_sync.market_temperature_snapshot_cache"
    assert result["source_chain"] == ["data_sync.market_temperature_snapshot_cache"]
    assert result["fallback_used"] is False
    assert result["meta"]["side_effect"]["level"] == "local_state"
    assert result["meta"]["data_sync_source_chain"] == [
        "data_sync.market_temperature_snapshot_cache",
        "db.stocks",
        "db.kline_1d",
        "market_temperature.service",
    ]


def test_market_temperature_cache_sync_tool_validates_inputs():
    mcp = FakeMcp()
    data_sync_tools.register(mcp)

    invalid_date = asyncio.run(mcp.tools["sync_market_temperature_snapshot_cache"](as_of="bad-date"))
    assert invalid_date["success"] is False
    assert "as_of invalid" in invalid_date["error"]

    invalid_limit = asyncio.run(mcp.tools["sync_market_temperature_snapshot_cache"](limit=1001))
    assert invalid_limit["success"] is False
    assert "limit" in invalid_limit["error"]

    invalid_min_bars = asyncio.run(mcp.tools["sync_market_temperature_snapshot_cache"](min_bars=1))
    assert invalid_min_bars["success"] is False
    assert "min_bars" in invalid_min_bars["error"]


def test_market_temperature_cache_sync_contract_is_registered():
    contract = get_tool_contract("sync_market_temperature_snapshot_cache")

    assert contract is not None
    assert contract["category"] == "data_sync"
    assert contract["side_effect"]["level"] == "stateful"
    assert contract["input_schema"]["properties"]["limit"]["maximum"] == 1000


def test_market_temperature_cache_sync_is_available_through_data_sync_manager(monkeypatch):
    from akshare_mcp.tools.managers import data_sync_manager

    captured = {}

    async def fake_execute(db, *, task_type, codes, priority, payload, **kwargs):
        captured.update({
            "db": db,
            "task_type": task_type,
            "codes": codes,
            "priority": priority,
            "payload": payload,
        })
        return {"task_id": "sync_market_temperature_snapshot_cache", "status": "completed"}

    monkeypatch.setattr(data_sync_manager, "get_db", lambda: "fake-db")
    monkeypatch.setattr(data_sync_manager._data_sync_core_mod, "get_db", lambda: "fake-db")
    monkeypatch.setattr(data_sync_manager._data_sync_sync_mod, "get_db", lambda: "fake-db")
    monkeypatch.setattr(data_sync_manager, "_execute_sync_task", fake_execute)

    mcp = FakeMcp()
    data_sync_manager.register_data_sync_manager(mcp)

    result = asyncio.run(
        mcp.tools["data_sync_manager"](
            action="sync",
            params={
                "task_type": "market_temperature_snapshot_cache",
                "limit": 123,
                "top_n": 9,
                "min_bars": 25,
                "priority": "high",
            },
        )
    )

    assert result["success"] is True
    assert captured["task_type"] == "market_temperature_snapshot_cache"
    assert captured["codes"] == []
    assert captured["priority"] == "high"
    assert captured["payload"]["limit"] == 123
    assert captured["payload"]["top_n"] == 9
    assert captured["payload"]["min_bars"] == 25


def test_market_temperature_cache_sync_manager_runner_refreshes_cache(monkeypatch):
    from akshare_mcp.tools.managers import _data_sync_manager_support_sync as sync_support

    captured = {}

    async def fake_refresh(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "data": {"cache": {"status": "written"}, "snapshot": {"as_of": kwargs.get("as_of")}},
            "error": None,
            "source_chain": ["market_temperature.service"],
        }

    monkeypatch.setattr(market_temperature_tools, "refresh_market_temperature_snapshot_cache", fake_refresh)

    result = asyncio.run(
        sync_support._sync_market_temperature_snapshot_cache_now(
            {"limit": 12, "top_n": 4, "as_of": "2026-06-08", "min_bars": 21}
        )
    )

    assert result["success"] == 1
    assert result["failed"] == 0
    assert captured == {"limit": 12, "top_n": 4, "as_of": "2026-06-08", "min_bars": 21}
    assert result["cache"]["status"] == "written"


def test_data_sync_scheduler_bootstraps_market_temperature_runtime_schedule(monkeypatch):
    from akshare_mcp import storage
    from akshare_mcp.services.data_sync_scheduler import DataSyncScheduler
    from akshare_mcp.tools.managers import data_sync_manager

    captured = {}

    async def fake_due_schedules(db, **kwargs):
        captured["due"] = {"db": db, **kwargs}
        return {"matched": 0, "executed": 0, "schedules": []}

    async def fake_runtime_warmup(**kwargs):
        captured["warmup"] = kwargs
        return {
            "ok": True,
            "status": "completed",
            "source": kwargs.get("source"),
            "task_type": kwargs.get("task_type"),
            "bootstrapped_task_types": ["market_temperature_snapshot_cache"],
            "executed": 1,
        }

    monkeypatch.setenv("DATA_SYNC_RUN_MANAGER_SCHEDULES", "true")
    monkeypatch.setenv("DATA_SYNC_BOOTSTRAP_RUNTIME_SCHEDULES", "true")
    monkeypatch.setenv("DATA_SYNC_RUN_TDX", "false")
    monkeypatch.setenv("DATA_SYNC_RUNTIME_WARMUP_TASK_TYPES", "market_temperature_snapshot_cache")
    monkeypatch.setattr(storage, "get_db", lambda: "fake-db")
    monkeypatch.setattr(data_sync_manager, "_sync_data_sync_support_overrides", lambda: None)
    monkeypatch.setattr(data_sync_manager, "_run_due_schedules", fake_due_schedules)
    monkeypatch.setattr(data_sync_manager, "run_runtime_data_warmup", fake_runtime_warmup)

    scheduler = DataSyncScheduler(sync_on_startup=False)
    result = asyncio.run(scheduler.run_once(reason="unit_test"))

    assert captured["due"]["db"] == "fake-db"
    assert captured["due"]["force"] is False
    assert captured["warmup"]["task_type"] == "market_temperature_snapshot_cache"
    assert captured["warmup"]["source"] == "data_sync_scheduler"
    assert captured["warmup"]["bootstrap_missing"] is True
    assert result["runtime_warmup"]["bootstrapped_task_types"] == ["market_temperature_snapshot_cache"]
    assert result["manager_schedules"]["executed"] == 0
