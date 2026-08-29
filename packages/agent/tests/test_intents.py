from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from aiask_agent.intents import ActionIntentStore, IntentExecutor, IntentTransition, public_intent
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import build_default_tool_registry
from aiask_agent.tools.policy import ToolPolicy, ToolPolicyEngine


def _force_status(store: ActionIntentStore, intent_id: str, status: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(str(store.path))) as conn:
        conn.execute(
            "UPDATE action_intents SET status = ?, updated_at = ? WHERE intent_id = ?",
            (status, ts, intent_id),
        )
        conn.commit()


def _force_target(store: ActionIntentStore, intent_id: str, *, target_tool: str, target_action: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(str(store.path))) as conn:
        conn.execute(
            "UPDATE action_intents SET target_tool = ?, target_action = ?, updated_at = ? WHERE intent_id = ?",
            (target_tool, target_action, ts, intent_id),
        )
        conn.commit()


def _force_params(store: ActionIntentStore, intent_id: str, params: dict) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(str(store.path))) as conn:
        conn.execute(
            "UPDATE action_intents SET params_json = ?, updated_at = ? WHERE intent_id = ?",
            (json.dumps(params, ensure_ascii=False, sort_keys=True), ts, intent_id),
        )
        conn.commit()


def _force_executing_updated_at(store: ActionIntentStore, intent_id: str, updated_at: datetime) -> None:
    with closing(sqlite3.connect(str(store.path))) as conn:
        conn.execute(
            "UPDATE action_intents SET status = 'executing', updated_at = ? WHERE intent_id = ?",
            (updated_at.isoformat(), intent_id),
        )
        conn.commit()


def test_intent_create_get_deny_and_unauthorized_action(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    intent = store.create(action="factory_run_once", params={"execution_mode": "dry_run"}, user_id="u1")
    assert intent["status"] == "awaiting_confirmation"
    assert intent["target_action"] == "factory_run_once"
    assert store.get(intent["intent_id"])["params"]["execution_mode"] == "dry_run"

    with pytest.raises(ValueError):
        store.create(action="strategy_manager.not_allowed", params={})


def test_confirm_executes_allowed_action_once_and_rejects_repeat(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(action="strategy_manager.factory_run_once", params={"execution_mode": "dry_run"})
    calls: list[dict] = []

    async def fake_execute_confirmed_action(action: str, params: dict | None = None) -> dict:
        calls.append({"action": action, "params": dict(params or {})})
        return {"success": True, "data": {"dispatch_id": "dispatch_test", "queued": True}, "error": None}

    from aiask_agent.adapters import strategy_factory

    monkeypatch.setattr(strategy_factory, "execute_confirmed_action", fake_execute_confirmed_action)

    confirmed = asyncio.run(executor.confirm(intent["intent_id"]))
    assert confirmed["success"] is True
    assert calls == [{"action": "factory_run_once", "params": {"execution_mode": "dry_run"}}]
    assert store.get(intent["intent_id"])["status"] == "succeeded"

    repeated = asyncio.run(executor.confirm(intent["intent_id"]))
    assert repeated["success"] is False
    assert repeated["error_code"] == "INVALID_STATUS"


def test_confirm_does_not_execute_without_executing_transition(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(action="data_sync.sync", params={"codes": ["600519"]})
    original_transition = store.transition
    transition_attempts: list[dict] = []
    execution_calls: list[dict] = []

    def block_executing_transition(intent_id: str, **kwargs) -> IntentTransition:
        if kwargs.get("expected_status") == "confirmed" and kwargs.get("next_status") == "executing":
            transition_attempts.append({"intent_id": intent_id, **kwargs})
            return IntentTransition(changed=False, intent=store.get(intent_id))
        return original_transition(intent_id, **kwargs)

    async def fake_execute_confirmed_action(target_tool: str, action: str, params: dict | None = None) -> dict:
        execution_calls.append({"target_tool": target_tool, "action": action, "params": dict(params or {})})
        return {"success": True, "data": {"should_not_execute": True}, "error": None}

    from aiask_agent.adapters import confirmed_actions

    monkeypatch.setattr(store, "transition", block_executing_transition)
    monkeypatch.setattr(confirmed_actions, "execute_confirmed_action", fake_execute_confirmed_action)

    response = asyncio.run(executor.confirm(intent["intent_id"]))

    assert response["success"] is False
    assert response["error_code"] == "INVALID_STATUS"
    assert transition_attempts
    assert execution_calls == []
    assert store.get(intent["intent_id"])["status"] == "confirmed"


def test_confirm_resumes_confirmed_intent_without_recreating_approval(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(action="data_sync.sync", params={"codes": ["600519"]})
    _force_status(store, intent["intent_id"], "confirmed")
    calls: list[dict] = []

    async def fake_execute_confirmed_action(target_tool: str, action: str, params: dict | None = None) -> dict:
        calls.append({"target_tool": target_tool, "action": action, "params": dict(params or {})})
        return {"success": True, "data": {"resumed": True}, "error": None}

    from aiask_agent.adapters import confirmed_actions

    monkeypatch.setattr(confirmed_actions, "execute_confirmed_action", fake_execute_confirmed_action)

    response = asyncio.run(executor.confirm(intent["intent_id"]))

    assert response["success"] is True
    assert calls == [{"target_tool": "data_sync", "action": "sync", "params": {"codes": ["600519"]}}]
    stored = store.get(intent["intent_id"])
    assert stored["status"] == "succeeded"
    assert stored["result"]["data"]["resumed"] is True


def test_gateway_retry_intent_executes_only_after_confirm(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(action="gateway.retry", params={"message_id": "gwmsg_original", "session_id": "sess_gateway"})
    calls: list[str] = []

    async def fake_retry(self, message_id: str) -> dict:
        calls.append(message_id)
        return {
            "message": {"message_id": "gwmsg_retry"},
            "adapter": {"ok": True, "status": "sent"},
        }

    from aiask_agent.gateway import DeliveryRouter

    monkeypatch.setattr(DeliveryRouter, "retry", fake_retry)

    assert calls == []
    response = asyncio.run(executor.confirm(intent["intent_id"]))

    assert response["success"] is True
    assert calls == ["gwmsg_original"]
    assert response["data"]["execution_result"]["data"]["message"]["message_id"] == "gwmsg_retry"
    stored = store.get(intent["intent_id"])
    assert stored["status"] == "succeeded"
    assert stored["target_tool"] == "gateway"
    assert stored["target_action"] == "retry"


def test_external_message_intents_execute_only_after_confirm(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    gateway_calls: list[dict] = []
    feishu_reply_calls: list[dict] = []
    feishu_add_calls: list[dict] = []
    discord_admin_calls: list[dict] = []
    ha_calls: list[dict] = []

    async def fake_send(self, **kwargs) -> dict:
        gateway_calls.append(dict(kwargs))
        return {
            "message": {"message_id": f"gwmsg_{len(gateway_calls)}", "status": "delivered"},
            "adapter": {"ok": True, "status": "delivered"},
        }

    def fake_reply(self, **kwargs) -> dict:
        feishu_reply_calls.append(dict(kwargs))
        return {"configured": True, "file_token": kwargs["file_token"], "comment_id": kwargs["comment_id"], "response": {"ok": True}}

    def fake_add(self, **kwargs) -> dict:
        feishu_add_calls.append(dict(kwargs))
        return {"configured": True, "file_token": kwargs["file_token"], "response": {"ok": True}}

    def fake_discord_call(self, *, action: str, **kwargs) -> dict:
        discord_admin_calls.append({"action": action, **dict(kwargs)})
        return {"configured": True, "action": action, "ok": True, "data": {"id": "discord_admin"}}

    def fake_ha_request(method: str, path: str, payload: dict | None = None, **_kwargs) -> dict:
        ha_calls.append({"method": method, "path": path, "payload": dict(payload or {})})
        return {"ok": True, "changed": True}

    from aiask_agent import homeassistant as ha_native
    from aiask_agent.gateway import DeliveryRouter
    from aiask_agent.platform_apis import DiscordServerClient, FeishuClient

    monkeypatch.setattr(DeliveryRouter, "send", fake_send)
    monkeypatch.setattr(FeishuClient, "reply_comment", fake_reply)
    monkeypatch.setattr(FeishuClient, "add_comment", fake_add)
    monkeypatch.setattr(DiscordServerClient, "call", fake_discord_call)
    monkeypatch.setattr(ha_native, "_request", fake_ha_request)
    monkeypatch.setenv("HASS_URL", "http://ha.local")
    monkeypatch.setenv("HASS_TOKEN", "ha-token")

    gateway_intent = store.create(
        action="gateway.send_message",
        params={"platform": "webhook", "target": "ops", "message": "hello", "session_id": "sess"},
    )
    feishu_reply_intent = store.create(
        action="feishu_drive.reply_comment",
        params={"domain": "feishu", "file_token": "file_1", "comment_id": "comment_1", "message": "reply"},
    )
    feishu_add_intent = store.create(
        action="feishu_drive.add_comment",
        params={"domain": "feishu", "file_token": "file_1", "message": "comment"},
    )
    discord_intent = store.create(
        action="discord.channel_send",
        params={"channel_id": "channel_1", "message": "discord", "thread_id": "thread_1"},
    )
    discord_pin_intent = store.create(
        action="discord.pin_message",
        params={"channel_id": "channel_1", "message_id": "message_1"},
    )
    discord_role_intent = store.create(
        action="discord.add_role",
        params={"guild_id": "guild_1", "discord_user_id": "member_1", "role_id": "role_1"},
    )
    ha_intent = store.create(
        action="homeassistant.call_service",
        params={"domain": "light", "service": "turn_on", "entity_id": "light.desk", "data": {"brightness": 128}},
    )

    assert gateway_calls == []
    assert feishu_reply_calls == []
    assert feishu_add_calls == []
    assert discord_admin_calls == []
    assert ha_calls == []

    gateway_response = asyncio.run(executor.confirm(gateway_intent["intent_id"]))
    feishu_reply_response = asyncio.run(executor.confirm(feishu_reply_intent["intent_id"]))
    feishu_add_response = asyncio.run(executor.confirm(feishu_add_intent["intent_id"]))
    discord_response = asyncio.run(executor.confirm(discord_intent["intent_id"]))
    discord_pin_response = asyncio.run(executor.confirm(discord_pin_intent["intent_id"]))
    discord_role_response = asyncio.run(executor.confirm(discord_role_intent["intent_id"]))
    ha_response = asyncio.run(executor.confirm(ha_intent["intent_id"]))

    assert gateway_response["success"] is True
    assert feishu_reply_response["success"] is True
    assert feishu_add_response["success"] is True
    assert discord_response["success"] is True
    assert discord_pin_response["success"] is True
    assert discord_role_response["success"] is True
    assert ha_response["success"] is True
    assert gateway_calls[0]["platform"] == "webhook"
    assert gateway_calls[0]["target"] == "ops"
    assert gateway_calls[0]["message"] == "hello"
    assert gateway_calls[1]["platform"] == "discord"
    assert gateway_calls[1]["target"] == "channel_1"
    assert gateway_calls[1]["thread_id"] == "thread_1"
    assert feishu_reply_calls == [{"file_token": "file_1", "comment_id": "comment_1", "message": "reply"}]
    assert feishu_add_calls == [{"file_token": "file_1", "message": "comment"}]
    assert discord_admin_calls[0]["action"] == "pin_message"
    assert discord_admin_calls[0]["channel_id"] == "channel_1"
    assert discord_admin_calls[0]["message_id"] == "message_1"
    assert discord_admin_calls[1]["action"] == "add_role"
    assert discord_admin_calls[1]["guild_id"] == "guild_1"
    assert discord_admin_calls[1]["user_id"] == "member_1"
    assert discord_admin_calls[1]["role_id"] == "role_1"
    assert ha_calls == [
        {
            "method": "POST",
            "path": "/api/services/light/turn_on",
            "payload": {"brightness": 128, "entity_id": "light.desk"},
        }
    ]
    for intent in (
        gateway_intent,
        feishu_reply_intent,
        feishu_add_intent,
        discord_intent,
        discord_pin_intent,
        discord_role_intent,
        ha_intent,
    ):
        assert store.get(intent["intent_id"])["status"] == "succeeded"


def test_capability_management_intents_execute_only_after_confirm(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_MCP_CONFIG", str(tmp_path / "mcp_servers.json"))
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)

    skill_intent = store.create(
        action="skill.save",
        params={"name": "risk-review", "description": "Risk review", "content": "# Risk Review\nCheck drawdown."},
    )
    plugin_intent = store.create(
        action="plugin.set_enabled",
        params={"name": "audit-export", "enabled": True, "description": "Audit export plugin"},
    )
    acp_intent = store.create(
        action="acp.register_mcp_server",
        params={
            "name": "client-demo",
            "transport": "http",
            "url": "http://127.0.0.1:3100/mcp",
            "domain": "financial",
            "tools": [{"name": "quote", "description": "Quote lookup"}],
        },
    )
    pack_intent = store.create(
        action="skill_pack.install",
        params={"pack": "aiask-financial-modeling"},
    )

    skill_path = tmp_path / "home" / "skills" / "risk-review" / "SKILL.md"
    plugin_path = tmp_path / "home" / "plugins" / "audit-export" / "plugin.json"
    mcp_config_path = tmp_path / "mcp_servers.json"
    assert not skill_path.exists()
    assert not plugin_path.exists()
    assert not mcp_config_path.exists()

    skill_response = asyncio.run(executor.confirm(skill_intent["intent_id"]))
    plugin_response = asyncio.run(executor.confirm(plugin_intent["intent_id"]))
    acp_response = asyncio.run(executor.confirm(acp_intent["intent_id"]))
    pack_response = asyncio.run(executor.confirm(pack_intent["intent_id"]))

    assert skill_response["success"] is True
    assert plugin_response["success"] is True
    assert acp_response["success"] is True
    assert pack_response["success"] is True
    assert skill_path.exists()
    assert plugin_path.exists()
    assert json.loads(plugin_path.read_text(encoding="utf-8"))["enabled"] is True
    mcp_config = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    assert any(item.get("name") == "client-demo" for item in mcp_config.get("servers", []))
    assert (tmp_path / "home" / "skills" / "aiask-finance-dcf-model" / "SKILL.md").exists()
    for intent in (skill_intent, plugin_intent, acp_intent, pack_intent):
        assert store.get(intent["intent_id"])["status"] == "succeeded"


def test_dynamic_plugin_tool_intents_execute_only_after_confirm(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AIASK_AGENT_HOME", str(home))
    plugin_dir = home / "plugins" / "stateful"
    plugin_dir.mkdir(parents=True)
    plugin_dir.joinpath("stateful_runner.py").write_text(
        "from pathlib import Path\n"
        "def run(payload):\n"
        "    marker = payload['arguments'].get('marker')\n"
        "    if marker:\n"
        "        Path(marker).write_text('executed', encoding='utf-8')\n"
        "    return {'tool': payload['tool'], 'value': payload['arguments'].get('value')}\n",
        encoding="utf-8",
    )
    plugin_dir.joinpath("plugin.json").write_text(
        json.dumps(
            {
                "name": "stateful",
                "enabled": True,
                "tools": [
                    {
                        "name": "mutate",
                        "runner": {"type": "python_module", "module": "stateful_runner", "function": "run"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = build_default_tool_registry(
        store,
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    wrapped = "agent_plugin_stateful_mutate"
    marker = tmp_path / "plugin_executed.txt"

    for action in ("plugin.tool_call", "plugin.command_call"):
        generic = asyncio.run(registry.call_tool("agent_action_intent_create", {"action": action, "params": {}}))
        assert generic["success"] is False
        assert generic["error_code"] == "PLUGIN_INTENT_DIRECT_CREATE_BLOCKED"

    pending = asyncio.run(registry.call_tool(wrapped, {"value": 7, "marker": str(marker)}))
    intent = pending["data"]["intent"]
    assert pending["success"] is False
    assert pending["error_code"] == "ACTION_INTENT_REQUIRED"
    assert intent["action"] == "plugin.tool_call"
    assert intent["params"]["wrapped_name"] == wrapped
    assert intent["params"]["arguments"]["value"] == 7
    assert "_created_by_agent_plugin_tool" not in intent["params"]
    assert not marker.exists()

    confirmed = asyncio.run(IntentExecutor(store).confirm(intent["intent_id"]))
    assert confirmed["success"] is True
    plugin_call = confirmed["data"]["execution_result"]["data"]["plugin_call"]
    assert plugin_call["runner"] == "python_module"
    assert plugin_call["result"] == {"tool": "mutate", "value": 7}
    assert marker.read_text(encoding="utf-8") == "executed"
    assert store.get(intent["intent_id"])["status"] == "succeeded"

    forged_marker = tmp_path / "forged_plugin_executed.txt"
    forged = store.create(
        action="plugin.tool_call",
        params={
            "plugin": "stateful",
            "tool": "mutate",
            "kind": "tool",
            "wrapped_name": wrapped,
            "arguments": {"marker": str(forged_marker)},
        },
    )
    forged_result = asyncio.run(IntentExecutor(store).confirm(forged["intent_id"]))
    assert forged_result["success"] is False
    assert forged_result["data"]["execution_result"]["error_code"] == "PLUGIN_INTENT_SCOPE_BLOCKED"
    assert not forged_marker.exists()


def test_memory_learning_and_rl_intents_execute_only_after_confirm(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    state_path = tmp_path / "state.sqlite3"
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)

    from aiask_agent.learning_loop import LearningLoopStore
    from aiask_agent.rl_atropos import RLAtroposManager

    learning_store = LearningLoopStore(state_path)
    apply_proposal = learning_store.create(
        kind="memory",
        title="portfolio-memory",
        content="beta learning memory",
        metadata={"source": "test"},
    )

    def fake_start_training(self, *, environment=None, config_patch=None):
        env_name = str(environment or self.store.get_state("current_environment") or "").strip()
        if not env_name:
            raise ValueError("environment is required")
        config = self.edit_config(dict(config_patch or {}))["config"] if config_patch else dict(self.store.get_state("current_config", {}) or {})
        self.store.set_state("current_environment", env_name)
        run = self.store.create_run(
            environment=env_name,
            config=config,
            pid=None,
            log_path=None,
            status="running",
        )
        run["started"] = True
        run["configured"] = True
        return run

    monkeypatch.setattr(RLAtroposManager, "start_training", fake_start_training)

    memory_intent = store.create(
        action="memory.save",
        params={"content": "alpha memory", "symbol": "AAPL", "_state_path": str(state_path)},
    )
    apply_intent = store.create(
        action="learning.apply",
        params={"proposal_id": apply_proposal["proposal_id"], "_state_path": str(state_path)},
    )
    reflect_intent = store.create(
        action="learning.reflect_skill",
        params={"name": "risk-review", "observation": "Check drawdown.", "_state_path": str(state_path)},
    )
    rl_select_intent = store.create(
        action="rl.select_environment",
        params={"environment": "demo", "_state_path": str(state_path)},
    )
    rl_config_intent = store.create(
        action="rl.edit_config",
        params={"config": {"learning_rate": 0.01, "total_steps": 1000}, "_state_path": str(state_path)},
    )
    rl_start_intent = store.create(
        action="rl.start_training",
        params={"environment": "demo", "config": {"batch_size": 8}, "_state_path": str(state_path)},
    )
    rl_inference_intent = store.create(
        action="rl.test_inference",
        params={"prompt": "hello", "_state_path": str(state_path)},
    )

    assert "_state_path" not in public_intent(memory_intent)["params"]
    assert "_state_path" not in public_intent(apply_intent)["params"]
    assert "state.sqlite3" not in json.dumps(public_intent(memory_intent), ensure_ascii=False)

    memory_response = asyncio.run(executor.confirm(memory_intent["intent_id"]))
    apply_response = asyncio.run(executor.confirm(apply_intent["intent_id"]))
    reflect_response = asyncio.run(executor.confirm(reflect_intent["intent_id"]))
    rl_select_response = asyncio.run(executor.confirm(rl_select_intent["intent_id"]))
    rl_config_response = asyncio.run(executor.confirm(rl_config_intent["intent_id"]))
    rl_start_response = asyncio.run(executor.confirm(rl_start_intent["intent_id"]))
    run_id = rl_start_response["data"]["execution_result"]["data"]["run_id"]
    rl_stop_intent = store.create(
        action="rl.stop_training",
        params={"run_id": run_id, "_state_path": str(state_path)},
    )
    rl_stop_response = asyncio.run(executor.confirm(rl_stop_intent["intent_id"]))
    rl_inference_response = asyncio.run(executor.confirm(rl_inference_intent["intent_id"]))

    assert memory_response["success"] is True
    assert memory_response["data"]["execution_result"]["data"]["memory"]["symbol"] == "AAPL"
    assert apply_response["success"] is True
    assert apply_response["data"]["execution_result"]["data"]["proposal"]["status"] == "applied"
    assert reflect_response["success"] is True
    assert reflect_response["data"]["execution_result"]["data"]["proposal"]["status"] == "pending"
    assert rl_select_response["success"] is True
    assert rl_select_response["data"]["execution_result"]["data"]["current_environment"] == "demo"
    assert rl_config_response["success"] is True
    assert rl_config_response["data"]["execution_result"]["data"]["config"]["learning_rate"] == 0.01
    assert "total_steps" in rl_config_response["data"]["execution_result"]["data"]["rejected_locked_fields"]
    assert rl_start_response["success"] is True
    assert rl_start_response["data"]["execution_result"]["data"]["started"] is True
    assert rl_stop_response["success"] is True
    assert rl_stop_response["data"]["execution_result"]["data"]["run"]["status"] == "stopped"
    assert rl_inference_response["success"] is True
    assert rl_inference_response["data"]["execution_result"]["data"]["configured"] is False

    from aiask_agent.memory import FinancialMemoryStore
    from aiask_agent.rl_atropos import RLAtroposStore

    assert FinancialMemoryStore(state_path).search(query="alpha")[0]["symbol"] == "AAPL"
    assert FinancialMemoryStore(state_path).search(query="beta")[0]["research_topic"] == "learning_loop"
    assert learning_store.get(apply_proposal["proposal_id"])["status"] == "applied"
    assert learning_store.list(status="pending", limit=10)[0]["title"] == "risk-review-reflection"
    rl_store = RLAtroposStore(state_path)
    assert rl_store.get_state("current_environment") == "demo"
    assert rl_store.get_state("current_config") == {"batch_size": 8, "learning_rate": 0.01}
    assert rl_store.get_run(run_id)["status"] == "stopped"
    for intent in (
        memory_intent,
        apply_intent,
        reflect_intent,
        rl_select_intent,
        rl_config_intent,
        rl_start_intent,
        rl_stop_intent,
        rl_inference_intent,
    ):
        assert store.get(intent["intent_id"])["status"] == "succeeded"


def test_desktop_asset_confirmed_action_tool_is_gone() -> None:
    """The desktop_asset ActionIntent target proxied the removed Desktop service.

    It forwarded confirmed intents to AIASK_DESKTOP_API_BASE_URL (127.0.0.1:8001)
    to mutate strategies, stock pools and files. That service is not part of a
    backend-only tree, so the target is removed rather than left pointing at
    something unreachable.
    """
    import asyncio

    from aiask_agent.adapters import confirmed_actions

    assert not hasattr(confirmed_actions, "_execute_desktop_asset")
    assert not hasattr(confirmed_actions, "_execute_desktop_file_upload")

    result = asyncio.run(
        confirmed_actions.execute_confirmed_action(
            "desktop_asset", "strategy_create", {"user_id": "local", "payload": {}}
        )
    )
    # Falls through to "no executor registered" rather than reaching a proxy. The
    # reply echoes the requested target, which is what makes the refusal legible.
    assert result["success"] is False
    assert result["data"]["target_tool"] == "desktop_asset"
    assert "No confirmed-action executor is registered" in result["data"]["detail"]


def test_job_intents_execute_only_after_confirm(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)

    create_intent = store.create(
        action="job.create",
        params={
            "name": "daily review",
            "prompt": "review portfolio",
            "interval_seconds": 3600,
            "_state_path": str(state_path),
        },
    )
    assert "_state_path" not in public_intent(create_intent)["params"]

    create_response = asyncio.run(executor.confirm(create_intent["intent_id"]))

    assert create_response["success"] is True
    job = create_response["data"]["execution_result"]["data"]["job"]
    assert job["name"] == "daily review"
    job_id = job["job_id"]

    pause_intent = store.create(action="job.pause", params={"job_id": job_id, "_state_path": str(state_path)})
    update_intent = store.create(
        action="job.update",
        params={"job_id": job_id, "prompt": "updated review", "_state_path": str(state_path)},
    )
    resume_intent = store.create(action="job.resume", params={"job_id": job_id, "_state_path": str(state_path)})

    pause_response = asyncio.run(executor.confirm(pause_intent["intent_id"]))
    update_response = asyncio.run(executor.confirm(update_intent["intent_id"]))
    resume_response = asyncio.run(executor.confirm(resume_intent["intent_id"]))

    assert pause_response["success"] is True
    assert pause_response["data"]["execution_result"]["data"]["job"]["enabled"] is False
    assert update_response["success"] is True
    assert update_response["data"]["execution_result"]["data"]["job"]["prompt"] == "updated review"
    assert resume_response["success"] is True
    assert resume_response["data"]["execution_result"]["data"]["job"]["enabled"] is True

    from aiask_agent.scheduler import AgentJobStore

    jobs = AgentJobStore(state_path)
    assert jobs.get(job_id)["prompt"] == "updated review"

    remove_intent = store.create(action="job.remove", params={"job_id": job_id, "_state_path": str(state_path)})
    remove_response = asyncio.run(executor.confirm(remove_intent["intent_id"]))
    assert remove_response["success"] is True
    assert jobs.get(job_id) is None
    for intent in (create_intent, pause_intent, update_intent, resume_intent, remove_intent):
        assert store.get(intent["intent_id"])["status"] == "succeeded"


def test_file_intents_execute_only_after_confirm(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    registry = build_default_tool_registry(
        store,
        session_store=AgentSessionStore(state_path),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )

    write_pending = asyncio.run(
        registry.call_tool("agent_file_write", {"path": "notes.txt", "content": "original"})
    )
    write_intent = write_pending["data"]["intent"]
    assert write_pending["success"] is False
    assert write_pending["error_code"] == "ACTION_INTENT_REQUIRED"
    assert write_intent["action"] == "file.write"
    assert "_state_path" not in public_intent(store.get(write_intent["intent_id"]))["params"]
    assert not (tmp_path / "notes.txt").exists()

    write_response = asyncio.run(executor.confirm(write_intent["intent_id"]))
    assert write_response["success"] is True
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "original"

    patch_pending = asyncio.run(
        registry.call_tool("agent_file_patch", {"path": "notes.txt", "old": "original", "new": "changed"})
    )
    patch_intent = patch_pending["data"]["intent"]
    assert patch_pending["success"] is False
    assert patch_intent["action"] == "file.patch"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "original"

    patch_response = asyncio.run(executor.confirm(patch_intent["intent_id"]))
    assert patch_response["success"] is True
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "changed"
    checkpoint_id = patch_response["data"]["execution_result"]["data"]["checkpoint"]["checkpoint_id"]

    rollback_pending = asyncio.run(
        registry.call_tool("agent_file_rollback", {"checkpoint_id": checkpoint_id})
    )
    rollback_intent = rollback_pending["data"]["intent"]
    assert rollback_pending["success"] is False
    assert rollback_intent["action"] == "file.rollback"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "changed"

    rollback_response = asyncio.run(executor.confirm(rollback_intent["intent_id"]))
    assert rollback_response["success"] is True
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "original"

    for intent in (write_intent, patch_intent, rollback_intent):
        assert store.get(intent["intent_id"])["status"] == "succeeded"


def test_browser_intents_execute_with_runtime_only_after_confirm(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = build_default_tool_registry(
        store,
        session_store=AgentSessionStore(state_path),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    captured: list[dict] = []

    class _FakeBrowserToolRegistry:
        async def call_tool(self, name: str, arguments: dict) -> dict:
            from aiask_agent.control_context import control_context_active

            captured.append({"name": name, "arguments": dict(arguments), "control_context": control_context_active()})
            return {
                "success": True,
                "data": {"name": name, "arguments": dict(arguments)},
                "error": None,
                "meta": {
                    "side_effect": {
                        "level": "browser_state",
                        "target": arguments.get("selector") or name,
                        "confirmation_required": False,
                        "idempotent": False,
                    }
                },
            }

    class _FakeRuntime:
        tool_registry = _FakeBrowserToolRegistry()

    async def _execute_confirmed(target_tool: str, target_action: str, params: dict) -> dict:
        from aiask_agent.adapters.confirmed_actions import execute_confirmed_action

        return await execute_confirmed_action(target_tool, target_action, params, runtime=_FakeRuntime())

    executor = IntentExecutor(store, confirmed_action_executor=_execute_confirmed)

    pending = asyncio.run(registry.call_tool("agent_browser_click", {"selector": "#buy"}))
    intent = pending["data"]["intent"]
    assert pending["success"] is False
    assert pending["error_code"] == "ACTION_INTENT_REQUIRED"
    assert intent["action"] == "browser.click"
    assert "_created_by_agent_browser_tool" not in intent["params"]

    confirmed = asyncio.run(executor.confirm(intent["intent_id"]))

    assert confirmed["success"] is True
    assert captured == [
        {
            "name": "agent_browser_click",
            "arguments": {"selector": "#buy"},
            "control_context": True,
        }
    ]
    assert store.get(intent["intent_id"])["status"] == "succeeded"
    execution_result = confirmed["data"]["execution_result"]
    assert execution_result["data"]["name"] == "agent_browser_click"

    forged = store.create(action="browser.click", params={"selector": "#forged"})
    forged_result = asyncio.run(executor.confirm(forged["intent_id"]))

    assert forged_result["success"] is False
    assert forged_result["data"]["execution_result"]["error_code"] == "BROWSER_INTENT_SCOPE_BLOCKED"
    assert store.get(forged["intent_id"])["status"] == "failed"
    assert len(captured) == 1


def test_execution_intents_execute_with_runtime_only_after_confirm(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    registry = build_default_tool_registry(
        store,
        session_store=AgentSessionStore(state_path),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    captured: list[dict] = []

    class _FakeExecutionToolRegistry:
        async def call_tool(self, name: str, arguments: dict) -> dict:
            from aiask_agent.control_context import control_context_active

            captured.append({"name": name, "arguments": dict(arguments), "control_context": control_context_active()})
            return {
                "success": True,
                "data": {"name": name, "arguments": dict(arguments)},
                "error": None,
                "meta": {
                    "side_effect": {
                        "level": "process_execution" if name == "agent_terminal" else "code_execution",
                        "target": arguments.get("cwd") or arguments.get("process_id") or name,
                        "confirmation_required": False,
                        "idempotent": False,
                    }
                },
            }

    class _FakeRuntime:
        tool_registry = _FakeExecutionToolRegistry()

    async def _execute_confirmed(target_tool: str, target_action: str, params: dict) -> dict:
        from aiask_agent.adapters.confirmed_actions import execute_confirmed_action

        return await execute_confirmed_action(target_tool, target_action, params, runtime=_FakeRuntime())

    executor = IntentExecutor(store, confirmed_action_executor=_execute_confirmed)

    terminal_pending = asyncio.run(registry.call_tool("agent_terminal", {"command": "echo ok", "cwd": "."}))
    process_pending = asyncio.run(registry.call_tool("agent_process", {"action": "kill", "process_id": "proc_demo"}))
    code_pending = asyncio.run(registry.call_tool("agent_execute_python", {"code": "print(5)", "cwd": "."}))

    terminal_intent = terminal_pending["data"]["intent"]
    process_intent = process_pending["data"]["intent"]
    code_intent = code_pending["data"]["intent"]
    assert terminal_pending["error_code"] == "ACTION_INTENT_REQUIRED"
    assert process_pending["error_code"] == "ACTION_INTENT_REQUIRED"
    assert code_pending["error_code"] == "ACTION_INTENT_REQUIRED"
    assert terminal_intent["action"] == "terminal.run"
    assert process_intent["action"] == "process.kill"
    assert code_intent["action"] == "code.execute_python"
    assert "_created_by_agent_terminal_tool" not in terminal_intent["params"]
    assert "_created_by_agent_process_tool" not in process_intent["params"]
    assert "_created_by_agent_code_tool" not in code_intent["params"]

    terminal_confirmed = asyncio.run(executor.confirm(terminal_intent["intent_id"]))
    process_confirmed = asyncio.run(executor.confirm(process_intent["intent_id"]))
    code_confirmed = asyncio.run(executor.confirm(code_intent["intent_id"]))

    assert terminal_confirmed["success"] is True
    assert process_confirmed["success"] is True
    assert code_confirmed["success"] is True
    assert captured == [
        {
            "name": "agent_terminal",
            "arguments": {"command": "echo ok", "cwd": str(tmp_path.resolve()), "backend": "local"},
            "control_context": True,
        },
        {
            "name": "agent_process",
            "arguments": {"action": "kill", "process_id": "proc_demo"},
            "control_context": True,
        },
        {
            "name": "agent_execute_python",
            "arguments": {"code": "print(5)", "cwd": str(tmp_path.resolve())},
            "control_context": True,
        },
    ]
    for intent in (terminal_intent, process_intent, code_intent):
        assert store.get(intent["intent_id"])["status"] == "succeeded"

    forged_terminal = store.create(action="terminal.run", params={"command": "echo forged"})
    forged_process = store.create(action="process.kill", params={"process_id": "proc_forged"})
    forged_code = store.create(action="code.execute_python", params={"code": "print('forged')"})

    terminal_forged_result = asyncio.run(executor.confirm(forged_terminal["intent_id"]))
    process_forged_result = asyncio.run(executor.confirm(forged_process["intent_id"]))
    code_forged_result = asyncio.run(executor.confirm(forged_code["intent_id"]))

    assert terminal_forged_result["success"] is False
    assert terminal_forged_result["data"]["execution_result"]["error_code"] == "TERMINAL_INTENT_SCOPE_BLOCKED"
    assert process_forged_result["success"] is False
    assert process_forged_result["data"]["execution_result"]["error_code"] == "PROCESS_INTENT_SCOPE_BLOCKED"
    assert code_forged_result["success"] is False
    assert code_forged_result["data"]["execution_result"]["error_code"] == "CODE_INTENT_SCOPE_BLOCKED"
    assert len(captured) == 3


def test_gateway_control_intents_execute_only_after_confirm(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AIASK_AGENT_HOME", str(home))
    home.mkdir(parents=True)
    (home / "gateway_config.json").write_text(
        json.dumps(
            {
                "platforms": {
                    "local": {
                        "enabled": True,
                        "home_channel": "desktop-home",
                    }
                }
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    state_path = tmp_path / "state.sqlite3"
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)

    pairing_intent = store.create(
        action="gateway.pairing_create",
        params={"platform": "discord", "user_id": "user-1", "session_id": "sess-1", "_state_path": str(state_path)},
    )
    upsert_intent = store.create(
        action="gateway.directory_upsert",
        params={
            "platform": "local",
            "name": "ops",
            "target": "desktop",
            "thread_id": "thread-1",
            "_state_path": str(state_path),
        },
    )
    refresh_intent = store.create(action="gateway.directory_refresh", params={"_state_path": str(state_path)})
    platform_start_intent = store.create(action="gateway.platform_start", params={"platform": "feishu"})
    platform_stop_intent = store.create(action="gateway.platform_stop", params={"platform": "feishu"})
    assert "_state_path" not in public_intent(upsert_intent)["params"]

    import aiask_agent.gateway as gateway_native
    from aiask_agent.gateway import GatewayChannelDirectoryStore

    directory = GatewayChannelDirectoryStore(state_path)
    assert directory.resolve(platform="local", name="ops", kind="channel") is None
    platform_calls: list[str] = []

    class FakeAdapter:
        async def start(self):
            platform_calls.append("start")
            return {"ok": True, "status": "started", "access_token": "platform-start-secret"}

        async def stop(self):
            platform_calls.append("stop")
            return {"ok": True, "status": "stopped", "clientSecret": "platform-stop-secret"}

    monkeypatch.setattr(gateway_native, "adapter_for", lambda status: FakeAdapter())

    pairing_response = asyncio.run(executor.confirm(pairing_intent["intent_id"]))
    upsert_response = asyncio.run(executor.confirm(upsert_intent["intent_id"]))
    refresh_response = asyncio.run(executor.confirm(refresh_intent["intent_id"]))
    start_response = asyncio.run(executor.confirm(platform_start_intent["intent_id"]))
    stop_response = asyncio.run(executor.confirm(platform_stop_intent["intent_id"]))

    assert pairing_response["success"] is True
    assert pairing_response["data"]["execution_result"]["data"]["action"] == "create"
    assert upsert_response["success"] is True
    assert upsert_response["data"]["execution_result"]["data"]["item"]["target"] == "desktop"
    assert refresh_response["success"] is True
    assert refresh_response["data"]["execution_result"]["data"]["refreshed_count"] == 1
    assert start_response["success"] is True
    assert start_response["data"]["execution_result"]["data"]["status"] == "started"
    assert start_response["data"]["execution_result"]["data"]["access_token"] == "[redacted]"
    assert stop_response["success"] is True
    assert stop_response["data"]["execution_result"]["data"]["status"] == "stopped"
    assert stop_response["data"]["execution_result"]["data"]["clientSecret"] == "[redacted]"
    assert platform_calls == ["start", "stop"]

    ops = directory.resolve(platform="local", name="ops", kind="channel")
    assert ops is not None
    assert ops["target"] == "desktop"
    home_alias = directory.resolve(platform="local", name="home", kind="channel")
    assert home_alias is not None
    assert home_alias["target"] == "desktop-home"
    for intent in (pairing_intent, upsert_intent, refresh_intent, platform_start_intent, platform_stop_intent):
        assert store.get(intent["intent_id"])["status"] == "succeeded"


def test_webhook_intents_execute_only_after_confirm(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state.sqlite3"
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    deliveries: list[dict] = []

    async def fake_send(self, **kwargs) -> dict:
        deliveries.append(dict(kwargs))
        return {
            "message": {"message_id": f"gwmsg_{len(deliveries)}", "status": "delivered"},
            "adapter": {"ok": True, "status": "delivered"},
        }

    from aiask_agent.gateway import DeliveryRouter
    from aiask_agent.webhooks import WebhookStore

    monkeypatch.setattr(DeliveryRouter, "send", fake_send)

    direct_deliver = json.dumps(
        {
            "mode": "direct_platform",
            "platform": "local",
            "target": "ops",
            "thread_id": "thread-1",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    subscribe_intent = store.create(
        action="webhook.subscribe",
        params={
            "name": "factory-alerts",
            "events": ["factory.done"],
            "prompt": "Handle {event}: {payload}",
            "deliver": direct_deliver,
            "_state_path": str(state_path),
        },
    )
    assert "_state_path" not in public_intent(subscribe_intent)["params"]
    webhooks = WebhookStore(state_path)
    assert webhooks.list() == []

    subscribe_response = asyncio.run(executor.confirm(subscribe_intent["intent_id"]))

    assert subscribe_response["success"] is True
    webhook = subscribe_response["data"]["execution_result"]["data"]["webhook"]
    webhook_id = webhook["webhook_id"]
    assert webhook["name"] == "factory-alerts"
    assert webhooks.get(webhook_id) is not None
    assert deliveries == []

    trigger_intent = store.create(
        action="webhook.trigger",
        params={
            "webhook_id": webhook_id,
            "event": "factory.done",
            "payload": {"run_id": "run-demo"},
            "_state_path": str(state_path),
        },
    )
    trigger_response = asyncio.run(executor.confirm(trigger_intent["intent_id"]))

    assert trigger_response["success"] is True
    execution_data = trigger_response["data"]["execution_result"]["data"]
    assert execution_data["prompt"] == 'Handle factory.done: {"run_id": "run-demo"}'
    assert execution_data["direct_delivery"]["message"]["message_id"] == "gwmsg_1"
    assert deliveries == [
        {
            "platform": "local",
            "target": "ops",
            "thread_id": "thread-1",
            "message": 'Handle factory.done: {"run_id": "run-demo"}',
        }
    ]

    remove_intent = store.create(
        action="webhook.remove",
        params={"webhook_id": webhook_id, "_state_path": str(state_path)},
    )
    remove_response = asyncio.run(executor.confirm(remove_intent["intent_id"]))

    assert remove_response["success"] is True
    assert remove_response["data"]["execution_result"]["data"]["deleted"] is True
    assert webhooks.get(webhook_id) is None
    for intent in (subscribe_intent, trigger_intent, remove_intent):
        assert store.get(intent["intent_id"])["status"] == "succeeded"


def test_confirm_revalidates_stored_intent_target_before_executing(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(action="data_sync.sync", params={"codes": ["600519"]})
    _force_target(store, intent["intent_id"], target_tool="data_sync", target_action="schedule")
    calls: list[dict] = []

    async def fake_execute_confirmed_action(target_tool: str, action: str, params: dict | None = None) -> dict:
        calls.append({"target_tool": target_tool, "action": action, "params": dict(params or {})})
        return {"success": True, "data": {"should_not_execute": True}, "error": None}

    from aiask_agent.adapters import confirmed_actions

    monkeypatch.setattr(confirmed_actions, "execute_confirmed_action", fake_execute_confirmed_action)

    response = asyncio.run(executor.confirm(intent["intent_id"]))

    assert response["success"] is False
    assert response["error_code"] == "INTENT_EXECUTION_SCOPE_BLOCKED"
    assert calls == []
    stored = store.get(intent["intent_id"])
    assert stored["status"] == "failed"
    assert stored["result"]["error_code"] == "INTENT_EXECUTION_SCOPE_BLOCKED"


def test_confirm_revalidates_financial_dry_run_policy_before_executing(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(
        action="paper_trading_manager.submit_order",
        params={"code": "600519", "quantity": 100},
    )
    _force_params(store, intent["intent_id"], {"code": "600519", "quantity": 100, "dry_run": False})
    calls: list[dict] = []

    async def fake_execute_confirmed_action(target_tool: str, action: str, params: dict | None = None) -> dict:
        calls.append({"target_tool": target_tool, "action": action, "params": dict(params or {})})
        return {"success": True, "data": {"should_not_execute": True}, "error": None}

    from aiask_agent.adapters import confirmed_actions

    monkeypatch.setattr(confirmed_actions, "execute_confirmed_action", fake_execute_confirmed_action)

    response = asyncio.run(executor.confirm(intent["intent_id"]))

    assert response["success"] is False
    assert response["error_code"] == "INTENT_EXECUTION_SCOPE_BLOCKED"
    assert calls == []
    stored = store.get(intent["intent_id"])
    assert stored["status"] == "failed"
    assert stored["result"]["error_code"] == "INTENT_EXECUTION_SCOPE_BLOCKED"
    assert stored["result"]["error"] == "FINANCIAL_MANAGER_DRY_RUN_REQUIRED"


def test_stale_executing_intent_becomes_unknown_and_is_not_reexecuted(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(action="data_sync.sync", params={"codes": ["600519"]})
    _force_executing_updated_at(
        store,
        intent["intent_id"],
        datetime.now(timezone.utc) - timedelta(hours=3),
    )
    calls: list[dict] = []

    async def fake_execute_confirmed_action(target_tool: str, action: str, params: dict | None = None) -> dict:
        calls.append({"target_tool": target_tool, "action": action, "params": dict(params or {})})
        return {"success": True, "data": {"should_not_execute": True}, "error": None}

    from aiask_agent.adapters import confirmed_actions

    monkeypatch.setattr(confirmed_actions, "execute_confirmed_action", fake_execute_confirmed_action)

    response = asyncio.run(executor.confirm(intent["intent_id"]))

    assert response["success"] is False
    assert response["error_code"] == "INVALID_STATUS"
    assert calls == []
    stored = store.get(intent["intent_id"])
    assert stored["status"] == "execution_unknown"
    assert stored["result"]["status"] == "execution_unknown"
    assert stored["result"]["reconciliation_required"] is True
    assert stored["error"] == "intent execution result is unknown after stale executing timeout"


def test_finalize_execution_only_writes_terminal_result_from_executing(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    intent = store.create(action="data_sync.sync", params={"codes": ["600519"]})
    _force_status(store, intent["intent_id"], "confirmed")

    rejected = store.finalize_execution(
        intent["intent_id"],
        "succeeded",
        result={"success": True, "data": {"unsafe": "not-from-executing"}},
    )

    assert rejected.changed is False
    assert store.get(intent["intent_id"])["status"] == "confirmed"

    claimed = store.transition(intent["intent_id"], expected_status="confirmed", next_status="executing")
    assert claimed.changed is True
    finalized = store.finalize_execution(
        intent["intent_id"],
        "succeeded",
        result={"success": True, "data": {"safe": "from-executing"}},
    )

    assert finalized.changed is True
    assert finalized.intent["status"] == "succeeded"
    assert finalized.intent["result"]["data"]["safe"] == "from-executing"


def test_financial_manager_intent_creation_enforces_confirmed_scope_and_dry_run(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")

    with pytest.raises(ValueError):
        store.create(
            action="paper_trading_manager.submit_order",
            params={"code": "600519", "quantity": 100, "dry_run": False},
        )
    with pytest.raises(ValueError):
        store.create(action="paper_trading_manager.cancel_order", params={"order_id": "paper_1", "dry_run": True})

    intent = store.create(
        action="paper_trading_manager.submit_order",
        params={"code": "600519", "quantity": 100},
    )

    assert intent["target_tool"] == "financial_manager"
    assert intent["target_action"] == "paper_trading_manager.submit_order"
    assert intent["params"]["dry_run"] is True


def test_financial_manager_executor_rejects_legacy_non_dry_run_params(tmp_path) -> None:
    from aiask_agent.adapters import confirmed_actions

    result = asyncio.run(
        confirmed_actions.execute_confirmed_action(
            "financial_manager",
            "paper_trading_manager.submit_order",
            {"code": "600519", "quantity": 100, "dry_run": False},
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "FINANCIAL_MANAGER_DRY_RUN_REQUIRED"


def test_confirmed_action_executor_blocks_direct_unallowlisted_call(tmp_path) -> None:
    from aiask_agent.adapters import confirmed_actions

    result = asyncio.run(
        confirmed_actions.execute_confirmed_action(
            "data_sync",
            "schedule",
            {"codes": ["600519"], "cron": "0 9 * * *"},
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "CONFIRMED_ACTION_SCOPE_BLOCKED"


def test_confirmed_action_executor_redacts_downstream_public_result(monkeypatch) -> None:
    from aiask_agent.adapters import confirmed_actions

    calls: list[dict] = []

    async def fake_execute_data_sync(action: str, params: dict) -> dict:
        calls.append({"action": action, "params": dict(params)})
        return {
            "success": False,
            "data": {
                "broker_token": "downstream-data-token",
                "nested": {
                    "callback": "https://broker.local/cb?password=downstream-url-secret",
                    "headers": {"Authorization": "Bearer downstream-header-secret-123456"},
                },
            },
            "error": (
                "failed with Authorization: Bearer downstream-error-secret-123456 "
                "broker_token=downstream-error-token"
            ),
            "meta": {"debug": "password=downstream-meta-secret"},
        }

    monkeypatch.setattr(confirmed_actions, "_execute_data_sync", fake_execute_data_sync)

    result = asyncio.run(
        confirmed_actions.execute_confirmed_action(
            "data_sync",
            "sync",
            {"codes": ["600519"], "broker_token": "raw-param-secret"},
        )
    )

    assert calls == [{"action": "sync", "params": {"codes": ["600519"], "broker_token": "raw-param-secret"}}]
    assert result["success"] is False
    assert result["secrets_redacted"] is True
    result_json = json.dumps(result, ensure_ascii=False)
    for leaked in (
        "downstream-data-token",
        "downstream-url-secret",
        "downstream-header-secret",
        "downstream-error-secret",
        "downstream-error-token",
        "downstream-meta-secret",
        "raw-param-secret",
    ):
        assert leaked not in result_json
    assert result["data"]["broker_token"] == "[redacted]"
    assert result["error"] == "failed with Authorization: Bearer [redacted] broker_token=[redacted]"


def test_public_intent_redacts_without_mutating_stored_execution_params(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    intent = store.create(
        action="data_sync.sync",
        params={
            "codes": ["600519"],
            "broker_token": "broker-secret-value",
            "nested": {"password": "nested-secret-value"},
            "note": "Authorization: Bearer inline-secret-value",
        },
        rationale="created with broker_token=rationale-secret and Authorization: Bearer rationale-bearer-secret",
    )

    raw = store.get(intent["intent_id"])
    public = public_intent(raw)

    assert raw is not None
    assert public is not None
    assert raw["params"]["broker_token"] == "broker-secret-value"
    assert raw["params"]["nested"]["password"] == "nested-secret-value"
    assert "rationale-secret" in raw["rationale"]

    public_json = json.dumps(public, ensure_ascii=False)
    assert public["secrets_redacted"] is True
    assert "broker-secret-value" not in public_json
    assert "nested-secret-value" not in public_json
    assert "inline-secret-value" not in public_json
    assert "rationale-secret" not in public_json
    assert "rationale-bearer-secret" not in public_json

    still_raw = store.get(intent["intent_id"])
    assert still_raw["params"]["broker_token"] == "broker-secret-value"


def test_executor_public_response_redacts_result_but_executes_raw_params(tmp_path, monkeypatch) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(
        action="data_sync.sync",
        params={"codes": ["600519"], "broker_token": "raw-param-secret"},
    )
    calls: list[dict] = []

    async def fake_execute_confirmed_action(target_tool: str, action: str, params: dict | None = None) -> dict:
        calls.append({"target_tool": target_tool, "action": action, "params": dict(params or {})})
        return {
            "success": False,
            "data": {"broker_token": "result-secret-value"},
            "error": "Authorization: Bearer result-bearer-secret",
        }

    from aiask_agent.adapters import confirmed_actions

    monkeypatch.setattr(confirmed_actions, "execute_confirmed_action", fake_execute_confirmed_action)

    response = asyncio.run(executor.confirm(intent["intent_id"]))

    assert calls == [
        {
            "target_tool": "data_sync",
            "action": "sync",
            "params": {"codes": ["600519"], "broker_token": "raw-param-secret"},
        }
    ]
    assert response["success"] is False
    assert response["secrets_redacted"] is True
    response_json = json.dumps(response, ensure_ascii=False)
    assert "raw-param-secret" not in response_json
    assert "result-secret-value" not in response_json
    assert "result-bearer-secret" not in response_json

    stored = store.get(intent["intent_id"])
    assert stored["params"]["broker_token"] == "raw-param-secret"
    assert stored["result"]["data"]["broker_token"] == "result-secret-value"
    assert "result-bearer-secret" in stored["error"]


def test_deny_public_response_redacts_reason_but_keeps_raw_audit_value(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(action="data_sync.sync", params={"codes": ["600519"]})

    response = asyncio.run(
        executor.deny(
            intent["intent_id"],
            reason="blocked because password=deny-secret and Authorization: Bearer deny-bearer-secret",
        )
    )

    assert response["success"] is True
    assert response["secrets_redacted"] is True
    response_json = json.dumps(response, ensure_ascii=False)
    assert "deny-secret" not in response_json
    assert "deny-bearer-secret" not in response_json
    stored = store.get(intent["intent_id"])
    assert "deny-secret" in stored["error"]
    assert "deny-bearer-secret" in stored["error"]


def test_deny_rejects_repeat_confirm(tmp_path) -> None:
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(action="runtime_alert_ack", params={"alert_id": "a1"})

    denied = asyncio.run(executor.deny(intent["intent_id"], reason="not now"))
    assert denied["success"] is True
    assert store.get(intent["intent_id"])["status"] == "denied"

    confirmed = asyncio.run(executor.confirm(intent["intent_id"]))
    assert confirmed["success"] is False
    assert confirmed["error_code"] == "INVALID_STATUS"


def _force_expire(store: ActionIntentStore, intent_id: str) -> None:
    """Backdate expires_at so the intent is past its TTL."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(str(store.path))) as conn:
        conn.execute(
            "UPDATE action_intents SET expires_at = ? WHERE intent_id = ?",
            ("2000-01-01T00:00:00+00:00", intent_id),
        )
        conn.commit()


def test_get_on_expired_intent_does_not_recurse(tmp_path) -> None:
    # Regression: an awaiting_confirmation intent past its TTL used to recurse
    # infinitely (get -> update_status -> get -> ...) and raise
    # "maximum recursion depth exceeded" on GET /intents/{id}.
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    intent = store.create(action="data_sync.sync", params={"codes": ["600519"]})
    _force_expire(store, intent["intent_id"])

    fetched = store.get(intent["intent_id"])
    assert fetched is not None
    assert fetched["status"] == "expired"
    # A second get stays expired (no flip-flop / no recursion).
    assert store.get(intent["intent_id"])["status"] == "expired"


def test_deny_on_expired_intent_is_graceful(tmp_path) -> None:
    # Regression: deny() on an expired intent used to surface a 500 because
    # the underlying get() recursed. It should now return a clean failure.
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)
    intent = store.create(action="data_sync.sync", params={"codes": ["600519"]})
    _force_expire(store, intent["intent_id"])

    result = asyncio.run(executor.deny(intent["intent_id"], reason="cleanup"))
    # The intent already expired, so deny is rejected with a defined error
    # code rather than crashing.
    assert result["success"] is False
    assert result["error_code"] == "INVALID_STATUS"
    assert store.get(intent["intent_id"])["status"] == "expired"
