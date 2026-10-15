"""A factor that could not be measured must not be stored as a zero.

`_compute_scalar_factor_bundle` wrote 0.0 for every missing input, which made
three different facts identical: "the factor is genuinely zero", "the input it
needs is absent", and "there is too little history". Momentum was the worst case
— between 20 and 59 bars it averaged a real m20 with a fabricated m60, halving
the signal, and below 20 bars it returned exactly 0.0, ranking a stock with no
history as if it had been perfectly flat. A run over the full universe wrote 34
such rows in one day.

These are behaviour assertions on the returned bundle, not on the source.
"""

from __future__ import annotations

import pytest

from akshare_mcp.tools.managers.quant_mgr_helpers import _compute_scalar_factor_bundle

ALL = ["momentum", "value", "quality", "growth", "volatility", "reversal"]

FULL_FINANCIALS = {
    "roe": 15.0,
    "roa": 7.0,
    "gross_margin": 30.0,
    "debt_ratio": 0.4,
    "revenue_growth": 20.0,
    "profit_growth": 10.0,
}
FULL_VALUATION = {"pe_ratio": 20.0, "pb_ratio": 3.0}


def _rising(n: int) -> list[float]:
    return [100.0 + i * 0.5 for i in range(n)]


def test_too_little_history_omits_momentum_instead_of_halving_it() -> None:
    """20-59 bars used to average a real m20 with a fabricated m60."""

    reasons: dict[str, str] = {}
    bundle = _compute_scalar_factor_bundle(
        _rising(59), financial_snapshot=FULL_FINANCIALS, valuation_snapshot=FULL_VALUATION,
        factors=["momentum"], unmeasurable=reasons,
    )

    assert "momentum" not in bundle
    assert reasons["momentum"] == "insufficient_history"


def test_very_short_history_does_not_read_as_a_flat_stock() -> None:
    """Below 20 bars the old code returned exactly 0.0 — the same as no move."""

    reasons: dict[str, str] = {}
    bundle = _compute_scalar_factor_bundle(
        _rising(19), factors=["momentum", "reversal"], unmeasurable=reasons
    )

    assert "momentum" not in bundle
    assert reasons["momentum"] == "insufficient_history"


def test_a_measurable_momentum_is_still_computed() -> None:
    bundle = _compute_scalar_factor_bundle(_rising(70), factors=["momentum"])

    assert "momentum" in bundle
    assert isinstance(bundle["momentum"], float)


def test_fundamental_factors_are_omitted_without_a_snapshot() -> None:
    """`_safe_float(default=0.0)` made an absent ROE look like a measured zero one.

    The debt component was worse: it defaulted to 1.0, so a company with no
    balance sheet scored the same as one carrying no debt at all.
    """

    reasons: dict[str, str] = {}
    bundle = _compute_scalar_factor_bundle(
        _rising(70), factors=["quality", "growth", "value"], unmeasurable=reasons
    )

    assert "quality" not in bundle
    assert "growth" not in bundle
    assert "value" not in bundle
    assert reasons == {
        "value": "no_positive_valuation_multiple",
        "quality": "no_financial_snapshot",
        "growth": "no_growth_fields",
    }


def test_a_loss_maker_has_no_value_factor_rather_than_a_zero() -> None:
    """Negative PE and PB used to be dropped, leaving 0.0 — indistinguishable
    from a company with no valuation row at all, and from a very expensive one."""

    reasons: dict[str, str] = {}
    bundle = _compute_scalar_factor_bundle(
        _rising(70),
        financial_snapshot={"roe": -5.0, "roa": -2.0, "gross_margin": 10.0, "debt_ratio": 0.9},
        valuation_snapshot={"pe_ratio": -8.0, "pb_ratio": -1.5},
        factors=["value", "quality"],
        unmeasurable=reasons,
    )

    assert "value" not in bundle
    assert reasons["value"] == "no_positive_valuation_multiple"
    # Quality is still measurable here: three of its four inputs are present, and
    # the negative ones contribute their floor rather than being dropped.
    assert bundle["quality"] == pytest.approx(0.2 * (10.0 / 50.0) + 0.1 * (1.0 - 0.9))


def test_growth_uses_the_field_it_has_instead_of_halving_against_a_zero() -> None:
    reasons: dict[str, str] = {}
    bundle = _compute_scalar_factor_bundle(
        _rising(70),
        financial_snapshot={"revenue_growth": -10.0},
        factors=["growth"],
        unmeasurable=reasons,
    )

    assert bundle["growth"] == pytest.approx(-0.1)
    assert "growth" not in reasons


def test_growth_with_both_fields_is_unchanged() -> None:
    """The formula must not shift for the ordinary case."""

    bundle = _compute_scalar_factor_bundle(
        _rising(70),
        financial_snapshot={"revenue_growth": 20.0, "profit_growth": 10.0},
        factors=["growth"],
    )

    assert bundle["growth"] == pytest.approx((20.0 + 10.0) / 200.0)


def test_a_flat_series_has_no_volatility_factor() -> None:
    """Writing 0.0 for zero variance ranked a flat stock as the most volatile."""

    reasons: dict[str, str] = {}
    bundle = _compute_scalar_factor_bundle([50.0] * 70, factors=["volatility"], unmeasurable=reasons)

    assert "volatility" not in bundle
    assert reasons["volatility"] == "no_return_variation"


def test_a_complete_input_measures_all_six() -> None:
    reasons: dict[str, str] = {}
    bundle = _compute_scalar_factor_bundle(
        _rising(70),
        financial_snapshot=FULL_FINANCIALS,
        valuation_snapshot=FULL_VALUATION,
        factors=ALL,
        unmeasurable=reasons,
    )

    assert sorted(bundle) == sorted(ALL)
    assert reasons == {}
