"""Behaviour tests for the WP-D directional scorecard.

The scorecard is where a fake 85% would be certified, so these tests are mostly
about refusal: unmeasurable stays ``unmeasurable``, profit factor blocks a
high-hit/low-PF sample, and the same-day base rate uses the whole market rather
than the predicted names.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from akshare_mcp.services.incubation_factory.direction_stats import (
    binomial_pass_probability,
    block_bootstrap_lcb,
    profit_factor,
    required_hits_for_lcb,
    wilson_lcb,
)
from akshare_mcp.services.incubation_factory.directional_scorecard import (
    GATE_FAIL,
    GATE_PASS,
    GATE_UNMEASURABLE,
    METRIC_DEFINITION_VERSION,
    ScoredPrediction,
    _group_by_day,
    build_directional_scorecard,
    market_base_rate,
)


def test_wilson_lcb_matches_hand_calculation() -> None:
    assert wilson_lcb(172, 200) == pytest.approx(0.8051, abs=1e-3)
    assert wilson_lcb(0, 0) is None


def test_required_hits_and_power_are_the_documented_numbers() -> None:
    assert required_hits_for_lcb(200, 0.80) == 172
    assert binomial_pass_probability(0.85, 200, 0.80) == pytest.approx(0.391, abs=0.005)
    assert binomial_pass_probability(0.90, 200, 0.80) == pytest.approx(0.973, abs=0.005)


def test_profit_factor_is_unmeasurable_without_a_loss_side() -> None:
    assert profit_factor([0.1, 0.2]) is None
    assert profit_factor([0.1, -0.05]) == pytest.approx(2.0)


def test_block_bootstrap_reports_point_and_lower_bound() -> None:
    lcb, point = block_bootstrap_lcb([0.02] * 100, block_size=20)
    assert lcb == pytest.approx(0.02)
    assert point == pytest.approx(0.02)


def test_market_base_rate_uses_same_entry_day_and_horizon() -> None:
    day0, day1, day2 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
    panel = {
        "a": {day0: 10.0, day1: 11.0, day2: 12.0},
        "b": {day0: 10.0, day1: 10.0, day2: 9.0},
        "__open__": {"a": {day1: 10.0}, "b": {day1: 10.0}},
    }
    # entering at day1 open: a gains, b loses -> 50% of the market clears the band
    assert market_base_rate(panel, entry_day=day1, horizon=1, cost=0.0, flat_band=0.0) == pytest.approx(0.5)
    # entering at day0 with horizon 1 uses day0 open... absent -> unmeasurable
    assert market_base_rate(panel, entry_day=day0, horizon=1, cost=0.0, flat_band=0.0) is None


def _prediction(day: date, hit: bool, *, net: float = 0.01, horizon: int = 5, confidence: float = 0.7) -> ScoredPrediction:
    return ScoredPrediction(
        prediction_id=f"tp-{day}-{hit}-{net}",
        strategy_id="s",
        stock_code="600519",
        as_of=day,
        entry=day + timedelta(days=1),
        exit=day + timedelta(days=6),
        direction="up",
        horizon=horizon,
        net_return=net,
        direction_hit=hit,
        confidence=confidence,
    )


def test_group_by_day_uses_one_unit_per_trading_day() -> None:
    day = date(2026, 10, 1)
    grouped = _group_by_day([_prediction(day, True), _prediction(day, False), _prediction(day + timedelta(days=1), True)])
    assert len(grouped) == 2
    assert grouped[day].hit_rate == pytest.approx(0.5)


class _EmptyDB:
    async def fetch(self, sql, *args):
        return []


@pytest.mark.asyncio
async def test_unmeasurable_is_not_fail_and_not_pass() -> None:
    card = await build_directional_scorecard(_EmptyDB())
    assert card["metric_definition_version"] == METRIC_DEFINITION_VERSION
    assert card["hit_rate"] is None
    assert card["gates"]["hit_rate_lcb_gt_0_80"] == GATE_UNMEASURABLE
    assert card["gates"]["profit_factor_ge_1_5"] == GATE_UNMEASURABLE
    assert card["overall"] == GATE_UNMEASURABLE
    assert "no_scorable_predictions" in card["reason_codes"]


@pytest.mark.asyncio
async def test_high_hit_low_profit_factor_is_blocked() -> None:
    """The measured Baogaier-style骗局: 87% hits, PF 0.01 must fail the gate."""

    class _DB:
        async def fetch(self, sql, *args):
            # load_scored_predictions query
            if "strategy_trade_prediction_outcomes" in sql and "LIMIT" in sql and "prediction_executions" not in sql:
                rows = []
                for index in range(20):
                    day = date(2026, 9, 1) + timedelta(days=index)
                    rows.append(
                        {
                            "prediction_id": f"p{index}",
                            "strategy_id": "s",
                            "stock_code": "600519",
                            "prediction_as_of": day.isoformat(),
                            "direction": "up",
                            "horizon": "5d",
                            "confidence": 0.7,
                            "net_return": 0.001 if index < 17 else -10.0,
                            "entry_date": (day + timedelta(days=1)).isoformat(),
                            "exit_date": (day + timedelta(days=6)).isoformat(),
                            "direction_hit": 1 if index < 17 else 0,
                        }
                    )
                return rows
            return []

    card = await build_directional_scorecard(_DB())
    assert card["profit_factor"] is not None
    assert card["profit_factor"] < 1.5
    assert card["gates"]["profit_factor_ge_1_5"] == GATE_FAIL
    assert card["overall"] == GATE_FAIL


@pytest.mark.asyncio
async def test_gates_never_boolean() -> None:
    card = await build_directional_scorecard(_EmptyDB())
    for name, value in card["gates"].items():
        assert value in {GATE_PASS, GATE_FAIL, GATE_UNMEASURABLE}, name
        assert not isinstance(value, bool)
