from __future__ import annotations

import asyncio

import pytest

from aiask_agent.intents import ALLOWED_ACTIONS, ActionIntentStore, IntentExecutor
from aiask_agent.scheduler import AgentJobStore, BackgroundScheduler
from aiask_agent.tool_registry import AgentToolRegistry
from aiask_agent.tools.policy import ToolPolicy, ToolPolicyEngine


class _FakeSessionStore:
    def __init__(self, path) -> None:
        self.path = path


class _FakeRunResult:
    response_id = "resp_job"
    run_id = "run_job"

    def __init__(self, content: str) -> None:
        self.content = content


class _PromptCapturingRuntime:
    def __init__(self, tmp_path, workspace_root) -> None:
        self.session_store = _FakeSessionStore(tmp_path / "runtime.sqlite3")
        self.tool_registry = AgentToolRegistry(
            policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(workspace_root),)))
        )
        self.messages: list[list[dict]] = []

    async def run(self, messages, *args, **kwargs):
        self.messages.append(list(messages))
        return _FakeRunResult(str(messages[-1]["content"]))


@pytest.mark.parametrize("action", ["run_once", "push_digest", "schedule_update"])
def test_stock_radar_actions_are_allowlisted_for_intent(action: str) -> None:
    descriptor = ALLOWED_ACTIONS[f"stock_radar.{action}"]
    assert descriptor == {"tool": "stock_radar", "action": action}


def test_stock_radar_intent_create_does_not_execute_without_confirm(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    async def fake_execute(tool: str, action: str, params: dict):
        calls.append((tool, action, params))
        return {"success": True, "data": {"ok": True}, "error": None}

    monkeypatch.setattr("aiask_agent.adapters.confirmed_actions.execute_confirmed_action", fake_execute)
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    intent = store.create(
        action="stock_radar.run_once",
        params={"days": 3, "limit": 5, "allow_network": False},
        rationale="test stock radar",
    )

    assert intent["status"] == "awaiting_confirmation"
    assert intent["target_tool"] == "stock_radar"
    assert intent["target_action"] == "run_once"
    assert calls == []


def test_stock_radar_intent_confirm_dispatches_executor(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    async def fake_execute(tool: str, action: str, params: dict):
        calls.append((tool, action, params))
        return {"success": True, "data": {"status": "completed"}, "error": None}

    monkeypatch.setattr("aiask_agent.adapters.confirmed_actions.execute_confirmed_action", fake_execute)
    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    intent = store.create(
        action="stock_radar.push_digest",
        params={"channels": ["wecom", "telegram"], "dry_run": True},
        rationale="preview push digest",
    )

    result = asyncio.run(IntentExecutor(store).confirm(intent["intent_id"]))

    assert result["success"] is True
    assert calls == [("stock_radar", "push_digest", {"channels": ["wecom", "telegram"], "dry_run": True})]
    assert store.get(intent["intent_id"])["status"] == "succeeded"


def test_stock_radar_schedule_update_persists_agent_job(tmp_path, monkeypatch) -> None:
    import aiask_agent.adapters.confirmed_actions as confirmed_actions

    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))

    first = asyncio.run(
        confirmed_actions.execute_confirmed_action(
            "stock_radar",
            "schedule_update",
            {"interval_seconds": 3600, "enabled": True, "days": 2, "limit": 12, "allow_network": False, "allow_llm": False},
        )
    )

    assert first["success"] is True
    assert first["data"]["object"] == "stock_radar.schedule_update"
    assert first["data"]["status"] == "scheduled"
    assert first["data"]["preview"] is False
    assert first["data"]["enabled"] is True
    assert first["data"]["interval_seconds"] == 3600
    job_id = first["data"]["job_id"]
    stored = AgentJobStore().get(job_id)
    assert stored is not None
    assert stored["enabled"] is True
    assert stored["toolset"] == "finance_safe"
    assert stored["payload"]["stock_radar"] is True
    assert stored["payload"]["run_params"]["days"] == 2
    assert stored["payload"]["run_params"]["limit"] == 12

    second = asyncio.run(
        confirmed_actions.execute_confirmed_action(
            "stock_radar",
            "schedule_update",
            {"interval_seconds": 7200, "enabled": True, "days": 4, "limit": 24},
        )
    )

    assert second["success"] is True
    assert second["data"]["status"] == "updated"
    assert second["data"]["job_id"] == job_id
    updated = AgentJobStore().get(job_id)
    assert updated is not None
    assert updated["interval_seconds"] == 7200
    assert updated["payload"]["run_params"]["days"] == 4
    assert len([job for job in AgentJobStore().list() if job["payload"].get("stock_radar")]) == 1


def test_stock_radar_scheduled_job_executes_adapter(tmp_path, monkeypatch) -> None:
    import aiask_agent.adapters.confirmed_actions as confirmed_actions

    script = tmp_path / "should_not_run.py"
    script.write_text("print('should not run')\n", encoding="utf-8")
    store = AgentJobStore(tmp_path / "jobs.sqlite3")
    job = store.create(
        name="radar job",
        prompt="radar",
        interval_seconds=3600,
        toolset="finance_safe",
        enabled=True,
        payload={
            "stock_radar": True,
            "action": "run_once",
            "run_params": {"days": 2, "limit": 10},
            "script": str(script),
        },
    )
    calls: list[tuple[str, dict]] = []

    async def fake_execute(action: str, params: dict):
        calls.append((action, params))
        return {"success": True, "data": {"object": "stock_radar.run", "status": "completed"}, "error": None}

    class FakeSessionStore:
        path = tmp_path / "runtime.sqlite3"

    class FakeRuntime:
        session_store = FakeSessionStore()

        async def run(self, *args, **kwargs):
            raise AssertionError("stock radar jobs should execute the adapter directly")

    monkeypatch.setattr(confirmed_actions, "_execute_stock_radar", fake_execute)
    monkeypatch.setattr(
        "aiask_agent.scheduler.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stock radar job must not execute script payload")),
    )

    result = asyncio.run(BackgroundScheduler(runtime=FakeRuntime(), store=store).run_job(job["job_id"]))

    assert result["success"] is True
    assert result["data"]["stock_radar"]["object"] == "stock_radar.run"
    assert calls == [("run_once", {"days": 2, "limit": 10})]
    assert store.list_runs(job["job_id"], limit=1)[0]["status"] == "completed"


def test_scheduled_job_executes_script_only_inside_workspace(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "daily_review.py"
    script.write_text("print('workspace script')\n", encoding="utf-8")
    store = AgentJobStore(tmp_path / "jobs.sqlite3")
    job = store.create(
        name="workspace script job",
        prompt="review",
        interval_seconds=3600,
        toolset="finance_safe",
        enabled=True,
        payload={"script": str(script), "silent_pattern": "SCRIPT_OK"},
    )
    runtime = _PromptCapturingRuntime(tmp_path, workspace)
    calls: list[tuple] = []

    class Completed:
        stdout = "SCRIPT_OK\n"
        stderr = ""

    def fake_run(*args, **kwargs):
        calls.append(args)
        assert args[0] == [str(script)]
        assert "SECRET_TOKEN" not in kwargs.get("env", {})
        return Completed()

    monkeypatch.setenv("SECRET_TOKEN", "must-not-leak")
    monkeypatch.setattr("aiask_agent.scheduler.subprocess.run", fake_run)

    result = asyncio.run(BackgroundScheduler(runtime=runtime, store=store).run_job(job["job_id"]))

    assert result["success"] is True
    assert calls == [([str(script)],)]
    prompt = runtime.messages[0][-1]["content"]
    assert "[SCRIPT stdout]\nSCRIPT_OK" in prompt
    assert result["data"]["silent"] is True
    assert store.list_runs(job["job_id"], limit=1)[0]["status"] == "completed"


def test_scheduled_job_blocks_script_outside_workspace(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    script = outside / "escape.py"
    script.write_text("print('escape')\n", encoding="utf-8")
    store = AgentJobStore(tmp_path / "jobs.sqlite3")
    job = store.create(
        name="outside script job",
        prompt="review",
        interval_seconds=3600,
        toolset="finance_safe",
        enabled=True,
        payload={"script": str(script)},
    )
    runtime = _PromptCapturingRuntime(tmp_path, workspace)

    monkeypatch.setattr(
        "aiask_agent.scheduler.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("outside workspace script must not execute")),
    )

    result = asyncio.run(BackgroundScheduler(runtime=runtime, store=store).run_job(job["job_id"]))

    assert result["success"] is False
    assert result["error_code"] == "JOB_RUN_FAILED"
    assert "outside allowed AIASK Agent workspace roots" in result["error"]
    assert runtime.messages == []
    run = store.list_runs(job["job_id"], limit=1)[0]
    assert run["status"] == "failed"
    assert "outside allowed AIASK Agent workspace roots" in run["error"]


def test_stock_radar_push_digest_dry_run_does_not_send_gateway(tmp_path, monkeypatch) -> None:
    import aiask_agent.adapters.confirmed_actions as confirmed_actions

    confirmed_actions._ensure_monorepo_paths()
    calls: list[dict] = []

    class FakeDb:
        logs: list[dict] = []

        async def initialize(self):
            return None

        async def save_stock_radar_push_log(self, item):
            self.logs.append(dict(item))
            return dict(item)

    async def fake_push(db, payload):
        return {
            "success": True,
            "data": {
                "dry_run": True,
                "channels": payload["channels"],
                "message_preview": "preview",
                "gateway_status": "preview_recorded",
            },
            "error": None,
        }

    class FakeRouter:
        def __init__(self, *args, **kwargs):
            pass

        async def send(self, **kwargs):
            calls.append(kwargs)
            return {"adapter": {"ok": True, "status": "delivered"}, "message": {"message_id": "m1"}, "platform": {"name": kwargs["platform"]}}

    monkeypatch.setattr("akshare_mcp.storage.get_db", lambda: FakeDb())
    monkeypatch.setattr("akshare_mcp.services.stock_radar.push_stock_radar_digest", fake_push)
    monkeypatch.setattr("aiask_agent.gateway.DeliveryRouter", FakeRouter)

    result = asyncio.run(
        confirmed_actions.execute_confirmed_action(
            "stock_radar",
            "push_digest",
            {"channels": ["local"], "dry_run": True, "target": "radar"},
        )
    )

    assert result["success"] is True
    assert result["data"]["gateway_status"] == "preview_recorded"
    assert calls == []


def test_stock_radar_push_digest_blocked_does_not_send_gateway(tmp_path, monkeypatch) -> None:
    import aiask_agent.adapters.confirmed_actions as confirmed_actions

    confirmed_actions._ensure_monorepo_paths()
    calls: list[dict] = []

    class FakeDb:
        logs: list[dict] = []

        async def initialize(self):
            return None

        async def save_stock_radar_push_log(self, item):
            self.logs.append(dict(item))
            return dict(item)

    async def fake_push(db, payload):
        return {
            "success": False,
            "data": {"dry_run": False, "channels": payload["channels"], "message_preview": "preview"},
            "error": "blocked",
            "error_code": "STOCK_RADAR_PUSH_REQUIRES_HIGH_CONFIDENCE",
        }

    class FakeRouter:
        def __init__(self, *args, **kwargs):
            pass

        async def send(self, **kwargs):
            calls.append(kwargs)
            return {"adapter": {"ok": True, "status": "delivered"}, "message": {"message_id": "m1"}, "platform": {"name": kwargs["platform"]}}

    monkeypatch.setattr("akshare_mcp.storage.get_db", lambda: FakeDb())
    monkeypatch.setattr("akshare_mcp.services.stock_radar.push_stock_radar_digest", fake_push)
    monkeypatch.setattr("aiask_agent.gateway.DeliveryRouter", FakeRouter)

    result = asyncio.run(
        confirmed_actions.execute_confirmed_action(
            "stock_radar",
            "push_digest",
            {"channels": ["local"], "dry_run": False, "target": "radar"},
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "STOCK_RADAR_PUSH_REQUIRES_HIGH_CONFIDENCE"
    assert calls == []


def test_stock_radar_push_digest_non_dry_run_sends_gateway(tmp_path, monkeypatch) -> None:
    import aiask_agent.adapters.confirmed_actions as confirmed_actions

    confirmed_actions._ensure_monorepo_paths()
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    calls: list[dict] = []

    class FakeDb:
        def __init__(self):
            self.logs: list[dict] = []

        async def initialize(self):
            return None

        async def save_stock_radar_push_log(self, item):
            self.logs.append(dict(item))
            return {**dict(item), "push_id": f"audit_{len(self.logs)}"}

    async def fake_push(db, payload):
        return {
            "success": True,
            "data": {
                "dry_run": False,
                "channels": payload["channels"],
                "message_preview": "Stock Radar Digest",
                "gateway_status": "queued_for_gateway_adapter",
            },
            "error": None,
        }

    class FakeRouter:
        def __init__(self, *args, **kwargs):
            pass

        async def send(self, **kwargs):
            calls.append(kwargs)
            return {
                "adapter": {"ok": True, "status": "delivered", "configured": True},
                "message": {"message_id": f"m{len(calls)}"},
                "platform": {"name": kwargs["platform"]},
            }

    fake_db = FakeDb()
    monkeypatch.setattr("akshare_mcp.storage.get_db", lambda: fake_db)
    monkeypatch.setattr("akshare_mcp.services.stock_radar.push_stock_radar_digest", fake_push)
    monkeypatch.setattr("aiask_agent.gateway.DeliveryRouter", FakeRouter)

    result = asyncio.run(
        confirmed_actions.execute_confirmed_action(
            "stock_radar",
            "push_digest",
            {"channels": ["wecom", "telegram"], "dry_run": False, "target": "radar"},
        )
    )

    assert result["success"] is True
    assert result["data"]["gateway_status"] == "delivered"
    assert result["data"]["gateway_delivered_count"] == 2
    assert [item["status"] for item in result["data"]["gateway_push_logs"]] == ["delivered", "delivered"]
    assert fake_db.logs[0]["metadata"]["gateway_message_id"] == "m1"
    assert fake_db.logs[1]["metadata"]["gateway_message_id"] == "m2"
    assert calls == [
        {
            "platform": "wecom",
            "target": "radar",
            "message": "Stock Radar Digest",
            "thread_id": None,
            "session_id": None,
            "user_id": None,
        },
        {
            "platform": "telegram",
            "target": "radar",
            "message": "Stock Radar Digest",
            "thread_id": None,
            "session_id": None,
            "user_id": None,
        }
    ]
