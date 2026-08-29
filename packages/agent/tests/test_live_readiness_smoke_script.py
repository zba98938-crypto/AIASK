from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_smoke_module():
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "ops" / "live_readiness_smoke.py"
    spec = importlib.util.spec_from_file_location("live_readiness_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_readiness_smoke_contract_accepts_explicitly_blocked_data_state() -> None:
    smoke = _load_smoke_module()
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = []

    def requester(method: str, path: str, token: str, body: dict[str, Any] | None, timeout: float) -> tuple[int, dict[str, Any]]:
        calls.append((method, path, token, body))
        if path == "/health/detailed":
            return 200, {"status": "ok", "tools": {"count": 31}}
        if path == "/v1/tools":
            return 200, {
                "data": [
                    {"name": "agent_tool_catalog"},
                    {"name": "agent_analyze_stock"},
                    {"name": "agent_action_intent_create"},
                    {"name": "agent_market_temperature_cache_readiness"},
                    {"name": "agent_market_temperature_forward_validation"},
                ]
            }
        if path == "/v1/financial-system/readiness":
            return 200, {
                "status": "degraded",
                "production_ready": False,
                "summary": {"required_degraded": 1, "required_blocked": 0},
                "next_actions": [{"action_id": "configure_model_provider"}],
            }
        if path.startswith("/v1/desktop/workbench/summary?"):
            return 200, {
                "object": "aiask.desktop.workbench.summary",
                "recent_sessions": [{"session_id": "sess_test"}],
                "recent_runs": [{"run_id": "run_test", "status": "completed"}],
                "queues": {
                    "pending_intents": 1,
                    "pending_approvals": 0,
                    "gateway_failed": 0,
                    "mcp_degraded": 0,
                },
                "access": {
                    "full_mode_active": True,
                    "control_token_configured": True,
                    "sessions_admin_available": True,
                },
            }
        if path == "/v1/desktop/settings/status":
            return 200, {
                "memory": {
                    "object": "aiask.memory_provider_status",
                    "status": "implemented",
                    "active_provider": "sqlite",
                    "providers": [{"name": "sqlite", "configured": True, "status": "implemented"}],
                }
            }
        if path.startswith("/v1/search?"):
            return 200, {"object": "list", "data": []}
        if path == "/v1/tools/agent_memory_search":
            assert method == "POST"
            assert body is not None and body["query"] == "AIASK"
            return 200, {"success": True, "data": {"items": []}, "error": None}
        if path == "/v1/mcp/servers?all=true":
            return 200, {"data": []}
        if path == "/v1/mcp/tools?all=true":
            return 200, {"data": []}
        if path == "/v1/desktop/financial-manager/catalog":
            return 200, {
                "actions": [{"status": "ready"}, {"status": "intent_ready"}],
                "summary": {"ready": 1, "intent_ready": 1},
            }
        if path == "/v1/desktop/financial-manager/query":
            assert method == "POST"
            assert body is not None
            assert body["capability_id"] == "portfolio"
            assert body["action_id"] == "risk"
            return 200, {
                "object": "aiask.desktop.financial_manager.query",
                "capability_id": "portfolio",
                "action_id": "risk",
                "tool": "agent_portfolio_risk",
                "success": True,
                "data": {"status": "ready", "portfolio_risk": {"volatility": 0.12}},
                "error": None,
                "meta": {"side_effect": {"level": "read_only"}},
            }
        if path.startswith("/v1/desktop/data/status?"):
            return 200, {"status": "blocked", "database": {"writable": True}, "quality_gate": {"status": "blocked"}}
        if path == "/v1/tools/agent_factory_status":
            assert method == "POST"
            assert body == {"recent_run_limit": 5, "_timeout_seconds": 5}
            return 200, {
                "success": True,
                "data": {
                    "status": "ready",
                    "runtime_enabled": True,
                    "event_runtime_mode": "readonly",
                    "daily_run_count": 3,
                    "cycle_count": 9,
                    "recent_run_diagnostics": {"analyzed_run_count": 2, "recent_runs": [{"run_id": "run_1"}]},
                    "configured": True,
                    "database_configured": True,
                    "run_count": 3,
                },
                "error": None,
                "meta": {"side_effect": {"level": "read_only"}},
            }
        if path == "/v1/tools/agent_market_temperature_cache_readiness":
            assert method == "POST"
            assert body == {"max_stale_days": 1}
            return 200, {
                "success": True,
                "data": {
                    "ready": False,
                    "status": "missing",
                    "read_only": True,
                    "blockers": ["market_temperature_cache_missing"],
                    "warnings": [],
                },
                "error": None,
                "meta": {"side_effect": {"level": "read_only"}},
            }
        if path == "/v1/tools/agent_market_temperature_forward_validation":
            assert method == "POST"
            assert body is not None
            assert body["target_field"] == "benchmark_return"
            assert body["benchmark_code"] == "000300"
            return 200, {
                "success": True,
                "data": {
                    "matrix": {},
                    "states": [],
                    "horizons": [1, 3, 5],
                    "count": 0,
                    "target_field": "weighted_pct_change",
                    "requested_target_field": "benchmark_return",
                    "benchmark_code": "000300",
                    "benchmark_status": "unavailable_fallback_to_weighted_pct_change",
                    "benchmark_bar_count": 0,
                },
                "error": None,
                "meta": {
                    "quality": {"status": "empty", "warnings": ["benchmark_kline_unavailable"]},
                    "side_effect": {"level": "read_only"},
                },
            }
        if path == "/v1/desktop/quant/research-runs":
            assert method == "POST"
            assert body is not None and body["universe"] == ["600519", "000001"]
            return 200, {
                "data": {
                    "research": {
                        "research_id": "research_test",
                        "status": "blocked",
                        "report": {"stages": [{"status": "completed"}, {"status": "blocked"}]},
                    }
                }
            }
        raise AssertionError(f"unexpected request: {method} {path}")

    payload = smoke.run_smoke(
        "testclient://contract",
        api_token="api-token",
        control_token="control-token",
        requester=requester,
    )

    assert payload["passed"] is True
    assert payload["summary"] == {"total": 16, "passed": 16, "failed": 0}
    assert {item["name"]: item["status"] for item in payload["results"]}["workbench_summary"] == "ready"
    assert {item["name"]: item["status"] for item in payload["results"]}["memory_status"] == "ready"
    assert {item["name"]: item["status"] for item in payload["results"]}["session_search"] == "ready"
    assert {item["name"]: item["status"] for item in payload["results"]}["memory_search"] == "ready"
    assert {item["name"]: item["status"] for item in payload["results"]}["financial_manager_query"] == "ready"
    assert {item["name"]: item["status"] for item in payload["results"]}["data_status"] == "blocked"
    assert {item["name"]: item["status"] for item in payload["results"]}["factory_status"] == "ready"
    factory = next(item for item in payload["results"] if item["name"] == "factory_status")
    assert factory["data"]["runtime_enabled"] is True
    assert factory["data"]["event_runtime_mode"] == "readonly"
    assert factory["data"]["daily_run_count"] == 3
    assert factory["data"]["cycle_count"] == 9
    assert factory["data"]["recent_run_count"] == 1
    assert {item["name"]: item["status"] for item in payload["results"]}["market_temperature_cache"] == "missing"
    assert {item["name"]: item["status"] for item in payload["results"]}["market_temperature_forward_validation"] == "unavailable_fallback_to_weighted_pct_change"
    market_forward = next(item for item in payload["results"] if item["name"] == "market_temperature_forward_validation")
    assert market_forward["data"]["quality_status"] == "empty"
    assert market_forward["data"]["warnings"] == ["benchmark_kline_unavailable"]
    assert {item["name"]: item["status"] for item in payload["results"]}["quant_research"] == "blocked"
    assert any(call[0] == "GET" and call[1].startswith("/v1/desktop/workbench/summary?") for call in calls)
    assert ("GET", "/v1/desktop/settings/status", "control-token", None) in calls
    assert any(call[0] == "GET" and call[1].startswith("/v1/search?") for call in calls)
    assert ("POST", "/v1/tools/agent_memory_search", "api-token", {"query": "AIASK", "limit": 5}) in calls
    assert any(call[0] == "POST" and call[1] == "/v1/desktop/financial-manager/query" for call in calls)
    assert ("POST", "/v1/tools/agent_factory_status", "api-token", {"recent_run_limit": 5, "_timeout_seconds": 5}) in calls
    assert ("POST", "/v1/tools/agent_market_temperature_cache_readiness", "api-token", {"max_stale_days": 1}) in calls
    assert any(call[0] == "POST" and call[1] == "/v1/tools/agent_market_temperature_forward_validation" for call in calls)
    assert ("GET", "/v1/mcp/servers?all=true", "control-token", None) in calls
    assert ("GET", "/v1/mcp/tools?all=true", "control-token", None) in calls


def test_live_readiness_smoke_plan_is_offline_and_matches_checklist() -> None:
    smoke = _load_smoke_module()

    payload = smoke.plan_payload("http://127.0.0.1:9999")

    assert payload["object"] == "aiask.live_readiness_smoke.plan"
    assert payload["status"] == "planned"
    assert payload["endpoint"] == "http://127.0.0.1:9999"
    assert payload["network_required"] is False
    assert payload["working_directory"] == "packages/agent"
    assert payload["self_test_command"].startswith("uv run python")
    assert "--self-test --pretty" in payload["self_test_command"]
    assert "--endpoint http://127.0.0.1:9999 --pretty" in payload["live_command"]
    assert "FastAPI/pandas" in payload["environment_note"]
    assert payload["summary"] == {"total": 16}
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["workbench_summary"]["path"] == "/v1/desktop/workbench/summary?session_limit=5&run_limit=5"
    assert checks["mcp_servers"]["path"] == "/v1/mcp/servers?all=true"
    assert checks["mcp_tools"]["path"] == "/v1/mcp/tools?all=true"
    assert "runtime_enabled" in checks["factory_status"]["observes"]
    assert "daily_run_count" in checks["factory_status"]["observes"]
    assert payload["side_effects"].startswith("none")
