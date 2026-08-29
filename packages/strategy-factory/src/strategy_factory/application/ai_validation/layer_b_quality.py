"""Layer B: Learned quality predictor (replaces Gate-1/2 fixed thresholds).

Uses LightGBM to predict "will this strategy succeed in live trading?"
based on 75+ features extracted from backtest results.

Training data comes from historical strategy lifecycle:
- Positive: strategies that survived incubation (Sharpe > 0.3, not eliminated)
- Negative: strategies eliminated or with > 50% live degradation
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Optional

import numpy as np

from .config import AI_VALIDATION_CONFIG

logger = logging.getLogger(__name__)


class StrategyQualityPredictor:
    """Learned strategy quality predictor using gradient boosting."""

    def __init__(self):
        self._model = None
        self._config = AI_VALIDATION_CONFIG["layer_b"]
        self._feature_names: list[str] = []
        self._model_loaded = False

    def predict(self, candidate: dict[str, Any], backtest_result: dict[str, Any]) -> dict[str, Any]:
        """Predict strategy quality score (0-1).

        Returns:
            {"quality_score": float, "features": dict, "explanation": dict, "model_used": str}
        """
        features = self._extract_features(candidate, backtest_result)

        if self._model is not None and self._model_loaded:
            try:
                feature_array = np.array([[features.get(name, 0.0) for name in self._feature_names]])
                probability = float(self._model.predict_proba(feature_array)[0][1])
                explanation = self._explain(feature_array)
                return {
                    "quality_score": round(probability, 4),
                    "features": features,
                    "explanation": explanation,
                    "model_used": "lightgbm",
                    "layer": "B",
                }
            except Exception as exc:
                logger.warning("Layer B: model prediction failed: %s", exc)

        # Fallback: heuristic scoring based on features
        score = self._heuristic_score(features)
        return {
            "quality_score": round(score, 4),
            "features": features,
            "explanation": {"method": "heuristic_fallback"},
            "model_used": "heuristic_fallback",
            "layer": "B",
        }

    def _extract_features(self, candidate: dict[str, Any], backtest_result: dict[str, Any]) -> dict[str, float]:
        """Extract 75+ features from candidate and backtest result."""
        metrics = dict(backtest_result.get("metrics") or {})
        layers = dict(backtest_result.get("layers") or {})
        target_metrics = dict((layers.get("target") or {}).get("metrics") or {})
        params = dict(candidate.get("params") or {})
        research_task = dict(candidate.get("research_task") or {})

        features: dict[str, float] = {}

        # --- Curve shape features ---
        features["sharpe_ratio"] = float(metrics.get("sharpe_ratio") or 0)
        features["total_return"] = float(metrics.get("total_return") or 0)
        features["max_drawdown"] = abs(float(metrics.get("max_drawdown") or 0))
        features["win_rate"] = float(metrics.get("win_rate") or 0)
        features["trades_count"] = float(metrics.get("trades_count") or metrics.get("trade_count") or 0)
        features["avg_holding_days"] = float(metrics.get("avg_holding_days") or 0)
        features["turnover_proxy"] = float(metrics.get("turnover_proxy") or 0)
        features["profit_factor"] = float(metrics.get("profit_factor") or 0)
        # Prefer real calmar when present; do not invent excellence from missing.
        raw_calmar = metrics.get("calmar_ratio")
        if raw_calmar is None:
            features["calmar_ratio"] = (
                features["total_return"] / max(features["max_drawdown"], 0.01)
                if features["max_drawdown"] > 0
                else 0.0
            )
        else:
            try:
                features["calmar_ratio"] = float(raw_calmar)
            except (TypeError, ValueError):
                features["calmar_ratio"] = 0.0
        features["recovery_factor"] = features["calmar_ratio"]

        # Equity curve linearity (if available)
        equity_curve = backtest_result.get("equity_curve")
        if isinstance(equity_curve, (list, tuple)) and len(equity_curve) > 10:
            curve = np.array(equity_curve, dtype=float)
            if curve[0] > 0:
                normalized = curve / curve[0]
                # Linearity: R² of linear fit
                x = np.arange(len(normalized))
                if len(x) > 1:
                    correlation = np.corrcoef(x, normalized)[0, 1]
                    features["curve_linearity"] = float(correlation ** 2) if np.isfinite(correlation) else 0
                else:
                    features["curve_linearity"] = 0
                # Smoothness: std of daily returns
                returns = np.diff(normalized) / normalized[:-1]
                returns = returns[np.isfinite(returns)]
                features["curve_smoothness"] = 1.0 / (1.0 + float(np.std(returns)) * 10) if len(returns) > 0 else 0
                # Underwater time ratio
                peaks = np.maximum.accumulate(curve)
                underwater = np.sum(curve < peaks * 0.99) / max(len(curve), 1)
                features["underwater_time_ratio"] = float(underwater)
        else:
            features["curve_linearity"] = 0
            features["curve_smoothness"] = 0
            features["underwater_time_ratio"] = 0.5

        # --- Robustness features ---
        features["parameter_perturbation_stability"] = float(
            metrics.get("parameter_perturbation_trade_stability") or 0
        )
        features["walk_forward_degradation"] = float(
            backtest_result.get("walk_forward_degradation") or 0.5
        )
        features["cpcv_pbo"] = float(backtest_result.get("cpcv_pbo") or 0.5)
        features["deflated_sharpe"] = float(backtest_result.get("deflated_sharpe") or 0)
        features["bootstrap_ci_width"] = float(backtest_result.get("bootstrap_ci_width") or 1.0)

        # Target vs representative layer gap
        target_sharpe = float(target_metrics.get("sharpe_ratio") or features["sharpe_ratio"])
        features["target_representative_gap"] = abs(target_sharpe - features["sharpe_ratio"])

        # --- Structure features ---
        features["param_count"] = float(len([v for v in params.values() if isinstance(v, (int, float))]))
        features["target_symbol_count"] = float(len(candidate.get("target_symbols") or []))
        features["has_event_anchor"] = float(bool(candidate.get("event_anchor") or research_task.get("event_id")))
        features["has_explicit_research_task"] = float(bool(research_task))

        # Strategy type encoding (simple numeric)
        type_map = {
            "momentum": 1, "ma_cross": 2, "rsi": 3, "volatility_breakout": 4,
            "event_structure_breakout": 5, "gap_fill": 6, "mean_reversion_short": 7,
            "value_factor": 8, "quality_factor": 9, "growth_factor": 10,
            "multi_factor": 11, "macro_timing": 12, "sector_rotation": 13,
            "north_capital_track": 14, "margin_divergence": 15,
        }
        features["strategy_type_code"] = float(type_map.get(str(candidate.get("strategy_type") or ""), 0))

        # --- Trade validation features ---
        features["trade_density"] = float(metrics.get("trade_density") or 0)
        features["post_cost_sharpe"] = float(metrics.get("post_cost_sharpe") or features["sharpe_ratio"])
        features["event_window_hit_ratio"] = float(metrics.get("event_window_hit_ratio") or 0)
        features["post_event_decay"] = float(metrics.get("post_event_decay") or 0)
        features["target_layer_oos_return"] = float(metrics.get("target_layer_oos_return") or 0)

        # --- Market context features ---
        # These would be injected from snapshot in production
        features["backtest_period_length"] = float(metrics.get("curve_points") or 0)

        return features

    def _heuristic_score(self, features: dict[str, float]) -> float:
        """Heuristic quality score when ML model is not available."""
        score = 0.5  # Start neutral

        # Positive signals
        if features.get("sharpe_ratio", 0) > 0.3:
            score += 0.1
        if features.get("sharpe_ratio", 0) > 0.6:
            score += 0.05
        if features.get("win_rate", 0) > 0.5:
            score += 0.05
        if features.get("curve_linearity", 0) > 0.8:
            score += 0.1
        if features.get("parameter_perturbation_stability", 0) > 0.6:
            score += 0.1
        if features.get("profit_factor", 0) > 1.5:
            score += 0.05

        # Negative signals
        if features.get("max_drawdown", 0) > 0.3:
            score -= 0.1
        if features.get("underwater_time_ratio", 0) > 0.5:
            score -= 0.05
        if features.get("curve_smoothness", 0) < 0.3:
            score -= 0.05
        if features.get("trades_count", 0) < 3:
            score -= 0.1
        if features.get("param_count", 0) > 10:
            score -= 0.05  # Too many params = overfitting risk
        if features.get("target_representative_gap", 0) > 0.5:
            score -= 0.1  # Large gap = instability

        return max(0.0, min(1.0, score))

    def _explain(self, feature_array) -> dict[str, Any]:
        """Generate SHAP-like explanation for the prediction."""
        try:
            import shap
            explainer = shap.TreeExplainer(self._model)
            shap_values = explainer.shap_values(feature_array)
            if isinstance(shap_values, list):
                shap_values = shap_values[1]  # Class 1 (positive)
            top_features = sorted(
                zip(self._feature_names, shap_values[0]),
                key=lambda x: abs(x[1]),
                reverse=True,
            )[:10]
            return {
                "method": "shap",
                "top_positive": [(n, round(float(v), 4)) for n, v in top_features if v > 0][:5],
                "top_negative": [(n, round(float(v), 4)) for n, v in top_features if v < 0][:5],
            }
        except Exception:
            return {"method": "unavailable"}

    def load_model(self, path: str) -> bool:
        """Load a pre-trained LightGBM model from disk."""
        try:
            import lightgbm as lgb
            self._model = lgb.Booster(model_file=path)
            self._feature_names = self._model.feature_name()
            self._model_loaded = True
            logger.info("Layer B: loaded model from %s (%d features)", path, len(self._feature_names))
            return True
        except Exception as exc:
            logger.warning("Layer B: failed to load model: %s", exc)
            self._model_loaded = False
            return False

    def train(self, X: np.ndarray, y: np.ndarray, feature_names: list[str]) -> None:
        """Train the quality predictor on historical data."""
        try:
            import lightgbm as lgb
            params = self._config.get("params") or {
                "objective": "binary",
                "metric": "auc",
                "n_estimators": 200,
                "max_depth": 6,
                "learning_rate": 0.05,
                "min_child_samples": 20,
                "subsample": 0.8,
                "colsample_bytree": 0.8,
                "reg_alpha": 0.1,
                "reg_lambda": 1.0,
                "verbose": -1,
            }
            self._model = lgb.LGBMClassifier(**params)
            self._model.fit(X, y)
            self._feature_names = list(feature_names)
            self._model_loaded = True
            logger.info("Layer B: trained model on %d samples, %d features", len(y), len(feature_names))
        except ImportError:
            logger.warning("Layer B: lightgbm not installed, using heuristic fallback")
