"""Neutral research routes: factor factory, stock radar, trade predictions.

Replaces ``/v1/desktop/factor-factory/*``, ``/v1/desktop/stock-radar/*`` and
``/v1/desktop/trade-predictions/*``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Query, Request


def _split_csv(raw: str | None) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def create_research_router(
    *,
    require_api: Callable[[Request], None],
    factor_factory_status: Callable[..., Awaitable[dict[str, Any]]],
    audited_tool_call: Callable[..., Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    async def _call(request: Request, tool: str, arguments: dict[str, Any], surface: str) -> dict[str, Any]:
        return await audited_tool_call(
            tool,
            arguments,
            request=request,
            source_chain=["aiask_agent.server", f"research.{surface}"],
        )

    @router.get("/v1/research/factor-factory/status")
    async def factor_factory(request: Request, limit: int = Query(50, ge=1, le=1000)) -> dict[str, Any]:
        require_api(request)
        return await factor_factory_status(limit=limit)

    # ── trade predictions ─────────────────────────────────────────────────────

    @router.get("/v1/research/trade-predictions/status")
    async def trade_predictions_status(
        request: Request,
        strategy_id: str | None = None,
        stock_code: str | None = None,
        limit: int = Query(1000, ge=1, le=5000),
    ) -> dict[str, Any]:
        require_api(request)
        return await _call(
            request,
            "agent_trade_prediction_status",
            {"strategy_id": strategy_id, "stock_code": stock_code, "limit": limit},
            "trade_predictions",
        )

    @router.get("/v1/research/trade-predictions/outcomes")
    async def trade_predictions_outcomes(
        request: Request,
        prediction_id: str | None = None,
        strategy_id: str | None = None,
        stock_code: str | None = None,
        score_version: str | None = None,
        score_status: str | None = None,
        data_quality_status: str | None = None,
        actual_trading_date_lte: str | None = None,
        actual_trading_date_gte: str | None = None,
        limit: int = Query(100, ge=1, le=5000),
    ) -> dict[str, Any]:
        require_api(request)
        return await _call(
            request,
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
            "trade_predictions",
        )

    @router.get("/v1/research/trade-predictions/matrix")
    async def trade_predictions_matrix(
        request: Request,
        strategy_id: str | None = None,
        stock_code: str | None = None,
        score_version: str | None = None,
        dimensions: str = "",
        limit: int = Query(1000, ge=1, le=5000),
    ) -> dict[str, Any]:
        require_api(request)
        return await _call(
            request,
            "agent_trade_prediction_matrix",
            {
                "strategy_id": strategy_id,
                "stock_code": stock_code,
                "score_version": score_version,
                "dimensions": _split_csv(dimensions),
                "limit": limit,
            },
            "trade_predictions",
        )

    # ── stock radar ───────────────────────────────────────────────────────────

    @router.get("/v1/research/stock-radar/status")
    async def stock_radar_status(
        request: Request,
        run_id: str | None = None,
        limit: int = Query(20, ge=1, le=1000),
    ) -> dict[str, Any]:
        require_api(request)
        return await _call(
            request,
            "agent_stock_radar_status",
            {"run_id": run_id, "limit": limit},
            "stock_radar",
        )

    @router.get("/v1/research/stock-radar/candidates")
    async def stock_radar_candidates(
        request: Request,
        run_id: str | None = None,
        tier: str | None = None,
        symbol: str | None = None,
        min_score: float | None = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        require_api(request)
        return await _call(
            request,
            "agent_stock_radar_candidates",
            {
                "run_id": run_id,
                "tier": tier,
                "symbol": symbol,
                "min_score": min_score,
                "limit": limit,
            },
            "stock_radar",
        )

    @router.get("/v1/research/stock-radar/digest")
    async def stock_radar_digest(
        request: Request,
        run_id: str | None = None,
        limit: int = Query(20, ge=1, le=1000),
        channels: str = "wecom,telegram",
    ) -> dict[str, Any]:
        require_api(request)
        return await _call(
            request,
            "agent_stock_radar_digest",
            {"run_id": run_id, "limit": limit, "channels": _split_csv(channels)},
            "stock_radar",
        )

    return router


__all__ = ["create_research_router"]
