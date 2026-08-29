"""统一成本模型服务：回测/执行/绩效共享同一口径。"""

from __future__ import annotations

import math
from typing import Any


def _to_float(v: Any, default: float | None = 0.0) -> float | None:
    try:
        if v is None:
            return float(default) if default is not None else None
        parsed = float(v)
        if not math.isfinite(parsed):
            return float(default) if default is not None else None
        return parsed
    except Exception:
        return float(default) if default is not None else None


# 模式默认值：execution 模式考虑滑点与市场冲击，backtest 模式默认为零
_MODE_DEFAULTS: dict[str, dict[str, float]] = {
    "execution": {"slippage_bps": 5.0, "market_impact_bps": 3.0},
    "backtest": {"slippage_bps": 0.0, "market_impact_bps": 0.0},
}


def resolve_cost_assumptions(kwargs: dict, *, default_mode: str) -> dict:
    """解析统一成本参数，兼容历史字段名。

    default_mode 决定未显式传入时的滑点/冲击默认值：
    - "execution": slippage_bps=5.0, market_impact_bps=3.0
    - "backtest":  slippage_bps=0.0, market_impact_bps=0.0
    """
    mode_defaults = _MODE_DEFAULTS.get(default_mode, {})
    commission_rate = _to_float(kwargs.get("commission_rate", kwargs.get("commission", 0.0003)), 0.0003)

    # slippage: 优先 slippage_bps → 兼容 slippage(rate) → 模式默认值
    slippage_bps = _to_float(kwargs.get("slippage_bps"), None)
    if kwargs.get("slippage_bps") is not None and slippage_bps is None:
        slippage_bps = mode_defaults.get("slippage_bps", 0.0)
    if slippage_bps is None:
        slippage_raw = kwargs.get("slippage")
        if slippage_raw is not None:
            slippage_bps = _to_float(slippage_raw, 0.0) * 10000.0
        else:
            slippage_bps = mode_defaults.get("slippage_bps", 0.0)

    # market_impact: 显式传入 → 模式默认值
    market_impact_raw = kwargs.get("market_impact_bps")
    if market_impact_raw is not None:
        market_impact_bps = _to_float(market_impact_raw, 0.0)
    else:
        market_impact_bps = mode_defaults.get("market_impact_bps", 0.0)

    return {
        "reference_price": _to_float(kwargs.get("reference_price", 0.0), 0.0),
        "commission_rate": max(0.0, commission_rate),
        "slippage_bps": max(0.0, slippage_bps),
        "market_impact_bps": max(0.0, market_impact_bps),
        "rebalance_frequency": str(kwargs.get("rebalance_frequency", default_mode) or default_mode),
    }


def build_cost_model(kwargs: dict, *, notional: float, default_mode: str, reference_price_fallback: float = 0.0) -> dict:
    """构建统一成本模型。"""
    assumptions = resolve_cost_assumptions(kwargs, default_mode=default_mode)
    if assumptions["reference_price"] <= 0 and reference_price_fallback > 0:
        assumptions["reference_price"] = float(reference_price_fallback)

    notional = max(0.0, _to_float(notional, 0.0))
    commission = notional * assumptions["commission_rate"]
    slippage = notional * (assumptions["slippage_bps"] / 10000.0)
    market_impact = notional * (assumptions["market_impact_bps"] / 10000.0)

    return {
        "assumptions": assumptions,
        "estimated": {
            "notional": float(notional),
            "commission": float(commission),
            "slippage": float(slippage),
            "market_impact": float(market_impact),
            "total": float(commission + slippage + market_impact),
        },
    }


def effective_cost_rate(cost_model: dict, *, fallback_commission: float = 0.0, fallback_slippage: float = 0.0) -> float:
    """给回测引擎提供统一费率输入。"""
    try:
        assumptions = cost_model.get("assumptions", {}) if isinstance(cost_model, dict) else {}
        c = _to_float(assumptions.get("commission_rate", fallback_commission), fallback_commission)
        s = _to_float(assumptions.get("slippage_bps"), None)
        if s is None:
            s_rate = _to_float(fallback_slippage, 0.0)
        else:
            s_rate = s / 10000.0
        impact_rate = _to_float(assumptions.get("market_impact_bps", 0.0), 0.0) / 10000.0
        return float(max(0.0, c + s_rate + impact_rate))
    except Exception:
        return float(max(0.0, _to_float(fallback_commission, 0.0) + _to_float(fallback_slippage, 0.0)))
