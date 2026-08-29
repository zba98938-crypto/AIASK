from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

def create_desktop_finance_router(
    *,
    require_api: Callable[[Request], None],
    control_authorized: Callable[[Request], tuple[bool, str | None]],
    require_user_scope: Callable[[Request, str | None], str],
    factor_factory_status: Callable[..., Awaitable[dict[str, Any]]],
    audited_desktop_tool_call: Callable[..., Awaitable[dict[str, Any]]],
    quant_presets: Callable[[], dict[str, Any]],
    quant_store: Any,
    financial_catalog_payload: Callable[[], dict[str, Any]],
    financial_status_payload: Callable[[], Awaitable[dict[str, Any]]],
    financial_query_payload: Callable[..., Awaitable[dict[str, Any]]],
    financial_intent_payload: Callable[..., Awaitable[dict[str, Any]]],
    read_only_result_guard: Callable[[str, dict[str, Any]], dict[str, Any]],
    broker_readiness_payload: Callable[[], dict[str, Any]],
    broker_sync_payload: Callable[..., Awaitable[dict[str, Any]]],
    broker_accounts_payload: Callable[[dict[str, Any]], dict[str, Any]],
    broker_analytics_payload: Callable[[dict[str, Any]], dict[str, Any]],
    session_store: Any,
    normalize_provider: Callable[[str | None], str],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/desktop/factor-factory/status")
    async def desktop_factor_factory_status(request: Request, limit: int = 50) -> dict[str, Any]:
        require_api(request)
        return await factor_factory_status(limit=limit)

    @router.get("/v1/desktop/trade-predictions/status")
    async def desktop_trade_predictions_status(
        request: Request,
        strategy_id: str | None = None,
        stock_code: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        require_api(request)
        return await audited_desktop_tool_call(
            "agent_trade_prediction_status",
            {"strategy_id": strategy_id, "stock_code": stock_code, "limit": limit},
            request=request,
            source_chain=["aiask_agent.server", "desktop.finance.trade_predictions"],
        )

    @router.get("/v1/desktop/trade-predictions/outcomes")
    async def desktop_trade_predictions_outcomes(
        request: Request,
        prediction_id: str | None = None,
        strategy_id: str | None = None,
        stock_code: str | None = None,
        score_version: str | None = None,
        score_status: str | None = None,
        data_quality_status: str | None = None,
        actual_trading_date_lte: str | None = None,
        actual_trading_date_gte: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        require_api(request)
        return await audited_desktop_tool_call(
            "agent_trade_prediction_outcomes",
            {
                "prediction_id": prediction_id,
                "strategy_id": strategy_id,
                "stock_code": stock_code,
                "score_version": score_version,
                "score_status": score_status,
                "data_quality_status": data_quality_status,
                "actual_trading_date_lte": actual_trading_date_lte,
                "actual_trading_date_gte": actual_trading_date_gte,
                "limit": limit,
            },
            request=request,
            source_chain=["aiask_agent.server", "desktop.finance.trade_predictions"],
        )

    @router.get("/v1/desktop/trade-predictions/matrix")
    async def desktop_trade_predictions_matrix(
        request: Request,
        strategy_id: str | None = None,
        stock_code: str | None = None,
        score_version: str | None = None,
        dimensions: str = "",
        limit: int = 1000,
    ) -> dict[str, Any]:
        require_api(request)
        dimension_list = [item.strip() for item in str(dimensions or "").split(",") if item.strip()]
        return await audited_desktop_tool_call(
            "agent_trade_prediction_matrix",
            {
                "strategy_id": strategy_id,
                "stock_code": stock_code,
                "score_version": score_version,
                "dimensions": dimension_list,
                "limit": limit,
            },
            request=request,
            source_chain=["aiask_agent.server", "desktop.finance.trade_predictions"],
        )

    @router.get("/v1/desktop/stock-radar/status")
    async def desktop_stock_radar_status(request: Request, run_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        require_api(request)
        return await audited_desktop_tool_call(
            "agent_stock_radar_status",
            {"run_id": run_id, "limit": limit},
            request=request,
            source_chain=["aiask_agent.server", "desktop.finance.stock_radar"],
        )

    @router.get("/v1/desktop/stock-radar/candidates")
    async def desktop_stock_radar_candidates(
        request: Request,
        run_id: str | None = None,
        tier: str | None = None,
        symbol: str | None = None,
        min_score: float | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        require_api(request)
        return await audited_desktop_tool_call(
            "agent_stock_radar_candidates",
            {
                "run_id": run_id,
                "tier": tier,
                "symbol": symbol,
                "min_score": min_score,
                "limit": limit,
            },
            request=request,
            source_chain=["aiask_agent.server", "desktop.finance.stock_radar"],
        )

    @router.get("/v1/desktop/stock-radar/digest")
    async def desktop_stock_radar_digest(
        request: Request,
        run_id: str | None = None,
        limit: int = 20,
        channels: str = "wecom,telegram",
    ) -> dict[str, Any]:
        require_api(request)
        return await audited_desktop_tool_call(
            "agent_stock_radar_digest",
            {"run_id": run_id, "limit": limit, "channels": [item.strip() for item in channels.split(",") if item.strip()]},
            request=request,
            source_chain=["aiask_agent.server", "desktop.finance.stock_radar"],
        )

    @router.get("/v1/desktop/quant/presets")
    async def desktop_quant_presets(request: Request) -> dict[str, Any]:
        require_api(request)
        return quant_presets()

    @router.post("/v1/desktop/quant/research-runs")
    async def desktop_quant_research_create(request: Request) -> dict[str, Any]:
        require_api(request)
        payload = dict(await request.json() or {})
        return await audited_desktop_tool_call(
            "agent_quant_research_run",
            payload,
            request=request,
            source_chain=["aiask_agent.server", "desktop.quant_research"],
        )

    @router.get("/v1/desktop/quant/research-runs/{research_id}")
    async def desktop_quant_research_get(request: Request, research_id: str) -> dict[str, Any]:
        require_api(request)
        item = quant_store.get(research_id)
        if item is None:
            raise HTTPException(404, detail=f"quant research run not found: {research_id}")
        return {"object": "aiask.quant_research_run", **item}

    @router.get("/v1/desktop/quant/research-runs/{research_id}/report")
    async def desktop_quant_research_report(request: Request, research_id: str) -> dict[str, Any]:
        require_api(request)
        report = quant_store.report(research_id)
        if report is None:
            raise HTTPException(404, detail=f"quant research report not found: {research_id}")
        return report

    @router.get("/v1/desktop/financial-manager/catalog")
    async def desktop_financial_manager_catalog(request: Request) -> dict[str, Any]:
        require_api(request)
        return financial_catalog_payload()

    @router.get("/v1/desktop/financial-manager/status")
    async def desktop_financial_manager_status(request: Request) -> dict[str, Any]:
        require_api(request)
        return await financial_status_payload()

    @router.post("/v1/desktop/financial-manager/query")
    async def desktop_financial_manager_query(request: Request) -> dict[str, Any]:
        require_api(request)
        payload = dict(await request.json() or {})

        async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return await audited_desktop_tool_call(
                tool_name,
                arguments,
                request=request,
                source_chain=["aiask_agent.server", "desktop.financial_manager"],
                result_guard=read_only_result_guard,
            )

        return await financial_query_payload(payload, tool_caller=call_tool)

    @router.post("/v1/desktop/financial-manager/intent")
    async def desktop_financial_manager_intent(request: Request) -> dict[str, Any]:
        ok, reason = control_authorized(request)
        if not ok:
            raise HTTPException(503 if reason == "control token is not configured" else 401, detail=reason)
        payload = dict(await request.json() or {})

        async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return await audited_desktop_tool_call(
                tool_name,
                arguments,
                request=request,
                source_chain=["aiask_agent.server", "desktop.financial_manager.intent"],
            )

        return await financial_intent_payload(payload, tool_caller=call_tool)

    @router.get("/v1/desktop/broker-readiness")
    async def desktop_broker_readiness(request: Request) -> dict[str, Any]:
        require_api(request)
        return broker_readiness_payload()

    @router.post("/v1/desktop/broker/sync")
    async def desktop_broker_sync(request: Request) -> dict[str, Any]:
        ok, reason = control_authorized(request)
        if not ok:
            raise HTTPException(503 if reason == "control token is not configured" else 401, detail=reason)
        payload = dict(await request.json() or {})

        async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return await audited_desktop_tool_call(
                tool_name,
                arguments,
                request=request,
                source_chain=["aiask_agent.server", "desktop.broker_readonly"],
                result_guard=read_only_result_guard,
            )

        return await broker_sync_payload(payload, headers=request.headers, tool_caller=call_tool)

    @router.get("/v1/desktop/broker/accounts")
    async def desktop_broker_accounts(request: Request, user_id: str | None = None, provider: str | None = None) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = require_user_scope(request, user_id) if user_id else None
        return broker_accounts_payload({"user_id": scoped_user_id, "provider": provider})

    @router.get("/v1/desktop/broker/positions")
    async def desktop_broker_positions(request: Request, user_id: str | None = None, provider: str | None = None) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = require_user_scope(request, user_id) if user_id else None
        return broker_accounts_payload({"user_id": scoped_user_id, "provider": provider})

    @router.get("/v1/desktop/broker/orders")
    async def desktop_broker_orders(request: Request, user_id: str | None = None, provider: str | None = None) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = require_user_scope(request, user_id) if user_id else None
        return broker_accounts_payload({"user_id": scoped_user_id, "provider": provider})

    @router.post("/v1/desktop/broker/analytics/run")
    async def desktop_broker_analytics_run(request: Request) -> dict[str, Any]:
        ok, reason = control_authorized(request)
        if not ok:
            raise HTTPException(503 if reason == "control token is not configured" else 401, detail=reason)
        return broker_analytics_payload(dict(await request.json() or {}))

    @router.get("/v1/desktop/broker/analytics/latest")
    async def desktop_broker_analytics_latest(request: Request, user_id: str | None = None, provider: str | None = None) -> dict[str, Any]:
        require_api(request)
        scoped_user_id = require_user_scope(request, user_id) if user_id else None
        latest = session_store.latest_broker_analytics(user_id=scoped_user_id, provider=normalize_provider(provider) if provider else None)
        return {
            "object": "aiask.desktop.broker_readonly.analytics",
            "success": True,
            "data": {"analytics": latest},
            "error": None,
            "read_only": True,
            "live_trading_enabled": False,
            "secrets_redacted": True,
            "source_chain": ["aiask_agent.broker_readonly"],
        }

    return router
