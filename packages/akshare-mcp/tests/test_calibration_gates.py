"""Behaviour tests for the WP-B calibration start gates.

The point of these tests is the refusal: with the measured data (confidence is
a lookup table on horizon+direction, max 0.835, zero rows >= 0.85) the service
must report ``unmeasurable`` and persist no active model.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from akshare_mcp.services.incubation_factory.calibration import (
    MIN_HIGH_CONFIDENCE_ROWS,
    Observation,
    apply_platt,
    brier,
    evaluate_start_gates,
    fit_platt,
    spearman,
)


def _obs(day: date, confidence: float, hit: bool, horizon: int = 5, uplift: float | None = None) -> Observation:
    return Observation(as_of=day, horizon=horizon, confidence=confidence, hit=hit, uplift=uplift)


def test_spearman_detects_perfect_monotonicity() -> None:
    assert spearman([1.0, 2.0, 3.0, 4.0], [0.1, 0.2, 0.3, 0.4]) == pytest.approx(1.0)
    assert spearman([1.0, 2.0, 3.0, 4.0], [0.4, 0.3, 0.2, 0.1]) == pytest.approx(-1.0)
    assert spearman([1.0], [1.0]) is None


def test_lookup_table_confidence_fails_all_start_gates() -> None:
    """Shipped data shape: same confidence everywhere, zero high-confidence rows."""
    observations = [_obs(date(2026, 1, 1) + timedelta(days=i % 90), 0.675807, i % 2 == 0) for i in range(200)]
    result = evaluate_start_gates(observations)
    assert result["passed"] is False
    assert result["gates"]["b3_high_confidence_coverage"] == "fail"
    # A single confidence value carries no ordering at all: B1 cannot be
    # measured, which is a different verdict from "measured and non-monotone".
    assert result["gates"]["b1_monotonicity"] == "unmeasurable"
    assert "no_high_confidence_rows" in result["reason_codes"]
    assert result["high_confidence_rows"] == 0


def test_gate_needs_independent_days_not_just_rows() -> None:
    # 400 rows but only 5 distinct days: the bucket is not 30 independent days.
    observations = [_obs(date(2026, 1, 1) + timedelta(days=i % 5), 0.6 + 0.05 * (i % 4), i % 3 != 0) for i in range(400)]
    result = evaluate_start_gates(observations)
    for horizon in result["by_horizon"].values():
        assert horizon["eligible_buckets"] == 0
        assert horizon["b1_monotonic"] == "unmeasurable"


def test_all_gates_pass_needs_monotone_uplift_and_high_confidence() -> None:
    observations = []
    start = date(2025, 1, 1)
    for index in range(MIN_HIGH_CONFIDENCE_ROWS * 2):
        day = start + timedelta(days=index % 60)
        confidence = 0.86 if index % 2 == 0 else 0.60
        observations.append(_obs(day, confidence, hit=(confidence > 0.8), uplift=0.2 if confidence > 0.8 else 0.01))
    result = evaluate_start_gates(observations)
    assert result["gates"]["b3_high_confidence_coverage"] == "pass"
    assert result["gates"]["b2_positive_uplift"] == "pass"


def test_platt_fit_learns_direction_and_brier_beats_constant() -> None:
    pairs = [(0.3, False)] * 100 + [(0.9, True)] * 100
    model = fit_platt(pairs)
    assert model is not None
    assert apply_platt(model, 0.9) > apply_platt(model, 0.3)
    calibrated = brier([(apply_platt(model, score), label) for score, label in pairs])
    constant = brier([(0.5, label) for _, label in pairs])
    assert calibrated is not None and constant is not None
    assert calibrated < constant


class _DB:
    def __init__(self) -> None:
        self.saved: list[dict] = []

    async def save_calibration_model(self, payload):
        self.saved.append(payload)
        return payload


@pytest.mark.asyncio
async def test_service_writes_no_model_when_gates_fail() -> None:
    from akshare_mcp.services.incubation_factory.calibration import CalibrationService

    db = _DB()
    observations = [_obs(date(2026, 1, 1) + timedelta(days=i % 20), 0.675807, i % 2 == 0) for i in range(50)]
    result = await CalibrationService().try_fit(db, observations=observations)
    assert result["status"] == "unmeasurable"
    assert result["active_model"] is None
    assert db.saved == []


@pytest.mark.asyncio
async def test_service_refuses_model_that_does_not_beat_base_rate() -> None:
    from akshare_mcp.services.incubation_factory.calibration import CalibrationService

    db = _DB()
    observations = []
    start = date(2025, 1, 1)
    # Confidence is unrelated to the label: holdout Brier cannot beat the base rate.
    for index in range(300):
        day = start + timedelta(days=index % 80)
        confidence = 0.86 if index % 2 == 0 else 0.60
        observations.append(_obs(day, confidence, hit=index % 2 == 0, uplift=0.1 if index % 2 == 0 else 0.01))
    result = await CalibrationService().try_fit(db, observations=observations)
    if result["status"] == "ok":
        pytest.fail("a model was accepted although holdout did not beat the base rate")
    assert result["active_model"] is None
