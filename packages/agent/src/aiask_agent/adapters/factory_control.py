"""Unified read-only Agent facade for all four factory runtimes."""

from __future__ import annotations

from typing import Any, Awaitable, Callable


async def _call(
    tool: str,
    handler: Callable[..., Awaitable[dict[str, Any]]],
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        payload = await handler(**kwargs)
    except Exception as exc:
        return {
            "success": False,
            "data": None,
            "error": str(exc),
            "error_code": "FACTORY_CONTROL_READ_FAILED",
        }
    return {
        "success": True,
        "data": payload,
        "error": None,
        "meta": {
            "tool": tool,
            "source_chain": ["aiask_agent.adapters.factory_control", "factory_run_ledger"],
            "side_effect": {
                "level": "read_only",
                "confirmation_required": False,
                "idempotent": True,
            },
        },
    }


async def catalog(arguments: dict[str, Any]) -> dict[str, Any]:
    del arguments
    from strategy_factory.api import list_factories

    return await _call("agent_factory_catalog", list_factories)


async def status(arguments: dict[str, Any]) -> dict[str, Any]:
    from strategy_factory.api import factory_status

    return await _call(
        "agent_factory_control_status",
        factory_status,
        factory=str(arguments.get("factory") or ""),
    )


async def runs(arguments: dict[str, Any]) -> dict[str, Any]:
    from strategy_factory.api import factory_runs

    return await _call(
        "agent_factory_control_runs",
        factory_runs,
        factory=str(arguments.get("factory") or "") or None,
        status=str(arguments.get("status") or "") or None,
        limit=int(arguments.get("limit") or 20),
    )


async def run_detail(arguments: dict[str, Any]) -> dict[str, Any]:
    from strategy_factory.api import factory_run_detail

    return await _call(
        "agent_factory_run_detail",
        factory_run_detail,
        run_id=str(arguments.get("run_id") or ""),
    )


async def readiness(arguments: dict[str, Any]) -> dict[str, Any]:
    from strategy_factory.api import factory_readiness

    return await _call(
        "agent_factory_readiness",
        factory_readiness,
        factory=str(arguments.get("factory") or "") or None,
    )


__all__ = ["catalog", "readiness", "run_detail", "runs", "status"]
