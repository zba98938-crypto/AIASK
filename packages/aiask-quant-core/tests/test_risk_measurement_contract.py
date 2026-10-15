"""``RiskModel.calculate_var`` must distinguish "not measured" from "measured 0".

Context
-------
``calculate_var`` returned ``{'var': 0, 'cvar': 0}`` for an empty series —
a dict with **no** ``var_percent`` key at all. Callers read it as
``var_report.get("var_percent", 0.0)`` and got a measured 0% risk, which then
passed ``RISK_REPORT_THRESHOLDS["var_percent_max"] = 4.5`` without ever being
computed. Eight stored quality reports carry ``var_percent = 0.0`` for exactly
this reason.

It also applied ``abs()`` to the percentile. When the 5% tail of the return
distribution is a *gain*, ``abs()`` reports that gain as a positive loss
percentage: a number that looks like measured downside risk and is its
opposite.

These tests call the real function and assert on returned values.
"""

from __future__ import annotations

import numpy as np

from aiask_quant_core.risk_model import RiskModel


def _loss_series(n: int = 120, seed: int = 7) -> list[float]:
    """A series with a genuine negative tail."""

    rng = np.random.default_rng(seed)
    return rng.normal(loc=0.0005, scale=0.02, size=n).tolist()


def _all_positive_series(n: int = 120) -> list[float]:
    """Every observation is a gain, so the 5% tail is positive too."""

    return [0.001 + (i % 7) * 0.0005 for i in range(n)]


def test_empty_series_is_unmeasurable_not_zero() -> None:
    result = RiskModel.calculate_var([])

    assert result["measurable"] is False
    assert result["var_percent"] is None, (
        "an empty series has no VaR; returning 0.0 lets it pass a risk ceiling"
    )
    assert result["cvar_percent"] is None
    assert result["var"] is None
    assert result["reason"] == "no_returns"
    # The key must exist, so a caller's `.get(..., 0.0)` cannot silently
    # fabricate a value from its absence.
    assert "var_percent" in result


def test_insufficient_samples_is_unmeasurable() -> None:
    result = RiskModel.calculate_var([0.01, -0.02, 0.005])

    assert result["measurable"] is False
    assert result["var_percent"] is None
    assert result["reason"] == "insufficient_samples"
    assert result["sample_size"] == 3


def test_measured_loss_reports_positive_magnitude() -> None:
    returns = _loss_series()
    result = RiskModel.calculate_var(returns, confidence=0.95, portfolio_value=1_000_000)

    assert result["measurable"] is True
    assert result["reason"] is None
    assert result["sample_size"] == len(returns)
    assert result["var_percent"] > 0.0
    assert result["cvar_percent"] >= result["var_percent"]
    assert result["var"] > 0.0


def test_positive_tail_is_not_reported_as_a_loss() -> None:
    """The ``abs()`` bug: a profitable tail became a positive loss percentage."""

    returns = _all_positive_series()
    result = RiskModel.calculate_var(returns, confidence=0.95)

    assert result["measurable"] is True
    assert np.percentile(returns, 5) > 0, "fixture must have a positive 5% tail"
    assert result["var_percent"] == 0.0, (
        "when even the tail is a gain there is no downside to report; "
        f"abs() turned it into {result['var_percent']}"
    )
    assert result["cvar_percent"] == 0.0


def test_multiple_calls_agree_on_the_same_input() -> None:
    """A gate input must not depend on run-to-run randomness.

    Note this is a weak guard: ``calculate_var`` is a pure percentile, so it is
    deterministic by construction and this test could not have failed. The real
    non-determinism in this repository is ``_handle_calculate_var``'s Monte
    Carlo branch, which draws 10,000 normals with no seed — same input,
    different VaR every call. It is guarded separately.
    """

    returns = _loss_series()
    first = RiskModel.calculate_var(returns, confidence=0.95)
    second = RiskModel.calculate_var(returns, confidence=0.95)

    assert first["var_percent"] == second["var_percent"]
    assert first["cvar_percent"] == second["cvar_percent"]


# --------------------------------------------------------------------------
# calculate_beta
#
# Three failure paths used to return ``beta = 1.0`` — a confident
# market-neutral beta derived from a length mismatch, a one-point sample, or a
# flat market. stress_test reads ``beta`` off each holding, so a fabricated 1.0
# there becomes a fabricated stress loss here.
# --------------------------------------------------------------------------


def test_beta_length_mismatch_is_unmeasurable() -> None:
    result = RiskModel.calculate_beta(np.array([0.01, 0.02]), np.array([0.01]))

    assert result["beta"] is None, "a length mismatch cannot yield a beta of 1.0"
    assert result["measurable"] is False
    assert result["reason"] == "length_mismatch"


def test_beta_single_observation_is_unmeasurable() -> None:
    result = RiskModel.calculate_beta(np.array([0.01]), np.array([0.01]))

    assert result["beta"] is None
    assert result["measurable"] is False
    assert result["reason"] == "insufficient_samples"


def test_beta_flat_market_is_unmeasurable() -> None:
    """Zero market variance makes beta a division by zero, not a 1.0."""

    result = RiskModel.calculate_beta(
        np.array([0.01, -0.02, 0.03, 0.01]),
        np.array([0.0, 0.0, 0.0, 0.0]),
    )

    assert result["beta"] is None
    assert result["measurable"] is False
    assert result["reason"] == "zero_market_variance"


def test_beta_is_computed_when_estimable() -> None:
    rng = np.random.default_rng(11)
    market = rng.normal(0.0, 0.01, size=200)
    stock = 1.5 * market + rng.normal(0.0, 0.001, size=200)

    result = RiskModel.calculate_beta(stock, market)

    assert result["measurable"] is True
    assert result["reason"] is None
    assert result["sample_size"] == 200
    assert 1.3 < result["beta"] < 1.7, result["beta"]
    assert result["r_squared"] is not None


def test_beta_reports_unmeasurable_r_squared_when_correlation_is_nan() -> None:
    """A constant stock series makes corrcoef nan even with a live market."""

    result = RiskModel.calculate_beta(
        np.array([0.01, 0.01, 0.01, 0.01]),
        np.array([0.01, -0.02, 0.03, -0.01]),
    )

    assert result["measurable"] is True
    # beta itself is 0 for a constant series, which is a real answer.
    assert result["beta"] == 0.0
    assert result["r_squared"] is None, "nan**2 is nan, not a measurement"
