"""Unified uncertainty output contract for prediction tools.

Standardizes how prediction probabilities, calibration quality, and
prediction intervals are reported to AI consumers.

Usage::

    from akshare_mcp.services.uncertainty_contract import build_uncertainty_report

    report = build_uncertainty_report(
        raw_probability=0.72,
        calibrated_probability=0.65,
        calibration_method="platt",
        sample_size=200,
    )
    result["uncertainty"] = report.to_dict()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .probability_calibration import (
    CalibrationQualityReport,
    estimate_prediction_interval,
)


@dataclass
class UncertaintyReport:
    """Standardized uncertainty output for AI consumption.

    All prediction tools should output this structure so AI can
    consistently assess confidence and reliability.
    """

    raw_probability: float | None
    calibrated_probability: float | None
    calibration_method: str  # "platt" / "isotonic" / "raw" / "none"
    calibration_sample_size: int
    ece: float | None  # Expected Calibration Error
    brier_score: float | None
    reliability_summary: str  # "good" / "fair" / "poor" / "unknown"
    prediction_interval: dict[str, Any] | None  # {lower, upper, coverage_target, method}
    coverage_target: float | None
    realized_coverage: float | None
    quality_band: str  # "good" / "fair" / "poor" / "unknown"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "raw_probability": self.raw_probability,
            "calibrated_probability": self.calibrated_probability,
            "calibration_method": self.calibration_method,
            "calibration_sample_size": self.calibration_sample_size,
            "ece": self.ece,
            "brier_score": self.brier_score,
            "reliability_summary": self.reliability_summary,
            "quality_band": self.quality_band,
        }
        if self.prediction_interval is not None:
            result["prediction_interval"] = self.prediction_interval
        if self.coverage_target is not None:
            result["coverage_target"] = self.coverage_target
        if self.realized_coverage is not None:
            result["realized_coverage"] = self.realized_coverage
        if self.warnings:
            result["warnings"] = self.warnings
        return result


def _infer_quality_band(
    brier: float | None,
    ece: float | None,
    sample_size: int,
) -> str:
    """Infer quality band from calibration metrics."""
    if brier is None and ece is None:
        return "unknown"
    score = 0
    if brier is not None:
        if brier < 0.05:
            score += 2
        elif brier < 0.15:
            score += 1
    if ece is not None:
        if ece < 0.03:
            score += 2
        elif ece < 0.08:
            score += 1
    if sample_size < 30:
        score = max(0, score - 1)
    if score >= 3:
        return "good"
    if score >= 1:
        return "fair"
    return "poor"


def build_uncertainty_report(
    *,
    raw_probability: float | None = None,
    calibrated_probability: float | None = None,
    calibration_method: str = "none",
    sample_size: int = 0,
    ece: float | None = None,
    brier_score: float | None = None,
    coverage_target: float = 0.90,
    realized_coverage: float | None = None,
    calibration_report: CalibrationQualityReport | None = None,
) -> UncertaintyReport:
    """Build a standardized UncertaintyReport.

    Can accept a CalibrationQualityReport from probability_calibration.py
    or individual metric values.
    """
    # If a full calibration report is provided, extract values from it
    if calibration_report is not None:
        ece = ece or calibration_report.ece
        brier_score = brier_score or calibration_report.brier_score
        calibration_method = calibration_method if calibration_method != "none" else calibration_report.calibration_method
        sample_size = sample_size or calibration_report.sample_size
        reliability_summary = calibration_report.quality_band
    else:
        reliability_summary = _infer_quality_band(brier_score, ece, sample_size)

    quality_band = _infer_quality_band(brier_score, ece, sample_size)

    # Build prediction interval if we have a probability
    prediction_interval: dict[str, Any] | None = None
    prob_for_interval = calibrated_probability or raw_probability
    if prob_for_interval is not None and sample_size > 0:
        interval = estimate_prediction_interval(
            calibrated_probability=prob_for_interval,
            sample_size=sample_size,
            coverage_target=coverage_target,
            method="wilson",
            calibrated=calibrated_probability is not None,
        )
        prediction_interval = interval.to_dict()

    # Build warnings
    warnings: list[str] = []
    if calibration_method in ("none", "raw"):
        warnings.append("概率未经校准，可能存在系统性偏差")
    if sample_size < 30:
        warnings.append(f"样本量仅 {sample_size}，统计估计不稳定")
    if brier_score is not None and brier_score > 0.25:
        warnings.append("Brier Score 偏高，预测概率与实际命中率偏差较大")
    if ece is not None and ece > 0.10:
        warnings.append("ECE 超过 0.10，概率校准质量偏差，建议重新校准")

    return UncertaintyReport(
        raw_probability=raw_probability,
        calibrated_probability=calibrated_probability,
        calibration_method=calibration_method,
        calibration_sample_size=sample_size,
        ece=ece,
        brier_score=brier_score,
        reliability_summary=reliability_summary,
        prediction_interval=prediction_interval,
        coverage_target=coverage_target if prediction_interval else None,
        realized_coverage=realized_coverage,
        quality_band=quality_band,
        warnings=warnings,
    )
