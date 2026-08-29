from __future__ import annotations

from fastapi.testclient import TestClient

from aiask_agent.adapters import quant
from aiask_agent.model_client import MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore


def _runtime(tmp_path) -> AgentRuntime:
    return AgentRuntime(model_client=MockModelClient(), session_store=AgentSessionStore(tmp_path / "state.sqlite3"), max_iterations=2)


def test_quant_presets_report_database_preflight(tmp_path, monkeypatch) -> None:
    for key in quant.DATABASE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(tmp_path / "akshare_mcp.sqlite3"))

    payload = quant.quant_presets()

    assert payload["object"] == "aiask.quant_presets"
    assert payload["data_status"]["status"] == "ready"
    assert payload["data_status"]["database"]["backend"] == "sqlite"
    assert payload["data_status"]["database"]["required_for_full_quant"] is True
    assert "NOT_INVESTMENT_ADVICE" in payload["disclaimer"]


def test_quant_research_desktop_api_blocks_without_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    for key in quant.DATABASE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", "/dev/null/akshare_mcp.sqlite3")
    client = TestClient(create_app(runtime=_runtime(tmp_path)))

    created = client.post(
        "/v1/desktop/quant/research-runs",
        json={
            "universe": ["600519", "000001"],
            "factors": ["momentum"],
            "benchmark": "000300",
            "cost_bps": 3,
            "slippage_bps": 1,
        },
    )

    assert created.status_code == 200
    envelope = created.json()
    assert envelope["success"] is True
    research = envelope["data"]["research"]
    assert research["status"] == "blocked"
    assert research["report"]["summary"]["failed_stage"] == "data_gate"
    assert "NOT_INVESTMENT_ADVICE" in research["report"]["disclaimer"]

    research_id = research["research_id"]
    fetched = client.get(f"/v1/desktop/quant/research-runs/{research_id}")
    assert fetched.status_code == 200
    assert fetched.json()["research_id"] == research_id

    report = client.get(f"/v1/desktop/quant/research-runs/{research_id}/report")
    assert report.status_code == 200
    assert report.json()["research_id"] == research_id


def test_desktop_capabilities_includes_quant_preflight(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIASK_AGENT_HOME", str(tmp_path / "home"))
    for key in quant.DATABASE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(tmp_path / "akshare_mcp.sqlite3"))
    client = TestClient(create_app(runtime=_runtime(tmp_path)))

    response = client.get("/v1/desktop/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["quant"]["status"] == "ready"
    assert payload["quant"]["data_status"]["database"]["configured"] is True
    assert payload["quant"]["data_status"]["database"]["backend"] == "sqlite"
    assert payload["raw_refs"]["quant_research_runs"] == "/v1/desktop/quant/research-runs"
