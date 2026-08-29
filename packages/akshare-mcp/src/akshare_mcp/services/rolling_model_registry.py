"""Rolling model registry with champion/challenger lifecycle tracking.

Provides:
    RollingModelRegistry
        - record_evaluation()       record a model evaluation snapshot
        - compute_rolling_stability() compute IC stability across time windows
        - transition_stage()        champion/challenger stage transition
        - lifecycle_report()        full lifecycle summary
        - detect_degradation()      flag if champion model is degrading
        - compare_champion_challenger() head-to-head comparison

Usage::

    registry = RollingModelRegistry()
    registry.record_evaluation("momentum_20d", evaluation_payload, window_tag="2024-Q1")
    registry.transition_stage("momentum_20d", from_stage="challenger", to_stage="champion")
    report = registry.lifecycle_report()
    degraded = registry.detect_degradation("momentum_20d")
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return int(default)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    m = _mean(values)
    if m is None:
        return None
    variance = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _information_ratio(values: list[float]) -> float | None:
    m = _mean(values)
    s = _std(values)
    if m is None or s is None or s <= 1e-10:
        return None
    return round(m / s, 4)


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

def _make_evaluation_snapshot(
    model_name: str,
    payload: dict[str, Any],
    window_tag: str,
) -> dict[str, Any]:
    """Extract key quality metrics from a model evaluation payload."""
    validation = dict(payload.get("validation") or payload.get("oos_validation") or {})
    purged = dict(validation.get("purged_kfold") or {})
    metrics = dict(payload.get("metrics") or {})
    rating = dict(payload.get("rating") or {})
    risk_audit = dict(payload.get("risk_audit") or payload.get("lookahead_audit") or {})

    return {
        "model_name": str(model_name or ""),
        "window_tag": str(window_tag or ""),
        "recorded_at": _now_iso(),
        "deployment_stage": str(payload.get("deployment_stage") or "unknown"),
        "ic_mean": _safe_float(
            metrics.get("ic_mean") or validation.get("ic_mean")
        ),
        "rank_ic_mean": _safe_float(
            metrics.get("rank_ic_mean") or validation.get("rank_ic_mean")
        ),
        "rank_ic_ir": _safe_float(
            metrics.get("rank_ic_ir") or validation.get("rank_ic_ir")
        ),
        "oos_rank_ic_mean": _safe_float(
            purged.get("oos_rank_ic_mean") or validation.get("oos_rank_ic_mean")
        ),
        "purged_kfold_stability_ratio": _safe_float(
            purged.get("stability_ratio") or payload.get("purged_kfold_stability_ratio")
        ),
        "degradation": _safe_float(
            purged.get("degradation") or payload.get("purged_kfold_degradation")
        ),
        "total_score": _safe_float(
            rating.get("total_score") or payload.get("total_score")
        ),
        "overall_rating": str(rating.get("overall_rating") or payload.get("overall_rating") or "unknown"),
        "recommendation": str(rating.get("recommendation") or payload.get("recommendation") or "unknown"),
        "lookahead_risk": str(
            risk_audit.get("risk_level") or risk_audit.get("lookahead_risk_level") or "unknown"
        ),
        "sample_dates": _safe_int(metrics.get("sample_dates") or validation.get("sample_dates")),
    }


def _make_transition_record(
    model_name: str,
    from_stage: str,
    to_stage: str,
    reason: str,
    triggered_by: str,
) -> dict[str, Any]:
    return {
        "model_name": str(model_name or ""),
        "from_stage": str(from_stage or "unknown"),
        "to_stage": str(to_stage or "unknown"),
        "reason": str(reason or ""),
        "triggered_by": str(triggered_by or "system"),
        "transitioned_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Main registry class
# ---------------------------------------------------------------------------

class RollingModelRegistry:
    """Track model evaluations, stage transitions, and rolling stability.

    Designed as a lightweight in-process store.  In production, persist
    snapshot data to a database or MLflow-style tracking server.
    """

    def __init__(self, max_history_per_model: int = 50) -> None:
        self._max = max(5, int(max_history_per_model or 50))
        # model_name -> list of evaluation snapshots (chronological)
        self._evaluations: dict[str, list[dict[str, Any]]] = {}
        # model_name -> list of stage transition records
        self._transitions: dict[str, list[dict[str, Any]]] = {}
        # current stage per model
        self._current_stage: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def record_evaluation(
        self,
        model_name: str,
        payload: dict[str, Any],
        *,
        window_tag: str = "",
    ) -> dict[str, Any]:
        """Record a model evaluation snapshot from quant_manager output."""
        snapshot = _make_evaluation_snapshot(model_name, payload, window_tag or _now_iso()[:10])
        history = self._evaluations.setdefault(model_name, [])
        history.append(snapshot)
        if len(history) > self._max:
            self._evaluations[model_name] = history[-self._max:]
        # Update current stage from payload if provided
        stage = str(payload.get("deployment_stage") or "").strip().lower()
        if stage:
            self._current_stage[model_name] = stage
        return snapshot

    def transition_stage(
        self,
        model_name: str,
        *,
        from_stage: str,
        to_stage: str,
        reason: str = "",
        triggered_by: str = "system",
    ) -> dict[str, Any]:
        """Record a champion/challenger stage transition."""
        record = _make_transition_record(model_name, from_stage, to_stage, reason, triggered_by)
        transitions = self._transitions.setdefault(model_name, [])
        transitions.append(record)
        self._current_stage[model_name] = str(to_stage or "unknown")
        return record

    # ------------------------------------------------------------------
    # Rolling stability
    # ------------------------------------------------------------------

    def compute_rolling_stability(
        self,
        model_name: str,
        *,
        metric: str = "rank_ic_mean",
        window_size: int = 4,
    ) -> dict[str, Any]:
        """Compute rolling stability statistics for a given metric.

        Stability ratio = windows where metric > 0 / total windows.
        IR = mean / std of metric across all evaluation windows.
        """
        history = list(self._evaluations.get(model_name) or [])
        if not history:
            return {
                "model_name": model_name,
                "metric": metric,
                "available": False,
                "reason": "no_history",
            }

        values = [_safe_float(e.get(metric)) for e in history]
        values = [v for v in values if v is not None]

        if not values:
            return {
                "model_name": model_name,
                "metric": metric,
                "available": False,
                "reason": "metric_missing_in_history",
            }

        # Rolling windows
        effective_window = min(int(window_size or 4), len(values))
        recent_values = values[-effective_window:]

        stability_ratio = round(
            sum(1 for v in values if v > 0) / len(values), 4
        )
        recent_stability_ratio = round(
            sum(1 for v in recent_values if v > 0) / max(len(recent_values), 1), 4
        )

        all_mean = _mean(values)
        all_std = _std(values)
        all_ir = _information_ratio(values)
        recent_mean = _mean(recent_values)

        # Trend: compare recent half vs older half
        half = len(values) // 2
        early = values[:max(half, 1)]
        late = values[max(half, 1):]
        early_mean = _mean(early)
        late_mean = _mean(late)
        trend = (
            "improving" if (early_mean is not None and late_mean is not None and float(late_mean) > float(early_mean) + 0.005)
            else "degrading" if (early_mean is not None and late_mean is not None and float(late_mean) < float(early_mean) - 0.005)
            else "stable"
        )

        # Degradation: recent mean vs historical mean
        degradation = (
            round(float(all_mean) - float(recent_mean), 6)
            if all_mean is not None and recent_mean is not None
            else None
        )

        return {
            "model_name": model_name,
            "metric": metric,
            "available": True,
            "evaluation_count": len(values),
            "window_size_used": effective_window,
            "all_windows": {
                "mean": round(float(all_mean), 6) if all_mean is not None else None,
                "std": round(float(all_std), 6) if all_std is not None else None,
                "ir": all_ir,
                "stability_ratio": stability_ratio,
                "min": round(min(values), 6),
                "max": round(max(values), 6),
            },
            "recent_window": {
                "mean": round(float(recent_mean), 6) if recent_mean is not None else None,
                "stability_ratio": recent_stability_ratio,
                "values": [round(v, 6) for v in recent_values],
            },
            "trend": trend,
            "degradation_vs_all_time_mean": degradation,
        }

    # ------------------------------------------------------------------
    # Champion / challenger comparison
    # ------------------------------------------------------------------

    def compare_champion_challenger(
        self,
        champion_name: str,
        challenger_name: str,
        *,
        metric: str = "rank_ic_mean",
    ) -> dict[str, Any]:
        """Head-to-head quality comparison of champion vs challenger."""
        champ_history = list(self._evaluations.get(champion_name) or [])
        chal_history = list(self._evaluations.get(challenger_name) or [])

        def _latest_metric(history: list[dict], key: str) -> float | None:
            for entry in reversed(history):
                v = _safe_float(entry.get(key))
                if v is not None:
                    return v
            return None

        champ_val = _latest_metric(champ_history, metric)
        chal_val = _latest_metric(chal_history, metric)
        champ_score = _latest_metric(champ_history, "total_score")
        chal_score = _latest_metric(chal_history, "total_score")
        champ_stability = _latest_metric(champ_history, "purged_kfold_stability_ratio")
        chal_stability = _latest_metric(chal_history, "purged_kfold_stability_ratio")

        metric_delta = (
            round(float(chal_val) - float(champ_val), 6)
            if chal_val is not None and champ_val is not None
            else None
        )
        score_delta = (
            round(float(chal_score) - float(champ_score), 6)
            if chal_score is not None and champ_score is not None
            else None
        )

        # Promote recommendation
        promote_challenger = bool(
            metric_delta is not None and metric_delta > 0.005
            and (score_delta is None or score_delta >= -2.0)
        )

        return {
            "champion": champion_name,
            "challenger": challenger_name,
            "metric": metric,
            "champion_metric": champ_val,
            "challenger_metric": chal_val,
            "metric_delta": metric_delta,
            "champion_total_score": champ_score,
            "challenger_total_score": chal_score,
            "score_delta": score_delta,
            "champion_stability_ratio": champ_stability,
            "challenger_stability_ratio": chal_stability,
            "promote_challenger_recommended": promote_challenger,
            "recommendation_reason": (
                f"Challenger {metric} exceeds champion by {metric_delta:.4f}"
                if promote_challenger
                else "Champion retains lead or insufficient evidence to promote"
            ),
        }

    # ------------------------------------------------------------------
    # Degradation detection
    # ------------------------------------------------------------------

    def detect_degradation(
        self,
        model_name: str,
        *,
        metric: str = "rank_ic_mean",
        degradation_threshold: float = 0.02,
        stability_threshold: float = 0.50,
        lookahead_risk_allowed: str = "low",
    ) -> dict[str, Any]:
        """Flag if the model is showing signs of degradation."""
        history = list(self._evaluations.get(model_name) or [])
        if len(history) < 3:
            return {
                "model_name": model_name,
                "degraded": False,
                "reason": "insufficient_history",
                "evaluation_count": len(history),
            }

        stability = self.compute_rolling_stability(model_name, metric=metric)
        recent_stab = (stability.get("recent_window") or {}).get("stability_ratio")
        degradation_val = stability.get("degradation_vs_all_time_mean")
        trend = stability.get("trend", "unknown")

        # Lookahead risk check
        latest = history[-1] if history else {}
        current_lookahead = str(latest.get("lookahead_risk") or "unknown").strip().lower()
        risky_lookahead = current_lookahead not in {"low", "unknown"}

        issues: list[str] = []
        if degradation_val is not None and float(degradation_val) > degradation_threshold:
            issues.append(f"metric_degradation:{degradation_val:.4f}>{degradation_threshold}")
        if recent_stab is not None and float(recent_stab) < stability_threshold:
            issues.append(f"low_recent_stability:{recent_stab:.4f}<{stability_threshold}")
        if trend == "degrading":
            issues.append("trend:degrading")
        if risky_lookahead:
            issues.append(f"lookahead_risk:{current_lookahead}")

        degraded = len(issues) >= 2

        return {
            "model_name": model_name,
            "degraded": degraded,
            "severity": "high" if len(issues) >= 3 else ("medium" if degraded else "low"),
            "issues": issues,
            "metric": metric,
            "degradation_vs_mean": degradation_val,
            "recent_stability_ratio": recent_stab,
            "trend": trend,
            "lookahead_risk": current_lookahead,
            "action_recommended": (
                "retire_or_retrain" if degraded and len(issues) >= 3
                else "review" if degraded
                else "monitor"
            ),
        }

    # ------------------------------------------------------------------
    # Lifecycle report
    # ------------------------------------------------------------------

    def lifecycle_report(self) -> dict[str, Any]:
        """Generate a full lifecycle summary across all registered models."""
        models = sorted(set(list(self._evaluations.keys()) + list(self._transitions.keys())))

        model_summaries: list[dict[str, Any]] = []
        champion_names: list[str] = []
        challenger_names: list[str] = []
        degraded_names: list[str] = []

        for model_name in models:
            history = list(self._evaluations.get(model_name) or [])
            transitions = list(self._transitions.get(model_name) or [])
            current_stage = self._current_stage.get(model_name, "unknown")

            stability = self.compute_rolling_stability(model_name) if history else {}
            degradation_check = self.detect_degradation(model_name) if len(history) >= 3 else {}

            latest = history[-1] if history else {}
            summary: dict[str, Any] = {
                "model_name": model_name,
                "current_stage": current_stage,
                "evaluation_count": len(history),
                "transition_count": len(transitions),
                "latest_recorded_at": latest.get("recorded_at"),
                "latest_total_score": latest.get("total_score"),
                "latest_rank_ic_mean": latest.get("rank_ic_mean"),
                "latest_oos_rank_ic_mean": latest.get("oos_rank_ic_mean"),
                "rolling_stability_ratio": (stability.get("all_windows") or {}).get("stability_ratio"),
                "rolling_trend": stability.get("trend"),
                "degraded": bool(degradation_check.get("degraded")),
                "lookahead_risk": latest.get("lookahead_risk"),
                "recent_transition": transitions[-1] if transitions else None,
            }
            model_summaries.append(summary)

            if current_stage == "champion":
                champion_names.append(model_name)
            elif current_stage == "challenger":
                challenger_names.append(model_name)
            if summary["degraded"]:
                degraded_names.append(model_name)

        return {
            "report_at": _now_iso(),
            "total_models": len(models),
            "champion_count": len(champion_names),
            "challenger_count": len(challenger_names),
            "degraded_count": len(degraded_names),
            "champion_names": champion_names,
            "challenger_names": challenger_names,
            "degraded_names": degraded_names,
            "models": model_summaries,
        }

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_current_stage(self, model_name: str) -> str:
        return self._current_stage.get(model_name, "unknown")

    def list_models(self) -> list[str]:
        return sorted(set(list(self._evaluations.keys()) + list(self._transitions.keys())))

    def clear(self) -> None:
        self._evaluations.clear()
        self._transitions.clear()
        self._current_stage.clear()


# Module-level default registry
default_rolling_registry = RollingModelRegistry()
