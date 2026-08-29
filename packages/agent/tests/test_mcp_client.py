from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from aiask_agent.mcp_client import MCPAggregator


class MethodNotFound(RuntimeError):
    pass


class FakePartialSession:
    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                {
                    "name": "quote",
                    "description": "Read quote",
                    "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}},
                }
            ]
        )

    async def list_resources(self):
        raise MethodNotFound("Method not found: resources/list")

    async def list_prompts(self):
        raise MethodNotFound("Method not found: prompts/list")


class FakeSideEffectSession(FakePartialSession):
    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                {
                    "name": "quote",
                    "description": "Read quote",
                    "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}},
                    "annotations": {"readOnlyHint": True},
                },
                {
                    "name": "cache_writer",
                    "description": "Writes cache state",
                    "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}},
                    "side_effect": {"level": "stateful", "target": "cache_writer"},
                },
                {
                    "name": "annotation_writer",
                    "description": "Writes through MCP annotations",
                    "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}},
                    "annotations": {"destructiveHint": True, "idempotentHint": False},
                },
            ]
        )


class FakeBrokenToolsSession(FakePartialSession):
    async def list_tools(self):
        raise MethodNotFound("Method not found: tools/list")


def _write_config(path) -> None:
    path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "finance",
                        "domain": "financial",
                        "transport": "stdio",
                        "command": "dummy",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_default_mcp_summaries_share_safe_financial_surface_filter(tmp_path) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "finance-demo",
                        "domain": "financial",
                        "transport": "stdio",
                        "command": "dummy",
                        "tools": [
                            {"name": "quote", "description": "read-only quote"},
                            {"name": "research_manager", "description": "raw research manager"},
                            {"name": "portfolio_manager", "description": "raw portfolio manager"},
                            {"name": "terminal", "description": "shell tool"},
                            {"name": "available_tools", "description": "legacy catalog"},
                            {"name": "get_tool_contract", "description": "legacy contract lookup"},
                            {"name": "strategy_manager", "description": "raw manager"},
                            {"name": "ths_place_order", "description": "live broker order"},
                            {"name": "qmt_cancel_order", "description": "live broker cancel"},
                            {
                                "name": "broker_place_order",
                                "description": "badly labelled read-only live broker order tool",
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
                            },
                            {
                                "name": "query_broker_orders",
                                "description": "read-only broker order query",
                                "side_effect": {"level": "read_only"},
                            },
                        ],
                    },
                    {
                        "name": "general-demo",
                        "domain": "general",
                        "transport": "stdio",
                        "command": "dummy",
                        "tools": [{"name": "browser", "description": "browser automation"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    mcp = MCPAggregator(config_path)

    assert [item["name"] for item in mcp.financial_tools()] == ["quote", "query_broker_orders"]
    assert mcp.servers_summary()[0]["tools"] == ["quote", "query_broker_orders"]
    assert [item["name"] for item in mcp.tools_summary()] == ["quote", "query_broker_orders"]
    internal_research = mcp.internal_financial_tool("research_manager")
    assert internal_research["name"] == "research_manager"
    assert internal_research["allowed_actions"] == ["get_reports"]
    assert mcp.internal_financial_tool("strategy_manager") is None
    assert mcp.internal_financial_tool("available_tools") is None

    all_servers = mcp.servers_summary(include_all=True)
    all_tools = mcp.tools_summary(include_all=True)
    all_servers_by_name = {item["name"]: item for item in all_servers}
    all_tools_by_name = {item["name"]: item for item in all_tools}
    assert {item["name"] for item in all_servers} == {"finance-demo", "general-demo"}
    assert all_servers_by_name["finance-demo"]["safe_financial_tools"] == ["quote", "query_broker_orders"]
    assert set(all_servers_by_name["finance-demo"]["trade_risk_tools_blocked"]) == {
        "ths_place_order",
        "qmt_cancel_order",
        "broker_place_order",
    }
    assert all_servers_by_name["finance-demo"]["live_trading_enabled"] is False
    assert all_servers_by_name["finance-demo"]["live_trading_policy"] == "blocked_in_agent_mcp_surface"
    assert "safe_financial_tools" not in all_servers_by_name["general-demo"]
    assert "trade_risk_tools_blocked" not in all_servers_by_name["general-demo"]
    assert "live_trading_enabled" not in all_servers_by_name["general-demo"]
    assert {item["name"] for item in all_tools} == {
        "quote",
        "research_manager",
        "portfolio_manager",
        "terminal",
        "available_tools",
        "get_tool_contract",
        "strategy_manager",
        "ths_place_order",
        "qmt_cancel_order",
        "broker_place_order",
        "query_broker_orders",
        "browser",
    }
    assert all_tools_by_name["ths_place_order"]["side_effect"]["level"] == "trade_risk"
    assert all_tools_by_name["qmt_cancel_order"]["side_effect"]["level"] == "trade_risk"
    assert all_tools_by_name["broker_place_order"]["side_effect"]["level"] == "trade_risk"
    assert all_tools_by_name["query_broker_orders"]["side_effect"]["level"] == "read_only"


def test_mcp_financial_tools_drop_wrapped_name_collisions(tmp_path) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "finance-demo",
                        "domain": "financial",
                        "transport": "stdio",
                        "command": "dummy",
                        "tools": [
                            {"name": "foo-bar", "description": "read foo"},
                            {"name": "foo_bar", "description": "read foo collision"},
                            {"name": "quote", "description": "read quote"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    mcp = MCPAggregator(config_path)

    assert [item["name"] for item in mcp.financial_tools()] == ["quote"]
    assert mcp.servers_summary()[0]["safe_financial_tools"] == ["quote"]
    assert [item["name"] for item in mcp.tools_summary()] == ["quote"]

    try:
        mcp.unwrap_name("agent_mcp_finance_demo_foo_bar")
    except ValueError as exc:
        assert "not configured" in str(exc)
    else:
        raise AssertionError("colliding wrapped MCP tools must not be unwrapped")


def test_discovered_mcp_side_effect_metadata_is_preserved_and_filtered(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "mcp_servers.json"
    _write_config(config_path)
    mcp = MCPAggregator(config_path)

    async def fake_with_session(server, operation):
        return await operation(FakeSideEffectSession())

    monkeypatch.setattr(mcp, "_with_session", fake_with_session)

    asyncio.run(mcp.discover_and_update("finance"))

    all_tools = {item["name"]: item for item in mcp.tools_summary(include_all=True)}
    assert [item["name"] for item in mcp.financial_tools()] == ["quote"]
    assert all_tools["quote"]["side_effect"]["level"] == "read_only"
    assert all_tools["cache_writer"]["side_effect"]["level"] == "stateful"
    assert all_tools["annotation_writer"]["side_effect"]["level"] == "stateful"


def test_static_mcp_side_effect_aliases_and_annotations_are_filtered(tmp_path) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "finance-demo",
                        "domain": "financial",
                        "transport": "stdio",
                        "command": "dummy",
                        "tools": [
                            {"name": "quote", "description": "read quote", "annotations": {"readOnlyHint": True}},
                            {"name": "cache-writer", "description": "cache write", "sideEffect": {"level": "stateful"}},
                            {"name": "annotation-writer", "description": "annotation write", "annotations": {"destructiveHint": True}},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    mcp = MCPAggregator(config_path)

    all_tools = {item["name"]: item for item in mcp.tools_summary(include_all=True)}
    assert [item["name"] for item in mcp.financial_tools()] == ["quote"]
    assert all_tools["quote"]["side_effect"]["level"] == "read_only"
    assert all_tools["cache-writer"]["side_effect"]["level"] == "stateful"
    assert all_tools["annotation-writer"]["side_effect"]["level"] == "stateful"


def test_internal_financial_mcp_calls_are_scoped_to_catalog_read_actions(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "finance-demo",
                        "domain": "financial",
                        "transport": "stdio",
                        "command": "dummy",
                        "tools": [
                            {"name": "portfolio_manager", "description": "raw portfolio manager"},
                            {"name": "ths_query_balance", "description": "read-only broker balance"},
                            {"name": "strategy_manager", "description": "raw strategy manager"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    mcp = MCPAggregator(config_path)
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeSession:
        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
            calls.append((tool_name, dict(arguments)))
            return {"success": True, "data": {"ok": True}, "error": None}

    async def fake_with_session(server, operation):
        return await operation(FakeSession())

    monkeypatch.setattr(mcp, "_with_session", fake_with_session)

    portfolio = mcp.internal_financial_tool("portfolio_manager")
    assert portfolio is not None
    assert portfolio["allowed_actions"] == ["get_holdings", "list"]
    assert mcp.internal_financial_tool("strategy_manager") is None

    ok = asyncio.run(mcp.call_internal_financial_tool("portfolio_manager", {"action": "list", "params": {}}))
    direct = asyncio.run(mcp.call_internal_financial_tool("ths_query_balance", {}))

    assert ok["success"] is True
    assert direct["success"] is True
    assert calls == [("portfolio_manager", {"action": "list", "params": {}}), ("ths_query_balance", {})]

    try:
        asyncio.run(mcp.call_internal_financial_tool("portfolio_manager", {"action": "create", "params": {}}))
    except PermissionError as exc:
        assert "portfolio_manager.create" in str(exc)
    else:
        raise AssertionError("non-catalog manager actions must be rejected")


def test_discover_keeps_tools_when_resources_prompts_are_unsupported(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "mcp_servers.json"
    _write_config(config_path)
    mcp = MCPAggregator(config_path)

    async def fake_with_session(server, operation):
        return await operation(FakePartialSession())

    monkeypatch.setattr(mcp, "_with_session", fake_with_session)

    async def scenario():
        discovered = await mcp.discover("finance")
        updated = await mcp.discover_and_update("finance")
        return discovered, updated

    discovered, updated = asyncio.run(scenario())

    assert discovered["tools"][0]["name"] == "quote"
    assert discovered["resources"] == []
    assert discovered["prompts"] == []
    assert discovered["partial_success"] is True
    assert set(discovered["unsupported_methods"]) == {"resources/list", "prompts/list"}
    assert updated["tools_count"] == 1
    assert updated["resources_count"] == 0
    assert updated["prompts_count"] == 0
    assert updated["partial_success"] is True
    assert updated["registration"]["partial_success"] is True
    assert set(updated["registration"]["unsupported_methods"]) == {"resources/list", "prompts/list"}


def test_discover_fails_when_tools_list_is_unsupported(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "mcp_servers.json"
    _write_config(config_path)
    mcp = MCPAggregator(config_path)

    async def fake_with_session(server, operation):
        return await operation(FakeBrokenToolsSession())

    monkeypatch.setattr(mcp, "_with_session", fake_with_session)

    try:
        asyncio.run(mcp.discover("finance"))
    except MethodNotFound as exc:
        assert "tools/list" in str(exc)
    else:
        raise AssertionError("tools/list failure must fail server discovery")
