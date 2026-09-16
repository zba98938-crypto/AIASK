"""Behaviour tests for RiskModel.stress_test.

The scenario used to return a constant ``loss_percent`` (-20.0 for market_crash,
-30.0 for black_swan) computed as ``portfolio_value * 0.8``, i.e. independent of
what was held. 179 of 179 stored quality reports carried exactly -20.0, and the
submission gate only fails at <= -25.0, so the check could never fire.

These tests assert the two properties that were missing: the result must depend
on the positions, and an unmeasurable scenario must be reported as missing
rather than as a measured number.
"""

from __future__ import annotations

import numpy as np
import pytest

from aiask_quant_core.risk_model import RiskModel


def _holding(code: str, value: float, beta: float | None = None) -> dict:
    row: dict = {"code": code, "value": value}
    if beta is not None:
        row["beta"] = beta
    return row


class TestStressDependsOnPositions:
    def test_higher_beta_portfolio_loses_more(self) -> None:
        defensive = RiskModel.stress_test([_holding("600519", 100_000.0, beta=0.5)])
        aggressive = RiskModel.stress_test([_holding("300750", 100_000.0, beta=2.0)])

        assert defensive["measurable"] is True
        assert aggressive["measurable"] is True
        # The whole point: two different books must not report the same loss.
        assert aggressive["loss_percent"] < defensive["loss_percent"]
        assert defensive["loss_percent"] == pytest.approx(-10.0)
        assert aggressive["loss_percent"] == pytest.approx(-40.0)

    def test_beta_one_reproduces_the_market_shock(self) -> None:
        result = RiskModel.stress_test([_holding("000001", 50_000.0, beta=1.0)])
        assert result["loss_percent"] == pytest.approx(-20.0)
        assert result["market_shock"] == pytest.approx(-0.20)

    def test_loss_is_value_weighted_across_holdings(self) -> None:
        result = RiskModel.stress_test(
            [
                _holding("a", 90_000.0, beta=0.0),
                _holding("b", 10_000.0, beta=1.0),
            ]
        )
        # Only 10% of the book moves, and it moves -20%.
        assert result["loss_percent"] == pytest.approx(-2.0)
        assert result["coverage"] == pytest.approx(1.0)

    def test_negative_beta_holding_gains(self) -> None:
        result = RiskModel.stress_test([_holding("hedge", 100_000.0, beta=-1.0)])
        assert result["loss_percent"] == pytest.approx(20.0)
        assert result["shocked_value"] == pytest.approx(120_000.0)

    def test_black_swan_is_a_deeper_shock_than_market_crash(self) -> None:
        book = [_holding("600519", 100_000.0, beta=1.0)]
        crash = RiskModel.stress_test(book, scenario="market_crash")
        swan = RiskModel.stress_test(book, scenario="black_swan")
        assert swan["loss_percent"] < crash["loss_percent"]
        assert swan["loss_percent"] == pytest.approx(-30.0)

    def test_sensitivity_is_accepted_as_a_beta_alias(self) -> None:
        result = RiskModel.stress_test([{"code": "x", "value": 1000.0, "sensitivity": 1.5}])
        assert result["measurable"] is True
        assert result["loss_percent"] == pytest.approx(-30.0)


class TestUnmeasurableIsReportedAsMissing:
    def test_no_positions_yields_none_not_zero(self) -> None:
        result = RiskModel.stress_test([])
        assert result["loss_percent"] is None
        assert result["measurable"] is False
        assert result["reason"] == "no_positions"

    def test_holdings_without_beta_are_not_assumed_to_track_the_market(self) -> None:
        result = RiskModel.stress_test([_holding("600519", 100_000.0)])
        assert result["loss_percent"] is None
        assert result["measurable"] is False
        assert result["reason"] == "no_position_sensitivity"
        assert result["missing_sensitivity"] == ["600519"]

    def test_a_pure_cash_book_is_not_reported_as_crashing(self) -> None:
        # The panel builder used to substitute [{"code": "cash", ...}] when no
        # sampled code had a live signal, and that book reported -20.0.
        result = RiskModel.stress_test([{"code": "cash", "weight": 1.0, "value": 100_000.0}])
        assert result["loss_percent"] is None
        assert result["measurable"] is False

    def test_zero_value_book_is_unmeasurable(self) -> None:
        result = RiskModel.stress_test([_holding("a", 0.0, beta=1.0)])
        assert result["loss_percent"] is None
        assert result["reason"] == "no_positions"

    def test_unknown_scenario_reports_none_rather_than_a_default_shock(self) -> None:
        result = RiskModel.stress_test(
            [_holding("a", 1000.0, beta=1.0)], scenario="not_a_scenario"
        )
        assert result["loss_percent"] is None
        assert result["measurable"] is False
        assert result["reason"] == "unknown_scenario"
        assert "error" in result

    def test_partial_coverage_is_measured_but_flagged(self) -> None:
        result = RiskModel.stress_test(
            [
                _holding("known", 50_000.0, beta=1.0),
                _holding("unknown", 50_000.0),
            ]
        )
        assert result["measurable"] is True
        assert result["reason"] == "partial_position_sensitivity"
        assert result["coverage"] == pytest.approx(0.5)
        assert result["missing_sensitivity"] == ["unknown"]
        # Only the covered half is shocked; the uncovered half is carried through.
        assert result["loss_percent"] == pytest.approx(-10.0)


class TestCustomShocks:
    def test_custom_shock_needs_no_beta(self) -> None:
        result = RiskModel.stress_test(
            [_holding("600519", 100_000.0)],
            scenario="custom_event",
            custom_shocks={"600519": -0.5},
        )
        assert result["measurable"] is True
        assert result["loss_percent"] == pytest.approx(-50.0)

    def test_custom_shock_overrides_the_scenario_for_that_code(self) -> None:
        result = RiskModel.stress_test(
            [
                _holding("a", 50_000.0, beta=1.0),
                _holding("b", 50_000.0, beta=1.0),
            ],
            scenario="market_crash",
            custom_shocks={"a": 0.0},
        )
        # a is pinned flat, b takes the -20% scenario shock.
        assert result["loss_percent"] == pytest.approx(-10.0)


class TestAliases:
    @pytest.mark.parametrize(
        "alias,canonical",
        [
            ("crash", "market_crash"),
            ("swan", "black_swan"),
            ("rate_hike", "interest_rate_hike"),
            ("rotation", "sector_rotation"),
        ],
    )
    def test_alias_resolves_to_canonical_scenario(self, alias: str, canonical: str) -> None:
        book = [_holding("a", 1000.0, beta=1.0)]
        assert (
            RiskModel.stress_test(book, scenario=alias)["loss_percent"]
            == RiskModel.stress_test(book, scenario=canonical)["loss_percent"]
        )

    def test_liquidity_crisis_is_its_own_scenario_not_an_alias_of_crash(self) -> None:
        book = [_holding("a", 1000.0, beta=1.0)]
        liquidity = RiskModel.stress_test(book, scenario="liquidity_crisis")
        crash = RiskModel.stress_test(book, scenario="market_crash")
        assert liquidity["scenario"] == "liquidity_crisis"
        assert liquidity["loss_percent"] != crash["loss_percent"]


class TestGateInteraction:
    def test_a_severe_book_crosses_the_gate_threshold(self) -> None:
        # The gate fails at <= -25.0. Prove that is now reachable, which it was
        # not while every book reported exactly -20.0.
        result = RiskModel.stress_test([_holding("leveraged", 100_000.0, beta=1.5)])
        assert result["loss_percent"] <= -25.0

    def test_a_calm_book_stays_inside_the_threshold(self) -> None:
        result = RiskModel.stress_test([_holding("defensive", 100_000.0, beta=0.3)])
        assert result["loss_percent"] > -25.0
