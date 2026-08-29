from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from aiask_agent.adapters.akshare import STOCK_ANALYSIS_DISCLAIMER, STOCK_ANALYSIS_FRESHNESS_EXPECTATION
from aiask_agent.gateway import DeliveryRouter, GatewayChannelDirectoryStore
from aiask_agent.intents import ActionIntentStore
from aiask_agent.mcp_client import MCPAggregator
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.session_store_utils import sanitize_public_response
from aiask_agent.tool_registry import build_default_tool_registry
from aiask_agent.tools.policy import ToolPolicy, ToolPolicyEngine
from aiask_agent.webhooks import WebhookStore


def test_registry_exposes_only_aiask_financial_allowlist(tmp_path) -> None:
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))
    names = set(registry.names())
    assert {
        "agent_tool_catalog",
        "agent_analyze_stock",
        "agent_governance_check",
        "agent_data_validation",
        "agent_quant_data_gate",
        "agent_factor_validation",
        "agent_backtest_suite",
        "agent_portfolio_risk",
        "agent_quant_research_run",
        "agent_market_temperature_snapshot",
        "agent_market_temperature_cache_readiness",
        "agent_market_temperature_cache_history",
        "agent_market_temperature_industry_history",
        "agent_market_temperature_industry_constituents",
        "agent_market_temperature_forward_validation",
        "agent_factory_status",
        "agent_factory_runs",
        "agent_strategy_review_snapshot",
        "agent_trade_prediction_status",
        "agent_trade_prediction_outcomes",
        "agent_trade_prediction_matrix",
        "agent_stock_radar_status",
        "agent_stock_radar_candidates",
        "agent_stock_radar_digest",
        "agent_action_intent_create",
        "agent_action_intent_get",
        "agent_memory",
        "agent_memory_save",
        "agent_memory_search",
        "agent_session_search",
    } <= names
    blocked = ("terminal", "file", "browser", "code", "strategy_manager", "available_tools", "get_tool_contract")
    assert not any(any(token in name for token in blocked) for name in names)

    catalog = asyncio.run(registry.call_tool("agent_tool_catalog", {}))
    assert catalog["success"] is True
    catalog_names = {item["name"] for item in catalog["data"]["tools"]}
    openai_names = {
        item["function"]["name"]
        for item in registry.openai_tools()
        if item.get("type") == "function"
    }
    assert catalog_names == openai_names
    assert {"agent_memory", "agent_memory_save", "agent_memory_search"} <= catalog_names
    assert catalog["data"]["count"] == len(catalog_names)
    text = str(catalog["data"]).lower()
    assert "available_tools" not in text
    assert "get_tool_contract" not in text
    assert "strategy_manager" not in text
    assert catalog["meta"]["trace_id"]
    assert catalog["meta"]["source_chain"]
    assert catalog["meta"]["side_effect"]["level"] == "read_only"


def test_registry_catalog_exposes_formal_tool_contract_annotations(tmp_path) -> None:
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))

    catalog = asyncio.run(registry.call_tool("agent_tool_catalog", {}))

    assert catalog["success"] is True
    tools = catalog["data"]["tools"]
    assert tools
    for item in tools:
        annotations = item["annotations"]
        assert set(annotations) >= {
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
            "requiresApproval",
            "tradeRisk",
        }
        assert item["outputSchema"]["required"] == ["success", "data", "error", "meta"]
        assert item["output_schema"] == item["outputSchema"]
        assert item["contract_version"] == "aiask_tool_contract_v2"

    by_name = {item["name"]: item for item in tools}
    analysis_data = by_name["agent_analyze_stock"]["outputSchema"]["properties"]["data"]["properties"]
    assert {
        "freshness",
        "data_quality",
        "quality_gate",
        "decision_policy",
        "limitations",
        "disclaimer",
        "source_chain",
    } <= set(analysis_data)

    quote = by_name["agent_stock_live_quote"]
    assert quote["annotations"]["readOnlyHint"] is True
    assert quote["annotations"]["destructiveHint"] is False
    assert quote["annotations"]["openWorldHint"] is True
    quote_data = quote["outputSchema"]["properties"]["data"]["properties"]
    assert {"code", "price", "provider", "data_timestamp", "source_chain"} <= set(quote_data)

    news_data = by_name["agent_stock_news_digest"]["outputSchema"]["properties"]["data"]["properties"]
    assert {"items", "news", "sources", "source_chain"} <= set(news_data)

    market_data = by_name["agent_market_temperature_snapshot"]["outputSchema"]["properties"]["data"]["properties"]
    assert {"market", "industries", "quality", "source_chain"} <= set(market_data)

    risk_data = by_name["agent_portfolio_risk"]["outputSchema"]["properties"]["data"]["properties"]
    assert {"portfolio_risk", "risk_metrics", "stress", "source_chain"} <= set(risk_data)

    intent_create = by_name["agent_action_intent_create"]
    assert intent_create["annotations"]["readOnlyHint"] is False
    assert intent_create["annotations"]["requiresApproval"] is True
    assert intent_create["annotations"]["idempotentHint"] is False
    intent_data = intent_create["outputSchema"]["properties"]["data"]["properties"]
    assert {"intent", "intent_id", "status", "side_effect"} <= set(intent_data)

    registry_item = registry.get("agent_action_intent_create")
    assert registry_item is not None
    assert registry_item.metadata["annotations"] == intent_create["annotations"]
    assert registry_item.metadata["outputSchema"] == intent_create["outputSchema"]


def _full_registry(tmp_path, store: ActionIntentStore):
    return build_default_tool_registry(
        store,
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )


def _assert_action_intent_required(result: dict, action: str) -> dict:
    assert result["success"] is False
    assert result["error_code"] == "ACTION_INTENT_REQUIRED"
    assert result["data"]["action_intent_required"] is True
    assert result["data"]["executed"] is False
    assert result["meta"]["side_effect"]["confirmation_required"] is True
    intent = result["data"]["intent"]
    assert intent["action"] == action
    assert intent["status"] == "awaiting_confirmation"
    return intent


def test_general_full_external_message_tools_create_intents_without_delivery(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = _full_registry(tmp_path, store)
    delivery_calls: list[dict] = []

    async def blocked_send(self, **kwargs):
        delivery_calls.append(dict(kwargs))
        raise AssertionError("external delivery must not run before ActionIntent confirmation")

    def blocked_platform_request(*_args, **_kwargs):
        raise AssertionError("platform API must not run before ActionIntent confirmation")

    from aiask_agent import platform_apis
    from aiask_agent import homeassistant as ha_native

    monkeypatch.setattr(DeliveryRouter, "send", blocked_send)
    monkeypatch.setattr(platform_apis, "_json_request", blocked_platform_request)
    monkeypatch.setattr(ha_native, "_request", blocked_platform_request)

    cases = [
        (
            "agent_gateway_send_message",
            {"platform": "webhook", "target": "ops", "message": "hello", "ttl_seconds": 120},
            "gateway.send_message",
            {"platform": "webhook", "target": "ops", "message": "hello"},
        ),
        (
            "agent_gateway_direct_deliver",
            {"platform": "local", "target": "desk", "message": "direct"},
            "gateway.direct_deliver",
            {"platform": "local", "target": "desk", "message": "direct"},
        ),
        (
            "agent_message_send",
            {"target": "discord:123:456", "message": "generic"},
            "gateway.send_message",
            {"platform": "", "target": "discord:123:456", "message": "generic"},
        ),
        (
            "agent_webhook",
            {"action": "trigger", "webhook_id": "wh_1", "event": "tick", "payload": {"x": 1}},
            "webhook.trigger",
            {"webhook_id": "wh_1", "event": "tick", "payload": {"x": 1}},
        ),
        (
            "agent_feishu_drive_reply_comment",
            {"file_token": "file_1", "comment_id": "comment_1", "message": "reply"},
            "feishu_drive.reply_comment",
            {"file_token": "file_1", "comment_id": "comment_1", "message": "reply"},
        ),
        (
            "agent_feishu_drive_add_comment",
            {"file_token": "file_1", "message": "comment"},
            "feishu_drive.add_comment",
            {"file_token": "file_1", "message": "comment"},
        ),
        (
            "agent_discord_channel_send",
            {"channel_id": "channel_1", "message": "discord"},
            "discord.channel_send",
            {"channel_id": "channel_1", "message": "discord"},
        ),
        (
            "agent_discord_server",
            {"action": "pin_message", "channel_id": "channel_1", "message_id": "message_1"},
            "discord.pin_message",
            {"channel_id": "channel_1", "message_id": "message_1"},
        ),
        (
            "agent_discord_server",
            {"action": "add_role", "guild_id": "guild_1", "user_id": "member_1", "role_id": "role_1"},
            "discord.add_role",
            {"guild_id": "guild_1", "discord_user_id": "member_1", "role_id": "role_1"},
        ),
        (
            "agent_ha_call_service",
            {"domain": "light", "service": "turn_on", "entity_id": "light.desk", "data": {"brightness": 128}},
            "homeassistant.call_service",
            {"domain": "light", "service": "turn_on", "entity_id": "light.desk", "data": {"brightness": 128}},
        ),
    ]

    created_intents: list[dict] = []
    for tool_name, arguments, action, expected_params in cases:
        result = asyncio.run(registry.call_tool(tool_name, arguments))
        public_intent = _assert_action_intent_required(result, action)
        stored = store.get(public_intent["intent_id"])
        assert stored is not None
        assert stored["action"] == action
        for key, value in expected_params.items():
            assert stored["params"][key] == value
        created_intents.append(stored)

    ttl_intent = created_intents[0]
    ttl = datetime.fromisoformat(ttl_intent["expires_at"]) - datetime.fromisoformat(ttl_intent["created_at"])
    assert timedelta(seconds=60) <= ttl <= timedelta(seconds=180)
    assert delivery_calls == []


def test_general_full_gateway_control_tools_create_intents_without_mutation(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = _full_registry(tmp_path, store)
    state_path = tmp_path / "state.sqlite3"
    upsert_calls: list[dict] = []

    def blocked_upsert(self, **kwargs):
        upsert_calls.append(dict(kwargs))
        raise AssertionError("gateway directory must not mutate before ActionIntent confirmation")

    monkeypatch.setattr(GatewayChannelDirectoryStore, "upsert", blocked_upsert)

    cases = [
        (
            "agent_gateway_pairing",
            {"action": "create", "platform": "discord", "user_id": "user-1", "session_id": "sess-1"},
            "gateway.pairing_create",
            {"action": "create", "platform": "discord", "user_id": "user-1", "session_id": "sess-1"},
        ),
        (
            "agent_gateway_directory",
            {"action": "upsert", "platform": "local", "name": "ops", "target": "desktop", "thread_id": "thread-1"},
            "gateway.directory_upsert",
            {"platform": "local", "name": "ops", "target": "desktop", "thread_id": "thread-1"},
        ),
        (
            "agent_gateway_directory",
            {"action": "refresh"},
            "gateway.directory_refresh",
            {"action": "refresh"},
        ),
    ]

    for tool_name, arguments, action, expected_params in cases:
        result = asyncio.run(registry.call_tool(tool_name, arguments))
        intent = _assert_action_intent_required(result, action)
        assert "_state_path" not in intent["params"]
        stored = store.get(intent["intent_id"])
        assert stored is not None
        assert stored["params"]["_state_path"] == str(state_path)
        for key, value in expected_params.items():
            assert stored["params"][key] == value

    assert upsert_calls == []

    generic = asyncio.run(
        registry.call_tool(
            "agent_action_intent_create",
            {
                "action": "gateway.directory_upsert",
                "params": {"platform": "local", "name": "ops-generic", "target": "desktop"},
            },
        )
    )
    assert generic["success"] is True
    generic_intent = generic["data"]["intent"]
    assert "_state_path" not in generic_intent["params"]
    generic_stored = store.get(generic_intent["intent_id"])
    assert generic_stored is not None
    assert generic_stored["params"]["_state_path"] == str(state_path)


def test_general_full_webhook_tools_create_intents_without_mutation(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = _full_registry(tmp_path, store)
    state_path = tmp_path / "state.sqlite3"
    mutations: list[dict] = []

    def blocked_subscribe(self, **kwargs):
        mutations.append({"action": "subscribe", **dict(kwargs)})
        raise AssertionError("webhook subscribe must not run before ActionIntent confirmation")

    def blocked_remove(self, webhook_id):
        mutations.append({"action": "remove", "webhook_id": webhook_id})
        raise AssertionError("webhook remove must not run before ActionIntent confirmation")

    def blocked_trigger(self, webhook_id, **kwargs):
        mutations.append({"action": "trigger", "webhook_id": webhook_id, **dict(kwargs)})
        raise AssertionError("webhook trigger must not run before ActionIntent confirmation")

    monkeypatch.setattr(WebhookStore, "subscribe", blocked_subscribe)
    monkeypatch.setattr(WebhookStore, "remove", blocked_remove)
    monkeypatch.setattr(WebhookStore, "render_trigger", blocked_trigger)

    cases = [
        (
            {"action": "subscribe", "name": "factory-alerts", "events": ["factory.done"], "prompt": "Handle {event}"},
            "webhook.subscribe",
            {"name": "factory-alerts", "events": ["factory.done"], "prompt": "Handle {event}", "deliver": "desktop_inbox"},
        ),
        (
            {"action": "remove", "webhook_id": "wh_demo"},
            "webhook.remove",
            {"webhook_id": "wh_demo"},
        ),
        (
            {"action": "trigger", "webhook_id": "wh_demo", "event": "factory.done", "payload": {"run_id": "run_demo"}},
            "webhook.trigger",
            {"webhook_id": "wh_demo", "event": "factory.done", "payload": {"run_id": "run_demo"}},
        ),
    ]

    for arguments, action, expected_params in cases:
        result = asyncio.run(registry.call_tool("agent_webhook", arguments))
        intent = _assert_action_intent_required(result, action)
        assert "_state_path" not in intent["params"]
        stored = store.get(intent["intent_id"])
        assert stored is not None
        assert stored["params"]["_state_path"] == str(state_path)
        for key, value in expected_params.items():
            assert stored["params"][key] == value

    assert mutations == []

    generic = asyncio.run(
        registry.call_tool(
            "agent_action_intent_create",
            {
                "action": "webhook.subscribe",
                "params": {"name": "generic-alerts", "events": ["tick"], "prompt": "Handle {event}"},
            },
        )
    )
    assert generic["success"] is True
    generic_intent = generic["data"]["intent"]
    assert "_state_path" not in generic_intent["params"]
    generic_stored = store.get(generic_intent["intent_id"])
    assert generic_stored is not None
    assert generic_stored["params"]["_state_path"] == str(state_path)


def test_general_full_capability_management_tools_create_intents_without_mutation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_MCP_CONFIG", str(tmp_path / "mcp_servers.json"))
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = _full_registry(tmp_path, store)

    cases = [
        (
            "agent_skill_save",
            {"name": "risk-review", "content": "# Risk Review\n"},
            "skill.save",
        ),
        (
            "agent_skill_manage",
            {"action": "install_finance_templates"},
            "skill.install_finance_templates",
        ),
        (
            "agent_plugin_set_enabled",
            {"name": "audit-export", "enabled": True, "description": "Audit export plugin"},
            "plugin.set_enabled",
        ),
        (
            "agent_plugin_manage",
            {"action": "upsert", "name": "audit-export", "manifest": {"enabled": True}},
            "plugin.upsert",
        ),
        (
            "agent_skill_pack_manage",
            {"action": "install", "pack": "aiask-financial-modeling"},
            "skill_pack.install",
        ),
        (
            "agent_acp_manage",
            {
                "action": "register_mcp_server",
                "name": "client-demo",
                "transport": "http",
                "url": "http://127.0.0.1:3100/mcp",
                "domain": "financial",
                "tools": [{"name": "quote", "description": "Quote lookup"}],
            },
            "acp.register_mcp_server",
        ),
    ]

    for tool_name, arguments, action in cases:
        result = asyncio.run(registry.call_tool(tool_name, arguments))
        public_intent = _assert_action_intent_required(result, action)
        stored = store.get(public_intent["intent_id"])
        assert stored is not None
        assert stored["action"] == action

    assert asyncio.run(registry.call_tool("agent_skill_list", {}))["data"]["skills"] == []
    assert asyncio.run(registry.call_tool("agent_plugin_list", {}))["data"]["plugins"] == []
    acp_status = asyncio.run(registry.call_tool("agent_acp_manage", {"action": "status"}))
    assert acp_status["data"]["client_provided_mcp_servers"]["count"] == 0

    oauth = asyncio.run(
        registry.call_tool(
            "agent_mcp_manage",
            {"action": "oauth_callback", "server": "demo", "access_token": "secret-token"},
        )
    )
    assert oauth["success"] is False
    assert oauth["error_code"] == "CONTROL_CONTEXT_REQUIRED"
    assert oauth["data"]["executed"] is False
    assert "secret-token" not in json.dumps(oauth)


def test_finance_memory_save_creates_intent_without_mutation(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    session_store = AgentSessionStore(tmp_path / "state.sqlite3")
    registry = build_default_tool_registry(store, session_store=session_store)

    result = asyncio.run(
        registry.call_tool(
            "agent_memory_save",
            {"content": "alpha memory", "symbol": "AAPL", "ttl_seconds": 120},
        )
    )
    intent = _assert_action_intent_required(result, "memory.save")
    assert "_state_path" not in intent["params"]
    stored = store.get(intent["intent_id"])
    assert stored is not None
    assert stored["params"]["content"] == "alpha memory"
    assert stored["params"]["symbol"] == "AAPL"
    assert stored["params"]["_state_path"] == str(session_store.path)

    search = asyncio.run(registry.call_tool("agent_memory_search", {"query": "alpha"}))
    assert search["success"] is True
    assert search["data"]["items"] == []


def test_general_full_stateful_learning_memory_and_rl_tools_create_intents(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = _full_registry(tmp_path, store)

    cases = [
        (
            "agent_memory_manage",
            {"action": "save", "content": "alpha memory", "symbol": "AAPL"},
            "memory.save",
        ),
        (
            "agent_learning_apply",
            {"proposal_id": "learn_demo"},
            "learning.apply",
        ),
        (
            "agent_skill_reflect",
            {"name": "risk-review", "observation": "Always check drawdown."},
            "learning.reflect_skill",
        ),
        (
            "agent_rl_select_environment",
            {"environment": "demo"},
            "rl.select_environment",
        ),
        (
            "agent_rl_edit_config",
            {"config": {"learning_rate": 0.01}},
            "rl.edit_config",
        ),
        (
            "agent_rl_start_training",
            {"environment": "demo"},
            "rl.start_training",
        ),
        (
            "agent_rl_stop_training",
            {"run_id": "rlrun_demo"},
            "rl.stop_training",
        ),
        (
            "agent_rl_test_inference",
            {"prompt": "hello"},
            "rl.test_inference",
        ),
    ]

    for tool_name, arguments, action in cases:
        result = asyncio.run(registry.call_tool(tool_name, arguments))
        intent = _assert_action_intent_required(result, action)
        assert "_state_path" not in intent["params"]
        stored = store.get(intent["intent_id"])
        assert stored is not None
        assert stored["params"]["_state_path"] == str(tmp_path / "state.sqlite3")

    memories = asyncio.run(registry.call_tool("agent_memory_manage", {"action": "search", "query": "alpha"}))
    assert memories["success"] is True
    assert memories["data"]["memories"] == []

    record_attempt = asyncio.run(
        registry.call_tool(
            "agent_model_manage",
            {
                "action": "record_attempt",
                "provider": "mock",
                "credential_id": "mock-local",
                "success": False,
                "error": "429 rate limit",
            },
        )
    )
    assert record_attempt["success"] is False
    assert record_attempt["error_code"] == "CONTROL_CONTEXT_REQUIRED"
    assert record_attempt["data"]["executed"] is False


def test_general_full_job_and_cron_tools_require_intents_or_control_context(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = _full_registry(tmp_path, store)

    cases = [
        (
            "agent_job_create",
            {"name": "daily review", "prompt": "review portfolio", "interval_seconds": 3600},
            "job.create",
        ),
        (
            "agent_cronjob",
            {"action": "create", "name": "cron review", "prompt": "review cron", "schedule": "*/5 * * * *"},
            "job.create",
        ),
        (
            "agent_cronjob",
            {"action": "pause", "job_id": "job_demo"},
            "job.pause",
        ),
        (
            "agent_cronjob",
            {"action": "resume", "job_id": "job_demo"},
            "job.resume",
        ),
        (
            "agent_cronjob",
            {"action": "update", "job_id": "job_demo", "prompt": "new prompt"},
            "job.update",
        ),
        (
            "agent_cronjob",
            {"action": "remove", "job_id": "job_demo"},
            "job.remove",
        ),
    ]

    for tool_name, arguments, action in cases:
        result = asyncio.run(registry.call_tool(tool_name, arguments))
        intent = _assert_action_intent_required(result, action)
        assert "_state_path" not in intent["params"]
        stored = store.get(intent["intent_id"])
        assert stored is not None
        assert stored["params"]["_state_path"] == str(tmp_path / "state.sqlite3")

    job_run = asyncio.run(registry.call_tool("agent_job_run", {"job_id": "job_demo"}))
    assert job_run["success"] is False
    assert job_run["error_code"] == "CONTROL_CONTEXT_REQUIRED"
    assert job_run["data"]["executed"] is False

    cron_trigger = asyncio.run(registry.call_tool("agent_cronjob", {"action": "trigger", "job_id": "job_demo"}))
    assert cron_trigger["success"] is False
    assert cron_trigger["error_code"] == "CONTROL_CONTEXT_REQUIRED"
    assert cron_trigger["data"]["executed"] is False


def test_general_full_file_mutation_tools_require_intents_without_disk_change(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = _full_registry(tmp_path, store)
    target = tmp_path / "notes.txt"
    target.write_text("original", encoding="utf-8")

    generic = asyncio.run(
        registry.call_tool(
            "agent_action_intent_create",
            {"action": "file.write", "params": {"path": "forged.txt", "content": "nope"}},
        )
    )
    assert generic["success"] is False
    assert generic["error_code"] == "FILE_INTENT_DIRECT_CREATE_BLOCKED"

    cases = [
        (
            "agent_file_write",
            {"path": "created.txt", "content": "temporary"},
            "file.write",
            "agent_file_write",
        ),
        (
            "agent_file_patch",
            {"path": "notes.txt", "old": "original", "new": "changed"},
            "file.patch",
            "agent_file_patch",
        ),
        (
            "agent_file_checkpoint",
            {"path": "notes.txt", "reason": "manual checkpoint"},
            "file.checkpoint",
            "agent_file_checkpoint",
        ),
        (
            "agent_file_rollback",
            {"path": "notes.txt"},
            "file.rollback",
            "agent_file_rollback",
        ),
    ]

    for tool_name, arguments, action, created_by in cases:
        result = asyncio.run(registry.call_tool(tool_name, arguments))
        intent = _assert_action_intent_required(result, action)
        assert "_state_path" not in intent["params"]
        assert "_workspace_roots" not in intent["params"]
        assert "_created_by_agent_file_tool" not in intent["params"]
        stored = store.get(intent["intent_id"])
        assert stored is not None
        assert stored["params"]["_state_path"] == str(tmp_path / "state.sqlite3")
        assert stored["params"]["_workspace_roots"] == [str(tmp_path.resolve())]
        assert stored["params"]["_created_by_agent_file_tool"] == created_by

    assert target.read_text(encoding="utf-8") == "original"
    assert not (tmp_path / "created.txt").exists()


def test_general_full_browser_interaction_tools_require_intents_without_runtime_action(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = _full_registry(tmp_path, store)
    monkeypatch.setenv("AIASK_AGENT_ENABLE_COMPUTER_USE", "1")

    generic = asyncio.run(
        registry.call_tool(
            "agent_action_intent_create",
            {"action": "browser.click", "params": {"selector": "#buy"}},
        )
    )
    assert generic["success"] is False
    assert generic["error_code"] == "BROWSER_INTENT_DIRECT_CREATE_BLOCKED"

    cases = [
        ("agent_browser_navigate", {"url": "https://example.com"}, "browser.navigate", "agent_browser_navigate"),
        ("agent_browser_click", {"selector": "#buy"}, "browser.click", "agent_browser_click"),
        ("agent_browser_type", {"selector": "input[name=q]", "text": "risk"}, "browser.type", "agent_browser_type"),
        ("agent_browser_scroll", {"direction": "down", "amount": 250}, "browser.scroll", "agent_browser_scroll"),
        ("agent_browser_back", {}, "browser.back", "agent_browser_back"),
        ("agent_browser_press", {"key": "Enter"}, "browser.press", "agent_browser_press"),
        ("agent_browser_cdp", {"method": "Runtime.evaluate", "params": {"expression": "1+1"}}, "browser.cdp", "agent_browser_cdp"),
        ("agent_computer_use", {"action": "browser_click", "selector": "#buy"}, "browser.computer_use", "agent_computer_use"),
    ]

    for tool_name, arguments, action, created_by in cases:
        result = asyncio.run(registry.call_tool(tool_name, arguments))
        intent = _assert_action_intent_required(result, action)
        assert "_created_by_agent_browser_tool" not in intent["params"]
        stored = store.get(intent["intent_id"])
        assert stored is not None
        assert stored["params"]["_created_by_agent_browser_tool"] == created_by

    status = asyncio.run(registry.call_tool("agent_computer_use", {"action": "status"}))
    assert status["success"] is True
    assert status["meta"]["side_effect"]["level"] == "read_only"


def test_general_full_execution_tools_require_intents_without_execution(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = _full_registry(tmp_path, store)

    generic_cases = [
        ("terminal.run", "TERMINAL_INTENT_DIRECT_CREATE_BLOCKED"),
        ("process.kill", "PROCESS_INTENT_DIRECT_CREATE_BLOCKED"),
        ("code.execute_python", "CODE_INTENT_DIRECT_CREATE_BLOCKED"),
    ]
    for action, error_code in generic_cases:
        generic = asyncio.run(
            registry.call_tool(
                "agent_action_intent_create",
                {"action": action, "params": {"command": "echo forged", "process_id": "proc_demo"}},
            )
        )
        assert generic["success"] is False
        assert generic["error_code"] == error_code

    cases = [
        (
            "agent_terminal",
            {"command": "echo should-not-run", "cwd": "."},
            "terminal.run",
            "_created_by_agent_terminal_tool",
            "agent_terminal",
        ),
        (
            "agent_process",
            {"action": "kill", "process_id": "proc_demo"},
            "process.kill",
            "_created_by_agent_process_tool",
            "agent_process",
        ),
        (
            "agent_process",
            {"action": "wait", "process_id": "proc_demo", "timeout_seconds": 1},
            "process.wait",
            "_created_by_agent_process_tool",
            "agent_process",
        ),
        (
            "agent_process",
            {"action": "watch", "process_id": "proc_demo", "timeout_seconds": 1},
            "process.watch",
            "_created_by_agent_process_tool",
            "agent_process",
        ),
        (
            "agent_execute_python",
            {"code": "open('should_not_exist.txt', 'w').write('nope')", "cwd": "."},
            "code.execute_python",
            "_created_by_agent_code_tool",
            "agent_execute_python",
        ),
    ]

    for tool_name, arguments, action, marker_key, marker_value in cases:
        result = asyncio.run(registry.call_tool(tool_name, arguments))
        intent = _assert_action_intent_required(result, action)
        assert "_state_path" not in intent["params"]
        assert "_workspace_roots" not in intent["params"]
        assert marker_key not in intent["params"]
        stored = store.get(intent["intent_id"])
        assert stored is not None
        assert stored["params"]["_state_path"] == str(tmp_path / "state.sqlite3")
        assert stored["params"]["_workspace_roots"] == [str(tmp_path.resolve())]
        assert stored["params"][marker_key] == marker_value

    assert not (tmp_path / "should_not_exist.txt").exists()


def test_registry_redacts_free_text_errors_from_handlers_and_exceptions(tmp_path) -> None:
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))

    async def returned_error(_: dict[str, object]) -> dict[str, object]:
        return {
            "success": False,
            "data": {
                "broker_token": "returned-data-secret",
                "accessToken": "returned-access-token-secret",
                "clientSecret": "returned-client-secret",
                "diagnostic": "callback password=returned-data-password",
                "usage": {"total_tokens": 42, "prompt_tokens": 20, "totalTokens": 62},
            },
            "error": "failed with broker_token=returned-secret and Authorization: Bearer returned-bearer-secret",
            "meta": {
                "debug": "Authorization: Bearer returned-meta-bearer-secret",
                "token_usage": {"total_tokens": 42},
            },
        }

    async def raised_error(_: dict[str, object]) -> dict[str, object]:
        raise RuntimeError(
            "raised with password=raised-secret and Authorization: Bearer raised-bearer-secret"
        )

    registry.register(
        "agent_error_return_probe",
        description="Test-only error return probe",
        handler=returned_error,
    )
    registry.register(
        "agent_error_raise_probe",
        description="Test-only error raise probe",
        handler=raised_error,
    )

    returned = asyncio.run(registry.call_tool("agent_error_return_probe", {}))
    raised = asyncio.run(registry.call_tool("agent_error_raise_probe", {}))

    assert returned["success"] is False
    assert returned["error"] == "failed with broker_token=[redacted] and Authorization: Bearer [redacted]"
    assert returned["data"]["broker_token"] == "[redacted]"
    assert returned["data"]["accessToken"] == "[redacted]"
    assert returned["data"]["clientSecret"] == "[redacted]"
    assert returned["data"]["diagnostic"] == "callback password=[redacted]"
    assert returned["data"]["usage"]["total_tokens"] == 42
    assert returned["data"]["usage"]["totalTokens"] == 62
    assert returned["meta"]["debug"] == "Authorization: Bearer [redacted]"
    assert returned["meta"]["token_usage"]["total_tokens"] == 42
    assert raised["success"] is False
    assert raised["error_code"] == "TOOL_EXECUTION_ERROR"
    payload_json = json.dumps({"returned": returned, "raised": raised}, ensure_ascii=False)
    assert "returned-data-secret" not in payload_json
    assert "returned-access-token-secret" not in payload_json
    assert "returned-client-secret" not in payload_json
    assert "returned-data-password" not in payload_json
    assert "returned-meta-bearer-secret" not in payload_json
    assert "returned-secret" not in payload_json
    assert "returned-bearer-secret" not in payload_json
    assert "raised-secret" not in payload_json
    assert "raised-bearer-secret" not in payload_json


def test_public_response_redaction_preserves_json_schema_secret_field_names() -> None:
    payload = {
        "data": {
            "brokerToken": "runtime-broker-token-secret",
            "totalTokens": 123,
            "schema": {
                "type": "object",
                "properties": {
                    "broker_token": {"type": "string", "description": "per-call confirmation token"},
                    "accessToken": {"type": "string"},
                    "totalTokens": {"type": "integer"},
                },
                "required": ["broker_token"],
            },
        }
    }

    sanitized = sanitize_public_response(payload)

    assert sanitized["data"]["brokerToken"] == "[redacted]"
    assert sanitized["data"]["totalTokens"] == 123
    assert sanitized["data"]["schema"]["properties"]["broker_token"]["type"] == "string"
    assert sanitized["data"]["schema"]["properties"]["accessToken"]["type"] == "string"
    assert sanitized["data"]["schema"]["properties"]["totalTokens"]["type"] == "integer"
    assert "runtime-broker-token-secret" not in json.dumps(sanitized, ensure_ascii=False)


def test_public_response_redaction_preserves_headers_from_env_names() -> None:
    payload = {
        "server": {
            "headers_from_env": {
                "Authorization": "AIASK_MCP_AKSHARE_LOCAL_AUTHORIZATION",
                "X-Token": "Bearer runtime-header-secret",
                "X-Blank": "",
            },
            "authorization": "Bearer runtime-authorization-secret",
        }
    }

    sanitized = sanitize_public_response(payload)

    assert sanitized["server"]["headers_from_env"]["Authorization"] == "AIASK_MCP_AKSHARE_LOCAL_AUTHORIZATION"
    assert sanitized["server"]["headers_from_env"]["X-Token"] == "[redacted]"
    assert sanitized["server"]["headers_from_env"]["X-Blank"] == "[redacted]"
    assert sanitized["server"]["authorization"] == "[redacted]"
    rendered = json.dumps(sanitized, ensure_ascii=False)
    assert "runtime-header-secret" not in rendered
    assert "runtime-authorization-secret" not in rendered


def test_analyze_stock_facade_adds_financial_grade_evidence_envelope(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    async def fake_analysis(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "data": {
                "workflow": "analyze_stock_workflow",
                "code": kwargs.get("code"),
                "summary": {"profile_found": True},
            },
            "error": None,
            "meta": {
                "quality": {
                    "status": "good",
                    "failed_steps": [],
                    "workflow": "analyze_stock_workflow",
                },
                "pit": {"as_of": kwargs.get("as_of")},
                "side_effect": {"level": "stateful", "target": kwargs.get("code")},
                "source_chain": "workflow.analyze_stock",
            },
        }

    def fake_loader(module_name, function_name):
        assert module_name == "akshare_mcp.tools.ai_workflows"
        assert function_name == "analyze_stock_workflow"
        return fake_analysis

    monkeypatch.setattr("aiask_agent.adapters.akshare.load_registered_tool", fake_loader)
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))

    result = asyncio.run(
        registry.call_tool(
            "agent_analyze_stock",
            {"symbol": "600519", "include_decision": "false", "as_of": "2026-06-12"},
        )
    )

    assert result["success"] is True
    assert captured == {"symbol": "600519", "include_decision": False, "as_of": "2026-06-12", "code": "600519"}
    data = result["data"]
    assert data["code"] == "600519"
    assert data["disclaimer"] == STOCK_ANALYSIS_DISCLAIMER
    assert data["freshness"]["expectation"] == STOCK_ANALYSIS_FRESHNESS_EXPECTATION
    assert data["freshness"]["as_of"] == "2026-06-12"
    assert data["data_quality"]["status"] == "good"
    assert data["quality_gate"]["mode"] == "report_only"
    assert data["quality_gate"]["status"] == "passed"
    assert data["decision_policy"]["default_include_decision"] is False
    assert data["decision_policy"]["include_decision_requested"] is False
    assert data["decision_policy"]["trade_instruction_allowed"] is False
    assert data["decision_policy"]["live_trading_allowed"] is False
    assert "workflow.analyze_stock" in data["source_chain"]
    assert "akshare_mcp.tools.ai_workflows.analyze_stock_workflow" in data["source_chain"]
    assert result["meta"]["side_effect"]["level"] == "read_only"
    assert result["meta"]["side_effect"]["upstream_level"] == "stateful"
    assert result["meta"]["toolset"] == "finance_safe"


def test_analyze_stock_facade_marks_unknown_quality_as_not_passed(monkeypatch, tmp_path) -> None:
    async def fake_analysis(**kwargs):
        return {
            "success": True,
            "data": {"workflow": "analyze_stock_workflow", "code": kwargs.get("code")},
            "error": None,
            "meta": {},
        }

    monkeypatch.setattr(
        "aiask_agent.adapters.akshare.load_registered_tool",
        lambda module_name, function_name: fake_analysis,
    )
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))

    result = asyncio.run(registry.call_tool("agent_analyze_stock", {"code": "600519"}))

    assert result["success"] is True
    assert result["data"]["quality_gate"]["status"] == "unknown"
    assert result["data"]["quality_gate"]["minimum_quality_passed"] is False
    assert result["data"]["decision_policy"]["include_decision_requested"] is False


def test_market_temperature_agent_facade_is_read_only(monkeypatch, tmp_path) -> None:
    async def fake_snapshot(**kwargs):
        return {
            "success": True,
            "data": {
                "market": {"temperature": 66.6},
                "quality": {"status": "healthy"},
                "arguments": kwargs,
            },
            "error": None,
            "meta": {"side_effect": {"level": "read_only"}, "source_chain": ["fake.market_temperature"]},
        }

    monkeypatch.setattr(
        "aiask_agent.adapters.akshare.load_registered_tool",
        lambda module_name, function_name: fake_snapshot,
    )
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))

    result = asyncio.run(
        registry.call_tool(
            "agent_market_temperature_snapshot",
            {"limit": 2, "top_n": 1, "use_cache": False},
        )
    )

    assert result["success"] is True
    assert result["data"]["market"]["temperature"] == 66.6
    assert result["data"]["arguments"]["limit"] == 2
    assert result["data"]["arguments"]["use_cache"] is False
    assert result["meta"]["side_effect"]["level"] == "read_only"
    assert result["meta"]["toolset"] == "finance_safe"
    assert registry.get("agent_market_temperature_snapshot").parameters["properties"]["use_cache"]["default"] is True


def test_market_temperature_cache_readiness_agent_facade_is_read_only(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    async def fake_readiness(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "data": {
                "ready": True,
                "status": "fresh",
                "read_only": True,
                "as_of": kwargs.get("as_of"),
                "max_stale_days": kwargs.get("max_stale_days"),
                "blockers": [],
            },
            "error": None,
            "meta": {"side_effect": {"level": "read_only"}, "source_chain": ["fake.db_freshness"]},
        }

    def fake_loader(module_name, function_name):
        assert module_name == "akshare_mcp.tools.db_freshness"
        assert function_name == "check_market_temperature_cache_readiness"
        return fake_readiness

    monkeypatch.setattr("aiask_agent.adapters.akshare.load_registered_tool", fake_loader)
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))

    result = asyncio.run(
        registry.call_tool(
            "agent_market_temperature_cache_readiness",
            {"as_of": "2026-06-08", "max_stale_days": 2},
        )
    )

    assert result["success"] is True
    assert result["data"]["ready"] is True
    assert result["data"]["status"] == "fresh"
    assert result["data"]["as_of"] == "2026-06-08"
    assert captured == {"as_of": "2026-06-08", "max_stale_days": 2}
    assert result["meta"]["side_effect"]["level"] == "read_only"
    assert result["meta"]["toolset"] == "finance_safe"
    schema = registry.get("agent_market_temperature_cache_readiness").parameters
    assert schema["properties"]["max_stale_days"]["default"] == 1


def test_market_temperature_cache_history_agent_facade_is_read_only(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    async def fake_history(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "data": {
                "items": [
                    {
                        "as_of": "2026-06-08",
                        "market_temperature": 55.5,
                        "market_state": "neutral",
                        "stock_count": 300,
                        "industry_count": 5,
                        "quality_status": "healthy",
                        "warnings": [],
                    }
                ],
                "count": 1,
                "limit": kwargs.get("limit"),
                "include_snapshot": kwargs.get("include_snapshot"),
            },
            "error": None,
            "meta": {"side_effect": {"level": "read_only"}, "source_chain": ["fake.market_temperature"]},
        }

    def fake_loader(module_name, function_name):
        assert module_name == "akshare_mcp.tools.market_temperature"
        assert function_name == "list_market_temperature_snapshot_cache"
        return fake_history

    monkeypatch.setattr("aiask_agent.adapters.akshare.load_registered_tool", fake_loader)
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))

    result = asyncio.run(
        registry.call_tool(
            "agent_market_temperature_cache_history",
            {"limit": 5, "include_snapshot": False},
        )
    )

    assert result["success"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["items"][0]["as_of"] == "2026-06-08"
    assert captured == {"limit": 5, "include_snapshot": False}
    assert result["meta"]["side_effect"]["level"] == "read_only"
    assert result["meta"]["toolset"] == "finance_safe"
    schema = registry.get("agent_market_temperature_cache_history").parameters
    assert schema["properties"]["limit"]["default"] == 30
    assert schema["properties"]["include_snapshot"]["default"] is False


def test_market_temperature_industry_history_agent_facade_is_read_only(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    async def fake_history(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "data": {
                "items": [
                    {
                        "as_of": "2026-06-08",
                        "name": "bank",
                        "temperature": 60.0,
                        "market_temperature": 55.5,
                        "quality_status": "healthy",
                    }
                ],
                "count": 1,
                "industry": kwargs.get("industry"),
                "match_mode": kwargs.get("match_mode"),
            },
            "error": None,
            "meta": {"side_effect": {"level": "read_only"}, "source_chain": ["fake.market_temperature"]},
        }

    def fake_loader(module_name, function_name):
        assert module_name == "akshare_mcp.tools.market_temperature"
        assert function_name == "list_market_temperature_industry_history"
        return fake_history

    monkeypatch.setattr("aiask_agent.adapters.akshare.load_registered_tool", fake_loader)
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))

    result = asyncio.run(
        registry.call_tool(
            "agent_market_temperature_industry_history",
            {"industry": "bank", "limit": 5, "top_n": 3, "match_mode": "exact"},
        )
    )

    assert result["success"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["items"][0]["temperature"] == 60.0
    assert captured == {"industry": "bank", "limit": 5, "top_n": 3, "match_mode": "exact"}
    assert result["meta"]["side_effect"]["level"] == "read_only"
    assert result["meta"]["toolset"] == "finance_safe"
    schema = registry.get("agent_market_temperature_industry_history").parameters
    assert schema["properties"]["limit"]["default"] == 120
    assert schema["properties"]["top_n"]["maximum"] == 50


def test_market_temperature_industry_constituents_agent_facade_is_read_only(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    async def fake_constituents(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "data": {
                "items": [
                    {
                        "code": "000001",
                        "name": "Ping An Bank",
                        "industry": "bank",
                        "market_cap": 3120.8,
                    }
                ],
                "count": 1,
                "industry": kwargs.get("industry"),
                "match_mode": kwargs.get("match_mode"),
            },
            "error": None,
            "meta": {"side_effect": {"level": "read_only"}, "source_chain": ["fake.market_temperature"]},
        }

    def fake_loader(module_name, function_name):
        assert module_name == "akshare_mcp.tools.market_temperature"
        assert function_name == "list_market_temperature_industry_constituents"
        return fake_constituents

    monkeypatch.setattr("aiask_agent.adapters.akshare.load_registered_tool", fake_loader)
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))

    result = asyncio.run(
        registry.call_tool(
            "agent_market_temperature_industry_constituents",
            {"industry": "bank", "limit": 5, "offset": 1, "match_mode": "contains"},
        )
    )

    assert result["success"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["items"][0]["code"] == "000001"
    assert captured == {"industry": "bank", "limit": 5, "offset": 1, "match_mode": "contains"}
    assert result["meta"]["side_effect"]["level"] == "read_only"
    assert result["meta"]["toolset"] == "finance_safe"
    schema = registry.get("agent_market_temperature_industry_constituents").parameters
    assert schema["required"] == ["industry"]
    assert schema["properties"]["limit"]["maximum"] == 1000
    assert schema["properties"]["match_mode"]["default"] == "contains"


def test_market_temperature_forward_validation_agent_facade_is_read_only(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    async def fake_forward_validation(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "data": {
                "matrix": {"warm": {"1d": {"sample_n": 3, "hit_rate": 0.67}}},
                "horizons": kwargs.get("horizons"),
                "target_field": kwargs.get("target_field"),
            },
            "error": None,
            "meta": {"side_effect": {"level": "read_only"}, "source_chain": ["fake.market_temperature"]},
        }

    def fake_loader(module_name, function_name):
        assert module_name == "akshare_mcp.tools.market_temperature"
        assert function_name == "get_market_temperature_forward_validation"
        return fake_forward_validation

    monkeypatch.setattr("aiask_agent.adapters.akshare.load_registered_tool", fake_loader)
    registry = build_default_tool_registry(ActionIntentStore(tmp_path / "intents.sqlite3"))

    result = asyncio.run(
        registry.call_tool(
            "agent_market_temperature_forward_validation",
            {"limit": 30, "horizons": [1, 3], "target_field": "benchmark_return", "benchmark_code": "000300"},
        )
    )

    assert result["success"] is True
    assert result["data"]["matrix"]["warm"]["1d"]["sample_n"] == 3
    assert captured == {"limit": 30, "horizons": [1, 3], "target_field": "benchmark_return", "benchmark_code": "000300"}
    assert result["meta"]["side_effect"]["level"] == "read_only"
    assert result["meta"]["toolset"] == "finance_safe"
    schema = registry.get("agent_market_temperature_forward_validation").parameters
    assert schema["properties"]["limit"]["maximum"] == 365
    assert schema["properties"]["target_field"]["default"] == "weighted_pct_change"
    assert "benchmark_return" in schema["properties"]["target_field"]["enum"]
    assert schema["properties"]["benchmark_code"]["default"] == "000300"


def test_mcp_contract_metadata_flows_into_agent_registry_without_raw_leaks(tmp_path) -> None:
    input_schema = {
        "type": "object",
        "properties": {"code": {"type": "string"}},
        "required": ["code"],
    }
    output_schema = {
        "type": "object",
        "properties": {"data": {"type": "object"}},
    }
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "akshare-demo",
                        "domain": "financial",
                        "command": "python",
                        "tools": [
                            {
                                "name": "get_realtime_quote",
                                "description": "quote tool",
                                "parameters": input_schema,
                                "input_schema": input_schema,
                                "output_schema": output_schema,
                                "freshness": {"expectation": "intraday_or_latest_quote_snapshot"},
                                "source_policy": {"priority": ["tdx_local", "akshare"]},
                                "examples": [{"arguments": {"code": "600519"}}],
                                "contract_version": "ai_tool_contract_v1",
                                "contract_source": "akshare_mcp.tool_catalog",
                                "standard_model": "EquityQuote",
                                "provider_choices": [{"rank": 1, "source": "tdx_local", "provider": "tdx_local"}],
                                "provider_status": {"providers": [{"provider": "tdx_local", "available": True}]},
                                "quality_gate": {"mode": "report_only", "status": "passed"},
                                "reconciliation": {"enabled": True, "mode": "sampled_report_only"},
                                "form_schema": input_schema,
                                "side_effect": {"level": "read_only"},
                            },
                            {
                                "name": "get_macro_indicator",
                                "description": "macro tool",
                                "parameters": {"type": "object", "properties": {"indicator": {"type": "string"}}, "required": ["indicator"]},
                                "input_schema": {"type": "object", "properties": {"indicator": {"type": "string"}}, "required": ["indicator"]},
                                "output_schema": output_schema,
                                "freshness": {"expectation": "latest_published_macro_indicator_snapshot"},
                                "source_policy": {"priority": ["tushare_pro.macro", "akshare.macro"]},
                                "examples": [{"arguments": {"indicator": "cpi"}}],
                                "contract_version": "ai_tool_contract_v1",
                                "contract_source": "akshare_mcp.tool_catalog",
                                "side_effect": {"level": "read_only"},
                            },
                            {
                                "name": "get_stock_fund_flow",
                                "description": "fund flow tool",
                                "parameters": input_schema,
                                "input_schema": input_schema,
                                "output_schema": output_schema,
                                "freshness": {"expectation": "intraday_or_latest_trading_day_fund_flow_snapshot"},
                                "source_policy": {"priority": ["db.stock_fund_flow", "tqcenter.more_info", "tushare.moneyflow"]},
                                "examples": [{"arguments": {"code": "600519"}}],
                                "contract_version": "ai_tool_contract_v1",
                                "contract_source": "akshare_mcp.tool_catalog",
                                "side_effect": {"level": "read_only"},
                            },
                            {
                                "name": "get_option_chain",
                                "description": "option chain tool",
                                "parameters": {"type": "object", "properties": {"underlying": {"type": "string"}}, "required": ["underlying"]},
                                "input_schema": {"type": "object", "properties": {"underlying": {"type": "string"}}, "required": ["underlying"]},
                                "output_schema": output_schema,
                                "freshness": {"expectation": "near_real_time_option_chain_snapshot"},
                                "source_policy": {"priority": ["akshare.option_sse_list_sina", "akshare.option_sse_codes_sina"]},
                                "examples": [{"arguments": {"underlying": "510050"}}],
                                "contract_version": "ai_tool_contract_v1",
                                "contract_source": "akshare_mcp.tool_catalog",
                                "side_effect": {"level": "read_only"},
                            },
                            {
                                "name": "research_manager",
                                "description": "raw multi-action research manager",
                                "parameters": {"type": "object", "properties": {}},
                                "side_effect": {"level": "read_only"},
                            },
                            {
                                "name": "ths_place_order",
                                "description": "mislabelled live order tool",
                                "parameters": {"type": "object", "properties": {}},
                                "side_effect": {"level": "read_only"},
                            },
                            {
                                "name": "qmt_cancel_order",
                                "description": "mislabelled live cancel tool",
                                "parameters": {"type": "object", "properties": {}},
                                "side_effect": {"level": "read_only"},
                            },
                            {
                                "name": "broker_place_order",
                                "description": "mislabelled read-only order placement",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "code": {"type": "string"},
                                        "price": {"type": "number"},
                                        "amount": {"type": "integer"},
                                        "side": {"type": "string"},
                                    },
                                    "required": ["code", "price", "amount", "side"],
                                },
                                "side_effect": {"level": "read_only"},
                            },
                            {
                                "name": "query_broker_orders",
                                "description": "read-only order query",
                                "parameters": {"type": "object", "properties": {}},
                                "side_effect": {"level": "read_only"},
                            },
                            {"name": "available_tools", "description": "legacy catalog"},
                            {"name": "get_tool_contract", "description": "legacy contract lookup"},
                            {"name": "strategy_manager", "description": "direct strategy manager"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    aggregator = MCPAggregator(config_path)
    tools = aggregator.financial_tools()
    assert [item["wrapped_name"] for item in tools] == [
        "agent_mcp_akshare_demo_get_realtime_quote",
        "agent_mcp_akshare_demo_get_macro_indicator",
        "agent_mcp_akshare_demo_get_stock_fund_flow",
        "agent_mcp_akshare_demo_get_option_chain",
        "agent_mcp_akshare_demo_query_broker_orders",
    ]
    assert tools[0]["parameters"]["required"] == ["code"]
    assert tools[0]["input_schema"]["required"] == ["code"]
    assert tools[0]["contract_source"] == "akshare_mcp.tool_catalog"

    registry = build_default_tool_registry(
        ActionIntentStore(tmp_path / "intents.sqlite3"),
        mcp_aggregator=aggregator,
    )
    wrapped = registry.get("agent_mcp_akshare_demo_get_realtime_quote")
    assert wrapped is not None
    assert wrapped.parameters["required"] == wrapped.metadata["input_schema"]["required"]
    assert wrapped.metadata["output_schema"] == output_schema
    assert wrapped.metadata["outputSchema"] == output_schema
    assert wrapped.metadata["annotations"]["readOnlyHint"] is True
    assert wrapped.metadata["annotations"]["openWorldHint"] is True
    assert wrapped.metadata["annotations"]["requiresApproval"] is False
    assert wrapped.metadata["freshness"]["expectation"] == "intraday_or_latest_quote_snapshot"
    assert wrapped.metadata["source_policy"]["priority"] == ["tdx_local", "akshare"]
    assert wrapped.metadata["contract_version"] == "ai_tool_contract_v1"
    assert wrapped.metadata["standard_model"] == "EquityQuote"
    assert wrapped.metadata["provider_choices"][0]["provider"] == "tdx_local"
    assert wrapped.metadata["provider_status"]["providers"][0]["available"] is True
    assert wrapped.metadata["quality_gate"]["mode"] == "report_only"
    assert wrapped.metadata["reconciliation"]["enabled"] is True
    assert wrapped.metadata["form_schema"]["required"] == ["code"]
    catalog_item = next(item for item in registry.catalog if item["name"] == wrapped.name)
    assert catalog_item["contract_source"] == "akshare_mcp.tool_catalog"
    assert catalog_item["annotations"] == wrapped.metadata["annotations"]
    assert catalog_item["outputSchema"] == output_schema

    macro_wrapped = registry.get("agent_mcp_akshare_demo_get_macro_indicator")
    fund_wrapped = registry.get("agent_mcp_akshare_demo_get_stock_fund_flow")
    option_wrapped = registry.get("agent_mcp_akshare_demo_get_option_chain")
    broker_query_wrapped = registry.get("agent_mcp_akshare_demo_query_broker_orders")
    assert macro_wrapped is not None
    assert fund_wrapped is not None
    assert option_wrapped is not None
    assert broker_query_wrapped is not None
    assert macro_wrapped.metadata["freshness"]["expectation"] == "latest_published_macro_indicator_snapshot"
    assert fund_wrapped.metadata["source_policy"]["priority"][0] == "db.stock_fund_flow"
    assert option_wrapped.metadata["source_policy"]["priority"][0] == "akshare.option_sse_list_sina"

    names = set(registry.names())
    assert names.isdisjoint({"available_tools", "get_tool_contract", "strategy_manager"})
    assert not any(name.startswith("agent_mcp_akshare_demo_available_tools") for name in names)
    assert "agent_mcp_akshare_demo_research_manager" not in names
    assert "agent_mcp_akshare_demo_ths_place_order" not in names
    assert "agent_mcp_akshare_demo_qmt_cancel_order" not in names
    assert "agent_mcp_akshare_demo_broker_place_order" not in names


def test_intent_tools_create_and_get_enveloped_results(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = build_default_tool_registry(store)
    created = asyncio.run(registry.call_tool(
        "agent_action_intent_create",
        {
            "action": "strategy_manager.factory_run_once",
            "params": {
                "execution_mode": "dry_run",
                "broker_token": "tool-param-secret",
                "nested": {"password": "tool-nested-secret"},
            },
            "user_id": "u1",
            "rationale": "test Authorization: Bearer tool-rationale-secret",
        },
    ))
    assert created["success"] is True
    assert created["meta"]["trace_id"]
    assert created["meta"]["source_chain"]
    assert created["meta"]["side_effect"]["confirmation_required"] is True
    intent_id = created["data"]["intent"]["intent_id"]
    created_json = json.dumps(created, ensure_ascii=False)
    assert created["data"]["intent"]["secrets_redacted"] is True
    assert "tool-param-secret" not in created_json
    assert "tool-nested-secret" not in created_json
    assert "tool-rationale-secret" not in created_json
    assert store.get(intent_id)["params"]["broker_token"] == "tool-param-secret"

    fetched = asyncio.run(registry.call_tool("agent_action_intent_get", {"intent_id": intent_id}))
    assert fetched["success"] is True
    assert fetched["data"]["intent"]["status"] == "awaiting_confirmation"
    fetched_json = json.dumps(fetched, ensure_ascii=False)
    assert fetched["data"]["intent"]["secrets_redacted"] is True
    assert "tool-param-secret" not in fetched_json
    assert "tool-nested-secret" not in fetched_json
    assert "tool-rationale-secret" not in fetched_json

    missing = asyncio.run(
        registry.call_tool(
            "agent_action_intent_get",
            {"intent_id": "missing?broker_token=tool-missing-secret"},
        )
    )
    assert missing["success"] is False
    assert "tool-missing-secret" not in json.dumps(missing, ensure_ascii=False)
