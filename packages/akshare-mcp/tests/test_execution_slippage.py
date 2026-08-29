from __future__ import annotations

import math

from akshare_mcp.services.execution_slippage import (
    ExecutionSlippageBundle,
    MarketImpactModel,
    PartialFillSimulator,
    VolumeShareSlippageModel,
    estimate_execution_slippage,
)


def _assert_all_finite(value) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_all_finite(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_all_finite(nested)
        return
    if isinstance(value, float):
        assert math.isfinite(value)


def test_estimate_execution_slippage_sanitizes_non_finite_inputs() -> None:
    result = estimate_execution_slippage(
        order_shares=float("inf"),
        avg_minute_volume="nan",
        adv_shares="-inf",
        duration_minutes=float("nan"),
        reference_price="inf",
        slices=float("inf"),
        spread_bps=float("inf"),
        eta=float("nan"),
    )

    summary = result["slippage_simulation"]

    assert summary["execution_quality"] in {"good", "acceptable", "poor"}
    assert 0.0 <= summary["participation_rate"] <= 1.0
    assert result["volume_share_slippage"]["inputs"]["order_shares"] == 1
    assert result["volume_share_slippage"]["inputs"]["avg_minute_volume"] == 1.0
    assert result["market_impact"]["inputs"]["adv_shares"] == 100000.0
    assert result["partial_fill"]["inputs"]["slices"] == 1
    _assert_all_finite(result)


def test_execution_slippage_bundle_sanitizes_non_finite_model_parameters() -> None:
    bundle = ExecutionSlippageBundle(
        volume_share_model=VolumeShareSlippageModel(
            spread_bps=float("inf"),
            linear_coeff=float("nan"),
            sqrt_coeff="-inf",
            max_participation_rate=float("nan"),
        ),
        market_impact_model=MarketImpactModel(
            eta=float("inf"),
            min_adv_shares=float("nan"),
            max_impact_bps="-inf",
        ),
        partial_fill_simulator=PartialFillSimulator(
            fill_probability_threshold=float("nan"),
            queue_discount=float("inf"),
        ),
    )

    result = bundle.simulate(
        order_shares=500,
        avg_minute_volume=1000,
        adv_shares=200000,
        duration_minutes=5,
        reference_price=12.3,
        slices=2,
    )

    assert result["volume_share_slippage"]["spread_cost_bps"] == 2.5
    assert result["volume_share_slippage"]["linear_impact_bps"] >= 0.0
    assert result["market_impact"]["impact_bps"] > 0.0
    assert result["market_impact"]["impact_bps"] <= 200.0
    assert result["partial_fill"]["queue_discount_applied"] == 0.7
    _assert_all_finite(result)
