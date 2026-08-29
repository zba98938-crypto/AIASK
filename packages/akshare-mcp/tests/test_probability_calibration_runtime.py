from __future__ import annotations

from akshare_mcp.services.probability_calibration import (
    build_calibration_quality_report,
    calibrate_probability_series,
)


# FIX-39 (F-N43-3): sklearn 校准主路径改为直接拟合 LogisticRegression(Platt)/Isotonic，
# 取代易碎的 CalibratedClassifierCV + 假估计器（在 sklearn>=1.6 恒抛 ValueError 降级）。
_SKLEARN_PLATT_BACKEND = "sklearn_logistic_platt"
_SKLEARN_ISOTONIC_BACKEND = "sklearn_isotonic_regression"


def test_calibrate_probability_series_exposes_backend_and_fallback_state():
    probabilities = [0.12, 0.18, 0.22, 0.31, 0.44, 0.51, 0.63, 0.71, 0.83, 0.91]
    labels = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]

    result = calibrate_probability_series(
        probabilities,
        labels,
        method="sigmoid",
    )

    assert len(result.probabilities) == len(probabilities)
    assert result.method == "sigmoid"
    # sklearn 可用时走 LR(Platt) 主路径且无降级；不可用时干净降级到 builtin
    if result.backend_used == _SKLEARN_PLATT_BACKEND:
        assert result.backend_requested == _SKLEARN_PLATT_BACKEND
        assert result.fallback_used is False
    else:
        assert result.backend_used == "builtin_lightweight"
        assert result.fallback_used is True
        assert result.fallback_reason

    report = build_calibration_quality_report(
        result.probabilities,
        labels,
        calibration_method=result.method,
        calibration_version="test_v1",
        calibration_backend=result.backend_used,
        backend_requested=result.backend_requested,
        backend_used=result.backend_used,
        fallback_used=result.fallback_used,
        fallback_reason=result.fallback_reason,
        cv_folds=result.cv_folds,
    ).to_dict()

    assert report["backend_requested"] in {_SKLEARN_PLATT_BACKEND, "builtin_lightweight"}
    assert report["backend_used"] in {_SKLEARN_PLATT_BACKEND, "builtin_lightweight"}
    assert isinstance(report["fallback_used"], bool)


def test_calibrate_probability_series_isotonic_backend():
    probabilities = [0.12, 0.18, 0.22, 0.31, 0.44, 0.51, 0.63, 0.71, 0.83, 0.91]
    labels = [0, 0, 0, 0, 1, 0, 1, 1, 1, 1]

    result = calibrate_probability_series(probabilities, labels, method="isotonic")
    assert result.method == "isotonic"
    if result.backend_used == _SKLEARN_ISOTONIC_BACKEND:
        assert result.fallback_used is False
    else:
        assert result.backend_used == "builtin_lightweight"
        assert result.fallback_used is True
