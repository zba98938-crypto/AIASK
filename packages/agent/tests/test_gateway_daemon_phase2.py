"""Tests for Phase 2 GatewayDaemon listeners — WeChat, WeCom, QQ, Discord."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiask_agent.gateway_daemon import GatewayDaemon


class TestWeixinILinkListener:
    """Tests for personal WeChat iLink Bot adapter."""

    def test_listener_registered(self):
        daemon = GatewayDaemon()
        listener = daemon._get_listener("weixin")
        assert listener is not None
        assert listener == daemon._poll_weixin_ilink

    @pytest.mark.asyncio
    async def test_requires_credentials(self):
        daemon = GatewayDaemon()
        daemon._running = True
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="WEIXIN_ILINK_APP_ID"):
                await daemon._poll_weixin_ilink(callback=AsyncMock())

    @pytest.mark.asyncio
    async def test_requires_both_credentials(self):
        daemon = GatewayDaemon()
        daemon._running = True
        with patch.dict(os.environ, {"WEIXIN_ILINK_APP_ID": "test"}, clear=True):
            with pytest.raises(RuntimeError, match="WEIXIN_ILINK_APP_SECRET"):
                await daemon._poll_weixin_ilink(callback=AsyncMock())


class TestWeComWebSocketListener:
    """Tests for WeCom WebSocket adapter."""

    def test_listener_registered(self):
        daemon = GatewayDaemon()
        listener = daemon._get_listener("wecom")
        assert listener is not None
        assert listener == daemon._ws_wecom

    @pytest.mark.asyncio
    async def test_requires_credentials(self):
        daemon = GatewayDaemon()
        daemon._running = True
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="WECOM_CORP_ID"):
                await daemon._ws_wecom(callback=AsyncMock())

    @pytest.mark.asyncio
    async def test_requires_ws_enabled(self):
        daemon = GatewayDaemon()
        daemon._running = True
        with patch.dict(os.environ, {"WECOM_CORP_ID": "x", "WECOM_SECRET": "y"}, clear=True):
            with pytest.raises(RuntimeError, match="WECOM_WS_ENABLED"):
                await daemon._ws_wecom(callback=AsyncMock())


class TestQQBotWebSocketListener:
    """Tests for QQ Bot v2 WebSocket adapter."""

    def test_listener_registered(self):
        daemon = GatewayDaemon()
        listener = daemon._get_listener("qqbot")
        assert listener is not None
        assert listener == daemon._ws_qqbot

    @pytest.mark.asyncio
    async def test_requires_credentials(self):
        daemon = GatewayDaemon()
        daemon._running = True
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="QQBOT_APP_ID"):
                await daemon._ws_qqbot(callback=AsyncMock())

    @pytest.mark.asyncio
    async def test_requires_ws_enabled(self):
        daemon = GatewayDaemon()
        daemon._running = True
        with patch.dict(os.environ, {"QQBOT_APP_ID": "x", "QQBOT_TOKEN": "y"}, clear=True):
            with pytest.raises(RuntimeError, match="QQBOT_WS_ENABLED"):
                await daemon._ws_qqbot(callback=AsyncMock())


class TestDiscordWebSocketListener:
    """Tests for Discord Gateway WebSocket adapter."""

    def test_listener_registered(self):
        daemon = GatewayDaemon()
        listener = daemon._get_listener("discord")
        assert listener is not None
        assert listener == daemon._ws_discord

    @pytest.mark.asyncio
    async def test_requires_token(self):
        daemon = GatewayDaemon()
        daemon._running = True
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN"):
                await daemon._ws_discord(callback=AsyncMock())


class TestAllListenersAvailable:
    """Verify all 6 platform listeners are registered."""

    def test_all_listeners(self):
        daemon = GatewayDaemon()
        expected = ["telegram", "email", "weixin", "wecom", "qqbot", "discord"]
        for platform in expected:
            listener = daemon._get_listener(platform)
            assert listener is not None, f"Listener for {platform} not found"

    def test_unknown_platform_returns_none(self):
        daemon = GatewayDaemon()
        assert daemon._get_listener("unknown_platform") is None
        assert daemon._get_listener("whatsapp") is None  # Not yet implemented


class TestWeixinConfiguredFromEnv:
    """Test that weixin platform detection includes iLink credentials."""

    def test_weixin_configured_with_ilink(self):
        from aiask_agent.gateway import _configured_from_env
        with patch.dict(os.environ, {"WEIXIN_ILINK_APP_ID": "x", "WEIXIN_ILINK_APP_SECRET": "y"}, clear=True):
            assert _configured_from_env("weixin") is True

    def test_weixin_configured_with_official(self):
        from aiask_agent.gateway import _configured_from_env
        with patch.dict(os.environ, {"WEIXIN_APP_ID": "x", "WEIXIN_APP_SECRET": "y"}, clear=True):
            assert _configured_from_env("weixin") is True

    def test_weixin_not_configured(self):
        from aiask_agent.gateway import _configured_from_env
        with patch.dict(os.environ, {}, clear=True):
            assert _configured_from_env("weixin") is False
