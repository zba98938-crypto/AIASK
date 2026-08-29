from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def _register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _register


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_fail_accepts_diagnostic_payload_and_source_chain():
    from akshare_mcp.utils import fail

    result = fail(
        "blocked",
        data={"reason": "compliance"},
        source_chain=["execution_manager", "compliance_manager"],
        fallback_reason="test",
    )

    assert result["success"] is False
    assert result["data"]["reason"] == "compliance"
    assert result["source_chain"] == ["execution_manager", "compliance_manager"]
    assert result["meta"]["quality"]["fallback_reason"] == "test"


def test_domain_projection_parse_time_normalizes_mixed_timezone_values():
    from akshare_mcp.services.domain_projection import StrategyDomainProjectionService

    service = StrategyDomainProjectionService()
    values = [
        service._parse_time(""),
        service._parse_time(datetime(2026, 5, 20, 10, 0, 0)),
        service._parse_time("2026-05-20T10:00:00+08:00"),
        service._parse_time(datetime(2026, 5, 20, 2, 0, 0, tzinfo=timezone.utc)),
    ]

    assert all(item.tzinfo is not None for item in values)
    assert sorted(values)


def test_user_profile_accepts_string_created_at(monkeypatch):
    from akshare_mcp.tools import sentiment

    class Conn:
        async def fetch(self, *_args):
            return [
                {
                    "neuroticism": 0.1,
                    "openness": 0.2,
                    "herd_tendency": 0.3,
                    "greed_fear_axis": 0.4,
                    "confidence": 0.5,
                    "created_at": "2026-05-20T10:00:00+00:00",
                }
            ]

    class Db:
        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(sentiment, "get_db", lambda: Db())
    mcp = FakeMcp()
    sentiment.register(mcp)

    result = asyncio.run(mcp.tools["get_user_profile"](user_id="u1"))
    assert result["success"] is True
    assert result["data"]["snapshot_count"] == 1


def test_option_chain_returns_degraded_empty_when_akshare_missing(monkeypatch):
    from akshare_mcp.tools import options

    monkeypatch.setattr(options, "ak", None)
    result = options.get_option_chain.__wrapped__("510050")

    assert result["success"] is True
    assert result["data"]["options"] == []
    assert result["data"]["degraded"] is True
    assert result["meta"]["provider_contract"]["fallback_reason"]


def test_macro_indicator_returns_degraded_empty_when_provider_missing(monkeypatch):
    from akshare_mcp.tools import macro

    monkeypatch.setattr(macro, "ak", None)
    monkeypatch.setattr(macro.data_source, "get_tushare_pro", lambda: None)

    result = macro.get_macro_indicator.__wrapped__("gdp", limit=3)
    assert result["success"] is True
    assert result["data"]["records"] == []
    assert result["data"]["degraded"] is True
    assert result["meta"]["provider_contract"]["provider_used"] == "none"


def test_market_blocks_degrades_to_empty_when_sources_and_cache_missing(monkeypatch):
    from akshare_mcp.tools import market_blocks

    class Conn:
        async def fetch(self, *_args):
            return []

    class Db:
        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(market_blocks, "ak", None)
    monkeypatch.setattr(market_blocks, "get_db", lambda: Db())

    result = asyncio.run(market_blocks.get_market_blocks(block_type="industry", limit=2))
    assert result["success"] is True
    assert result["data"]["blocks"] == []
    assert result["data"]["degraded"] is True


def test_resolve_existing_security_code_uses_db_fallback(monkeypatch):
    from akshare_mcp import utils

    class Db:
        async def get_stock_info(self, _code):
            return None

        async def list_stocks_by_codes(self, codes):
            return [{"code": codes[0], "name": "Demo", "industry": "Testing"}]

    async def scenario():
        monkeypatch.setattr("akshare_mcp.storage.get_db", lambda: Db())
        monkeypatch.setattr(
            "akshare_mcp.tools.finance.get_stock_info",
            lambda _code: {"success": False, "data": None, "error": "empty"},
        )
        code, info, error = await utils.resolve_existing_security_code_async("600519")
        assert error is None
        assert code == "600519"
        assert info["degraded"] is True
        assert info["source"] == "db.stocks"

    asyncio.run(scenario())


def test_data_sync_cancel_task_disables_schedule(monkeypatch):
    from akshare_mcp.tools.managers import data_sync_manager

    class Conn:
        def __init__(self):
            self.enabled = True

        async def execute(self, query, *args):
            if "UPDATE sync_schedules" in query and args[0] == "schedule_kline_test":
                self.enabled = False
                return "UPDATE 1"
            return "UPDATE 0"

    conn = Conn()

    class Db:
        def acquire(self):
            return FakeAcquire(conn)

    monkeypatch.setattr(data_sync_manager, "get_db", lambda: Db())
    monkeypatch.setattr(data_sync_manager._data_sync_core_mod, "get_db", lambda: Db())
    monkeypatch.setattr(data_sync_manager._data_sync_sync_mod, "get_db", lambda: Db())
    mcp = FakeMcp()
    data_sync_manager.register_data_sync_manager(mcp)

    result = asyncio.run(
        mcp.tools["data_sync_manager"](
            action="cancel_task",
            params={"task_id": "schedule_kline_test"},
        )
    )
    assert result["success"] is True
    assert result["data"]["enabled"] is False
    assert conn.enabled is False


def test_data_sync_get_task_reads_schedule_without_disabling(monkeypatch):
    from akshare_mcp.tools.managers import data_sync_manager

    class Conn:
        def __init__(self):
            self.updated = False

        async def fetchrow(self, query, *args):
            assert "SELECT * FROM sync_schedules" in query
            assert args[0] == "schedule_kline_test"
            return {
                "schedule_id": args[0],
                "task_type": "kline",
                "codes": ["600519"],
                "schedule": "daily",
                "params": "{}",
                "enabled": True,
            }

        async def execute(self, *_args):
            self.updated = True
            raise AssertionError("get_task(schedule_*) must be read-only")

    conn = Conn()

    class Db:
        def acquire(self):
            return FakeAcquire(conn)

    monkeypatch.setattr(data_sync_manager, "get_db", lambda: Db())
    monkeypatch.setattr(data_sync_manager._data_sync_core_mod, "get_db", lambda: Db())
    monkeypatch.setattr(data_sync_manager._data_sync_sync_mod, "get_db", lambda: Db())
    mcp = FakeMcp()
    data_sync_manager.register_data_sync_manager(mcp)

    result = asyncio.run(
        mcp.tools["data_sync_manager"](
            action="get_task",
            params={"task_id": "schedule_kline_test"},
        )
    )
    assert result["success"] is True
    assert result["data"]["schedule_id"] == "schedule_kline_test"
    assert result["data"]["enabled"] is True
    assert result["data"]["target_type"] == "schedule"
    assert conn.updated is False


def test_live_trading_non_dry_run_requires_token_before_readonly_preview(monkeypatch):
    from akshare_mcp.tools.managers import live_trading_manager

    class Adapter:
        provider_name = "fake"
        config = SimpleNamespace(read_only=True, paper=False)

        def can_write(self):
            return False

        def capabilities(self):
            return {}

        async def submit_order(self, _payload):
            raise AssertionError("submit_order should not be called")

        async def close(self):
            return None

    monkeypatch.delenv("AKSHARE_CONFIRM_TOKEN", raising=False)
    monkeypatch.setattr(live_trading_manager, "get_live_broker_adapter", lambda: Adapter())

    result = asyncio.run(
        live_trading_manager._dispatch_action(
            "submit_order",
            {"symbol": "AAPL", "qty": 1, "dry_run": False},
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "CONFIRMATION_REQUIRED"
    assert result["meta"]["side_effect"]["level"] == "trade_risk"
    assert result["meta"]["side_effect"]["explicit_token_required"] is True


def test_live_trading_dry_run_still_returns_preview(monkeypatch):
    from akshare_mcp.tools.managers import live_trading_manager

    class Adapter:
        provider_name = "fake"
        config = SimpleNamespace(read_only=True, paper=False)

        def can_write(self):
            return False

        def capabilities(self):
            return {}

        async def close(self):
            return None

    monkeypatch.setattr(live_trading_manager, "get_live_broker_adapter", lambda: Adapter())

    result = asyncio.run(
        live_trading_manager._dispatch_action(
            "submit_order",
            {"symbol": "AAPL", "qty": 1, "dry_run": True},
        )
    )
    assert result["success"] is True
    assert result["data"]["submitted"] is False
    assert result["data"]["mode"] == "dry_run"


def test_live_trading_rejects_non_finite_order_numbers(monkeypatch):
    from akshare_mcp.tools.managers import live_trading_manager

    class Adapter:
        provider_name = "fake"
        config = SimpleNamespace(read_only=True, paper=False)

        def can_write(self):
            return False

        def capabilities(self):
            return {}

        async def close(self):
            return None

    monkeypatch.setattr(live_trading_manager, "get_live_broker_adapter", lambda: Adapter())

    qty_result = asyncio.run(
        live_trading_manager._dispatch_action(
            "submit_order",
            {"symbol": "AAPL", "qty": "nan", "dry_run": True},
        )
    )
    notional_result = asyncio.run(
        live_trading_manager._dispatch_action(
            "submit_order",
            {"symbol": "AAPL", "notional": float("inf"), "dry_run": True},
        )
    )

    assert qty_result["success"] is False
    assert qty_result["error_code"] == "INVALID_ORDER_INPUT"
    assert qty_result["meta"]["side_effect"]["level"] == "trade_risk"
    assert "qty" in qty_result["data"]["invalid_fields"]

    assert notional_result["success"] is False
    assert notional_result["error_code"] == "INVALID_ORDER_INPUT"
    assert "notional" in notional_result["data"]["invalid_fields"]


def test_paper_archive_account_rejects_non_empty_and_archives_empty(monkeypatch):
    from akshare_mcp.tools.managers import paper_trading_manager

    class Conn:
        def __init__(self):
            self.archived = False
            self.pending = 0
            self.positions = 0

        async def fetchrow(self, query, *args):
            if "FROM paper_accounts" in query:
                return {"id": args[0], "user_id": "u1", "status": "active"}
            return None

        async def fetchval(self, query, *_args):
            if "FROM paper_orders" in query:
                return self.pending
            if "FROM paper_positions" in query:
                return self.positions
            return 0

        async def execute(self, query, *_args):
            if "UPDATE paper_accounts SET status='archived'" in query:
                self.archived = True
            return "UPDATE 1"

    conn = Conn()

    class Db:
        def acquire(self):
            return FakeAcquire(conn)

    monkeypatch.setattr(paper_trading_manager, "get_db", lambda: Db())
    mcp = FakeMcp()
    paper_trading_manager.register_paper_trading_manager(mcp)

    conn.pending = 1
    rejected = asyncio.run(
        mcp.tools["paper_trading_manager"](
            action="archive_account",
            params={"user_id": "u1", "account_id": "acc1"},
        )
    )
    assert rejected["success"] is False
    assert rejected["data"]["pending_orders_count"] == 1

    conn.pending = 0
    archived = asyncio.run(
        mcp.tools["paper_trading_manager"](
            action="archive_account",
            params={"user_id": "u1", "account_id": "acc1"},
        )
    )
    assert archived["success"] is True
    assert archived["data"]["archived"] is True
    assert conn.archived is True
