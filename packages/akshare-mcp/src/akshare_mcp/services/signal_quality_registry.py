"""Unified signal quality registry.

Aggregates quality metadata from all signal-producing modules into one place:
    - buy_probability  (Brier, ECE, calibration_gap, coverage)
    - sentiment_score  (news OOS hit_rate, alpha, stability)
    - factor_signal    (IC, OOS RankIC, lookahead risk, purged-kfold stability)

Usage::

    registry = SignalQualityRegistry()
    registry.register_probability(code="000001", as_of="2024-03-01", payload=quant_ctx)
    registry.register_sentiment(code="000001", as_of="2024-03-01", payload=sent_result)
    registry.register_factor(factor_name="momentum_20d", payload=factor_payload)
    report = registry.snapshot()
    drift = registry.drift_check()
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Internal helpers
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


def _quality_band(value: float | None, *, low: float, high: float) -> str:
    """Map a numeric quality metric to 'high' / 'medium' / 'low'."""
    if value is None:
        return "unknown"
    if value >= high:
        return "high"
    if value >= low:
        return "medium"
    return "low"


def _drift_label(
    current: float | None,
    baseline: float | None,
    *,
    threshold: float,
    higher_is_better: bool = True,
) -> str:
    """Return 'stable' / 'degraded' / 'improved' / 'unknown'.

    ``higher_is_better=False`` inverts the direction (e.g. Brier score, ECE).
    """
    if current is None or baseline is None:
        return "unknown"
    delta = float(current) - float(baseline)
    if abs(delta) <= threshold:
        return "stable"
    positive_is_good = delta > 0 if higher_is_better else delta < 0
    return "improved" if positive_is_good else "degraded"


# ---------------------------------------------------------------------------
# Registry entry dataclasses (plain dicts)
# ---------------------------------------------------------------------------

def _make_probability_entry(
    *,
    code: str,
    as_of: str,
    period: int,
    up_probability: float | None,
    brier_score: float | None,
    ece: float | None,
    calibration_gap: float | None,
    quality: str,
    sample_size: int,
    coverage_proxy: float | None,
    observed_coverage: float | None,
    coverage_gap: float | None,
    source: str = "decision_quant_builder",
) -> dict[str, Any]:
    return {
        "signal_type": "buy_probability",
        "code": str(code or ""),
        "as_of": str(as_of or ""),
        "period": int(period),
        "up_probability": _safe_float(up_probability),
        "brier_score": _safe_float(brier_score),
        "ece": _safe_float(ece),
        "calibration_gap": _safe_float(calibration_gap),
        "quality": str(quality or "unknown"),
        "sample_size": int(sample_size or 0),
        "coverage_proxy": _safe_float(coverage_proxy),
        "observed_coverage": _safe_float(observed_coverage),
        "coverage_gap": _safe_float(coverage_gap),
        "source": str(source or ""),
        "recorded_at": _now_iso(),
    }


def _make_sentiment_entry(
    *,
    code: str,
    as_of: str,
    score: float | None,
    sentiment: str,
    news_oos_available: bool,
    news_alpha_5d: float | None,
    news_signal_stability: str,
    price_momentum_hit_rate_5d: float | None,
    price_momentum_sample_count: int,
    source: str = "sentiment_analyzer",
) -> dict[str, Any]:
    return {
        "signal_type": "sentiment",
        "code": str(code or ""),
        "as_of": str(as_of or ""),
        "score": _safe_float(score),
        "sentiment": str(sentiment or "neutral"),
        "news_oos_available": bool(news_oos_available),
        "news_alpha_5d": _safe_float(news_alpha_5d),
        "news_signal_stability": str(news_signal_stability or "unknown"),
        "price_momentum_hit_rate_5d": _safe_float(price_momentum_hit_rate_5d),
        "price_momentum_sample_count": int(price_momentum_sample_count or 0),
        "source": str(source or ""),
        "recorded_at": _now_iso(),
    }


def _make_factor_entry(
    *,
    factor_name: str,
    ic_mean: float | None,
    ic_ir: float | None,
    rank_ic_mean: float | None,
    oos_rank_ic_mean: float | None,
    purged_kfold_stability_ratio: float | None,
    lookahead_risk: str,
    rating: str,
    source: str = "quant_manager",
) -> dict[str, Any]:
    return {
        "signal_type": "factor",
        "factor_name": str(factor_name or ""),
        "ic_mean": _safe_float(ic_mean),
        "ic_ir": _safe_float(ic_ir),
        "rank_ic_mean": _safe_float(rank_ic_mean),
        "oos_rank_ic_mean": _safe_float(oos_rank_ic_mean),
        "purged_kfold_stability_ratio": _safe_float(purged_kfold_stability_ratio),
        "lookahead_risk": str(lookahead_risk or "unknown"),
        "rating": str(rating or "unknown"),
        "source": str(source or ""),
        "recorded_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Main registry class
# ---------------------------------------------------------------------------

class SignalQualityRegistry:
    """In-memory registry that aggregates signal quality metadata.

    Designed as a lightweight, dependency-free store. In production, persist
    ``snapshot()`` output to a metrics table or monitoring dashboard.
    """

    def __init__(self, max_entries_per_type: int = 500) -> None:
        self._max = max(10, int(max_entries_per_type or 500))
        self._probability_entries: list[dict[str, Any]] = []
        self._sentiment_entries: list[dict[str, Any]] = []
        self._factor_entries: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Registration helpers — extract fields from module payloads
    # ------------------------------------------------------------------

    def register_probability(
        self,
        *,
        code: str,
        as_of: str = "",
        period: int = 5,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Extract and register probability quality from decision_quant_builder output.

        ``payload`` should be the ``prediction_quality`` sub-dict (or a top-level
        quant context dict that contains ``prediction_quality``).
        """
        pq = payload if "brier_score" in payload else dict(payload.get("prediction_quality") or {})
        pi = dict(payload.get("prediction_interval") or {})
        entry = _make_probability_entry(
            code=code,
            as_of=as_of,
            period=int(pq.get("period") or period),
            up_probability=_safe_float(payload.get("up_probability") or pq.get("up_probability")),
            brier_score=_safe_float(pq.get("brier_score")),
            ece=_safe_float(pq.get("ece")),
            calibration_gap=_safe_float(pq.get("calibration_gap")),
            quality=str(pq.get("quality") or "unknown"),
            sample_size=int(pq.get("sample_size") or pq.get("support_samples") or 0),
            coverage_proxy=_safe_float(pi.get("coverage_proxy")),
            observed_coverage=_safe_float(pi.get("observed_coverage")),
            coverage_gap=_safe_float(pi.get("coverage_gap")),
        )
        self._probability_entries.append(entry)
        if len(self._probability_entries) > self._max:
            self._probability_entries = self._probability_entries[-self._max:]
        return entry

    def register_sentiment(
        self,
        *,
        code: str,
        as_of: str = "",
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Extract and register sentiment quality from SentimentAnalyzer output."""
        news_oos = dict(payload.get("news_oos_validation") or {})
        hist_val = dict(payload.get("historical_validation") or {})
        # forward returns from price-momentum validation (5d)
        fwd_5d = dict((hist_val.get("forward_returns") or {}).get("5d") or {})
        entry = _make_sentiment_entry(
            code=code,
            as_of=as_of,
            score=_safe_float(payload.get("score")),
            sentiment=str(payload.get("sentiment") or "neutral"),
            news_oos_available=bool(news_oos.get("available")),
            news_alpha_5d=_safe_float(news_oos.get("alpha_5d_bull_vs_bear")),
            news_signal_stability=str(news_oos.get("signal_stability") or "unknown"),
            price_momentum_hit_rate_5d=_safe_float(fwd_5d.get("hit_rate")),
            price_momentum_sample_count=int(hist_val.get("sample_count") or 0),
        )
        self._sentiment_entries.append(entry)
        if len(self._sentiment_entries) > self._max:
            self._sentiment_entries = self._sentiment_entries[-self._max:]
        return entry

    def register_factor(
        self,
        *,
        factor_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Extract and register factor signal quality from quant_manager output."""
        validation = dict(payload.get("validation") or payload.get("oos_validation") or {})
        purged = dict(validation.get("purged_kfold") or {})
        entry = _make_factor_entry(
            factor_name=factor_name,
            ic_mean=_safe_float(payload.get("ic_mean") or validation.get("ic_mean")),
            ic_ir=_safe_float(payload.get("ic_ir") or validation.get("ic_ir")),
            rank_ic_mean=_safe_float(payload.get("rank_ic_mean") or validation.get("rank_ic_mean")),
            oos_rank_ic_mean=_safe_float(
                payload.get("oos_rank_ic_mean")
                or purged.get("oos_rank_ic_mean")
                or validation.get("oos_rank_ic_mean")
            ),
            purged_kfold_stability_ratio=_safe_float(
                payload.get("purged_kfold_stability_ratio")
                or purged.get("stability_ratio")
            ),
            lookahead_risk=str(
                (payload.get("lookahead_audit") or {}).get("risk_level")
                or payload.get("lookahead_risk_level")
                or "unknown"
            ),
            rating=str(payload.get("rating") or payload.get("overall_rating") or "unknown"),
        )
        self._factor_entries.append(entry)
        if len(self._factor_entries) > self._max:
            self._factor_entries = self._factor_entries[-self._max:]
        return entry

    # ------------------------------------------------------------------
    # Aggregation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate_floats(entries: list[dict], field: str) -> dict[str, Any]:
        values = [float(e[field]) for e in entries if e.get(field) is not None]
        if not values:
            return {"count": 0, "mean": None, "min": None, "max": None, "p50": None}
        ordered = sorted(values)
        mid = len(ordered) // 2
        p50 = ordered[mid] if len(ordered) % 2 == 1 else (ordered[mid - 1] + ordered[mid]) / 2
        return {
            "count": len(ordered),
            "mean": round(sum(ordered) / len(ordered), 6),
            "min": round(ordered[0], 6),
            "max": round(ordered[-1], 6),
            "p50": round(p50, 6),
        }

    @staticmethod
    def _quality_distribution(entries: list[dict], field: str = "quality") -> dict[str, int]:
        dist: dict[str, int] = {}
        for e in entries:
            k = str(e.get(field) or "unknown")
            dist[k] = dist.get(k, 0) + 1
        return dist

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a unified quality report snapshot across all signal types."""
        prob = self._probability_entries
        sent = self._sentiment_entries
        fct = self._factor_entries

        prob_summary: dict[str, Any] = {
            "entry_count": len(prob),
            "brier_score": self._aggregate_floats(prob, "brier_score"),
            "ece": self._aggregate_floats(prob, "ece"),
            "calibration_gap": self._aggregate_floats(prob, "calibration_gap"),
            "coverage_gap": self._aggregate_floats(prob, "coverage_gap"),
            "quality_distribution": self._quality_distribution(prob),
        }
        sent_summary: dict[str, Any] = {
            "entry_count": len(sent),
            "news_oos_available_ratio": (
                round(sum(1 for e in sent if e.get("news_oos_available")) / len(sent), 4) if sent else None
            ),
            "news_alpha_5d": self._aggregate_floats(sent, "news_alpha_5d"),
            "price_momentum_hit_rate_5d": self._aggregate_floats(sent, "price_momentum_hit_rate_5d"),
            "sentiment_distribution": self._quality_distribution(sent, "sentiment"),
            "stability_distribution": self._quality_distribution(sent, "news_signal_stability"),
        }
        fct_summary: dict[str, Any] = {
            "entry_count": len(fct),
            "oos_rank_ic_mean": self._aggregate_floats(fct, "oos_rank_ic_mean"),
            "purged_kfold_stability_ratio": self._aggregate_floats(fct, "purged_kfold_stability_ratio"),
            "rating_distribution": self._quality_distribution(fct, "rating"),
            "lookahead_risk_distribution": self._quality_distribution(fct, "lookahead_risk"),
        }

        return {
            "snapshot_at": _now_iso(),
            "buy_probability": prob_summary,
            "sentiment": sent_summary,
            "factor": fct_summary,
            "total_entries": len(prob) + len(sent) + len(fct),
        }

    def drift_check(
        self,
        *,
        baseline_brier: float | None = None,
        baseline_ece: float | None = None,
        baseline_news_alpha: float | None = None,
        baseline_oos_rank_ic: float | None = None,
        brier_threshold: float = 0.03,
        ece_threshold: float = 0.05,
        alpha_threshold: float = 0.05,
        ic_threshold: float = 0.02,
    ) -> dict[str, Any]:
        """Compare current rolling metrics against a provided baseline.

        Returns a drift report with status for each signal dimension.
        """
        prob = self._probability_entries
        sent = self._sentiment_entries
        fct = self._factor_entries

        # Current means
        def _mean(entries: list[dict], field: str) -> float | None:
            vals = [float(e[field]) for e in entries if e.get(field) is not None]
            return round(sum(vals) / len(vals), 6) if vals else None

        current_brier = _mean(prob, "brier_score")
        current_ece = _mean(prob, "ece")
        current_alpha = _mean(sent, "news_alpha_5d")
        current_ic = _mean(fct, "oos_rank_ic_mean")

        checks: dict[str, Any] = {
            "brier_score": {
                "current": current_brier,
                "baseline": baseline_brier,
                "status": _drift_label(current_brier, baseline_brier, threshold=brier_threshold, higher_is_better=False),
                "note": "lower is better",
            },
            "ece": {
                "current": current_ece,
                "baseline": baseline_ece,
                "status": _drift_label(current_ece, baseline_ece, threshold=ece_threshold, higher_is_better=False),
                "note": "lower is better",
            },
            "news_alpha_5d": {
                "current": current_alpha,
                "baseline": baseline_news_alpha,
                "status": _drift_label(current_alpha, baseline_news_alpha, threshold=alpha_threshold),
                "note": "higher is better",
            },
            "oos_rank_ic_mean": {
                "current": current_ic,
                "baseline": baseline_oos_rank_ic,
                "status": _drift_label(current_ic, baseline_oos_rank_ic, threshold=ic_threshold),
                "note": "higher is better",
            },
        }
        degraded = [k for k, v in checks.items() if v["status"] == "degraded"]
        overall = "degraded" if degraded else ("stable" if all(v["status"] != "unknown" for v in checks.values()) else "partial")
        return {
            "drift_checked_at": _now_iso(),
            "overall_status": overall,
            "degraded_dimensions": degraded,
            "checks": checks,
        }

    def clear(self) -> None:
        """Reset all entries (useful between test runs)."""
        self._probability_entries.clear()
        self._sentiment_entries.clear()
        self._factor_entries.clear()

    # Convenience: recent N entries
    def recent_probability(self, n: int = 20) -> list[dict[str, Any]]:
        return list(self._probability_entries[-max(1, n):])

    def recent_sentiment(self, n: int = 20) -> list[dict[str, Any]]:
        return list(self._sentiment_entries[-max(1, n):])

    def recent_factor(self, n: int = 20) -> list[dict[str, Any]]:
        return list(self._factor_entries[-max(1, n):])


# Module-level default registry (shared instance, stateless across requests)
default_registry = SignalQualityRegistry()


def get_default_signal_quality_registry() -> SignalQualityRegistry:
    return default_registry


def get_default_signal_quality_registry_snapshot() -> dict[str, Any]:
    return default_registry.snapshot()
