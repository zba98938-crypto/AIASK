"""Tests for GatewayDaemon — 入站消息守护进程。"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiask_agent.gateway_daemon import GatewayDaemon, DaemonStatus, ListenerStatus, daemon_enabled


class TestDaemonEnabled:
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            assert daemon_enabled() is False

    def test_enabled_with_1(self):
        with patch.dict(os.environ, {"AIASK_GATEWAY_DAEMON_ENABLED": "1"}):
            assert daemon_enabled() is True

    def test_enabled_with_true(self):
        with patch.dict(os.environ, {"AIASK_GATEWAY_DAEMON_ENABLED": "true"}):
            assert daemon_enabled() is True

    def test_enabled_with_yes(self):
        with patch.dict(os.environ, {"AIASK_GATEWAY_DAEMON_ENABLED": "yes"}):
            assert daemon_enabled() is True

    def test_disabled_with_0(self):
        with patch.dict(os.environ, {"AIASK_GATEWAY_DAEMON_ENABLED": "0"}):
            assert daemon_enabled() is False


class TestGatewayDaemonLifecycle:
    @pytest.fixture
    def daemon(self):
        runtime = MagicMock()
        runtime.run = AsyncMock(return_value=MagicMock(content="test response", session_id="s1"))
        return GatewayDaemon(runtime=runtime)

    def test_initial_status(self, daemon):
        status = daemon.status()
        assert status["state"] == "idle"
        assert status["total_messages"] == 0
        assert status["listeners"] == {}

    def test_numeric_env_config_is_bounded(self):
        with patch.dict(
            os.environ,
            {
                "AIASK_GATEWAY_RECONNECT_DELAY": "nan",
                "AIASK_GATEWAY_MAX_RETRIES": "-5",
                "AIASK_GATEWAY_RESPONSE_TIMEOUT": "inf",
                "AIASK_GATEWAY_MAX_CONCURRENT": "0",
                "AIASK_GATEWAY_RATE_LIMIT_WINDOW": "-1",
                "AIASK_GATEWAY_RATE_LIMIT_MAX": "not-an-int",
            },
        ):
            daemon = GatewayDaemon()

        assert daemon._reconnect_delay == 30.0
        assert daemon._max_retries == 0
        assert daemon._response_timeout == 120.0
        assert daemon._max_concurrent_agents == 1
        assert daemon._rate_limit_window == 1.0
        assert daemon._rate_limit_max == 20

    @pytest.mark.asyncio
    async def test_start_no_configured_platforms(self, daemon):
        """Start with no configured platforms should succeed with 0 listeners."""
        result = await daemon.start()
        assert result["status"] == "started"
        assert result["total"] == 0
        await daemon.stop()

    @pytest.mark.asyncio
    async def test_stop(self, daemon):
        result = await daemon.stop()
        assert result["status"] == "stopped"
        status = daemon.status()
        assert status["state"] == "stopped"


class TestInboundMessageHandler:
    @pytest.fixture
    def daemon(self):
        runtime = MagicMock()
        runtime.run = AsyncMock(
            return_value=MagicMock(content="Agent response", session_id="s1")
        )
        daemon = GatewayDaemon(runtime=runtime)
        daemon._running = True
        return daemon

    @pytest.mark.asyncio
    async def test_process_normal_message(self, daemon):
        """Normal message should be processed by agent."""
        event = {
            "platform": "telegram",
            "text": "Hello agent",
            "chat_id": "12345",
            "target": "12345",
            "user_id": "user1",
            "message_id": "msg1",
            "thread_id": "",
        }
        await daemon._on_inbound_message(event)
        assert daemon._status.total_messages == 1
        daemon.runtime.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_control_command(self, daemon):
        """Control commands should not be sent to agent."""
        event = {
            "platform": "telegram",
            "text": "/help",
            "chat_id": "12345",
            "target": "12345",
            "user_id": "user1",
            "message_id": "msg2",
            "thread_id": "",
        }
        await daemon._on_inbound_message(event)
        # Agent should NOT be called for control commands
        # (record_inbound will detect the slash command)

    @pytest.mark.asyncio
    async def test_empty_message_skipped(self, daemon):
        """Empty messages should be skipped."""
        event = {
            "platform": "telegram",
            "text": "",
            "chat_id": "12345",
            "target": "12345",
            "user_id": "user1",
            "message_id": "msg3",
            "thread_id": "",
        }
        await daemon._on_inbound_message(event)
        daemon.runtime.run.assert_not_called()


class TestSessionResolve:
    def test_resolve_session(self):
        daemon = GatewayDaemon()
        event = {"platform": "telegram", "user_id": "user123", "chat_id": "chat456"}
        session_id = daemon._resolve_session(event)
        # chat_id != user_id → group session
        assert session_id == "gw_telegram_chat456"

    def test_resolve_session_defaults(self):
        daemon = GatewayDaemon()
        event = {"platform": "weixin"}
        session_id = daemon._resolve_session(event)
        assert "gw_weixin" in session_id


class TestConnectorManager:
    def test_import(self):
        from aiask_agent.connectors import ConnectorManager, FINANCE_CONNECTORS
        assert "tongdaxin" in FINANCE_CONNECTORS
        assert "tonghuashun" in FINANCE_CONNECTORS
        assert "eastmoney" in FINANCE_CONNECTORS
        assert "qmt" in FINANCE_CONNECTORS
        assert FINANCE_CONNECTORS["tonghuashun"]["capabilities"]["place_order"] is False
        assert FINANCE_CONNECTORS["tonghuashun"]["capabilities"]["cancel_order"] is False
        assert FINANCE_CONNECTORS["qmt"]["capabilities"]["place_order"] is False
        assert FINANCE_CONNECTORS["qmt"]["capabilities"]["cancel_order"] is False

    def test_summary(self):
        from aiask_agent.connectors import ConnectorManager
        mgr = ConnectorManager()
        summary = mgr.summary()
        assert summary["object"] == "aiask.connectors_summary"
        assert "total" in summary
        assert "connectors" in summary

    def test_finance_connectors_do_not_advertise_live_trading_as_executable(self):
        from aiask_agent.connectors import ConnectorManager

        connectors = {
            connector["name"]: connector
            for connector in ConnectorManager().summary()["connectors"]
            if connector["type"] == "finance"
        }

        ths = connectors["tonghuashun"]
        qmt = connectors["qmt"]
        tdx = connectors["tongdaxin"]

        for connector in (ths, qmt, tdx):
            assert connector["capabilities"]["live_trading"] is False
            assert connector["capabilities"].get("place_order") is False
            assert connector["capabilities"].get("cancel_order") is False
            assert connector["tools_trade"] == []
            assert connector["tools_trade_executable"] == []
            assert connector["live_trading_enabled"] is False
            assert connector["trade_direct_execution_allowed"] is False
            assert connector["live_trading_policy"] == "blocked_in_agent_connector_surface"

        assert set(ths["tools_trade_blocked"]) == {"ths_place_order", "ths_cancel_order"}
        assert set(qmt["tools_trade_blocked"]) == {"qmt_place_order", "qmt_cancel_order"}
        assert {"tdx_place_order", "tdx_cancel_order"} <= set(tdx["tools_trade_blocked"])
        assert {"tdx_query_position", "tdx_query_orders", "tdx_query_balance"} <= set(tdx["tools_read"])
