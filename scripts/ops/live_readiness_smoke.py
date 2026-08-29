from __future__ import annotations

import argparse
import asyncio
import os
import json
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "http://127.0.0.1:8765"
DEFAULT_CODES = ("600519", "000001")
DEFAULT_BENCHMARK_CODE = "000300"
RECOMMENDED_WORKING_DIRECTORY = "packages/agent"
SELF_TEST_COMMAND = r"uv run python ..\..\scripts\ops\live_readiness_smoke.py --self-test --pretty"
LIVE_COMMAND_TEMPLATE = r"uv run python ..\..\scripts\ops\live_readiness_smoke.py --endpoint {endpoint} --pretty"
ENVIRONMENT_NOTE = "Run from packages/agent so the Agent runtime dependencies are loaded; root or system Python may report missing FastAPI/pandas dependencies."
SMOKE_CHECKS: tuple[dict[str, Any], ...] = (
    {"name": "health", "method": "GET", "path": "/health/detailed"},
    {"name": "tools", "method": "GET", "path": "/v1/tools"},
    {"name": "financial_readiness", "method": "GET", "path": "/v1/financial-system/readiness"},
    {"name": "workbench_summary", "method": "GET", "path": "/v1/desktop/workbench/summary?session_limit=5&run_limit=5"},
    {"name": "memory_status", "method": "GET", "path": "/v1/desktop/settings/status"},
    {"name": "session_search", "method": "GET", "path": "/v1/search?query=AIASK&limit=5"},
    {"name": "memory_search", "method": "POST", "path": "/v1/tools/agent_memory_search"},
    {"name": "mcp_servers", "method": "GET", "path": "/v1/mcp/servers?all=true"},
    {"name": "mcp_tools", "method": "GET", "path": "/v1/mcp/tools?all=true"},
    {"name": "financial_manager_catalog", "method": "GET", "path": "/v1/desktop/financial-manager/catalog"},
    {"name": "financial_manager_query", "method": "POST", "path": "/v1/desktop/financial-manager/query"},
    {"name": "data_status", "method": "GET", "path": "/v1/desktop/data/status?codes=600519,000001&max_stale_days=5"},
    {"name": "factory_status", "method": "POST", "path": "/v1/tools/agent_factory_status", "observes": ["success", "runtime_enabled", "event_runtime_mode", "daily_run_count", "cycle_count"]},
    {"name": "market_temperature_cache", "method": "POST", "path": "/v1/tools/agent_market_temperature_cache_readiness", "observes": ["ready", "status", "blockers", "warnings"]},
    {"name": "market_temperature_forward_validation", "method": "POST", "path": "/v1/tools/agent_market_temperature_forward_validation", "observes": ["benchmark_status", "quality_status", "warnings", "sample_count"]},
    {"name": "quant_research", "method": "POST", "path": "/v1/desktop/quant/research-runs"},
)
JsonRequester = Callable[[str, str, str, dict[str, Any] | None, float], tuple[int, dict[str, Any]]]


@dataclass
class CheckResult:
    name: str
    ok: bool
    status: str
    detail: str
    data: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
        }
        if self.data is not None:
            payload["data"] = self.data
        return payload


def _json_request(
    endpoint: str,
    method: str,
    path: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any]]:
    url = endpoint.rstrip("/") + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return response.status, json.loads(raw) if raw.strip() else {}


def _safe_get(record: dict[str, Any], path: tuple[str, ...], default: Any = None) -> Any:
    value: Any = record
    for key in path:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _run_check(name: str, fn) -> CheckResult:
    try:
        return fn()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return CheckResult(name, False, f"http_{exc.code}", detail[:500])
    except Exception as exc:
        return CheckResult(name, False, "failed", str(exc)[:500])


def plan_payload(endpoint: str = DEFAULT_ENDPOINT) -> dict[str, Any]:
    return {
        "object": "aiask.live_readiness_smoke.plan",
        "endpoint": endpoint,
        "status": "planned",
        "working_directory": RECOMMENDED_WORKING_DIRECTORY,
        "script": "scripts/ops/live_readiness_smoke.py",
        "self_test_command": SELF_TEST_COMMAND,
        "live_command": LIVE_COMMAND_TEMPLATE.format(endpoint=endpoint),
        "environment_note": ENVIRONMENT_NOTE,
        "checks": [dict(item) for item in SMOKE_CHECKS],
        "summary": {"total": len(SMOKE_CHECKS)},
        "network_required": False,
        "side_effects": "none; plan mode does not contact Agent, MCP, databases, brokers, or external services.",
        "secrets_redacted": True,
    }


def run_smoke(
    endpoint: str,
    *,
    api_token: str = "",
    control_token: str = "",
    timeout: float = 15.0,
    requester: JsonRequester | None = None,
) -> dict[str, Any]:
    results: list[CheckResult] = []
    request_json = requester or (lambda method, path, token, body, request_timeout: _json_request(endpoint, method, path, token=token, body=body, timeout=request_timeout))

    def health() -> CheckResult:
        _, payload = request_json("GET", "/health/detailed", api_token, None, timeout)
        status = str(payload.get("status") or "unknown")
        tool_count = _safe_get(payload, ("tools", "count"), 0)
        return CheckResult("health", status in {"ok", "healthy"}, status, f"tools={tool_count}", {"tool_count": tool_count})

    def tools() -> CheckResult:
        _, payload = request_json("GET", "/v1/tools", api_token, None, timeout)
        data = list(payload.get("data") or payload.get("tools") or [])
        names = {str(item.get("name") or "") for item in data if isinstance(item, dict)}
        required = {
            "agent_tool_catalog",
            "agent_analyze_stock",
            "agent_action_intent_create",
            "agent_market_temperature_cache_readiness",
            "agent_market_temperature_forward_validation",
        }
        missing = sorted(required - names)
        return CheckResult("tools", not missing, "ready" if not missing else "missing", f"count={len(data)} missing={missing}", {"count": len(data), "missing": missing})

    def financial_readiness() -> CheckResult:
        _, payload = request_json("GET", "/v1/financial-system/readiness", api_token, None, timeout)
        status = str(payload.get("status") or "unknown")
        next_actions = list(payload.get("next_actions") or [])
        return CheckResult(
            "financial_readiness",
            status in {"ready", "degraded", "blocked"},
            status,
            f"production_ready={payload.get('production_ready')} next_actions={len(next_actions)}",
            {
                "production_ready": payload.get("production_ready"),
                "required_summary": payload.get("summary"),
                "next_actions": [item.get("action_id") for item in next_actions if isinstance(item, dict)],
            },
        )

    def workbench_summary() -> CheckResult:
        path = f"/v1/desktop/workbench/summary?{urllib.parse.urlencode({'session_limit': '5', 'run_limit': '5'})}"
        _, payload = request_json("GET", path, api_token, None, timeout)
        queues = payload.get("queues") if isinstance(payload.get("queues"), dict) else {}
        access = payload.get("access") if isinstance(payload.get("access"), dict) else {}
        recent_sessions = list(payload.get("recent_sessions") or [])
        recent_runs = list(payload.get("recent_runs") or [])
        structured = payload.get("object") == "aiask.desktop.workbench.summary" and isinstance(queues, dict) and isinstance(access, dict)
        return CheckResult(
            "workbench_summary",
            structured,
            "ready" if structured else "invalid",
            f"sessions={len(recent_sessions)} runs={len(recent_runs)} pending_intents={queues.get('pending_intents', 0)}",
            {
                "recent_sessions": len(recent_sessions),
                "recent_runs": len(recent_runs),
                "pending_intents": queues.get("pending_intents"),
                "pending_approvals": queues.get("pending_approvals"),
                "mcp_degraded": queues.get("mcp_degraded"),
                "sessions_admin_available": access.get("sessions_admin_available"),
            },
        )

    def memory_status() -> CheckResult:
        _, payload = request_json("GET", "/v1/desktop/settings/status", control_token or api_token, None, timeout)
        memory = payload.get("memory") if isinstance(payload.get("memory"), dict) else {}
        providers = list(memory.get("providers") or [])
        active_provider = str(memory.get("active_provider") or memory.get("provider") or memory.get("default_provider") or "unknown")
        status = str(memory.get("status") or "unknown")
        sqlite_ready = any(isinstance(item, dict) and item.get("name") == "sqlite" and item.get("configured") is True for item in providers)
        ok = status in {"ready", "implemented"} or active_provider == "sqlite" or sqlite_ready
        return CheckResult(
            "memory_status",
            ok,
            "ready" if ok else status,
            f"active_provider={active_provider} providers={len(providers)}",
            {"active_provider": active_provider, "status": status, "provider_count": len(providers)},
        )

    def session_search() -> CheckResult:
        path = f"/v1/search?{urllib.parse.urlencode({'query': 'AIASK', 'limit': '5'})}"
        _, payload = request_json("GET", path, api_token, None, timeout)
        data = list(payload.get("data") or [])
        return CheckResult(
            "session_search",
            isinstance(payload.get("data"), list),
            "ready",
            f"hits={len(data)}",
            {"hit_count": len(data), "object": payload.get("object")},
        )

    def memory_search() -> CheckResult:
        _, payload = request_json(
            "POST",
            "/v1/tools/agent_memory_search",
            api_token,
            {"query": "AIASK", "limit": 5},
            timeout,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        items = list(data.get("items") or [])
        success = payload.get("success") is not False and isinstance(data.get("items"), list)
        return CheckResult(
            "memory_search",
            success,
            "ready" if success else str(payload.get("error_code") or "failed"),
            f"hits={len(items)}",
            {"hit_count": len(items), "error_code": payload.get("error_code")},
        )

    def mcp_servers() -> CheckResult:
        _, payload = request_json("GET", "/v1/mcp/servers?all=true", control_token or api_token, None, timeout)
        data = list(payload.get("data") or [])
        status = "ready" if data else "empty"
        return CheckResult("mcp_servers", True, status, f"servers={len(data)}", {"count": len(data)})

    def mcp_tools() -> CheckResult:
        _, payload = request_json("GET", "/v1/mcp/tools?all=true", control_token or api_token, None, timeout)
        data = list(payload.get("data") or [])
        status = "ready" if data else "empty"
        return CheckResult("mcp_tools", True, status, f"tools={len(data)}", {"count": len(data)})

    def financial_manager_catalog() -> CheckResult:
        _, payload = request_json("GET", "/v1/desktop/financial-manager/catalog", api_token, None, timeout)
        actions = list(payload.get("actions") or [])
        ready_count = sum(1 for item in actions if isinstance(item, dict) and item.get("status") in {"ready", "intent_ready"})
        return CheckResult("financial_manager_catalog", bool(actions), "ready" if ready_count else "degraded", f"actions={len(actions)} ready_or_intent={ready_count}", {"summary": payload.get("summary")})

    def financial_manager_query() -> CheckResult:
        body = {
            "capability_id": "portfolio",
            "action_id": "risk",
            "params": {"codes": list(DEFAULT_CODES), "weights": [0.5, 0.5]},
        }
        _, payload = request_json("POST", "/v1/desktop/financial-manager/query", api_token, body, timeout)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        success = payload.get("success") is not False
        status = str(data.get("status") or payload.get("error_code") or "ready")
        side_effect = _safe_get(payload, ("meta", "side_effect", "level"))
        read_only = side_effect in {None, "read_only"}
        return CheckResult(
            "financial_manager_query",
            success and read_only,
            status,
            f"tool={payload.get('tool')} success={payload.get('success')}",
            {
                "tool": payload.get("tool"),
                "success": payload.get("success"),
                "side_effect": side_effect,
                "error_code": payload.get("error_code"),
            },
        )

    def data_status() -> CheckResult:
        codes = ",".join(DEFAULT_CODES)
        path = f"/v1/desktop/data/status?{urllib.parse.urlencode({'codes': codes, 'max_stale_days': '5'})}"
        _, payload = request_json("GET", path, api_token, None, timeout)
        status = str(payload.get("status") or _safe_get(payload, ("data_gate", "data", "status"), "unknown"))
        return CheckResult("data_status", status not in {"failed", "error"}, status, f"codes={codes}", {"database": payload.get("database"), "quality_gate": payload.get("quality_gate")})

    def factory_status() -> CheckResult:
        _, payload = request_json(
            "POST",
            "/v1/tools/agent_factory_status",
            api_token,
            {"recent_run_limit": 5, "_timeout_seconds": 5},
            timeout,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        diagnostics = data.get("recent_run_diagnostics") if isinstance(data.get("recent_run_diagnostics"), dict) else {}
        recent_runs = list(diagnostics.get("recent_runs") or [])
        runtime_enabled = data.get("runtime_enabled")
        daily_run_count = data.get("daily_run_count", data.get("run_count"))
        cycle_count = data.get("cycle_count")
        status_value = data.get("status") or data.get("last_status")
        if not status_value:
            if payload.get("success") is False:
                status_value = payload.get("error_code") or "failed"
            elif data.get("running") is True:
                status_value = "running"
            elif runtime_enabled is False:
                status_value = "disabled"
            else:
                status_value = "ready"
        status = str(status_value)
        side_effect = _safe_get(payload, ("meta", "side_effect", "level"))
        success = payload.get("success") is not False
        ok = success and side_effect in {None, "read_only"}
        return CheckResult(
            "factory_status",
            ok,
            status,
            f"success={payload.get('success')} runtime_enabled={runtime_enabled} daily_runs={daily_run_count}",
            {
                "success": payload.get("success"),
                "runtime_enabled": runtime_enabled,
                "event_runtime_mode": data.get("event_runtime_mode"),
                "running": data.get("running"),
                "daily_run_count": daily_run_count,
                "cycle_count": cycle_count,
                "recent_run_count": len(recent_runs),
                "analyzed_run_count": diagnostics.get("analyzed_run_count"),
                "last_status": data.get("last_status"),
                "configured": data.get("configured", runtime_enabled),
                "database_configured": data.get("database_configured"),
                "run_count": data.get("run_count", daily_run_count),
                "side_effect": side_effect,
                "error_code": payload.get("error_code"),
            },
        )

    def market_temperature_cache() -> CheckResult:
        _, payload = request_json(
            "POST",
            "/v1/tools/agent_market_temperature_cache_readiness",
            api_token,
            {"max_stale_days": 1},
            timeout,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status = str(data.get("status") or payload.get("error_code") or "unknown")
        side_effect = _safe_get(payload, ("meta", "side_effect", "level"))
        structured = isinstance(data.get("ready"), bool) and bool(status)
        ok = payload.get("success") is not False and structured and side_effect in {None, "read_only"}
        return CheckResult(
            "market_temperature_cache",
            ok,
            status,
            f"ready={data.get('ready')} blockers={len(list(data.get('blockers') or []))}",
            {
                "ready": data.get("ready"),
                "as_of": data.get("as_of"),
                "blockers": list(data.get("blockers") or []),
                "warnings": list(data.get("warnings") or []),
                "side_effect": side_effect,
                "error_code": payload.get("error_code"),
            },
        )

    def market_temperature_forward_validation() -> CheckResult:
        body = {
            "limit": 30,
            "horizons": [1, 3, 5],
            "target_field": "benchmark_return",
            "benchmark_code": DEFAULT_BENCHMARK_CODE,
            "min_samples": 1,
            "include_samples": False,
        }
        _, payload = request_json(
            "POST",
            "/v1/tools/agent_market_temperature_forward_validation",
            api_token,
            body,
            timeout,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status = str(data.get("benchmark_status") or payload.get("error_code") or "unknown")
        side_effect = _safe_get(payload, ("meta", "side_effect", "level"))
        target_field = str(data.get("target_field") or "")
        sample_count = int(data.get("count") or 0)
        quality = _safe_get(payload, ("meta", "quality"), {})
        quality = quality if isinstance(quality, dict) else {}
        warnings = _string_list(data.get("warnings"))
        for warning in _string_list(quality.get("warnings")):
            if warning not in warnings:
                warnings.append(warning)
        structured = isinstance(data.get("matrix"), dict) and target_field in {"benchmark_return", "weighted_pct_change"}
        ok = payload.get("success") is not False and structured and side_effect in {None, "read_only"}
        return CheckResult(
            "market_temperature_forward_validation",
            ok,
            status,
            f"target={target_field or '-'} samples={sample_count}",
            {
                "target_field": target_field,
                "requested_target_field": data.get("requested_target_field"),
                "benchmark_code": data.get("benchmark_code"),
                "benchmark_status": data.get("benchmark_status"),
                "benchmark_bar_count": data.get("benchmark_bar_count"),
                "sample_count": sample_count,
                "quality_status": quality.get("status"),
                "warnings": warnings,
                "side_effect": side_effect,
                "error_code": payload.get("error_code"),
            },
        )

    def quant_research() -> CheckResult:
        body = {
            "universe": list(DEFAULT_CODES),
            "factors": ["momentum", "volatility"],
            "benchmark": DEFAULT_BENCHMARK_CODE,
            "rebalance_frequency": "monthly",
            "cost_bps": 3,
            "slippage_bps": 1,
            "include_strategy_review": False,
        }
        _, payload = request_json("POST", "/v1/desktop/quant/research-runs", api_token, body, max(timeout, 30.0))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        research = data.get("research") if isinstance(data, dict) else None
        if not isinstance(research, dict):
            research = payload.get("research") if isinstance(payload.get("research"), dict) else {}
        status = str(research.get("status") or payload.get("status") or "unknown")
        stages = list(_safe_get(research, ("report", "stages"), []) or _safe_get(research, ("payload", "stages"), []) or [])
        acceptable = status in {"completed", "partial", "blocked"} or any(isinstance(item, dict) and item.get("status") in {"blocked", "completed", "partial"} for item in stages)
        return CheckResult("quant_research", acceptable, status, f"stages={len(stages)}", {"research_id": research.get("research_id"), "stage_statuses": [item.get("status") for item in stages if isinstance(item, dict)]})

    checks = [
        health,
        tools,
        financial_readiness,
        workbench_summary,
        memory_status,
        session_search,
        memory_search,
        mcp_servers,
        mcp_tools,
        financial_manager_catalog,
        financial_manager_query,
        data_status,
        factory_status,
        market_temperature_cache,
        market_temperature_forward_validation,
        quant_research,
    ]
    for check in checks:
        results.append(_run_check(check.__name__, check))

    failed = [item for item in results if not item.ok]
    return {
        "object": "aiask.live_readiness_smoke",
        "endpoint": endpoint,
        "status": "passed" if not failed else "failed",
        "passed": not failed,
        "started_at_epoch": int(time.time()),
        "summary": {
            "total": len(results),
            "passed": sum(1 for item in results if item.ok),
            "failed": len(failed),
        },
        "results": [item.as_dict() for item in results],
        "secrets_redacted": True,
    }


def _add_repo_src_paths() -> None:
    root = Path(__file__).resolve().parents[2]
    for rel in (
        "packages/agent/src",
        "packages/akshare-mcp/src",
        "packages/strategy-factory/src",
        "packages/aiask-quant-core/src",
    ):
        path = str(root / rel)
        if path not in sys.path:
            sys.path.insert(0, path)


@contextmanager
def _temporary_env(values: dict[str, str]):
    old_values = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in old_values.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def run_self_test(*, timeout: float = 15.0) -> dict[str, Any]:
    """Run the smoke checks against an in-process temporary Agent.

    This is a development safety check. It does not touch live broker clients
    and stores Agent/SQLite state under a temporary directory.
    """
    _add_repo_src_paths()
    with tempfile.TemporaryDirectory(prefix="aiask-live-smoke-") as tmp:
        tmp_path = Path(tmp)
        control_token = "self-test-control-token"
        sqlite_path = str(tmp_path / "akshare_mcp.sqlite3")
        with _temporary_env(
            {
                "AIASK_AGENT_HOME": str(tmp_path / "agent-home"),
                "AIASK_AGENT_LOAD_PROJECT_ENV": "0",
                "AIASK_AGENT_ENABLE_HERMES_FULL": "1",
                "AIASK_AGENT_CONTROL_TOKEN": control_token,
                "AIASK_AGENT_MODEL_PROVIDER": "mock",
                "AIASK_SQLITE_PATH": sqlite_path,
                "AKSHARE_MCP_SQLITE_PATH": sqlite_path,
            }
        ):
            try:
                from fastapi.testclient import TestClient

                from aiask_agent.model_client import MockModelClient
                from aiask_agent.runtime import AgentRuntime
                from aiask_agent.server import create_app
                from aiask_agent.session_store import AgentSessionStore
            except ModuleNotFoundError as exc:
                missing = str(getattr(exc, "name", "") or exc)
                result = CheckResult(
                    "self_test_dependencies",
                    False,
                    "missing_dependency",
                    f"missing Python dependency: {missing}",
                    {
                        "missing_module": missing,
                        "hint": "Run from packages/agent with uv run, or install the aiask-agent runtime dependencies.",
                    },
                )
                return {
                    "object": "aiask.live_readiness_smoke",
                    "endpoint": "testclient://aiask-agent",
                    "status": "failed",
                    "passed": False,
                    "started_at_epoch": int(time.time()),
                    "summary": {"total": 1, "passed": 0, "failed": 1},
                    "results": [result.as_dict()],
                    "secrets_redacted": True,
                    "mode": "self_test",
                    "temp_state": "discarded",
                }

            runtime = AgentRuntime(
                model_client=MockModelClient(),
                session_store=AgentSessionStore(tmp_path / "agent_state.sqlite3"),
                max_iterations=2,
            )
            with TestClient(create_app(runtime=runtime)) as client:

                def requester(method: str, path: str, token: str, body: dict[str, Any] | None, request_timeout: float) -> tuple[int, dict[str, Any]]:
                    headers = {"Authorization": f"Bearer {token}"} if token.strip() else {}
                    response = client.request(method, path, headers=headers, json=body)
                    payload = response.json() if response.content else {}
                    if response.status_code >= 400:
                        raise RuntimeError(f"HTTP {response.status_code}: {payload}")
                    return response.status_code, payload

                payload = run_smoke(
                    "testclient://aiask-agent",
                    control_token=control_token,
                    timeout=timeout,
                    requester=requester,
                )
            payload["mode"] = "self_test"
            payload["temp_state"] = "discarded"
            try:
                from aiask_quant_core.storage.sqlite import _close_all_db_instances

                asyncio.run(_close_all_db_instances())
            except Exception:
                pass
            return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only AIASK live readiness smoke against a running Agent.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Agent endpoint, default http://127.0.0.1:8765")
    parser.add_argument("--api-token", default="", help="API token if the Agent requires one")
    parser.add_argument("--control-token", default="", help="Control token for gated MCP inventory checks")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--plan", action="store_true", help="Print the smoke plan without contacting the Agent")
    parser.add_argument("--self-test", action="store_true", help="Run against an in-process temporary Agent instead of a network endpoint")
    args = parser.parse_args(argv)

    if args.plan:
        payload = plan_payload(args.endpoint)
    elif args.self_test:
        payload = run_self_test(timeout=args.timeout)
    else:
        payload = run_smoke(
            args.endpoint,
            api_token=args.api_token,
            control_token=args.control_token,
            timeout=args.timeout,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    if args.plan:
        return 0
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
