"""Behaviour tests for direction-aware factor rating.

``_compute_rating`` used to wrap every predictive-power component in ``abs()``,
so a factor with OOS rank-IC of -0.05 scored identically to one with +0.05, and
``ci_lower > 0 or ci_upper < 0`` awarded the full significance points to a
confidence interval lying entirely *below* zero. A consistently wrong factor
collected 30 + 25 + 15 = 70 points and graded "A / 因子样本外表现稳健".

The expected direction is taken from in-sample IC, so a legitimate inverse
factor still scores, while a factor whose sign flips out of sample does not.
"""

from __future__ import annotations

import pytest

from aiask_quant_core.validation import FactorValidationPipeline, ValidationSummary


def _summary(
    *,
    rank_ic: float,
    ir: float,
    stability: float,
    positive: float,
    is_ic: float = 0.0,
) -> ValidationSummary:
    return ValidationSummary(
        oos_rank_ic_mean=rank_ic,
        oos_rank_ic_ir=ir,
        stability_ratio=stability,
        oos_positive_ratio=positive,
        is_ic_mean=is_ic,
    )


def _rate(wf: ValidationSummary, kf: ValidationSummary, ci: dict) -> dict:
    return FactorValidationPipeline._compute_rating(wf, kf, ci)


class TestSignBlindnessIsGone:
    def test_a_consistently_wrong_factor_no_longer_grades_a(self) -> None:
        # The exact shape the audit flagged: strong but backwards OOS IC, with a
        # CI entirely below zero, on a factor that was positive in sample.
        wrong = _summary(rank_ic=-0.05, ir=-0.5, stability=-1.0, positive=0.0, is_ic=0.04)
        rating = _rate(wrong, wrong, {"ci_lower": -0.05, "ci_upper": -0.01})

        assert rating["scores"]["oos_ic"] == 0.0
        assert rating["scores"]["oos_ir"] == 0.0
        assert rating["scores"]["ci_significance"] == 0.0
        assert rating["total_score"] == 0.0
        assert rating["grade"] == "D"
        assert "Weak" in rating["recommendation"]

    def test_right_and_wrong_direction_no_longer_score_the_same(self) -> None:
        right = _summary(rank_ic=0.05, ir=0.5, stability=1.0, positive=1.0, is_ic=0.04)
        wrong = _summary(rank_ic=-0.05, ir=-0.5, stability=-1.0, positive=0.0, is_ic=0.04)

        assert _rate(right, right, {"ci_lower": 0.01, "ci_upper": 0.05})["total_score"] > _rate(
            wrong, wrong, {"ci_lower": -0.05, "ci_upper": -0.01}
        )["total_score"]

    def test_ci_below_zero_is_not_significance_for_a_positive_factor(self) -> None:
        # Significantly negative is not significantly good.
        wf = _summary(rank_ic=0.0, ir=0.0, stability=0.0, positive=0.0, is_ic=0.04)
        rating = _rate(wf, wf, {"ci_lower": -0.08, "ci_upper": -0.02})
        assert rating["scores"]["ci_significance"] == 0.0

    def test_sign_flip_out_of_sample_scores_zero_on_predictive_power(self) -> None:
        # Positive in sample, negative out of sample: the overfitting signature.
        flipped = _summary(rank_ic=-0.06, ir=-0.8, stability=-1.2, positive=0.1, is_ic=0.05)
        rating = _rate(flipped, flipped, {"ci_lower": -0.09, "ci_upper": -0.02})
        assert rating["expected_direction"] == 1.0
        assert rating["scores"]["oos_ic"] == 0.0
        assert rating["scores"]["stability"] == 0.0


class TestLegitimateInverseFactor:
    def test_an_inverse_factor_consistent_with_its_in_sample_sign_still_scores(self) -> None:
        # Negative in sample AND negative out of sample: it predicts, inversely.
        inverse = _summary(rank_ic=-0.05, ir=-0.5, stability=1.0, positive=0.0, is_ic=-0.04)
        rating = _rate(inverse, inverse, {"ci_lower": -0.05, "ci_upper": -0.01})

        assert rating["expected_direction"] == -1.0
        assert rating["scores"]["oos_ic"] == pytest.approx(30.0)
        assert rating["scores"]["oos_ir"] == pytest.approx(25.0)
        assert rating["scores"]["ci_significance"] == 15.0
        # positive_ratio is read as "folds in the expected direction".
        assert rating["scores"]["positive_ratio"] == pytest.approx(10.0)
        assert rating["grade"] == "SSS"

    def test_inverse_factor_that_flips_positive_out_of_sample_scores_zero(self) -> None:
        flipped = _summary(rank_ic=0.05, ir=0.5, stability=-1.0, positive=1.0, is_ic=-0.04)
        rating = _rate(flipped, flipped, {"ci_lower": 0.01, "ci_upper": 0.05})

        assert rating["expected_direction"] == -1.0
        assert rating["scores"]["oos_ic"] == 0.0
        assert rating["scores"]["ci_significance"] == 0.0
        assert rating["grade"] == "D"

    def test_ci_above_zero_is_significance_for_an_inverse_factor(self) -> None:
        wf = _summary(rank_ic=0.0, ir=0.0, stability=0.0, positive=0.5, is_ic=-0.04)
        rating = _rate(wf, wf, {"ci_lower": -0.08, "ci_upper": -0.02})
        assert rating["scores"]["ci_significance"] == 15.0


class TestDirectionResolution:
    def test_absent_in_sample_ic_defaults_to_the_conventional_direction(self) -> None:
        wf = _summary(rank_ic=0.05, ir=0.5, stability=1.0, positive=1.0)
        rating = _rate(wf, wf, {"ci_lower": 0.01, "ci_upper": 0.05})
        assert rating["expected_direction"] == 1.0
        assert rating["grade"] == "SSS"

    def test_direction_uses_the_combined_in_sample_signal(self) -> None:
        # One validator positive, the other more strongly negative -> inverse.
        wf = _summary(rank_ic=-0.05, ir=-0.5, stability=1.0, positive=0.0, is_ic=0.01)
        kf = _summary(rank_ic=-0.05, ir=-0.5, stability=1.0, positive=0.0, is_ic=-0.05)
        rating = _rate(wf, kf, {"ci_lower": -0.05, "ci_upper": -0.01})
        assert rating["expected_direction"] == -1.0
        assert rating["scores"]["oos_ic"] == pytest.approx(30.0)


class TestScoreBoundsHold:
    def test_scores_never_go_negative(self) -> None:
        awful = _summary(rank_ic=-0.9, ir=-9.0, stability=-9.0, positive=0.0, is_ic=0.5)
        rating = _rate(awful, awful, {"ci_lower": -1.0, "ci_upper": -0.5})
        for name, value in rating["scores"].items():
            assert value >= 0.0, name
        assert rating["total_score"] == 0.0

    def test_scores_stay_capped_at_their_maxima(self) -> None:
        huge = _summary(rank_ic=0.9, ir=9.0, stability=9.0, positive=1.0, is_ic=0.5)
        rating = _rate(huge, huge, {"ci_lower": 0.5, "ci_upper": 1.0})
        assert rating["scores"]["oos_ic"] == 30.0
        assert rating["scores"]["oos_ir"] == 25.0
        assert rating["scores"]["stability"] == 20.0
        assert rating["scores"]["ci_significance"] == 15.0
        assert rating["scores"]["positive_ratio"] == 10.0
        assert rating["total_score"] == 100.0
        assert rating["grade"] == "SSS"

    def test_grade_c_floor_still_requires_real_directional_evidence(self) -> None:
        # 40 points is the C floor and the factory's default minimum grade.
        # abs()-scored noise used to clear it; a wrong-direction factor must not.
        wrong = _summary(rank_ic=-0.04, ir=-0.4, stability=-0.5, positive=0.2, is_ic=0.03)
        assert _rate(wrong, wrong, {"ci_lower": -0.06, "ci_upper": -0.01})["total_score"] < 40.0
