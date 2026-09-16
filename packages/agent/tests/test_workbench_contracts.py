from __future__ import annotations

import json

from fastapi.testclient import TestClient

from aiask_agent.approvals import ApprovalStore
from aiask_agent.gateway import GatewayMessageStore
from aiask_agent.intents import ActionIntentStore, IntentExecutor
from aiask_agent.model_client import MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore


def _client_with_state(tmp_path, monkeypatch) -> tuple[TestClient, AgentSessionStore, ActionIntentStore]:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_ENABLE_HERMES_FULL", "1")
    monkeypatch.setenv("AIASK_AGENT_CONTROL_TOKEN", "secret")
    monkeypatch.delenv("AIASK_AGENT_MCP_CONFIG", raising=False)
    monkeypatch.delenv("MCP_PORT", raising=False)

    store = AgentSessionStore(tmp_path / "state.sqlite3")
    intent_store = ActionIntentStore(tmp_path / "intents.sqlite3")
    runtime = AgentRuntime(model_client=MockModelClient(), session_store=store, max_iterations=2)
    client = TestClient(create_app(runtime=runtime, intent_executor=IntentExecutor(intent_store)))
    return client, store, intent_store


def _seed_main_chain(store: AgentSessionStore, intent_store: ActionIntentStore) -> str:
    session_id = store.create_session(session_id="sess_contract", user_id="local", title="Contract session")
    store.append_message(session_id, {"role": "user", "content": "analyze 600519"})
    store.append_message(session_id, {"role": "assistant", "content": "analysis ready"})
    run_id = store.create_run(session_id, {"response_id": "resp_contract", "tool_call_count": 1})
    store.append_run_event(run_id, "tool.called", {"tool": "agent_analyze_stock", "status": "completed"})
    store.append_run_event(run_id, "approval.intent_created", {"intent_id": "intent_contract", "status": "pending"})
    store.append_run_event(run_id, "gateway.message_failed", {"platform": "desktop", "status": "failed", "error": "mock failed"})
    store.update_run(run_id, status="completed", payload={"response_id": "resp_contract", "tool_call_count": 1})
    intent_store.create(action="watchlist_manager.add", params={"session_id": session_id, "code": "600519"}, user_id="local")
    ApprovalStore(store.path).create(tool_name="agent_gateway_send", action="gateway.send", arguments={"session_id": session_id}, reason="contract")
    GatewayMessageStore(store.path).record(
        direction="outbound",
        platform="desktop",
        target="local",
        content="failed",
        status="failed",
        session_id=session_id,
        user_id="local",
    )
    return run_id


def test_desktop_workbench_summary_contract_includes_session_run_flags(tmp_path, monkeypatch) -> None:
    client, store, intent_store = _client_with_state(tmp_path, monkeypatch)
    run_id = _seed_main_chain(store, intent_store)
    unknown = intent_store.create(
        action="watchlist_manager.add",
        params={
            "session_id": "sess_contract",
            "code": "000001",
            "broker_token": "unknown-execution-secret",
        },
        user_id="local",
    )
    intent_store.update_status(
        unknown["intent_id"],
        "execution_unknown",
        result={"status": "execution_unknown", "reconciliation_required": True},
    )

    response = client.get("/v1/workbench/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "aiask.workbench.summary"
    assert payload["queues"]["pending_intents"] == 1
    assert payload["queues"]["unknown_execution_intents"] == 1
    assert payload["queues"]["pending_approvals"] == 1
    assert payload["queues"]["gateway_failed"] == 1
    assert payload["access"]["full_mode_active"] is True
    assert payload["access"]["sessions_admin_available"] is True

    session = payload["recent_sessions"][0]
    assert session["session_id"] == "sess_contract"
    assert session["created_at"]
    assert session["message_count"] == 2
    assert session["has_pending_approval"] is True
    assert session["has_unknown_execution"] is True
    assert session["last_run_id"] == run_id
    assert session["last_run_summary"]["response_id"] == "resp_contract"
    assert session["last_event"]["kind"] == "gateway"

    run = payload["recent_runs"][0]
    assert run["run_id"] == run_id
    assert run["last_event"]["jump_target"] == "gateway"
    assert run["has_pending_approval"] is True
    assert run["has_errors"] is True


def test_financial_readiness_blocks_unknown_action_intent_execution(tmp_path, monkeypatch) -> None:
    client, _store, intent_store = _client_with_state(tmp_path, monkeypatch)
    unknown = intent_store.create(
        action="watchlist_manager.add",
        params={
            "session_id": "sess_reconciliation",
            "code": "600519",
            "broker_token": "readiness-unknown-secret",
        },
        user_id="local",
    )
    intent_store.update_status(
        unknown["intent_id"],
        "execution_unknown",
        result={"status": "execution_unknown", "reconciliation_required": True},
    )

    response = client.get("/v1/financial-system/readiness")

    assert response.status_code == 200
    payload = response.json()
    gates = {item["name"]: item for item in payload["required_gates"]}
    gate = gates["action_intent_reconciliation"]
    assert gate["status"] == "blocked"
    assert gate["required"] is True
    assert gate["evidence"]["execution_unknown"] == 1
    assert gate["evidence"]["auto_replay"] is False
    assert gate["evidence"]["sample"][0]["intent_id"] == unknown["intent_id"]
    assert "params" not in gate["evidence"]["sample"][0]
    assert payload["summary"]["unknown_execution_intents"] == 1
    assert payload["summary"]["required_blocked"] >= 1
    assert payload["backend_ready"] is False
    actions = {item["action_id"]: item for item in payload["next_actions"]}
    assert actions["reconcile_unknown_action_intents"]["priority"] == "critical"
    assert actions["reconcile_unknown_action_intents"]["endpoint"] == "/intents?status=execution_unknown"
    assert "readiness-unknown-secret" not in json.dumps(payload, ensure_ascii=False)


def test_personal_asset_proxy_routes_are_gone(tmp_path, monkeypatch) -> None:
    """Strategies / stock-pools were a proxy to the removed Desktop asset service.

    They forwarded to AIASK_DESKTOP_API_BASE_URL (127.0.0.1:8001), a service that
    no longer exists in a backend-only tree, and degraded to empty lists so the
    workbench had something to render. With no frontend there is nothing to
    degrade for, so the routes are gone rather than silently returning [].
    """
    client, _store, _intent_store = _client_with_state(tmp_path, monkeypatch)

    def unexpected_request(*args: object, **kwargs: object) -> dict:
        raise AssertionError("no route may still call the removed Desktop asset service")

    monkeypatch.setattr("aiask_agent.app_route_callbacks._json_request", unexpected_request)

    for path in (
        "/v1/users/local-user/strategies",
        "/v1/users/local-user/stock-pools",
        "/v1/files?user_id=local",
    ):
        assert client.get(path).status_code == 404, path

    removed = client.get("/v1/desktop/users/local-user/strategies")
    assert removed.status_code == 404
    assert removed.json()["error_code"] == "ROUTE_REMOVED"


def test_responses_contract_accepts_model_and_attachment_context(tmp_path, monkeypatch) -> None:
    client, store, _intent_store = _client_with_state(tmp_path, monkeypatch)

    response = client.post(
        "/v1/responses",
        json={
            "input": "Summarize the attached release note.",
            "model": "desktop-selected-model",
            "user_id": "local",
            "session_id": "sess_attachment_contract",
            "attachments": [
                {
                    "id": "file_contract",
                    "name": "release-note.txt",
                    "mime_type": "text/plain",
                    "size": 42,
                    "text_preview": "attachment text for contract test",
                },
                {
                    "id": "file_binary",
                    "name": "diagram.png",
                    "mime_type": "image/png",
                    "size": 1024,
                },
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "desktop-selected-model"
    assert payload["metadata"]["attachment_count"] == 2
    attachments = payload["metadata"]["attachments"]
    assert attachments[0]["name"] == "release-note.txt"
    assert attachments[0]["parse_status"] == "parsed_text_preview"
    assert attachments[0]["text_preview"] == "attachment text for contract test"
    assert attachments[1]["parse_status"] == "uploaded_unparsed"

    messages = store.list_session_messages("sess_attachment_contract")
    assert any("Desktop attachments supplied" in str(message.get("content") or "") for message in messages)
    assert any("release-note.txt" in str(message.get("content") or "") for message in messages)

    delete_denied = client.delete(f"/v1/responses/{payload['id']}")
    assert delete_denied.status_code == 401

    delete_allowed = client.delete(f"/v1/responses/{payload['id']}", headers={"Authorization": "Bearer secret"})
    assert delete_allowed.status_code == 200
    assert delete_allowed.json()["deleted"] is True


def test_desktop_runs_and_hermes_sessions_contracts_are_backward_compatible(tmp_path, monkeypatch) -> None:
    client, store, intent_store = _client_with_state(tmp_path, monkeypatch)
    run_id = _seed_main_chain(store, intent_store)

    runs_response = client.get("/v1/runs?limit=5")
    sessions_response = client.get("/v1/hermes/sessions")

    assert runs_response.status_code == 200
    run = runs_response.json()["data"][0]
    assert run["run_id"] == run_id
    assert run["event_count"] == 3
    assert run["last_event"]["event_type"] == "gateway.message_failed"
    assert run["last_event"]["error_message"] == "mock failed"

    assert sessions_response.status_code == 200
    session = sessions_response.json()["data"][0]
    assert session["session_id"] == "sess_contract"
    assert session["updated_at"]
    assert session["last_run_summary"]["last_event"]["kind"] == "gateway"
    assert session["metadata"] == {}


def test_hermes_sessions_contract_exposes_handoff_ownership_state(tmp_path, monkeypatch) -> None:
    client, store, intent_store = _client_with_state(tmp_path, monkeypatch)
    _seed_main_chain(store, intent_store)
    store.set_session_handoff_state(
        "sess_contract",
        status="pending",
        handoff_id="handoff_contract",
        target="risk_specialist",
        source_run_id="run_source",
        source_tool_call_id="call_source",
        context_snapshot_id="ctxsnap_source",
        summary="Continue with risk review.",
        reason="risk escalation",
    )
    store.consume_session_handoff_state("sess_contract", run_id="run_active", trace_id="trace_active")

    response = client.get("/v1/hermes/sessions")

    assert response.status_code == 200
    session = response.json()["data"][0]
    assert session["session_id"] == "sess_contract"
    assert session["handoff_status"] == "active"
    assert session["handoff_target"] == "risk_specialist"
    assert session["handoff_id"] == "handoff_contract"
    assert session["handoff_context_snapshot_id"] == "ctxsnap_source"
    assert session["active_agent"] == "risk_specialist"
    assert session["active_context_snapshot_id"] == "ctxsnap_source"
    assert session["handoff_state"]["active_run_id"] == "run_active"


def test_hermes_handoff_queue_and_resume_context_contract(tmp_path, monkeypatch) -> None:
    client, store, intent_store = _client_with_state(tmp_path, monkeypatch)
    _seed_main_chain(store, intent_store)
    snapshot = store.record_context_snapshot(
        session_id="sess_contract",
        user_id="local",
        run_id="run_source",
        trace_id="trace_source",
        context_summary_id="ctxsum_source",
        snapshot_id="ctxsnap_resume",
        policy="runtime_prepare",
        compacted=True,
        message_count=3,
        source_message_ids=["1", "2"],
        source_ids=["src_contract"],
        artifact_ids=["art_contract"],
        summary="Resume from risk handoff.",
        risk_flags=["lossy_compression"],
    )
    handoff = store.request_handoff(
        session_id="sess_contract",
        user_id="local",
        target="risk_specialist",
        reason="risk escalation",
        summary="Continue risk review.",
        metadata={"context_snapshot_id": snapshot["snapshot_id"]},
    )
    store.set_session_handoff_state(
        "sess_contract",
        status="pending",
        handoff_id=handoff["handoff_id"],
        target="risk_specialist",
        source_run_id="run_source",
        source_tool_call_id="call_source",
        context_snapshot_id=snapshot["snapshot_id"],
        summary="Continue risk review.",
        reason="risk escalation",
    )
    store.consume_session_handoff_state("sess_contract", run_id="run_active", trace_id="trace_active")

    denied = client.get("/v1/hermes/handoffs")
    assert denied.status_code == 401

    sessions = client.get("/v1/hermes/sessions")
    assert sessions.status_code == 200
    assert sessions.json()["data"][0]["handoff_id"] == handoff["handoff_id"]

    queue = client.get("/v1/hermes/handoffs?user_id=local", headers={"Authorization": "Bearer secret"})
    assert queue.status_code == 200
    queue_payload = queue.json()
    assert queue_payload["object"] == "aiask.handoff_queue"
    assert queue_payload["summary"]["active"] == 1
    item = queue_payload["data"][0]
    assert item["handoff_id"] == handoff["handoff_id"]
    assert item["runtime_status"] == "active"
    assert item["active_agent"] == "risk_specialist"
    assert item["resume_context_snapshot_id"] == "ctxsnap_resume"
    assert item["resume_ready"] is True

    resume = client.get(
        "/v1/hermes/sessions/sess_contract/resume-context",
        headers={"Authorization": "Bearer secret"},
    )
    assert resume.status_code == 200
    resume_payload = resume.json()
    assert resume_payload["object"] == "aiask.session_resume_context"
    assert resume_payload["handoff"]["runtime_status"] == "active"
    assert resume_payload["resume_context"]["handoff_id"] == handoff["handoff_id"]
    assert resume_payload["resume_context"]["target"] == "risk_specialist"
    assert resume_payload["resume_context"]["context_snapshot_id"] == "ctxsnap_resume"
    assert resume_payload["resume_context"]["risk_flags"] == ["lossy_compression"]
    assert resume_payload["resume_context"]["source_ids"] == ["src_contract"]
    assert "上下文快照 ctxsnap_resume" in resume_payload["resume_context"]["resume_prompt"]


def test_session_undo_route_requires_control_and_preserves_run_events(tmp_path, monkeypatch) -> None:
    client, store, intent_store = _client_with_state(tmp_path, monkeypatch)
    run_id = _seed_main_chain(store, intent_store)

    denied = client.post("/v1/sessions/sess_contract/undo", json={"turns": 1})
    assert denied.status_code == 401

    response = client.post(
        "/v1/sessions/sess_contract/undo",
        json={"turns": 1, "reason": "contract undo"},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "aiask.session_undo"
    assert payload["implementation"] == "aiask_native"
    assert payload["turns_undone"] == 1
    assert payload["message_count"] == 2
    assert payload["side_effects_rolled_back"] is False
    assert store.list_session_messages("sess_contract") == []
    assert len(store.list_run_events(run_id)) == 3


def test_session_archive_route_filters_lists_and_search(tmp_path, monkeypatch) -> None:
    client, store, intent_store = _client_with_state(tmp_path, monkeypatch)
    _seed_main_chain(store, intent_store)

    denied = client.post("/v1/sessions/sess_contract/archive", json={"archived": True})
    assert denied.status_code == 401

    archived = client.post(
        "/v1/sessions/sess_contract/archive",
        json={"archived": True, "reason": "done"},
        headers={"Authorization": "Bearer secret"},
    )

    assert archived.status_code == 200
    archive_payload = archived.json()
    assert archive_payload["object"] == "aiask.session_archive"
    assert archive_payload["archived"] is True
    assert archive_payload["session"]["metadata"]["archived_reason"] == "done"

    hidden = client.get("/v1/hermes/sessions")
    assert hidden.status_code == 200
    assert hidden.json()["data"] == []

    visible = client.get("/v1/hermes/sessions?include_archived=true")
    assert visible.status_code == 200
    visible_payload = visible.json()
    assert visible_payload["include_archived"] is True
    assert visible_payload["data"][0]["session_id"] == "sess_contract"
    assert visible_payload["data"][0]["archived"] is True


def test_session_batch_archive_requires_control_token(tmp_path, monkeypatch) -> None:
    client, store, intent_store = _client_with_state(tmp_path, monkeypatch)
    _seed_main_chain(store, intent_store)
    store.create_session(session_id="sess_contract_2", user_id="local", title="Second contract session")

    denied = client.post(
        "/v1/sessions/batch/archive",
        json={"session_ids": ["sess_contract", "sess_contract_2", "missing_session"]},
    )
    assert denied.status_code == 401

    archived = client.post(
        "/v1/sessions/batch/archive",
        json={
            "session_ids": ["sess_contract", "sess_contract_2", "missing_session"],
            "reason": "batch done",
        },
        headers={"Authorization": "Bearer secret"},
    )

    assert archived.status_code == 200
    payload = archived.json()
    assert payload["object"] == "aiask.session_archive_batch"
    assert payload["success"] is True
    assert payload["data"]["archived_count"] == 2
    assert payload["data"]["failed_count"] == 1
    assert store.get_session("sess_contract")["metadata"]["archived"] is True
    assert store.get_session("sess_contract_2")["metadata"]["archived"] is True
    missing = next(item for item in payload["data"]["results"] if item["session_id"] == "missing_session")
    assert missing["success"] is False
    assert missing["error_code"] == "SESSION_NOT_FOUND"


def test_approvals_list_is_api_visible_but_decision_stays_control_gated(tmp_path, monkeypatch) -> None:
    client, store, intent_store = _client_with_state(tmp_path, monkeypatch)
    _seed_main_chain(store, intent_store)
    approval_store = ApprovalStore(store.path)
    raw_approval = approval_store.create(
        tool_name="agent_terminal",
        action="terminal",
        arguments={
            "command": "rm -rf ./danger --broker_token=approval-arg-secret",
            "nested": {"password": "approval-nested-secret"},
            "note": "Authorization: Bearer approval-arg-bearer-secret",
        },
        reason="created with password=approval-reason-secret and Authorization: Bearer approval-reason-bearer-secret",
    )

    list_response = client.get("/v1/approvals")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    approval = next(item for item in list_payload["data"] if item["approval_id"] == raw_approval["approval_id"])
    approval_id = approval["approval_id"]
    assert list_payload["secrets_redacted"] is True
    assert approval["secrets_redacted"] is True
    list_json = json.dumps(list_payload, ensure_ascii=False)
    assert "approval-arg-secret" not in list_json
    assert "approval-nested-secret" not in list_json
    assert "approval-arg-bearer-secret" not in list_json
    assert "approval-reason-secret" not in list_json
    assert "approval-reason-bearer-secret" not in list_json
    assert approval_store.get(approval_id)["arguments"]["nested"]["password"] == "approval-nested-secret"

    denied = client.post(f"/v1/approvals/{approval_id}/approve", json={"reason": "no token"})
    assert denied.status_code == 401

    allowed = client.post(
        f"/v1/approvals/{approval_id}/approve",
        json={"reason": "approved with password=approval-decision-secret and Authorization: Bearer approval-decision-bearer-secret"},
        headers={"Authorization": "Bearer secret"},
    )
    assert allowed.status_code == 200
    payload = allowed.json()
    assert payload["object"] == "approval"
    assert payload["status"] == "approved"
    assert payload["secrets_redacted"] is True
    allowed_json = json.dumps(payload, ensure_ascii=False)
    assert "approval-decision-secret" not in allowed_json
    assert "approval-decision-bearer-secret" not in allowed_json
    assert "approval-arg-secret" not in allowed_json
    assert "approval-nested-secret" not in allowed_json
    stored = approval_store.get(approval_id)
    assert "approval-decision-secret" in stored["reason"]
    assert stored["arguments"]["command"].endswith("approval-arg-secret")


def test_run_events_sse_returns_normalized_event_payload(tmp_path, monkeypatch) -> None:
    client, store, intent_store = _client_with_state(tmp_path, monkeypatch)
    run_id = _seed_main_chain(store, intent_store)

    response = client.get(f"/v1/runs/{run_id}/events")

    assert response.status_code == 200
    chunks = [chunk for chunk in response.text.split("\n\n") if "data:" in chunk]
    events = [json.loads(chunk.split("data:", 1)[1].strip()) for chunk in chunks]
    assert [event["kind"] for event in events] == ["tool", "approval", "gateway"]
    assert events[0]["tool_name"] == "agent_analyze_stock"
    assert events[1]["jump_target"] == "tools-intents-approvals"
    assert events[2]["error_message"] == "mock failed"


def test_run_trace_eval_contract_scores_coordination_chain(tmp_path, monkeypatch) -> None:
    client, store, intent_store = _client_with_state(tmp_path, monkeypatch)
    run_id = _seed_main_chain(store, intent_store)
    store.append_run_event(run_id, "model.started", {"iteration": 1, "model": "mock"})
    store.append_run_event(run_id, "model.completed", {"iteration": 1, "tool_call_count": 1})
    store.start_tool_invocation(
        tool_name="agent_analyze_stock",
        arguments={"code": "600519"},
        user_id="local",
        session_id="sess_contract",
        run_id=run_id,
        trace_id="trace_contract",
        invocation_id="call_trace_eval",
        source_chain=["test"],
    )
    store.finish_tool_invocation("call_trace_eval", status="succeeded", result={"success": True})
    store.record_context_snapshot(
        session_id="sess_contract",
        user_id="local",
        run_id=run_id,
        trace_id="trace_contract",
        context_summary_id="ctxsum_trace",
        snapshot_id="ctxsnap_trace",
        compacted=False,
        message_count=2,
        source_message_ids=["1", "2"],
        source_ids=["src_trace"],
        artifact_ids=["art_trace"],
    )
    store.record_source(
        {
            "source_id": "src_trace",
            "user_id": "local",
            "session_id": "sess_contract",
            "run_id": run_id,
            "trace_id": "trace_contract",
            "tool_call_id": "call_trace_eval",
            "tool_name": "agent_analyze_stock",
            "source_type": "market_data",
            "title": "Trace source",
        }
    )
    store.record_artifact(
        {
            "artifact_id": "art_trace",
            "user_id": "local",
            "session_id": "sess_contract",
            "run_id": run_id,
            "trace_id": "trace_contract",
            "tool_call_id": "call_trace_eval",
            "tool_name": "agent_analyze_stock",
            "kind": "report",
            "title": "Trace artifact",
        }
    )

    response = client.get(f"/v1/runs/{run_id}/trace-eval")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "aiask.run_trace_eval"
    assert payload["run_id"] == run_id
    assert payload["summary"]["tool_invocation_count"] == 1
    assert payload["summary"]["context_snapshot_count"] == 1
    assert payload["summary"]["source_count"] == 1
    assert payload["summary"]["artifact_count"] == 1
    checks = {item["id"]: item for item in payload["checks"]}
    assert checks["model_trace"]["status"] == "pass"
    assert checks["tool_trace"]["status"] == "pass"
    assert checks["context_snapshot"]["status"] == "pass"
    assert checks["evidence_chain"]["status"] == "pass"
    assert payload["latest_context_snapshot"]["snapshot_id"] == "ctxsnap_trace"
    assert payload["score"] >= 80


def test_intent_create_route_records_tool_invocation(tmp_path, monkeypatch) -> None:
    client, store, _intent_store = _client_with_state(tmp_path, monkeypatch)
    session_id = store.create_session(session_id="sess_intent_audit", user_id="local", title="Intent audit")

    response = client.post(
        "/intents",
        headers={"Authorization": "Bearer secret"},
        json={
            "user_id": "local",
            "session_id": session_id,
            "action": "data_sync.sync",
            "params": {"task_type": "daily"},
            "rationale": "audit intent creation",
        },
    )

    assert response.status_code == 200
    intent_id = response.json()["data"]["intent"]["intent_id"]
    invocations = store.list_tool_invocations(session_id=session_id)
    assert invocations[0]["tool_name"] == "agent_action_intent_create"
    assert invocations[0]["action_intent_id"] == intent_id
    assert invocations[0]["source_chain"] == ["aiask_agent.server", "intent.api"]


def test_quant_research_route_records_tool_invocation(tmp_path, monkeypatch) -> None:
    client, store, _intent_store = _client_with_state(tmp_path, monkeypatch)
    session_id = store.create_session(session_id="sess_quant_audit", user_id="local", title="Quant audit")

    response = client.post(
        "/v1/finance/quant/research-runs",
        json={
            "user_id": "local",
            "session_id": session_id,
            "universe": ["600519", "000001"],
            "factors": ["momentum"],
            "benchmark": "000300",
        },
    )

    assert response.status_code == 200
    invocations = store.list_tool_invocations(session_id=session_id)
    assert invocations[0]["tool_name"] == "agent_quant_research_run"
    assert invocations[0]["source_chain"] == ["aiask_agent.server", "finance.quant_research"]


def test_user_activity_feedback_policy_and_tool_audit_routes(tmp_path, monkeypatch) -> None:
    client, store, _intent_store = _client_with_state(tmp_path, monkeypatch)
    session_id = store.create_session(session_id="sess_user_data", user_id="local", title="User data")

    events_response = client.post(
        "/v1/users/events",
        json={
            "user_id": "local",
            "session_id": session_id,
            "events": [
                {
                    "page_key": "workbench",
                    "route": "/workbench",
                    "event_type": "page_view",
                    "payload": {"api_key": "secret", "safe": "ok"},
                }
            ],
        },
    )
    assert events_response.status_code == 200
    event = events_response.json()["data"][0]
    assert event["event_type"] == "page_view"
    assert event["payload"]["api_key"] == "[redacted]"

    feedback_response = client.post(
        "/v1/users/feedback",
        json={
            "user_id": "local",
            "session_id": session_id,
            "target_type": "page",
            "target_id": "workbench",
            "feedback_type": "thumbs_up",
            "rating": 5,
            "allow_learning": True,
        },
    )
    assert feedback_response.status_code == 200
    assert feedback_response.json()["data"]["allow_learning"] is True

    policy_denied = client.patch(
        "/v1/users/local/data-policy",
        json={"event_ttl_days": 45, "allow_learning": True},
    )
    assert policy_denied.status_code == 401

    policy_response = client.patch(
        "/v1/users/local/data-policy",
        headers={"Authorization": "Bearer secret"},
        json={"event_ttl_days": 45, "allow_learning": True},
    )
    assert policy_response.status_code == 200
    assert policy_response.json()["data"]["event_ttl_days"] == 45

    tool_response = client.post(
        "/v1/tools/agent_tool_catalog",
        json={"user_id": "local", "session_id": session_id, "token": "secret"},
    )
    assert tool_response.status_code == 200
    invocations = store.list_tool_invocations(user_id="local")
    assert invocations[0]["tool_name"] == "agent_tool_catalog"
    assert invocations[0]["input_summary"]["token"] == "[redacted]"
    assert invocations[0]["status"] == "succeeded"

    activity_response = client.get("/v1/users/local/activity?limit=10")
    assert activity_response.status_code == 200
    activity = activity_response.json()
    assert activity["events"][0]["event_type"] == "page_view"
    assert activity["feedback"][0]["feedback_type"] == "thumbs_up"
    assert activity["tool_invocations"][0]["tool_name"] == "agent_tool_catalog"

    analytics_response = client.get("/v1/users/analytics/summary?user_id=local&limit=10")
    assert analytics_response.status_code == 200
    analytics = analytics_response.json()
    assert analytics["totals"]["events"] == 1
    assert analytics["totals"]["tool_invocations"] == 1

    aggregate_denied = client.get("/v1/users/analytics/summary")
    assert aggregate_denied.status_code == 401

    aggregate_allowed = client.get("/v1/users/analytics/summary", headers={"Authorization": "Bearer secret"})
    assert aggregate_allowed.status_code == 200
    assert aggregate_allowed.json()["scope"] == "aggregate"

    export_response = client.get("/v1/users/local/export?limit=20")
    assert export_response.status_code == 200
    export_payload = export_response.json()
    assert export_payload["user_id"] == "local"
    assert export_payload["sessions"][0]["session_id"] == session_id
    assert export_payload["analytics"]["totals"]["feedback"] == 1

    learning_response = client.get("/v1/users/local/learning-dataset?limit=10")
    assert learning_response.status_code == 200
    assert learning_response.json()["allowed"] is True
    assert learning_response.json()["items"][0]["feedback_type"] == "thumbs_up"

    recommendation_response = client.get("/v1/users/local/recommendations?limit=5")
    assert recommendation_response.status_code == 200
    assert recommendation_response.json()["object"] == "aiask.workflow_recommendations"

    retention_denied = client.post("/v1/users/retention/sweep", json={"user_id": "local", "dry_run": True})
    assert retention_denied.status_code == 401

    retention_response = client.post(
        "/v1/users/retention/sweep",
        json={"user_id": "local", "dry_run": True},
        headers={"Authorization": "Bearer secret"},
    )
    assert retention_response.status_code == 200
    assert retention_response.json()["market_data_affected"] is False

    delete_denied = client.post("/v1/users/local/delete", json={"dry_run": True})
    assert delete_denied.status_code == 401

    delete_preview = client.post(
        "/v1/users/local/delete",
        headers={"Authorization": "Bearer secret"},
        json={"dry_run": True},
    )
    assert delete_preview.status_code == 200
    assert delete_preview.json()["counts"]["sessions"] == 1
    assert store.list_activity_events(user_id="local")

    denied = client.get("/v1/users/other/activity?limit=10")
    assert denied.status_code == 401
