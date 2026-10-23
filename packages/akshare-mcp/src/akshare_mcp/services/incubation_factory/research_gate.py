"""Research gates E1/E2 for a learned direction model (WP-E).

v2 asked for "start WP-E when the >=0.85 bucket reaches 65% on holdout". On the
measured data that bucket holds zero rows, so the gate could only ever produce
a false "below 65%" verdict. v3 replaces it with two gates that can honestly
return ``unmeasurable``:

* **E1** — the rule signal already shows monotone, positively-uplifted
  confidence (the same B1/B2 gates as calibration). If it does not, a learned
  model is only fitting the same noise.
* **E2** — training on the earlier period and testing on later, non-overlapping
  windows must give a positive selection-uplift LCB in at least three windows.

Neither gate is a performance promise; both are about whether the evidence
could ever support one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional, Sequence

from .calibration import Observation, evaluate_start_gates
from .direction_stats import mean, wilson_lcb
from .directional_scorecard import ScoredPrediction, load_scored_predictions

logger = logging.getLogger(__name__)

RESEARCH_GATE_VERSION = "research_gate.v1"
MIN_OOS_WINDOWS = 3
WINDOW_TRADING_DAYS = 60
HIGH_CONFIDENCE_QUANTILE = 0.8

REASON_NO_RULE_STRUCTURE = "no_rule_structure"
REASON_INSUFFICIENT_OOS_WINDOWS = "insufficient_oos_windows"
REASON_NO_POSITIVE_OOS_UPLIFT = "no_positive_oos_uplift"


@dataclass(slots=True)
class WindowResult:
    start: Optional[date]
    end: Optional[date]
    n_predictions: int
    selected_hit_rate: Optional[float]
    selected_lcb95: Optional[float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "n_predictions": self.n_predictions,
            "selected_hit_rate": self.selected_hit_rate,
            "selected_lcb95": self.selected_lcb95,
        }


def _confidence(prediction: ScoredPrediction) -> Optional[float]:
    value = getattr(prediction, "confidence", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _hit_rate(items: Sequence[ScoredPrediction]) -> tuple[int, int]:
    hits = len([item for item in items if item.direction_hit])
    total = len([item for item in items if item.direction_hit is not None])
    return hits, total


async def evaluate_research_gates(
    db: Any,
    *,
    train_end: date | None = None,
    window_days: int = WINDOW_TRADING_DAYS,
) -> dict[str, Any]:
    predictions = await load_scored_predictions(db)
    if not predictions:
        return {
            "gate_version": RESEARCH_GATE_VERSION,
            "verdict": "unmeasurable",
            "e1": {"status": "unmeasurable", "reason": "no_scored_predictions"},
            "e2": {"status": "unmeasurable", "reason": "no_scored_predictions"},
            "reason_codes": ["no_scored_predictions"],
        }
    days = sorted({item.as_of for item in predictions if item.as_of})
    split = train_end or (days[len(days) // 2] if days else None)
    train = [item for item in predictions if item.as_of and split and item.as_of <= split]
    holdout = [item for item in predictions if item.as_of and split and item.as_of > split]

    observations = [
        Observation(as_of=item.as_of, horizon=item.horizon or 1, confidence=conf, hit=bool(item.direction_hit))
        for item in train
        if item.as_of and item.direction_hit is not None and (conf := _confidence(item)) is not None
    ]
    start_gates = evaluate_start_gates(observations)
    e1_pass = bool(start_gates.get("passed"))

    holdout_days = sorted({item.as_of for item in holdout if item.as_of})
    windows: list[WindowResult] = []
    for offset in range(0, len(holdout_days), max(1, window_days)):
        chunk_days = set(holdout_days[offset:offset + window_days])
        items = [item for item in holdout if item.as_of in chunk_days and item.direction_hit is not None]
        if not items:
            continue
        confidences = sorted(conf for conf in (_confidence(item) for item in items) if conf is not None)
        if len(confidences) >= 10:
            cutoff_index = int(len(confidences) * HIGH_CONFIDENCE_QUANTILE)
            cutoff = confidences[min(cutoff_index, len(confidences) - 1)]
            selected = [item for item in items if (_confidence(item) or 0.0) >= cutoff]
        else:
            selected = items
        hits, total = _hit_rate(selected)
        windows.append(
            WindowResult(
                start=min(chunk_days),
                end=max(chunk_days),
                n_predictions=total,
                selected_hit_rate=(hits / total) if total else None,
                selected_lcb95=wilson_lcb(hits, total) if total else None,
            )
        )
    positive = [w for w in windows if w.selected_lcb95 is not None and w.selected_lcb95 > 0.5]
    if len(windows) < MIN_OOS_WINDOWS:
        e2 = {"status": "unmeasurable", "reason": REASON_INSUFFICIENT_OOS_WINDOWS, "windows": [w.as_dict() for w in windows]}
    elif len(positive) < MIN_OOS_WINDOWS:
        e2 = {"status": "fail", "reason": REASON_NO_POSITIVE_OOS_UPLIFT, "windows": [w.as_dict() for w in windows]}
    else:
        e2 = {"status": "pass", "reason": None, "windows": [w.as_dict() for w in windows]}

    reasons: list[str] = []
    if not e1_pass:
        reasons.append(REASON_NO_RULE_STRUCTURE)
    if e2.get("status") != "pass":
        reasons.append(str(e2.get("reason") or "unmeasurable"))
    verdict = "pass" if (e1_pass and e2.get("status") == "pass") else ("fail" if any(r for r in reasons if r != REASON_NO_RULE_STRUCTURE) else "unmeasurable")
    if not e1_pass:
        verdict = "unmeasurable"
    return {
        "gate_version": RESEARCH_GATE_VERSION,
        "verdict": verdict,
        "split_date": split.isoformat() if split else None,
        "n_train": len(train),
        "n_holdout": len(holdout),
        "e1": {
            "status": "pass" if e1_pass else "unmeasurable",
            "gates": start_gates.get("gates"),
            "detail": start_gates.get("by_horizon"),
            "reason_codes": start_gates.get("reason_codes"),
        },
        "e2": e2,
        "holdout_mean_hit_rate": mean([w.selected_hit_rate for w in windows if w.selected_hit_rate is not None]),
        "reason_codes": reasons,
        "recommendation": _recommend(verdict),
    }


def _recommend(verdict: str) -> str:
    if verdict == "pass":
        return "e1_e2_passed_start_wp_e"
    if verdict == "fail":
        return "stop_wp_e_record_evidence"
    return "do_not_start_wp_e_evidence_unmeasurable"


__all__ = [
    "MIN_OOS_WINDOWS",
    "RESEARCH_GATE_VERSION",
    "WindowResult",
    "evaluate_research_gates",
]
