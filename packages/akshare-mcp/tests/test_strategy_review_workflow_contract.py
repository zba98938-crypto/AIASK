from __future__ import annotations

import asyncio

from akshare_mcp.tools.skills_strategy_workflows import build_strategy_review_workflow_payload


async def _fake_strategy_review_resource(strategy_id: str):
    assert strategy_id == "strategy-review"
    return {
        "found": True,
        "summary": {
            "current_status": "submitted",
            "open_risk_count": 2,
        },
    }


async def _fake_runtime_strategy_manager(*, action: str, params: dict):
    if action == "closure_review":
        assert params["strategy_id"] == "strategy-review"
        assert params["as_of"] == "2026-04-21"
        return {
            "success": True,
            "data": {
                "strategy_id": "strategy-review",
                "as_of": "2026-04-21",
                "correlation_id": "corr-review",
                "factory_run_id": "factory-review",
                "incubation": {
                    "overview": {
                        "status": "incubating",
                    },
                },
                "runtime": {
                    "risk_events": [{"id": "risk-1"}],
                },
            },
        }
    if action == "review_report":
        return {"success": True, "data": {"summary": {"validation_grade": "A"}}}
    if action == "factory_status":
        return {"success": True, "data": {"status": "idle"}}
    if action == "runtime_alerts":
        return {"success": True, "data": {"items": []}}
    raise AssertionError(f"unexpected action: {action}")


def test_strategy_review_workflow_uses_closure_review_as_primary_contract():
    result = asyncio.run(
        build_strategy_review_workflow_payload(
            "strategy-review",
            runtime_strategy_manager=_fake_runtime_strategy_manager,
            build_strategy_review_payload=_fake_strategy_review_resource,
            include_factory_status=True,
            include_review_report=True,
            include_runtime_alerts=True,
            as_of="2026-04-21",
        )
    )

    assert result["strategy_id"] == "strategy-review"
    assert result["summary"]["current_status"] == "incubating"
    assert result["summary"]["open_risk_count"] == 1
    assert result["summary"]["as_of"] == "2026-04-21"
    assert result["summary"]["correlation_id"] == "corr-review"
    assert result["summary"]["factory_run_id"] == "factory-review"
    assert result["closure_review"]["strategy_id"] == "strategy-review"
    assert [step["step"] for step in result["steps"][:2]] == [
        "resource.strategy_review",
        "strategy_manager.closure_review",
    ]
