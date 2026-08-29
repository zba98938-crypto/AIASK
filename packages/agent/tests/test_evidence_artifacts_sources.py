from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient

from aiask_agent.evidence import extract_tool_evidence
from aiask_agent.model_client import ModelResponse
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import AgentToolRegistry, aiask_envelope


def test_session_store_records_sources_and_artifacts(tmp_path) -> None:
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    session_id = store.create_session(session_id="sess_evidence", user_id="u1")
    run_id = store.create_run(session_id, {"trace_id": "trace_1"})

    source = store.record_source(
        {
            "user_id": "u1",
            "session_id": session_id,
            "run_id": run_id,
            "trace_id": "trace_1",
            "tool_call_id": "call_1",
            "tool_name": "agent_stock_news_digest",
            "provider": "eastmoney",
            "source_type": "news",
            "title": "Company news",
            "url": "https://example.com/news/1",
            "published_at": "2026-06-12T09:30:00+08:00",
            "excerpt": "linked evidence",
            "metadata": {"api_key": "hidden", "source_chain": ["eastmoney"]},
        }
    )
    artifact = store.record_artifact(
        {
            "user_id": "u1",
            "session_id": session_id,
            "run_id": run_id,
            "trace_id": "trace_1",
            "tool_call_id": "call_1",
            "tool_name": "agent_stock_news_digest",
            "kind": "news_digest",
            "title": "News digest",
            "preview_json": {"items": [{"title": "Company news"}]},
            "metadata": {"token": "secret"},
        }
    )

    assert store.get_source(source["source_id"])["metadata"]["api_key"] == "[redacted]"
    assert store.get_artifact(artifact["artifact_id"])["metadata"]["token"] == "[redacted]"
    assert store.list_sources(run_id=run_id)[0]["url"] == "https://example.com/news/1"
    assert store.list_artifacts(run_id=run_id)[0]["kind"] == "news_digest"
    search = store.search(query="Company", session_id=session_id)
    assert {item["kind"] for item in search} >= {"source", "artifact"}


def test_extract_tool_evidence_persists_news_quote_and_file_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_WORKSPACE_ROOTS", str(tmp_path))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    session_id = store.create_session(session_id="sess_extract", user_id="u1")
    run_id = store.create_run(session_id, {"trace_id": "trace_1"})
    report = tmp_path / "report.txt"
    report.write_text("generated report", encoding="utf-8")

    evidence = extract_tool_evidence(
        store,
        user_id="u1",
        session_id=session_id,
        run_id=run_id,
        trace_id="trace_1",
        tool_call_id="call_news",
        tool_name="agent_stock_news_digest",
        arguments={"code": "600519"},
        result={
            "success": True,
            "data": {
                "code": "600519",
                "price": 123.45,
                "data_timestamp": "2026-06-12T10:00:00+08:00",
                "source_chain": ["akshare", "sina"],
                "news": [
                    {
                        "title": "Realtime news",
                        "url": "https://example.com/realtime",
                        "provider": "example",
                        "published_at": "2026-06-12T09:59:00+08:00",
                    }
                ],
                "report_path": str(report),
            },
            "meta": {"source_chain": ["aiask_agent.test"]},
        },
    )

    assert any(item["source_type"] == "news" for item in evidence["sources"])
    assert any(item["kind"] == "quote_snapshot" for item in evidence["artifacts"])
    assert any(item["path"] == str(report.resolve()) for item in evidence["artifacts"])


def test_extract_tool_evidence_does_not_read_artifact_paths_outside_allowed_roots(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setenv("AIASK_AGENT_HOME", str(home))
    monkeypatch.setenv("AIASK_AGENT_WORKSPACE_ROOTS", str(workspace))
    secret_report = outside / "secret-report.txt"
    secret_report.write_text("do not preview", encoding="utf-8")
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    session_id = store.create_session(session_id="sess_guard", user_id="u1")
    run_id = store.create_run(session_id, {"trace_id": "trace_guard"})

    evidence = extract_tool_evidence(
        store,
        user_id="u1",
        session_id=session_id,
        run_id=run_id,
        trace_id="trace_guard",
        tool_call_id="call_guard",
        tool_name="agent_file_write",
        arguments={"path": str(secret_report)},
        result={"success": True, "data": {"path": str(secret_report)}},
    )

    artifact = evidence["artifacts"][0]
    assert artifact["status"] == "blocked"
    assert artifact["preview_text"] is None
    assert artifact["sha256"] is None
    assert artifact["metadata"]["read_allowed"] is False

    runtime = AgentRuntime(model_client=EvidenceModel(), tool_registry=AgentToolRegistry(), session_store=store)
    client = TestClient(create_app(runtime=runtime))
    content = client.get(f"/v1/artifacts/{artifact['artifact_id']}/content")

    assert content.status_code == 200
    assert content.json()["encoding"] == "blocked"
    assert content.json()["content"] is None


class EvidenceModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call_news",
                        "type": "function",
                        "function": {
                            "name": "agent_stock_news_digest",
                            "arguments": json.dumps({"code": "600519"}),
                        },
                    }
                ]
            )
        return ModelResponse(content="done")


def test_runtime_emits_evidence_events_for_tool_results(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    registry = AgentToolRegistry()

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return aiask_envelope(
            True,
            data={
                "code": arguments["code"],
                "price": 123.45,
                "data_timestamp": "2026-06-12T10:00:00+08:00",
                "source_chain": ["akshare", "sina"],
                "news": [{"title": "Realtime news", "url": "https://example.com/realtime", "provider": "example"}],
            },
            error=None,
            tool_name="agent_stock_news_digest",
            source_chain=["test"],
        )

    registry.register(
        "agent_stock_news_digest",
        description="test news",
        parameters={"type": "object", "properties": {"code": {"type": "string"}}},
        handler=handler,
    )
    runtime = AgentRuntime(
        model_client=EvidenceModel(),
        tool_registry=registry,
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=3,
    )

    result = asyncio.run(runtime.run([{"role": "user", "content": "news"}], user_id="u1"))

    events = {event["event"] for event in runtime.session_store.list_run_events(result.run_id)}
    assert "source.linked" in events
    assert "artifact.created" in events
    assert "market.quote_snapshot" in events
    assert runtime.session_store.list_sources(run_id=result.run_id)
    assert runtime.session_store.list_artifacts(run_id=result.run_id)
    assert result.tool_calls[0]["sources"]
    assert result.tool_calls[0]["artifacts"]


def test_artifact_source_http_contracts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIASK_AGENT_WORKSPACE_ROOTS", str(tmp_path))
    store = AgentSessionStore(tmp_path / "state.sqlite3")
    session_id = store.create_session(session_id="sess_http", user_id="local")
    run_id = store.create_run(session_id, {"trace_id": "trace_http"})
    source = store.record_source(
        {
            "user_id": "local",
            "session_id": session_id,
            "run_id": run_id,
            "tool_call_id": "call_http",
            "tool_name": "agent_web_search",
            "source_type": "web_search",
            "title": "Linked source",
            "url": "https://example.com/source",
        }
    )
    artifact = store.record_artifact(
        {
            "user_id": "local",
            "session_id": session_id,
            "run_id": run_id,
            "tool_call_id": "call_http",
            "tool_name": "agent_web_search",
            "kind": "news_digest",
            "title": "Digest",
        }
    )
    invocation = store.start_tool_invocation(
        tool_name="agent_web_search",
        invocation_id="call_http",
        user_id="local",
        session_id=session_id,
        run_id=run_id,
        trace_id="trace_http",
        arguments={"query": "AIASK"},
    )
    store.finish_tool_invocation(invocation["invocation_id"], result={"ok": True}, status="succeeded")
    artifact_file = tmp_path / "artifact.txt"
    artifact_file.write_text("artifact content", encoding="utf-8")
    file_artifact = store.record_artifact(
        {
            "user_id": "local",
            "session_id": session_id,
            "run_id": run_id,
            "tool_call_id": "call_file",
            "tool_name": "agent_file_write",
            "kind": "file",
            "title": "Artifact file",
            "path": str(artifact_file),
        }
    )
    runtime = AgentRuntime(model_client=EvidenceModel(), tool_registry=AgentToolRegistry(), session_store=store)
    client = TestClient(create_app(runtime=runtime))

    run_artifacts = client.get(f"/v1/runs/{run_id}/artifacts")
    run_sources = client.get(f"/v1/runs/{run_id}/sources")
    session_artifacts = client.get(f"/v1/sessions/{session_id}/artifacts")
    session_sources = client.get(f"/v1/sessions/{session_id}/sources")
    one_artifact = client.get(f"/v1/artifacts/{artifact['artifact_id']}")
    artifact_content = client.get(f"/v1/artifacts/{file_artifact['artifact_id']}/content")
    one_source = client.get(f"/v1/sources/{source['source_id']}")
    run_tool_invocations = client.get(f"/v1/runs/{run_id}/tool-invocations")

    assert run_artifacts.status_code == 200
    assert {item["artifact_id"] for item in run_artifacts.json()["data"]} >= {artifact["artifact_id"], file_artifact["artifact_id"]}
    assert run_sources.status_code == 200
    assert run_sources.json()["data"][0]["source_id"] == source["source_id"]
    assert {item["artifact_id"] for item in session_artifacts.json()["data"]} >= {artifact["artifact_id"], file_artifact["artifact_id"]}
    assert session_sources.json()["data"][0]["source_id"] == source["source_id"]
    assert one_artifact.json()["title"] == "Digest"
    assert artifact_content.status_code == 200
    assert artifact_content.json()["content"] == "artifact content"
    assert one_source.json()["url"] == "https://example.com/source"
    assert run_tool_invocations.status_code == 200
    assert run_tool_invocations.json()["data"][0]["invocation_id"] == "call_http"
