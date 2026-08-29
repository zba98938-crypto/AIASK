from __future__ import annotations

import asyncio

from aiask_agent.adapters import strategy_factory


def test_agent_confirmed_factory_action_queues_dispatch_without_strategy_manager(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_call_db_facade(loader, params=None):
        calls.append({"loader": getattr(loader, "__name__", ""), "params": dict(params or {})})
        return {"success": True, "data": {"dispatch_id": "dispatch_test", "queued": True}, "error": None}

    monkeypatch.setattr(strategy_factory, "_call_db_facade", fake_call_db_facade)

    result = asyncio.run(
        strategy_factory.execute_confirmed_action(
            "factory_run_once",
            {"execution_mode": "dry_run"},
        )
    )

    assert result["success"] is True
    assert result["data"]["queued"] is True
    assert calls == [{"loader": "_load_factory_dispatch_handler", "params": {"execution_mode": "dry_run"}}]


def test_agent_read_only_factory_status_uses_db_facade(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_call_db_facade(loader, params=None):
        calls.append(getattr(loader, "__name__", ""))
        return {"success": True, "data": {"running": False}, "error": None}

    monkeypatch.setattr(strategy_factory, "_call_db_facade", fake_call_db_facade)

    result = asyncio.run(strategy_factory.factory_status({"recent_run_limit": 1}))

    assert result["success"] is True
    assert result["data"]["running"] is False
    assert calls == ["_load_factory_status_handler"]


def test_agent_trade_prediction_read_tools_use_db_facade(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_call_db_facade(loader, params=None):
        calls.append({"loader": getattr(loader, "__name__", ""), "params": dict(params or {})})
        if getattr(loader, "__name__", "") == "_load_trade_prediction_status_handler":
            return {"success": True, "data": {"object": "trade_prediction.status", "sample_n": 3}, "error": None}
        if getattr(loader, "__name__", "") == "_load_trade_prediction_outcomes_handler":
            return {"success": True, "data": {"object": "trade_prediction.outcomes", "items": []}, "error": None}
        if getattr(loader, "__name__", "") == "_load_trade_prediction_matrix_handler":
            return {"success": True, "data": {"object": "trade_prediction.matrix", "rows": []}, "error": None}
        raise AssertionError("unexpected loader")

    monkeypatch.setattr(strategy_factory, "_call_db_facade", fake_call_db_facade)

    status = asyncio.run(strategy_factory.trade_prediction_status({"strategy_id": "s1"}))
    outcomes = asyncio.run(strategy_factory.trade_prediction_outcomes({"score_version": "trade_prediction_score_v2"}))
    matrix = asyncio.run(strategy_factory.trade_prediction_matrix({"dimensions": ["family"]}))

    assert status["success"] is True
    assert outcomes["data"]["object"] == "trade_prediction.outcomes"
    assert matrix["data"]["object"] == "trade_prediction.matrix"
    assert [item["loader"] for item in calls] == [
        "_load_trade_prediction_status_handler",
        "_load_trade_prediction_outcomes_handler",
        "_load_trade_prediction_matrix_handler",
    ]
    assert calls[0]["params"]["strategy_id"] == "s1"
