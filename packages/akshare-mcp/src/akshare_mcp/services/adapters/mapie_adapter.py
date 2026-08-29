"""Conformal prediction adapter.

Provides ``ConformalPredictionAdapter`` interface and two implementations:

1. ``BuiltinConformalAdapter`` — pure-Python approximate conformal prediction
   using split conformal method with empirical quantiles.
2. ``MapieConformalAdapter`` — wraps MAPIE library when installed.

Usage::

    adapter = get_conformal_adapter()
    result = adapter.predict_set(
        calibration_scores=[0.1, 0.3, 0.5, 0.7, 0.9],
        calibration_labels=[0, 0, 1, 1, 1],
        test_scores=[0.4, 0.6],
        alpha=0.1,
    )

"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class ConformalResult:
    """Result of a conformal prediction."""

    prediction_sets: list[list[int]]
    """For classification: list of label sets for each test sample."""

    prediction_intervals: list[tuple[float, float]]
    """For regression: list of (lower, upper) intervals."""

    coverage_target: float = 0.9
    """Target coverage level (1 - alpha)."""

    method: str = "split_conformal"
    """Method used for conformal prediction."""

    backend: str = "builtin"
    """Which backend produced this result."""

    backend_requested: str = "builtin"
    backend_used: str = "builtin"
    fallback_used: bool = False
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction_sets": self.prediction_sets,
            "prediction_intervals": [
                {"lower": lo, "upper": hi} for lo, hi in self.prediction_intervals
            ],
            "coverage_target": self.coverage_target,
            "method": self.method,
            "backend": self.backend,
            "backend_requested": self.backend_requested,
            "backend_used": self.backend_used,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "sample_count": max(len(self.prediction_sets), len(self.prediction_intervals)),
        }


# ── Abstract interface ────────────────────────────────────────────────────────

class ConformalPredictionAdapter(ABC):
    """Interface for conformal prediction adapters."""

    @abstractmethod
    def predict_set(
        self,
        calibration_scores: list[float],
        calibration_labels: list[float],
        test_scores: list[float],
        *,
        alpha: float = 0.1,
        n_classes: int = 2,
    ) -> ConformalResult:
        """Compute conformal prediction sets/intervals."""
        ...

    @abstractmethod
    def backend_name(self) -> str:
        ...


# ── Builtin implementation ────────────────────────────────────────────────────

class BuiltinConformalAdapter(ConformalPredictionAdapter):
    """Pure-Python split conformal prediction.

    Uses the simple split conformal method:
    1. Compute nonconformity scores on calibration set
    2. Find the (1-alpha)(1+1/n) quantile
    3. Build prediction sets/intervals using this threshold
    """

    def predict_set(
        self,
        calibration_scores: list[float],
        calibration_labels: list[float],
        test_scores: list[float],
        *,
        alpha: float = 0.1,
        n_classes: int = 2,
    ) -> ConformalResult:
        cal_scores = [float(s) for s in calibration_scores]
        cal_labels = [float(y) for y in calibration_labels]
        t_scores = [float(s) for s in test_scores]

        n = len(cal_scores)
        if n == 0:
            return ConformalResult(
                prediction_sets=[list(range(n_classes)) for _ in t_scores],
                prediction_intervals=[(0.0, 1.0) for _ in t_scores],
                coverage_target=1 - alpha,
                method="split_conformal_empty_cal",
                backend="builtin",
                backend_requested="builtin",
                backend_used="builtin",
            )

        # Nonconformity scores: |predicted_prob - actual_label|
        nc_scores = sorted([abs(cal_scores[i] - cal_labels[i]) for i in range(n)])

        # Quantile level
        q_level = math.ceil((n + 1) * (1 - alpha)) / n
        q_idx = min(int(q_level * n), n - 1)
        threshold = nc_scores[q_idx]

        # Build prediction sets (classification)
        prediction_sets: list[list[int]] = []
        for score in t_scores:
            included = []
            for label in range(n_classes):
                if abs(score - label) <= threshold:
                    included.append(label)
            if not included:
                included = [round(score)]
            prediction_sets.append(included)

        # Build prediction intervals (regression interpretation)
        prediction_intervals: list[tuple[float, float]] = [
            (
                round(max(0.0, score - threshold), 6),
                round(min(1.0, score + threshold), 6),
            )
            for score in t_scores
        ]

        return ConformalResult(
            prediction_sets=prediction_sets,
            prediction_intervals=prediction_intervals,
            coverage_target=1 - alpha,
            method="split_conformal",
            backend="builtin",
            backend_requested="builtin",
            backend_used="builtin",
        )

    def backend_name(self) -> str:
        return "builtin"


# ── MAPIE adapter (optional) ──────────────────────────────────────────────────

class MapieConformalAdapter(ConformalPredictionAdapter):
    """Wraps MAPIE library for conformal prediction.

    Falls back to BuiltinConformalAdapter if MAPIE is not installed.
    """

    def __init__(self) -> None:
        self._available = False
        self._fallback = BuiltinConformalAdapter()
        self._np = None
        self._train_test_split = None
        self._logistic_regression = None
        self._linear_regression = None
        self._split_classifier = None
        self._split_regressor = None
        try:
            import numpy as np
            from mapie.classification import SplitConformalClassifier
            from mapie.regression import SplitConformalRegressor
            from sklearn.linear_model import LinearRegression, LogisticRegression
            from sklearn.model_selection import train_test_split

            self._available = True
            self._np = np
            self._train_test_split = train_test_split
            self._logistic_regression = LogisticRegression
            self._linear_regression = LinearRegression
            self._split_classifier = SplitConformalClassifier
            self._split_regressor = SplitConformalRegressor
        except ImportError:
            pass

    @staticmethod
    def _required_conformal_samples(confidence_level: float) -> int:
        bounded = max(0.5, min(0.999, float(confidence_level)))
        return max(6, int(math.ceil(max(1.0 / bounded, 1.0 / max(1e-6, 1.0 - bounded)))) + 2)

    def _classify_labels(
        self,
        raw_labels: list[float],
    ) -> tuple[list[int], list[int]] | None:
        normalized = [int(round(float(item))) for item in raw_labels]
        class_labels = sorted(set(normalized))
        if len(class_labels) < 2:
            return None
        index_by_label = {label: idx for idx, label in enumerate(class_labels)}
        encoded = [index_by_label[label] for label in normalized]
        return encoded, class_labels

    def predict_set(
        self,
        calibration_scores: list[float],
        calibration_labels: list[float],
        test_scores: list[float],
        *,
        alpha: float = 0.1,
        n_classes: int = 2,
    ) -> ConformalResult:
        if not self._available:
            result = self._fallback.predict_set(
                calibration_scores, calibration_labels, test_scores,
                alpha=alpha, n_classes=n_classes,
            )
            result.backend_requested = "mapie"
            result.backend_used = "builtin"
            result.fallback_used = True
            result.fallback_reason = "mapie_not_installed"
            return result

        assert self._np is not None
        assert self._train_test_split is not None
        assert self._logistic_regression is not None
        assert self._linear_regression is not None
        assert self._split_classifier is not None
        assert self._split_regressor is not None

        try:
            cal_scores = self._np.asarray([float(s) for s in calibration_scores], dtype=float)
            test_array = self._np.asarray([float(s) for s in test_scores], dtype=float)
            classified = self._classify_labels(calibration_labels)
            if cal_scores.size < 12 or cal_scores.size != len(calibration_labels):
                raise ValueError("insufficient_mapie_samples")
            if classified is None:
                raise ValueError("mapie_requires_discrete_labels")

            encoded_labels, class_labels = classified
            class_count = max(2, len(class_labels), int(n_classes or 2))
            cal_labels = self._np.asarray(encoded_labels, dtype=int)
            if len(set(cal_labels.tolist())) < 2:
                raise ValueError("mapie_requires_multiclass_support")

            confidence_level = max(0.5, min(0.999, 1.0 - float(alpha)))
            required_conformal = self._required_conformal_samples(confidence_level)
            if cal_scores.size <= required_conformal + max(4, len(class_labels) * 2):
                raise ValueError("insufficient_conformal_holdout")

            class_counts = {
                label: int(sum(1 for item in encoded_labels if item == label))
                for label in set(encoded_labels)
            }
            if min(class_counts.values()) < 3:
                raise ValueError("mapie_class_support_too_small")

            test_size = max(required_conformal, int(round(cal_scores.size * 0.4)), len(class_labels) * 2)
            if test_size >= cal_scores.size:
                raise ValueError("mapie_invalid_split")

            features = cal_scores.reshape(-1, 1)
            (
                X_train,
                X_conf,
                y_train,
                y_conf,
            ) = self._train_test_split(
                features,
                cal_labels,
                test_size=test_size,
                random_state=42,
                stratify=cal_labels,
            )

            if len(set(y_train.tolist())) < 2 or len(set(y_conf.tolist())) < 2:
                raise ValueError("mapie_split_lost_class_support")

            classifier = self._split_classifier(
                self._logistic_regression(max_iter=1000),
                confidence_level=confidence_level,
                prefit=False,
                random_state=42,
            )
            classifier.fit(X_train, y_train)
            classifier.conformalize(X_conf, y_conf)
            predicted_labels, class_sets = classifier.predict_set(test_array.reshape(-1, 1))
            prediction_sets = []
            for sample_idx in range(class_sets.shape[0]):
                labels_for_sample = [
                    int(class_labels[label_idx])
                    for label_idx in range(min(class_sets.shape[1], len(class_labels), class_count))
                    if bool(class_sets[sample_idx, label_idx, 0])
                ]
                if not labels_for_sample:
                    predicted_idx = int(predicted_labels[sample_idx])
                    mapped_label = class_labels[predicted_idx] if 0 <= predicted_idx < len(class_labels) else class_labels[0]
                    labels_for_sample = [int(mapped_label)]
                prediction_sets.append(labels_for_sample)

            regressor = self._split_regressor(
                self._linear_regression(),
                confidence_level=confidence_level,
                prefit=False,
            )
            regressor.fit(X_train, X_train.reshape(-1))
            regressor.conformalize(X_conf, X_conf.reshape(-1))
            _, intervals = regressor.predict_interval(test_array.reshape(-1, 1))
            prediction_intervals = [
                (
                    round(max(0.0, float(intervals[idx, 0, 0])), 6),
                    round(min(1.0, float(intervals[idx, 1, 0])), 6),
                )
                for idx in range(intervals.shape[0])
            ]

            return ConformalResult(
                prediction_sets=prediction_sets,
                prediction_intervals=prediction_intervals,
                coverage_target=confidence_level,
                method="mapie_split_conformal",
                backend="mapie",
                backend_requested="mapie",
                backend_used="mapie",
                fallback_used=False,
                fallback_reason=None,
            )
        except Exception as exc:
            result = self._fallback.predict_set(
                calibration_scores,
                calibration_labels,
                test_scores,
                alpha=alpha,
                n_classes=n_classes,
            )
            result.backend_requested = "mapie"
            result.backend_used = "builtin"
            result.fallback_used = True
            result.fallback_reason = f"mapie_runtime_failed:{type(exc).__name__}"
            return result

    def backend_name(self) -> str:
        return "mapie" if self._available else "mapie_requested_builtin_fallback"


# ── Factory ───────────────────────────────────────────────────────────────────

def get_conformal_adapter(prefer_mapie: bool = True) -> ConformalPredictionAdapter:
    """Get the best available conformal prediction adapter.

    Parameters
    ----------
    prefer_mapie:
        If True, try MAPIE first, fallback to builtin.
    """
    if prefer_mapie:
        return MapieConformalAdapter()
    return BuiltinConformalAdapter()
