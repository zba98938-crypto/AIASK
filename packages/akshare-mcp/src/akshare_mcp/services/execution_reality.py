"""Execution reality contract for strategy tools.

Makes fill model, slippage, market impact, and promotion gate assumptions
explicitly visible to AI consumers, preventing misinterpretation of
backtest results as production-ready.

Usage::

    from akshare_mcp.services.execution_reality import build_execution_reality_report

    report = build_execution_reality_report(mode="backtest")
    result["execution_reality"] = report.to_dict()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cost_model import resolve_cost_assumptions


@dataclass
class ExecutionRealityReport:
    """Structured representation of execution assumptions.

    AI should read this to understand whether a strategy result
    is based on realistic production assumptions or idealized backtest.
    """

    fill_model: str  # "close_price" / "vwap" / "market_order" / "limit_order"
    slippage_assumption: dict[str, Any]
    market_impact_assumption: dict[str, Any]
    commission_assumption: dict[str, Any]
    liquidity_gate: dict[str, Any]
    promotion_gate: dict[str, Any]
    cost_model_mode: str  # "execution" / "backtest"
    total_cost_bps: float
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "fill_model": self.fill_model,
            "slippage_assumption": self.slippage_assumption,
            "market_impact_assumption": self.market_impact_assumption,
            "commission_assumption": self.commission_assumption,
            "liquidity_gate": self.liquidity_gate,
            "promotion_gate": self.promotion_gate,
            "cost_model_mode": self.cost_model_mode,
            "total_cost_bps": self.total_cost_bps,
        }
        if self.warnings:
            result["warnings"] = self.warnings
        return result


# Default promotion gate thresholds (from strategy_mgr_helpers.py patterns)
_DEFAULT_PROMOTION_GATE = {
    "min_sharpe_ratio": 0.5,
    "min_win_rate": 0.45,
    "max_drawdown": 0.20,
    "min_trade_count": 20,
    "min_incubation_days": 30,
    "min_statistical_checks_passed": 2,
}

# Default liquidity gate
_DEFAULT_LIQUIDITY_GATE = {
    "min_daily_volume_cny": 5_000_000,
    "min_daily_turnover_rate": 0.005,
}


def build_execution_reality_report(
    *,
    mode: str = "backtest",
    fill_model: str = "close_price",
    kwargs: dict[str, Any] | None = None,
    liquidity_gate: dict[str, Any] | None = None,
    promotion_gate: dict[str, Any] | None = None,
) -> ExecutionRealityReport:
    """Build a structured execution reality report.

    Parameters
    ----------
    mode:
        "backtest" or "execution". Determines default cost assumptions.
    fill_model:
        Fill model name.
    kwargs:
        Additional cost parameters (slippage_bps, market_impact_bps, etc.).
    liquidity_gate:
        Custom liquidity gate thresholds. None = use defaults.
    promotion_gate:
        Custom promotion gate thresholds. None = use defaults.
    """
    resolved_kwargs = dict(kwargs or {})
    cost = resolve_cost_assumptions(resolved_kwargs, default_mode=mode)

    slippage_bps = cost["slippage_bps"]
    impact_bps = cost["market_impact_bps"]
    commission_rate = cost["commission_rate"]
    commission_bps = commission_rate * 10000

    total_cost_bps = round(slippage_bps + impact_bps + commission_bps, 2)

    # Slippage assumption
    slippage_assumption = {
        "bps": slippage_bps,
        "description": "零滑点（研究态）" if slippage_bps == 0 else f"{slippage_bps} bps 滑点",
        "realistic": slippage_bps > 0,
    }

    # Market impact assumption
    market_impact_assumption = {
        "bps": impact_bps,
        "description": "零市场冲击（研究态）" if impact_bps == 0 else f"{impact_bps} bps 冲击",
        "realistic": impact_bps > 0,
    }

    # Commission
    commission_assumption = {
        "rate": commission_rate,
        "bps": round(commission_bps, 2),
        "description": f"佣金率 {commission_rate:.4%}",
    }

    # Liquidity gate
    resolved_liquidity = dict(_DEFAULT_LIQUIDITY_GATE)
    if liquidity_gate:
        resolved_liquidity.update(liquidity_gate)

    # Promotion gate
    resolved_promotion = dict(_DEFAULT_PROMOTION_GATE)
    if promotion_gate:
        resolved_promotion.update(promotion_gate)

    # Build warnings
    warnings: list[str] = []
    if mode == "backtest":
        if slippage_bps == 0:
            warnings.append("回测使用零滑点假设，实盘将产生滑点成本")
        if impact_bps == 0:
            warnings.append("回测未考虑市场冲击，大单交易成本可能显著更高")
        warnings.append("回测结果不等于实盘表现，请使用 execution 模式进行更真实的估算")
    if fill_model == "close_price":
        warnings.append("使用收盘价成交假设，实际可能无法以收盘价成交")

    return ExecutionRealityReport(
        fill_model=fill_model,
        slippage_assumption=slippage_assumption,
        market_impact_assumption=market_impact_assumption,
        commission_assumption=commission_assumption,
        liquidity_gate=resolved_liquidity,
        promotion_gate=resolved_promotion,
        cost_model_mode=mode,
        total_cost_bps=total_cost_bps,
        warnings=warnings,
    )
