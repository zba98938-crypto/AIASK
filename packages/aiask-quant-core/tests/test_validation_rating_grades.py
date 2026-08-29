from __future__ import annotations

from aiask_quant_core.validation import FactorValidationPipeline, ValidationSummary


def _summary(*, rank_ic: float, ir: float, stability: float, positive: float) -> ValidationSummary:
    return ValidationSummary(
        oos_rank_ic_mean=rank_ic,
        oos_rank_ic_ir=ir,
        stability_ratio=stability,
        oos_positive_ratio=positive,
    )


def test_factor_validation_rating_supports_s_grade_ladder() -> None:
    rating = FactorValidationPipeline._compute_rating(
        _summary(rank_ic=0.05, ir=0.5, stability=1.0, positive=1.0),
        _summary(rank_ic=0.05, ir=0.5, stability=1.0, positive=1.0),
        {"ci_lower": 0.01, "ci_upper": 0.05},
    )

    assert rating["total_score"] == 100.0
    assert rating["grade"] == "SSS"
    assert "Strong" in rating["recommendation"]


def test_factor_validation_rating_keeps_legacy_b_threshold() -> None:
    rating = FactorValidationPipeline._compute_rating(
        _summary(rank_ic=0.035, ir=0.25, stability=0.8, positive=0.6),
        _summary(rank_ic=0.035, ir=0.25, stability=0.8, positive=0.6),
        {"ci_lower": -0.01, "ci_upper": 0.05},
    )

    assert 55.0 <= rating["total_score"] < 70.0
    assert rating["grade"] == "B"
