from __future__ import annotations

from aiask_agent.memory import FinancialMemoryStore
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.todo import FinancialTodoStore


def test_session_store_creates_history_and_responses(tmp_path) -> None:
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    session_id = store.create_session(user_id="u1", title="demo")
    store.append_message(session_id, {"role": "user", "content": "hello"})
    messages = store.get_messages(session_id)
    assert messages == [{"role": "user", "content": "hello"}]

    store.save_response("resp_1", session_id, {"response_id": "resp_1", "content": "ok"})
    assert store.get_response("resp_1")["content"] == "ok"
    assert store.delete_response("resp_1") is True
    assert store.get_response("resp_1") is None


def test_session_store_soft_undo_removes_last_turn_from_active_context(tmp_path) -> None:
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    session_id = store.create_session(user_id="u1", title="undo demo")
    store.append_message(session_id, {"role": "user", "content": "first question"})
    store.append_message(session_id, {"role": "assistant", "content": "first answer"})
    store.append_message(session_id, {"role": "user", "content": "second question"})
    store.append_message(session_id, {"role": "tool", "name": "agent_echo", "content": "second tool output"})
    store.append_message(session_id, {"role": "assistant", "content": "second answer"})

    result = store.undo_last_turns(session_id, turns=1, reason="test undo", deleted_by="tester")

    assert result["turns_undone"] == 1
    assert result["message_count"] == 3
    assert result["side_effects_rolled_back"] is False
    assert result["external_side_effects"] == "not_rolled_back"
    assert [message["content"] for message in store.get_messages(session_id)] == ["first question", "first answer"]
    assert store.count_session_messages(session_id) == 2
    assert store.search(query="second", session_id=session_id) == []

    all_messages = store.list_session_messages(session_id, include_deleted=True)
    assert len(all_messages) == 5
    assert [item["deleted_reason"] for item in all_messages[-3:]] == ["test undo", "test undo", "test undo"]


def test_session_store_archive_filters_lists_and_search(tmp_path) -> None:
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    active_id = store.create_session(session_id="active", user_id="u1", title="active demo")
    archived_id = store.create_session(session_id="archived", user_id="u1", title="archived demo")
    store.append_message(active_id, {"role": "user", "content": "visible alpha"})
    store.append_message(archived_id, {"role": "user", "content": "hidden alpha"})

    result = store.set_session_archived(archived_id, archived=True, reason="finished", actor="tester")

    assert result["archived"] is True
    assert result["session"]["metadata"]["archived_reason"] == "finished"
    assert [item["session_id"] for item in store.list_sessions(user_id="u1")] == ["active"]
    assert {item["session_id"] for item in store.list_sessions(user_id="u1", include_archived=True)} == {"active", "archived"}
    assert [item["session_id"] for item in store.search(query="alpha", user_id="u1")] == ["active"]
    assert {item["session_id"] for item in store.search(query="alpha", user_id="u1", include_archived=True)} == {"active", "archived"}

    restored = store.set_session_archived(archived_id, archived=False, reason="restore", actor="tester")

    assert restored["archived"] is False
    assert restored["session"]["metadata"]["unarchived_reason"] == "restore"
    assert {item["session_id"] for item in store.list_sessions(user_id="u1")} == {"active", "archived"}


def test_session_store_records_user_activity_feedback_policy_and_tool_audit(tmp_path) -> None:
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    session_id = store.create_session(user_id="u1", title="audit demo")

    events = store.record_activity_events(
        [
            {
                "user_id": "u1",
                "session_id": session_id,
                "page_key": "workbench",
                "route": "/workbench",
                "event_type": "page_view",
                "payload": {"api_key": "secret", "safe": "ok"},
            }
        ]
    )
    assert events[0]["payload"]["api_key"] == "[redacted]"
    assert store.list_activity_events(user_id="u1")[0]["event_type"] == "page_view"

    invocation = store.start_tool_invocation(
        tool_name="agent_tool_catalog",
        arguments={"token": "secret-token", "limit": 1},
        user_id="u1",
        session_id=session_id,
        run_id="run_1",
        trace_id="trace_1",
        capability="tool_catalog",
        category="financial_read",
        side_effect={"level": "read_only"},
    )
    finished = store.finish_tool_invocation(
        invocation["invocation_id"],
        status="succeeded",
        result={"success": True, "data": {"ok": True}, "error": None},
        duration_ms=12,
    )
    assert finished is not None
    assert finished["input_summary"]["token"] == "[redacted]"
    assert finished["output_summary"]["data"]["ok"] is True
    assert store.list_tool_invocations(user_id="u1")[0]["status"] == "succeeded"

    failed = store.start_tool_invocation(
        tool_name="agent_mcp_finance_demo",
        arguments={
            "note": "broker_token=plain-secret",
            "url": "https://broker.local/query?password=url-secret&ok=1",
            "headers": {"Authorization": "Bearer input-secret-123456"},
        },
        user_id="u1",
        session_id=session_id,
    )
    failed_finished = store.finish_tool_invocation(
        failed["invocation_id"],
        status="failed",
        result={
            "success": False,
            "error": "request failed with Authorization: Bearer output-secret-123456",
            "data": {"callback": "https://broker.local/cb?broker_token=output-secret&ok=1"},
        },
        error_code="BROKER_TEST_FAILED",
        error_summary="broker_token=summary-secret Authorization: Bearer summary-secret-123456",
    )
    serialized_failed = str(failed_finished)
    assert failed_finished is not None
    assert "plain-secret" not in serialized_failed
    assert "url-secret" not in serialized_failed
    assert "input-secret" not in serialized_failed
    assert "output-secret" not in serialized_failed
    assert "summary-secret" not in serialized_failed
    assert failed_finished["input_summary"]["note"] == "broker_token=[redacted]"
    assert failed_finished["error_summary"] == "broker_token=[redacted] Authorization: Bearer [redacted]"

    run_event = store.append_run_event(
        "run_1",
        "diagnostic.failed",
        {
            "broker_token": "event-token-secret",
            "message": "request failed with Authorization: Bearer event-bearer-secret-123456",
            "callback": "https://broker.local/cb?password=event-url-secret&ok=1",
        },
    )
    listed_event = store.list_run_events("run_1")[-1]
    event_export = store.export_user_data(user_id="u1")
    serialized_event = str({"run_event": run_event, "listed_event": listed_event, "event_export": event_export})
    assert "event-token-secret" not in serialized_event
    assert "event-bearer-secret" not in serialized_event
    assert "event-url-secret" not in serialized_event
    assert run_event["data"]["broker_token"] == "[redacted]"
    assert listed_event["data"]["message"] == "request failed with Authorization: Bearer [redacted]"
    assert listed_event["data"]["callback"] == "https://broker.local/cb?password=[redacted]&ok=1"

    snapshot = store.record_context_snapshot(
        session_id=session_id,
        user_id="u1",
        run_id="run_1",
        trace_id="trace_1",
        context_summary_id="ctx_1",
        compacted=True,
        message_count=3,
        source_message_ids=["1", "2"],
        source_ids=["source_1"],
        artifact_ids=["artifact_1"],
        token_estimate_before=120,
        token_estimate_after=40,
        summary="compressed facts",
        summary_model="test-model",
        risk_flags=["lossy_compression"],
        metadata={"api_key": "hidden", "safe": "ok"},
    )
    assert snapshot["metadata"]["api_key"] == "[redacted]"
    assert snapshot["source_message_ids"] == ["1", "2"]
    assert store.get_context_snapshot(snapshot["snapshot_id"])["risk_flags"] == ["lossy_compression"]
    assert store.list_context_snapshots(user_id="u1")[0]["context_summary_id"] == "ctx_1"

    feedback = store.record_feedback(
        {
            "user_id": "u1",
            "session_id": session_id,
            "target_type": "message",
            "target_id": "msg_1",
            "feedback_type": "thumbs_up",
            "rating": 5,
            "allow_learning": True,
            "payload": {"password": "hidden"},
        }
    )
    assert feedback["allow_learning"] is True
    assert feedback["payload"]["password"] == "[redacted]"

    policy = store.update_user_data_policy("u1", {"event_ttl_days": 30, "allow_learning": True})
    assert policy["event_ttl_days"] == 30
    assert policy["allow_learning"] is True

    summary = store.user_activity_summary(user_id="u1")
    assert summary["sessions"][0]["session_id"] == session_id
    assert summary["events"][0]["event_type"] == "page_view"
    summary_invocations = {item["tool_name"]: item for item in summary["tool_invocations"]}
    assert summary_invocations["agent_tool_catalog"]["status"] == "succeeded"
    assert summary_invocations["agent_mcp_finance_demo"]["status"] == "failed"
    assert summary["feedback"][0]["feedback_type"] == "thumbs_up"

    analytics = store.analytics_summary(user_id="u1")
    assert analytics["totals"]["events"] == 1
    assert analytics["totals"]["tool_invocations"] == 2
    analytics_tools = {item["tool_name"]: item for item in analytics["tools"]}
    assert analytics_tools["agent_tool_catalog"]["count"] == 1
    assert analytics_tools["agent_mcp_finance_demo"]["count"] == 1

    export_payload = store.export_user_data(user_id="u1")
    assert export_payload["user_id"] == "u1"
    assert export_payload["sessions"][0]["session_id"] == session_id
    assert export_payload["context_snapshots"][0]["snapshot_id"] == snapshot["snapshot_id"]
    assert export_payload["analytics"]["totals"]["feedback"] == 1

    learning_blocked = store.learning_dataset(user_id="u1")
    assert learning_blocked["allowed"] is True
    assert learning_blocked["items"][0]["feedback_type"] == "thumbs_up"

    recommendations = store.workflow_recommendations(user_id="u1")
    assert recommendations["object"] == "aiask.workflow_recommendations"

    retention_preview = store.apply_retention_policies(user_id="u1", dry_run=True)
    assert retention_preview["market_data_affected"] is False
    assert retention_preview["dry_run"] is True

    delete_preview = store.delete_user_data(user_id="u1", dry_run=True)
    assert delete_preview["counts"]["sessions"] == 1
    assert delete_preview["counts"]["context_snapshots"] == 1
    assert store.user_activity_summary(user_id="u1")["events"][0]["event_type"] == "page_view"

    deleted = store.delete_user_data(user_id="u1", dry_run=False, hard_delete=False)
    assert deleted["anonymized_user_id"] == "deleted:u1"
    assert store.list_activity_events(user_id="u1") == []
    assert store.list_activity_events(user_id="deleted:u1")[0]["payload"] == {}
    anonymized_snapshots = store.list_context_snapshots(user_id="deleted:u1")
    assert anonymized_snapshots[0]["summary"] is None
    assert anonymized_snapshots[0]["source_ids"] == []


def test_memory_is_scoped_by_financial_context(tmp_path) -> None:
    store = FinancialMemoryStore(tmp_path / "state.sqlite3")
    store.add(content="maotai margin note", user_id="u1", symbol="600519", research_topic="margin")
    store.add(content="pingan risk note", user_id="u1", symbol="601318", research_topic="risk")

    maotai = store.search(user_id="u1", symbol="600519")
    assert len(maotai) == 1
    assert maotai[0]["content"] == "maotai margin note"
    assert store.search(user_id="u1", symbol="000001") == []


def test_todo_tracks_financial_workflow_state(tmp_path) -> None:
    store = FinancialTodoStore(tmp_path / "state.sqlite3")
    items = store.set_items(
        session_id="s1",
        user_id="u1",
        items=[
            {"id": "1", "content": "collect evidence", "status": "completed"},
            {"id": "2", "content": "review strategy", "status": "in_progress"},
        ],
    )
    assert [item["status"] for item in items] == ["completed", "in_progress"]
    assert store.list_items(session_id="s1")[1]["content"] == "review strategy"
