from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

from aiask_agent.adapters import desktop_ops, quant, stock_radar, strategy_factory
from aiask_agent.plugin_runtime import NativePluginManager


def test_quant_numeric_helpers_reject_non_finite_values() -> None:
    assert quant._rate_from_bps(float("nan"), 3) == 0.0003
    assert quant._weights_from_optimization(
        {"data": {"weights": {"600519": float("nan"), "000001": 2.0}}},
        ["600519", "000001"],
    ) == [0.0, 1.0]


def test_strategy_factory_limit_helper_is_bounded() -> None:
    assert strategy_factory._safe_limit("nan", default=50, maximum=200) == 50
    assert strategy_factory._safe_limit("-5", default=50, maximum=200) == 1
    assert strategy_factory._safe_limit("5000", default=50, maximum=200) == 200


def test_desktop_factor_factory_status_bounds_limit(monkeypatch) -> None:
    calls: list[int] = []

    class FakeGateway:
        async def get_pool_status(self) -> dict[str, Any]:
            return {"status": "ready"}

        async def get_active_factors(self, *, limit: int) -> list[dict[str, Any]]:
            calls.append(limit)
            return [{"factor": "momentum"}]

    api_module = types.ModuleType("akshare_mcp.services.factor_mining_factory.api")
    api_module.get_factor_pool_gateway = lambda: FakeGateway()
    monkeypatch.setitem(sys.modules, "akshare_mcp", types.ModuleType("akshare_mcp"))
    monkeypatch.setitem(sys.modules, "akshare_mcp.services", types.ModuleType("akshare_mcp.services"))
    monkeypatch.setitem(
        sys.modules,
        "akshare_mcp.services.factor_mining_factory",
        types.ModuleType("akshare_mcp.services.factor_mining_factory"),
    )
    monkeypatch.setitem(sys.modules, "akshare_mcp.services.factor_mining_factory.api", api_module)

    result = asyncio.run(desktop_ops.factor_factory_status(limit="nan"))

    assert result["status"] == "ready"
    assert calls == [50]


def test_desktop_factor_factory_status_redacts_public_payload(monkeypatch) -> None:
    class FakeGateway:
        async def get_pool_status(self) -> dict[str, Any]:
            return {
                "status": "ready",
                "broker_token": "factor-pool-token-secret",
                "engines": {"debug": "Authorization: Bearer factor-engine-bearer-secret"},
                "pool_health": {"callback": "https://factor.local/cb?password=factor-url-secret"},
            }

        async def get_active_factors(self, *, limit: int) -> list[dict[str, Any]]:
            return [{"factor": "momentum", "client_secret": "factor-active-client-secret"}]

    api_module = types.ModuleType("akshare_mcp.services.factor_mining_factory.api")
    api_module.get_factor_pool_gateway = lambda: FakeGateway()
    monkeypatch.setitem(sys.modules, "akshare_mcp", types.ModuleType("akshare_mcp"))
    monkeypatch.setitem(sys.modules, "akshare_mcp.services", types.ModuleType("akshare_mcp.services"))
    monkeypatch.setitem(
        sys.modules,
        "akshare_mcp.services.factor_mining_factory",
        types.ModuleType("akshare_mcp.services.factor_mining_factory"),
    )
    monkeypatch.setitem(sys.modules, "akshare_mcp.services.factor_mining_factory.api", api_module)

    result = asyncio.run(desktop_ops.factor_factory_status(limit=1))
    raw = str(result)

    assert result["status"] == "ready"
    assert result["secrets_redacted"] is True
    for leaked in (
        "factor-pool-token-secret",
        "factor-engine-bearer-secret",
        "factor-url-secret",
        "factor-active-client-secret",
    ):
        assert leaked not in raw
    assert result["factory"]["broker_token"] == "[redacted]"
    assert result["engine_health"]["debug"] == "Authorization: Bearer [redacted]"
    assert result["pool_health"]["callback"] == "https://factor.local/cb?password=[redacted]"
    assert result["active_factors"][0]["client_secret"] == "[redacted]"


def test_strategy_and_stock_radar_adapters_accept_invalid_timeout(monkeypatch) -> None:
    class FakeDb:
        async def initialize(self) -> None:
            return None

    storage_module = types.ModuleType("akshare_mcp.storage")
    storage_module.get_db = lambda: FakeDb()
    monkeypatch.setitem(sys.modules, "akshare_mcp", types.ModuleType("akshare_mcp"))
    monkeypatch.setitem(sys.modules, "akshare_mcp.storage", storage_module)

    async def handler(db: FakeDb, params: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "data": {"timeout": params.get("_timeout_seconds")}, "error": None}

    strategy_result = asyncio.run(strategy_factory._call_db_facade(lambda: handler, {"_timeout_seconds": "bad"}))
    radar_result = asyncio.run(stock_radar._call_db_handler(lambda: handler, {"_timeout_seconds": "bad"}))

    assert strategy_result["success"] is True
    assert radar_result["success"] is True


def test_plugin_http_runner_bounds_numeric_options(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, max_bytes: int) -> bytes:
            captured["max_bytes"] = max_bytes
            return b'{"ok": true}'

    def fake_urlopen(request: Any, *, timeout: float) -> FakeResponse:
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("aiask_agent.plugin_runtime.urlopen", fake_urlopen)
    manager = NativePluginManager(root=tmp_path)

    result = manager._run_http(
        {"url": "http://localhost/plugin", "timeout_seconds": "nan", "max_bytes": "bad"},
        {"name": "demo"},
        "tool",
        {},
    )

    assert result == {"ok": True}
    assert captured == {"timeout": 30.0, "max_bytes": 1048576}
