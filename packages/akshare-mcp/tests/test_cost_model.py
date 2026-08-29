from __future__ import annotations

import math

from akshare_mcp.services.cost_model import (
    build_cost_model,
    effective_cost_rate,
    resolve_cost_assumptions,
)


def test_cost_assumptions_reject_non_finite_inputs() -> None:
    assumptions = resolve_cost_assumptions(
        {
            "commission_rate": "nan",
            "slippage_bps": float("inf"),
            "market_impact_bps": "-inf",
            "reference_price": "nan",
        },
        default_mode="execution",
    )

    assert assumptions["commission_rate"] == 0.0003
    assert assumptions["slippage_bps"] == 5.0
    assert assumptions["market_impact_bps"] == 0.0
    assert assumptions["reference_price"] == 0.0
    assert all(math.isfinite(value) for key, value in assumptions.items() if key != "rebalance_frequency")


def test_build_cost_model_never_returns_non_finite_estimates() -> None:
    model = build_cost_model(
        {"commission_rate": float("inf"), "slippage": "nan"},
        notional=float("inf"),
        default_mode="execution",
        reference_price_fallback=100.0,
    )

    assert model["estimated"]["notional"] == 0.0
    assert model["assumptions"]["reference_price"] == 100.0
    assert all(math.isfinite(value) for value in model["estimated"].values())


def test_effective_cost_rate_uses_fallback_for_non_finite_assumptions() -> None:
    rate = effective_cost_rate(
        {"assumptions": {"commission_rate": "nan", "slippage_bps": "inf", "market_impact_bps": "-inf"}},
        fallback_commission=0.001,
        fallback_slippage=0.002,
    )

    assert rate == 0.003
