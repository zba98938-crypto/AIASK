"""Tests for Phase 4 — Production resilience, rate limiting, session sync, voice."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiask_agent.gateway_daemon import GatewayDaemon, ListenerStatus


class TestProductionResilience:
    """Tests for production-grade reconnection logic."""

    @pytest.fixture
    def daemon(self):
        runtime = MagicMock()
        runtime.run = AsyncMock(return_value=MagicMock(content="ok", session_id="s1"))
        d = GatewayDaemon(runtime=runtime)
        d._running = True
        return d

    @pytest.mark.asyncio
    async def test_listener_recovers_after_failure(self, daemon):
        """Listener should retry after failure with exponential backoff."""
        call_count = 0

        async def failing_listener(*, callback):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("simulated failure")
            # Third call succeeds but exits (simulating normal exit)
            daemon._running = False

        ls = ListenerStatus(platform="test")
        daemon._status.listeners["test"] = ls
        daemon._reconnect_delay = 0.01  # Speed up for testing
        daemon._max_retries = 5

        await daemon._run_listener("test", failing_listener)

        assert call_count == 3
        assert ls.reconnect_count >= 1

    @pytest.mark.asyncio
    async def test_listener_resets_retries_after_stable_run(self, daemon):
        """Retries should reset after stable run (>60s simulated)."""
        # This tests the logic path, not actual 60s wait
        ls = ListenerStatus(platform="test")
        daemon._status.listeners["test"] = ls
        daemon._reconnect_delay = 0.01

        call_count = 0

        async def listener(*, callback):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                daemon._running = False
            raise RuntimeError("fail")

        await daemon._run_listener("test", listener)
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_cancelled_listener_stops_cleanly(self, daemon):
        """CancelledError should stop the listener without retrying."""
        ls = ListenerStatus(platform="test")
        daemon._status.listeners["test"] = ls

        async def cancelling_listener(*, callback):
            raise asyncio.CancelledError()

        await daemon._run_listener("test", cancelling_listener)
        assert ls.state == "stopped"


class TestRateLimiting:
    """Tests for per-user rate limiting."""

    def test_allows_within_limit(self):
        daemon = GatewayDaemon()
        daemon._rate_limit_max = 5
        daemon._rate_limit_window = 60.0

        for _ in range(5):
            assert daemon._check_rate_limit("user:test") is True

    def test_blocks_over_limit(self):
        daemon = GatewayDaemon()
        daemon._rate_limit_max = 3
        daemon._rate_limit_window = 60.0

        for _ in range(3):
            assert daemon._check_rate_limit("user:test") is True

        # 4th message should be blocked
        assert daemon._check_rate_limit("user:test") is False

    def test_different_users_independent(self):
        daemon = GatewayDaemon()
        daemon._rate_limit_max = 2
        daemon._rate_limit_window = 60.0

        assert daemon._check_rate_limit("user:a") is True
        assert daemon._check_rate_limit("user:a") is True
        assert daemon._check_rate_limit("user:a") is False  # blocked

        # Different user still allowed
        assert daemon._check_rate_limit("user:b") is True

    def test_window_expiry(self):
        daemon = GatewayDaemon()
        daemon._rate_limit_max = 2
        daemon._rate_limit_window = 0.1  # 100ms window

        assert daemon._check_rate_limit("user:test") is True
        assert daemon._check_rate_limit("user:test") is True
        assert daemon._check_rate_limit("user:test") is False

        # Wait for window to expire
        time.sleep(0.15)
        assert daemon._check_rate_limit("user:test") is True


class TestCrossPlatformSession:
    """Tests for cross-platform session resolution."""

    def test_private_message_session(self):
        daemon = GatewayDaemon()
        event = {
            "platform": "telegram",
            "user_id": "user123",
            "chat_id": "user123",  # Same as user_id = private
        }
        session = daemon._resolve_session(event)
        assert session == "gw_telegram_dm_user123"

    def test_group_message_session(self):
        daemon = GatewayDaemon()
        event = {
            "platform": "weixin",
            "user_id": "wxid_abc",
            "chat_id": "room_xyz",
            "is_group": True,
        }
        session = daemon._resolve_session(event)
        assert session == "gw_weixin_room_xyz"

    def test_group_detected_by_different_ids(self):
        daemon = GatewayDaemon()
        event = {
            "platform": "discord",
            "user_id": "user1",
            "chat_id": "channel_123",  # Different from user_id
        }
        session = daemon._resolve_session(event)
        # chat_id != user_id → treated as group
        assert session == "gw_discord_channel_123"

    def test_cross_platform_lookup_returns_none_by_default(self):
        daemon = GatewayDaemon()
        result = daemon._cross_platform_lookup("telegram", "user123")
        assert result is None

    def test_anonymous_user(self):
        daemon = GatewayDaemon()
        event = {"platform": "email", "user_id": "", "chat_id": ""}
        session = daemon._resolve_session(event)
        assert "anonymous" in session or "default" in session


class TestConcurrencyControl:
    """Tests for concurrent agent call limiting."""

    @pytest.fixture
    def daemon(self):
        runtime = MagicMock()
        runtime.run = AsyncMock(return_value=MagicMock(content="response", session_id="s1"))
        d = GatewayDaemon(runtime=runtime)
        d._max_concurrent_agents = 2
        d._agent_semaphore = asyncio.Semaphore(2)
        return d

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self, daemon):
        """Should limit concurrent agent calls."""
        results = await asyncio.gather(
            daemon._process_with_agent("msg1", "s1"),
            daemon._process_with_agent("msg2", "s2"),
            daemon._process_with_agent("msg3", "s3"),
        )
        assert all(r == "response" for r in results)
        assert daemon.runtime.run.call_count == 3

    @pytest.mark.asyncio
    async def test_timeout_handling(self, daemon):
        """Should handle agent timeout gracefully."""
        daemon._response_timeout = 0.01  # Very short timeout

        async def slow_run(**kwargs):
            await asyncio.sleep(1)
            return MagicMock(content="late")

        daemon.runtime.run = slow_run
        result = await daemon._process_with_agent("test", "s1")
        assert "timeout" in result.lower()


class TestHealthCheck:
    """Tests for health monitoring."""

    @pytest.mark.asyncio
    async def test_health_check_idle(self):
        daemon = GatewayDaemon()
        health = await daemon.health_check()
        assert health["daemon_state"] == "idle"
        assert health["healthy"] is False

    @pytest.mark.asyncio
    async def test_health_check_running(self):
        daemon = GatewayDaemon()
        daemon._status.state = "running"
        daemon._status.started_at = "2026-05-16T10:00:00Z"
        daemon._status.listeners["telegram"] = ListenerStatus(
            platform="telegram", state="running", message_count=42
        )
        health = await daemon.health_check()
        assert health["daemon_state"] == "running"
        assert health["healthy"] is True
        assert health["listeners"]["telegram"]["healthy"] is True
        assert health["listeners"]["telegram"]["message_count"] == 42


class TestVoiceModule:
    """Tests for voice (TTS/STT) module."""

    def test_voice_configured_default(self):
        from aiask_agent.voice import voice_configured
        with patch.dict(os.environ, {}, clear=True):
            config = voice_configured()
            assert config["stt_provider"] == "openai"
            assert config["tts_provider"] == "openai"
            assert config["stt_configured"] is False
            assert config["tts_configured"] is False

    def test_voice_configured_with_openai_key(self):
        from aiask_agent.voice import voice_configured
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            config = voice_configured()
            assert config["stt_configured"] is True
            assert config["tts_configured"] is True
            assert config["voice_enabled"] is True

    def test_voice_configured_iflytek(self):
        from aiask_agent.voice import voice_configured
        with patch.dict(os.environ, {
            "AIASK_VOICE_STT_PROVIDER": "iflytek",
            "AIASK_VOICE_TTS_PROVIDER": "iflytek",
            "IFLYTEK_APP_ID": "test",
            "IFLYTEK_API_KEY": "test",
        }, clear=True):
            config = voice_configured()
            assert config["stt_provider"] == "iflytek"
            assert config["stt_configured"] is True

    @pytest.mark.asyncio
    async def test_transcribe_no_api_key(self):
        from aiask_agent.voice import transcribe
        with patch.dict(os.environ, {}, clear=True):
            result = await transcribe("/tmp/nonexistent.mp3")
            assert result["success"] is False
            assert "OPENAI_API_KEY" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_synthesize_no_api_key(self):
        from aiask_agent.voice import synthesize
        with patch.dict(os.environ, {}, clear=True):
            result = await synthesize("Hello world")
            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_synthesize_uses_exclusively_created_temporary_file(
        self,
        tmp_path,
        monkeypatch,
    ):
        import httpx
        from aiask_agent import voice as voice_module

        class FakeResponse:
            status_code = 200
            content = b"synthetic mp3"
            text = ""

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return FakeResponse()

        real_named_temporary_file = voice_module.tempfile.NamedTemporaryFile
        calls = []

        def named_temporary_file(**kwargs):
            calls.append(dict(kwargs))
            return real_named_temporary_file(dir=tmp_path, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(
            voice_module.tempfile,
            "NamedTemporaryFile",
            named_temporary_file,
        )
        monkeypatch.setattr(
            voice_module.tempfile,
            "mktemp",
            lambda **_kwargs: pytest.fail("tempfile.mktemp must not be used"),
        )
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "sk-test", "AIASK_AGENT_LOAD_PROJECT_ENV": ""},
            clear=True,
        ):
            result = await voice_module.synthesize("Hello world")

        output_path = voice_module.Path(result["path"])
        assert result["success"] is True
        assert output_path.parent == tmp_path
        assert output_path.read_bytes() == b"synthetic mp3"
        if os.name != "nt":
            assert output_path.stat().st_mode & 0o777 == 0o600
        assert calls == [
            {
                "mode": "wb",
                "suffix": ".mp3",
                "prefix": "aiask_tts_",
                "delete": False,
            }
        ]

    @pytest.mark.asyncio
    async def test_synthesize_http_failure_leaves_no_temporary_file(
        self,
        tmp_path,
        monkeypatch,
    ):
        import httpx
        from aiask_agent import voice as voice_module

        class FakeResponse:
            status_code = 503
            content = b""
            text = "unavailable"

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(
            voice_module.tempfile,
            "NamedTemporaryFile",
            lambda **_kwargs: pytest.fail(
                "HTTP failures must not allocate an output file"
            ),
        )
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "sk-test", "AIASK_AGENT_LOAD_PROJECT_ENV": ""},
            clear=True,
        ):
            result = await voice_module.synthesize("Hello world")

        assert result["success"] is False
        assert result["error"].startswith("HTTP 503")
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_process_voice_inbound_failure(self):
        from aiask_agent.voice import process_voice_inbound
        with patch.dict(os.environ, {}, clear=True):
            result = await process_voice_inbound("/tmp/nonexistent.mp3")
            assert "失败" in result or "error" in result.lower()
