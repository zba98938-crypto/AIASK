"""Calibration with an explicit start gate (WP-B).

The plan's v3 revision replaced "build a calibrator" with "first prove there is
something to calibrate". Three gates must all pass before a model row may be
``active``:

* **B1 monotonicity** — inside one horizon, bucketed confidence must be
  monotone against the hit rate (Spearman >= 0.5) with at least 3 buckets that
  each hold >= 30 independent trading days.
* **B2 positivity** — the top bucket's same-day selection uplift must have a
  positive 95% LCB.
* **B3 coverage** — at least 100 rows with ``confidence >= 0.85``.

When any gate fails the service records the reason code and writes **no active
model**. ``calibrated_confidence`` then stays NULL and the selective gate
abstains; that is the correct fail-closed outcome, and it must be reported as
"not measurable", never as "calibration failed".
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional, Sequence

from .direction_stats import mean, wilson_lcb

CALIBRATION_VERSION = "calibration.v1"
FEATURE_VERSION = "confidence_horizon_direction.v1"
MIN_BUCKET_DAYS = 30
MIN_BUCKETS = 3
MIN_SPEARMAN = 0.5
MIN_HIGH_CONFIDENCE_ROWS = 100
HIGH_CONFIDENCE_THRESHOLD = 0.85
BUCKET_EDGES = (0.0, 0.55, 0.62, 0.68, 0.72, 0.78, 0.85, 1.0001)

REASON_NO_MONOTONICITY = "no_monotonicity"
REASON_NO_POSITIVE_UPLIFT = "no_positive_uplift"
REASON_NO_HIGH_CONFIDENCE_ROWS = "no_high_confidence_rows"
REASON_INSUFFICIENT_BLOCKS = "insufficient_blocks"
REASON_HOLDOUT_NOT_BETTER = "holdout_brier_not_better_than_base_rate"
REASON_OK = "gates_passed"


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rank(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = mean(rx), mean(ry)
    if mx is None or my is None:
        return None
    numerator = sum((x - mx) * (y - my) for x, y in zip(rx, ry))
    denom_x = math.sqrt(sum((x - mx) ** 2 for x in rx))
    denom_y = math.sqrt(sum((y - my) ** 2 for y in ry))
    if denom_x == 0 or denom_y == 0:
        return None
    return numerator / (denom_x * denom_y)


def _bucket(confidence: float) -> tuple[float, float]:
    for low, high in zip(BUCKET_EDGES, BUCKET_EDGES[1:]):
        if low <= confidence < high:
            return low, high
    return BUCKET_EDGES[-2], BUCKET_EDGES[-1]


@dataclass(slots=True)
class Observation:
    as_of: date
    horizon: int
    confidence: float
    hit: bool
    uplift: Optional[float] = None


def _group(observations: Iterable[Observation]) -> dict[int, list[Observation]]:
    out: dict[int, list[Observation]] = {}
    for obs in observations:
        out.setdefault(obs.horizon, []).append(obs)
    return out


def evaluate_start_gates(observations: Sequence[Observation]) -> dict[str, Any]:
    """Evaluate B1/B2/B3 and return per-horizon detail plus a single verdict."""
    by_horizon = _group(observations)
    detail: dict[str, Any] = {}
    b1_pass = False
    b2_pass = False
    b1_measurable = False
    b2_measurable = False
    for horizon, items in sorted(by_horizon.items()):
        buckets: dict[tuple[float, float], dict[str, Any]] = {}
        for obs in items:
            key = _bucket(obs.confidence)
            slot = buckets.setdefault(key, {"hits": 0, "days": set(), "confidences": []})
            slot["hits"] += 1 if obs.hit else 0
            slot["days"].add(obs.as_of)
            slot["confidences"].append(obs.confidence)
        rows = []
        for key in sorted(buckets):
            slot = buckets[key]
            n_days = len(slot["days"])
            n_rows = len(slot["confidences"])
            rows.append(
                {
                    "bucket": [key[0], key[1]],
                    "mean_confidence": mean(slot["confidences"]),
                    "hit_rate": (slot["hits"] / n_rows) if n_rows else None,
                    "n_rows": n_rows,
                    "n_independent_days": n_days,
                }
            )
        eligible = [row for row in rows if row["n_independent_days"] >= MIN_BUCKET_DAYS]
        rho = spearman(
            [row["mean_confidence"] for row in eligible],
            [row["hit_rate"] for row in eligible],
        ) if len(eligible) >= MIN_BUCKETS else None
        top = max(items, key=lambda obs: obs.confidence) if items else None
        top_bucket = _bucket(top.confidence) if top else None
        top_uplifts = [obs.uplift for obs in items if obs.uplift is not None and _bucket(obs.confidence) == top_bucket]
        top_uplift_lcb, _ = _lcb_from_daily(top_uplifts)
        horizon_b1 = bool(rho is not None and rho >= MIN_SPEARMAN)
        horizon_b2 = bool(top_uplift_lcb is not None and top_uplift_lcb > 0)
        b1_pass = b1_pass or horizon_b1
        b2_pass = b2_pass or horizon_b2
        b1_measurable = b1_measurable or (rho is not None)
        b2_measurable = b2_measurable or (top_uplift_lcb is not None)
        detail[str(horizon)] = {
            "n_rows": len(items),
            "n_independent_days": len({obs.as_of for obs in items}),
            "eligible_buckets": len(eligible),
            "buckets": rows,
            "spearman": rho,
            "top_bucket_uplift_lcb95": top_uplift_lcb,
            "b1_monotonic": "pass" if horizon_b1 else "unmeasurable" if rho is None else "fail",
            "b2_positive_uplift": "pass" if horizon_b2 else "unmeasurable" if top_uplift_lcb is None else "fail",
        }

    high_rows = len([obs for obs in observations if obs.confidence >= HIGH_CONFIDENCE_THRESHOLD])
    b3_pass = high_rows >= MIN_HIGH_CONFIDENCE_ROWS
    reasons: list[str] = []
    if not by_horizon:
        reasons.append(REASON_INSUFFICIENT_BLOCKS)
    if not b1_pass:
        reasons.append(REASON_NO_MONOTONICITY)
    if not b2_pass:
        reasons.append(REASON_NO_POSITIVE_UPLIFT)
    if not b3_pass:
        reasons.append(REASON_NO_HIGH_CONFIDENCE_ROWS)
    gates = {
        "b1_monotonicity": "pass" if b1_pass else ("fail" if b1_measurable else "unmeasurable"),
        "b2_positive_uplift": "pass" if b2_pass else ("fail" if b2_measurable else "unmeasurable"),
        "b3_high_confidence_coverage": "pass" if b3_pass else "fail",
    }
    passed = b1_pass and b2_pass and b3_pass
    return {
        "calibration_version": CALIBRATION_VERSION,
        "feature_version": FEATURE_VERSION,
        "gates": gates,
        "gates_passed_count": len([value for value in gates.values() if value == "pass"]),
        "passed": passed,
        "high_confidence_rows": high_rows,
        "n_observations": len(observations),
        "by_horizon": detail,
        "reason_codes": [] if passed else reasons,
    }


def _lcb_from_daily(uplifts: Sequence[float]) -> tuple[Optional[float], Optional[float]]:
    """Wilson-style LCB for a mean uplift using the normal approximation per day."""
    values = [value for value in uplifts if value is not None]
    if len(values) < 2:
        return None, None
    centre = mean(values)
    if centre is None:
        return None, None
    variance = sum((value - centre) ** 2 for value in values) / (len(values) - 1)
    stderr = math.sqrt(variance / len(values))
    return centre - 1.959963984540054 * stderr, centre


def fit_platt(pairs: Sequence[tuple[float, bool]], *, iterations: int = 100, lr: float = 0.05) -> Optional[dict[str, float]]:
    """Fit ``p = sigmoid(a * score + b)`` with plain gradient descent.

    Deliberately dependency-free: the sample sizes here are hundreds, and a
    library call would hide the convergence behaviour the gate cares about.
    """
    data = [(float(score), 1.0 if label else 0.0) for score, label in pairs]
    if len(data) < MIN_BUCKET_DAYS:
        return None
    a, b = 1.0, 0.0
    for _ in range(iterations):
        grad_a = 0.0
        grad_b = 0.0
        for score, label in data:
            z = a * score + b
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            grad_a += (p - label) * score
            grad_b += (p - label)
        a -= lr * grad_a / len(data)
        b -= lr * grad_b / len(data)
    return {"a": a, "b": b}


def apply_platt(model: dict[str, float], score: float) -> float:
    z = model.get("a", 1.0) * float(score) + model.get("b", 0.0)
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def brier(pairs: Sequence[tuple[float, bool]]) -> Optional[float]:
    if not pairs:
        return None
    total = 0.0
    for probability, label in pairs:
        total += (float(probability) - (1.0 if label else 0.0)) ** 2
    return total / len(pairs)


def _model_hash(*, family: str, method: str, fit_start: str, fit_end: str, params: dict[str, Any]) -> str:
    payload = json.dumps(
        {"family": family, "method": method, "feature_version": FEATURE_VERSION, "fit_start": fit_start, "fit_end": fit_end, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "cm_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


@dataclass(slots=True)
class CalibrationService:
    """Evaluates the start gates and, only if they pass, fits and stores a model."""

    min_train: int = MIN_BUCKET_DAYS
    max_rows: int = 200000

    async def try_fit(self, db: Any, *, observations: Sequence[Observation], family: str = "default") -> dict[str, Any]:
        gates = evaluate_start_gates(observations)
        if not gates["passed"]:
            return {
                "status": "unmeasurable" if gates["reason_codes"] else "degraded",
                "active_model": None,
                "gates": gates["gates"],
                "reason_codes": gates["reason_codes"],
                "detail": gates,
            }
        pairs_with_dates = [(obs.confidence, obs.hit, obs.as_of) for obs in observations]
        pairs_with_dates.sort(key=lambda row: row[2])
        split = max(self.min_train, int(len(pairs_with_dates) * 0.8))
        train = pairs_with_dates[:split]
        holdout = pairs_with_dates[split:]
        if len(train) < self.min_train or not holdout:
            return {
                "status": "unmeasurable",
                "active_model": None,
                "gates": gates["gates"],
                "reason_codes": [REASON_INSUFFICIENT_BLOCKS],
                "detail": gates,
            }
        model = fit_platt([(score, label) for score, label, _ in train])
        if model is None:
            return {"status": "unmeasurable", "active_model": None, "gates": gates["gates"], "reason_codes": [REASON_INSUFFICIENT_BLOCKS], "detail": gates}
        holdout_pairs = [(apply_platt(model, score), label) for score, label, _ in holdout]
        base_rate = len([1 for _, label, _ in train if label]) / len(train)
        brier_holdout = brier(holdout_pairs)
        brier_baseline = brier([(base_rate, label) for _, label, _ in holdout])
        if brier_holdout is None or brier_baseline is None or brier_holdout >= brier_baseline:
            return {
                "status": "rejected",
                "active_model": None,
                "gates": gates["gates"],
                "reason_codes": [REASON_HOLDOUT_NOT_BETTER],
                "brier_holdout": brier_holdout,
                "brier_baseline": brier_baseline,
                "detail": gates,
            }
        fit_start = min(day for _, _, day in train).isoformat()
        fit_end = max(day for _, _, day in train).isoformat()
        model_hash = _model_hash(family=family, method="platt", fit_start=fit_start, fit_end=fit_end, params=model)
        record = {
            "model_hash": model_hash,
            "strategy_family": family,
            "method": "platt",
            "feature_version": FEATURE_VERSION,
            "fit_start": fit_start,
            "fit_end": fit_end,
            "n_train": len(train),
            "n_holdout": len(holdout),
            "base_rate_train": base_rate,
            "brier_holdout": brier_holdout,
            "brier_baseline": brier_baseline,
            "ece_holdout": None,
            "status": "active",
            "reason_code": REASON_OK,
            "params_json": json.dumps(model, sort_keys=True),
        }
        if hasattr(db, "save_calibration_model"):
            await db.save_calibration_model(record)
        return {"status": "ok", "active_model": record, "gates": gates["gates"], "reason_codes": [], "detail": gates}


__all__ = [
    "BUCKET_EDGES",
    "CALIBRATION_VERSION",
    "CalibrationService",
    "FEATURE_VERSION",
    "HIGH_CONFIDENCE_THRESHOLD",
    "MIN_HIGH_CONFIDENCE_ROWS",
    "Observation",
    "apply_platt",
    "brier",
    "evaluate_start_gates",
    "fit_platt",
    "spearman",
]
