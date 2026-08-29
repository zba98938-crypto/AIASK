from __future__ import annotations

import asyncio
import os

import pytest

from aiask_agent.capabilities import parity_summary
from aiask_agent.control_context import internal_control_context
from aiask_agent.gateway import ADAPTERS
from aiask_agent.model_client import MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import build_default_tool_registry
from aiask_agent.tools.policy import ToolPolicy, ToolPolicyEngine


def _full_runtime(tmp_path) -> AgentRuntime:
    policy = ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),)))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    return AgentRuntime(
        model_client=MockModelClient(),
        session_store=store,
        tool_registry=build_default_tool_registry(
            session_store=store,
            policy_engine=policy,
        ),
        max_iterations=2,
    )


def _live_enabled(request) -> bool:
    return bool(request.config.getoption("--run-live-hermes"))


def test_strict_code_parity_integration_entrypoint(tmp_path) -> None:
    runtime = _full_runtime(tmp_path)
    payload = parity_summary(runtime.tool_registry.names(), env=dict(os.environ), gateway_adapters=ADAPTERS.keys())
    assert payload["core_missing_hermes_tools"] == []
    assert payload["core_missing_gateway_platforms"] == []
    assert payload["core_code_status"] == "present"
    assert payload["v014_delta"]["missing_count"] == 0
    assert payload["v016_delta"]["missing_count"] == 0
    assert payload["mock_status"] == "passed"


def test_live_webhook_gateway_smoke_when_configured(tmp_path, request) -> None:
    if not _live_enabled(request):
        pytest.skip("live Hermes smoke tests disabled")
    if not os.getenv("AIASK_GATEWAY_WEBHOOK_URL"):
        pytest.skip("AIASK_GATEWAY_WEBHOOK_URL is not configured")
    runtime = _full_runtime(tmp_path)
    result = asyncio.run(
        runtime.tool_registry.call_tool(
            "agent_gateway_send_message",
            {"platform": "webhook", "target": "default", "message": "AIASK live Hermes webhook smoke"},
        )
    )
    assert result["success"] is True
    assert result["data"]["adapter"]["ok"] is True


def test_live_rl_readiness_reports_precise_missing_items(tmp_path, request) -> None:
    if not _live_enabled(request):
        pytest.skip("live Hermes smoke tests disabled")
    runtime = _full_runtime(tmp_path)
    with internal_control_context("test.live_rl_readiness"):
        result = asyncio.run(runtime.tool_registry.call_tool("agent_rl_start_training", {"environment": os.getenv("AIASK_RL_LIVE_ENVIRONMENT", "demo")}))
    if not (os.getenv("TINKER_API_KEY") and os.getenv("WANDB_API_KEY")):
        assert result["success"] is False
        assert result["data"]["configured"] is False
        assert "missing" in result["data"]["readiness"]
    else:
        assert "configured" in result["data"]
