from __future__ import annotations

import asyncio
import json
from typing import Any

from aiask_agent.control_context import internal_control_context
from aiask_agent.model_client import ModelResponse
from aiask_agent.native_capabilities import SkillStore, build_native_capability_handlers
from aiask_agent.runtime import AgentRuntime
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import AgentToolRegistry, aiask_envelope, build_default_tool_registry
from aiask_agent.tool_risk import mcp_call_side_effect, mcp_tool_metadata
from aiask_agent.tools.policy import ToolPolicy


class FakeMCPAggregator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def financial_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "server": "local",
                "name": "strategy_manager",
                "wrapped_name": "agent_mcp_local_strategy_action",
                "description": "Strategy action proxy",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "params": {"type": "object"},
                    },
                    "required": ["action"],
                },
            },
            {
                "server": "local",
                "name": "stateful_writer",
                "wrapped_name": "agent_mcp_local_stateful_writer",
                "description": "Stateful writer proxy",
                "parameters": {"type": "object", "properties": {}},
                "side_effect": {"level": "stateful", "target": "stateful_writer"},
            },
            {
                "server": "local",
                "name": "quote_reader",
                "wrapped_name": "agent_mcp_local_quote_reader",
                "description": "Read-only quote reader",
                "parameters": {"type": "object", "properties": {}},
                "side_effect": {"level": "read_only", "target": "quote_reader"},
            },
            {
                "server": "local",
                "name": "ths_place_order",
                "wrapped_name": "agent_mcp_local_ths_place_order",
                "description": "Mislabelled live broker order",
                "parameters": {"type": "object", "properties": {}},
                "side_effect": {"level": "read_only"},
            },
        ]

    async def call(self, wrapped_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((wrapped_name, arguments))
        if wrapped_name == "agent_mcp_local_quote_reader":
            return {
                "success": True,
                "data": {"unexpected_mutation_marker": "should-stay-visible-to-guards"},
                "error": None,
                "meta": {
                    "side_effect": {
                        "level": "stateful",
                        "target": "quote_reader.cache_write",
                        "confirmation_required": True,
                        "idempotent": False,
                    },
                    "source_chain": ["test", "upstream_mcp"],
                },
            }
        return {"success": True, "action": arguments.get("action")}


def test_mcp_strategy_actions_are_classified_and_confirmed(tmp_path) -> None:
    fake_mcp = FakeMCPAggregator()
    registry = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        mcp_aggregator=fake_mcp,  # type: ignore[arg-type]
    )

    read_only = asyncio.run(
        registry.call_tool("agent_mcp_local_strategy_action", {"action": "factory_status"})
    )
    assert read_only["success"] is True
    assert len(fake_mcp.calls) == 1

    blocked = asyncio.run(
        registry.call_tool(
            "agent_mcp_local_strategy_action",
            {"action": "submit", "params": {"strategy_id": "s1"}},
        )
    )
    assert blocked["success"] is False
    assert blocked["error_code"] == "ACTION_INTENT_REQUIRED"
    assert blocked["data"]["required_tool"] == "agent_action_intent_create"
    assert blocked["data"]["action"] == "strategy_manager.submit"
    assert len(fake_mcp.calls) == 1

    blocked_writer = asyncio.run(
        registry.call_tool("agent_mcp_local_stateful_writer", {"value": 1})
    )
    assert blocked_writer["success"] is False
    assert blocked_writer["error_code"] == "MCP_STATEFUL_ACTION_BLOCKED"
    assert len(fake_mcp.calls) == 1

    assert registry.get("agent_mcp_local_ths_place_order") is None
    blocked_live = asyncio.run(
        registry.call_tool("agent_mcp_local_ths_place_order", {"broker_token": "should-not-be-used"})
    )
    assert blocked_live["success"] is False
    assert blocked_live["error_code"] == "TOOL_NOT_FOUND"
    assert len(fake_mcp.calls) == 1


def test_mcp_wrapper_preserves_upstream_envelope_side_effect(tmp_path) -> None:
    fake_mcp = FakeMCPAggregator()
    registry = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        mcp_aggregator=fake_mcp,  # type: ignore[arg-type]
    )

    result = asyncio.run(registry.call_tool("agent_mcp_local_quote_reader", {}))

    assert result["success"] is True
    assert result["data"]["unexpected_mutation_marker"] == "should-stay-visible-to-guards"
    assert result["meta"]["side_effect"]["level"] == "stateful"
    assert result["meta"]["side_effect"]["target"] == "quote_reader.cache_write"
    assert result["meta"]["source_chain"] == ["test", "upstream_mcp"]


def test_live_broker_mcp_tools_are_trade_risk_even_if_mislabelled_read_only() -> None:
    metadata = mcp_tool_metadata(
        {
            "server": "local",
            "name": "qmt_cancel_order",
            "side_effect": {"level": "read_only"},
        }
    )
    side_effect = mcp_call_side_effect(
        "qmt_cancel_order",
        {"order_id": 1, "broker_token": "runtime-secret"},
        raw_side_effect={"level": "read_only"},
    )

    assert metadata["side_effect"]["level"] == "trade_risk"
    assert metadata["side_effect"]["confirmation_required"] is True
    assert side_effect["level"] == "trade_risk"
    assert side_effect["live_broker_tool"] is True


def test_dynamic_live_broker_mcp_tools_are_detected_from_schema_and_description() -> None:
    metadata = mcp_tool_metadata(
        {
            "server": "local",
            "name": "broker_place_order",
            "description": "Mislabelled read-only place order adapter",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "price": {"type": "number"},
                    "volume": {"type": "integer"},
                    "direction": {"type": "string"},
                },
                "required": ["code", "price", "volume", "direction"],
            },
            "side_effect": {"level": "read_only"},
        }
    )
    side_effect = mcp_call_side_effect(
        "broker_place_order",
        {"code": "600519", "price": 100.0, "volume": 100, "direction": "buy"},
        raw_side_effect={"level": "read_only"},
    )

    assert metadata["side_effect"]["level"] == "trade_risk"
    assert metadata["side_effect"]["confirmation_required"] is True
    assert metadata["side_effect"]["live_broker_tool"] is True
    assert side_effect["level"] == "trade_risk"
    assert side_effect["live_broker_tool"] is True


class RepeatingFailureModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            tool_calls=[
                {
                    "id": f"call_fail_{self.calls}",
                    "type": "function",
                    "function": {
                        "name": "agent_fail_tool",
                        "arguments": json.dumps({"same": True}),
                    },
                }
            ],
            usage={"total_tokens": 1},
        )


def test_runtime_halts_repeated_tool_failure(tmp_path) -> None:
    registry = AgentToolRegistry()

    async def fail_tool(_: dict[str, Any]) -> dict[str, Any]:
        return aiask_envelope(
            False,
            data=None,
            error="boom",
            tool_name="agent_fail_tool",
            source_chain=["test"],
            error_code="BOOM",
        )

    registry.register(
        "agent_fail_tool",
        description="always fails",
        parameters={"type": "object", "properties": {"same": {"type": "boolean"}}},
        handler=fail_tool,
        metadata={"category": "financial_read", "side_effect": "read_only"},
    )
    runtime = AgentRuntime(
        model_client=RepeatingFailureModel(),
        tool_registry=registry,
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=5,
        retry_attempts=1,
    )

    result = asyncio.run(runtime.run([{"role": "user", "content": "repeat"}]))
    assert result.status == "guardrail_halted"
    assert any(event["event"] == "tool.guardrail_halt" for event in result.audit_events)
    assert result.tool_calls[-1]["result"]["meta"]["guardrail"]["code"] == "repeated_tool_failure_halt"


def test_skill_audit_and_finance_templates_are_real(tmp_path) -> None:
    store = SkillStore(tmp_path / "skills")
    handlers = build_native_capability_handlers(
        policy=ToolPolicy("general_full", True, (str(tmp_path),)),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        skill_store=store,
    )

    empty_audit = asyncio.run(handlers["agent_skill_manage"]({"action": "audit"}))
    assert empty_audit["success"] is True
    assert empty_audit["data"]["issue_count"] >= 1
    assert empty_audit["data"]["issues"][0]["code"] == "no_skills_installed"

    with internal_control_context("test.skill_templates"):
        installed = asyncio.run(
            handlers["agent_skill_manage"]({"action": "install_finance_templates"})
        )
    assert installed["success"] is True
    assert installed["data"]["count"] >= 5

    post_audit = asyncio.run(handlers["agent_skill_manage"]({"action": "audit"}))
    assert post_audit["success"] is True
    assert post_audit["data"]["skills"]

    with internal_control_context("test.skill_pin"):
        pinned = asyncio.run(handlers["agent_skill_manage"]({"action": "pin", "name": "aiask-finance-dcf-model"}))
    assert pinned["data"]["skill"]["pinned"] is True


def test_native_full_management_tools_cover_provider_memory_acp_security_and_skill_packs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_MCP_CONFIG", str(tmp_path / "mcp_servers.json"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AIASK_AGENT_MODEL_PROVIDERS", raising=False)
    monkeypatch.setenv("OPENAI_API_KEYS", "sk-" + "A" * 32 + ",sk-" + "B" * 32)
    store = SkillStore(tmp_path / "skills")
    handlers = build_native_capability_handlers(
        policy=ToolPolicy("general_full", True, (str(tmp_path),)),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        skill_store=store,
    )

    providers = asyncio.run(handlers["agent_model_manage"]({"action": "status"}))
    assert providers["success"] is True
    openai = next(item for item in providers["data"]["providers"] if item["name"] == "openai")
    assert openai["configured"] is True
    assert len(openai["credentials"]) == 2
    assert "sk-" not in json.dumps(openai)

    selected = asyncio.run(handlers["agent_model_manage"]({"action": "select", "provider": "openai"}))
    assert selected["data"]["selected"] is True
    with internal_control_context("test.model_record_attempt"):
        attempt = asyncio.run(
            handlers["agent_model_manage"](
                {
                    "action": "record_attempt",
                    "provider": "openai",
                    "credential_id": selected["data"]["credential"]["credential_id"],
                    "success": False,
                    "error": "429 rate limit",
                }
            )
        )
    assert attempt["data"]["usage"]["last_error_class"] == "rate_limited"

    with internal_control_context("test.memory_save"):
        saved = asyncio.run(handlers["agent_memory_manage"]({"action": "save", "content": "alpha memory", "symbol": "AAPL"}))
    assert saved["success"] is True
    memories = asyncio.run(handlers["agent_memory_manage"]({"action": "search", "query": "alpha"}))
    assert memories["data"]["memories"][0]["symbol"] == "AAPL"

    with internal_control_context("test.acp_register"):
        acp = asyncio.run(
            handlers["agent_acp_manage"](
                {
                    "action": "register_mcp_server",
                    "name": "client-demo",
                    "transport": "http",
                    "url": "http://127.0.0.1:3100/mcp",
                    "domain": "financial",
                    "tools": [{"name": "quote", "description": "Quote lookup"}],
                }
            )
        )
    assert acp["success"] is True
    assert acp["data"]["server"]["registered_by"] == "acp_client"
    status = asyncio.run(handlers["agent_acp_manage"]({"action": "status"}))
    assert status["data"]["client_provided_mcp_servers"]["count"] == 1

    scan = asyncio.run(handlers["agent_security_scan"]({"text": "OPENAI_API_KEY=sk-" + "C" * 32, "url": "http://127.0.0.1:8000"}))
    assert scan["success"] is True
    codes = {item["code"] for item in scan["data"]["findings"]}
    assert {"openai_key", "private_or_loopback_url"} <= codes
    assert "sk-" not in json.dumps(scan["data"]["findings"])

    packs = asyncio.run(handlers["agent_skill_pack_manage"]({"action": "list"}))
    assert any(item["name"] == "aiask-financial-modeling" for item in packs["data"]["packs"])
    with internal_control_context("test.skill_pack_install"):
        installed = asyncio.run(handlers["agent_skill_pack_manage"]({"action": "install", "pack": "aiask-financial-modeling"}))
    assert installed["success"] is True
    assert installed["data"]["installed_count"] >= 5
    audit = asyncio.run(handlers["agent_skill_pack_manage"]({"action": "audit"}))
    assert audit["success"] is True
    assert audit["data"]["vendor_text_copied"] is False
