"""Neutral finance routes: quant research, financial manager, broker read-only.

Replaces ``/v1/desktop/quant/*``, ``/v1/desktop/financial-manager/*``,
``/v1/desktop/broker-readiness`` and ``/v1/desktop/broker/*``.

The broker resource endpoints return one resource each. They previously all
called the same full-snapshot payload, so ``/accounts``, ``/positions`` and
``/orders`` returned byte-identical bodies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request


def create_finance_router(
    *,
    require_api: Callable[[Request], None],
    control_authorized: Callable[[Request], tuple[bool, str | None]],
    require_user_scope: Callable[[Request, str | None], str],
    audited_tool_call: Callable[..., Awaitable[dict[str, Any]]],
    quant_presets: Callable[[], dict[str, Any]],
    quant_store: Any,
    financial_catalog_payload: Callable[[], dict[str, Any]],
    financial_status_payload: Callable[[], Awaitable[dict[str, Any]]],
    financial_query_payload: Callable[..., Awaitable[dict[str, Any]]],
    financial_intent_payload: Callable[..., Awaitable[dict[str, Any]]],
    read_only_result_guard: Callable[[str, dict[str, Any]], dict[str, Any]],
    broker_readiness_payload: Callable[[], dict[str, Any]],
    broker_sync_payload: Callable[..., Awaitable[dict[str, Any]]],
    broker_snapshot_payload: Callable[[dict[str, Any]], dict[str, Any]],
    broker_resource_payload: Callable[..., dict[str, Any]],
    broker_analytics_payload: Callable[[dict[str, Any]], dict[str, Any]],
    session_store: Any,
    normalize_provider: Callable[[str | None], str],
) -> APIRouter:
    router = APIRouter()

    def _require_control(request: Request) -> None:
        ok, reason = control_authorized(request)
        if not ok:
            raise HTTPException(
                503 if reason == "control token is not configured" else 401,
                detail=reason or "unauthorized",
            )

    def _scoped_user(request: Request, user_id: str | None) -> str | None:
        return require_user_scope(request, user_id) if user_id else None

    # ── quant research ────────────────────────────────────────────────────────

    @router.get("/v1/finance/quant/presets")
    async def quant_presets_get(request: Request) -> dict[str, Any]:
        require_api(request)
        return quant_presets()

    @router.post("/v1/finance/quant/research-runs")
    async def quant_research_create(request: Request) -> dict[str, Any]:
        require_api(request)
        payload = dict(await request.json() or {})
        return await audited_tool_call(
            "agent_quant_research_run",
            payload,
            request=request,
            source_chain=["aiask_agent.server", "finance.quant_research"],
        )

    @router.get("/v1/finance/quant/research-runs/{research_id}")
    async def quant_research_get(request: Request, research_id: str) -> dict[str, Any]:
        require_api(request)
        item = quant_store.get(research_id)
        if item is None:
            raise HTTPException(404, detail=f"quant research run not found: {research_id}")
        return {"object": "aiask.quant_research_run", **item}

    @router.get("/v1/finance/quant/research-runs/{research_id}/report")
    async def quant_research_report(request: Request, research_id: str) -> dict[str, Any]:
        require_api(request)
        report = quant_store.report(research_id)
        if report is None:
            raise HTTPException(404, detail=f"quant research report not found: {research_id}")
        return report

    # ── financial manager ─────────────────────────────────────────────────────

    @router.get("/v1/finance/manager/catalog")
    async def financial_manager_catalog(request: Request) -> dict[str, Any]:
        require_api(request)
        return financial_catalog_payload()

    @router.get("/v1/finance/manager/status")
    async def financial_manager_status(request: Request) -> dict[str, Any]:
        require_api(request)
        return await financial_status_payload()

    @router.post("/v1/finance/manager/query")
    async def financial_manager_query(request: Request) -> dict[str, Any]:
        require_api(request)
        payload = dict(await request.json() or {})

        async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return await audited_tool_call(
                tool_name,
                arguments,
                request=request,
                source_chain=["aiask_agent.server", "finance.manager"],
                result_guard=read_only_result_guard,
            )

        return await financial_query_payload(payload, tool_caller=call_tool)

    @router.post("/v1/finance/manager/intent")
    async def financial_manager_intent(request: Request) -> dict[str, Any]:
        _require_control(request)
        payload = dict(await request.json() or {})

        async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return await audited_tool_call(
                tool_name,
                arguments,
                request=request,
                source_chain=["aiask_agent.server", "finance.manager.intent"],
            )

        return await financial_intent_payload(payload, tool_caller=call_tool)

    # ── broker (read-only) ────────────────────────────────────────────────────

    @router.get("/v1/finance/broker/readiness")
    async def broker_readiness(request: Request) -> dict[str, Any]:
        require_api(request)
        return broker_readiness_payload()

    @router.post("/v1/finance/broker/sync")
    async def broker_sync(request: Request) -> dict[str, Any]:
        _require_control(request)
        payload = dict(await request.json() or {})

        async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return await audited_tool_call(
                tool_name,
                arguments,
                request=request,
                source_chain=["aiask_agent.server", "finance.broker_readonly"],
                result_guard=read_only_result_guard,
            )

        return await broker_sync_payload(payload, headers=request.headers, tool_caller=call_tool)

    @router.get("/v1/finance/broker/snapshot")
    async def broker_snapshot(
        request: Request,
        user_id: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Every broker resource at once, for callers that genuinely want all of them."""
        require_api(request)
        return broker_snapshot_payload(
            {"user_id": _scoped_user(request, user_id), "provider": provider}
        )

    @router.get("/v1/finance/broker/accounts")
    async def broker_accounts(
        request: Request,
        user_id: str | None = None,
        provider: str | None = None,
        limit: int = Query(20, ge=1, le=1000),
    ) -> dict[str, Any]:
        require_api(request)
        return broker_resource_payload(
            "accounts",
            {"user_id": _scoped_user(request, user_id), "provider": provider, "limit": limit},
        )

    @router.get("/v1/finance/broker/positions")
    async def broker_positions(
        request: Request,
        user_id: str | None = None,
        provider: str | None = None,
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        require_api(request)
        return broker_resource_payload(
            "positions",
            {"user_id": _scoped_user(request, user_id), "provider": provider, "limit": limit},
        )

    @router.get("/v1/finance/broker/orders")
    async def broker_orders(
        request: Request,
        user_id: str | None = None,
        provider: str | None = None,
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        require_api(request)
        return broker_resource_payload(
            "orders",
            {"user_id": _scoped_user(request, user_id), "provider": provider, "limit": limit},
        )

    @router.get("/v1/finance/broker/deals")
    async def broker_deals(
        request: Request,
        user_id: str | None = None,
        provider: str | None = None,
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        require_api(request)
        return broker_resource_payload(
            "deals",
            {"user_id": _scoped_user(request, user_id), "provider": provider, "limit": limit},
        )

    @router.post("/v1/finance/broker/analytics/run")
    async def broker_analytics_run(request: Request) -> dict[str, Any]:
        _require_control(request)
        return broker_analytics_payload(dict(await request.json() or {}))

    @router.get("/v1/finance/broker/analytics/latest")
    async def broker_analytics_latest(
        request: Request,
        user_id: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = _scoped_user(request, user_id)
        latest = session_store.latest_broker_analytics(
            user_id=scoped_user_id,
            provider=normalize_provider(provider) if provider else None,
        )
        return {
            "object": "aiask.broker_readonly.analytics",
            "success": True,
            "data": {"analytics": latest},
            "error": None,
            "error_code": None,
            "read_only": True,
            "live_trading_enabled": False,
            "secrets_redacted": True,
            "source_chain": ["aiask_agent.broker_readonly"],
        }

    return router


__all__ = ["create_finance_router"]
