"""Observation lifecycle policy: the daily observe / promote / halt decision.

Phase 3 of the extraction plan. This is a lifecycle *decision* — which
observation slots to keep, release, or advance — so it belongs to Strategy
Factory rather than to the AKShare host. It moved unchanged: stdlib only, no
database, no host services, and ``decide()`` is a pure function.

Design intent (preserved from the original):

* Judge on ``skill_lcb`` (a lower confidence bound on hit rate) rather than a
  single win-rate or Sharpe cutoff.
* When the sample is too small, keep observing — neither kill nor promote.
* Optionally fold in regime dimensions: if any qualifying regime label has turned
  negative recently, lean toward halt.

Decision meanings:

* ``promote`` — forward skill is significantly positive and stable; recommend a
  promotion review. Not a direct capital allocation.
* ``observe`` — insufficient sample or unremarkable performance; keep watching.
* ``halt`` — recent skill turned negative or stability broke down; suspend and
  free the observation slot.

Thresholds match the original defaults exactly, so the move is behaviour
preserving. All of them are constructor parameters for backtesting and tuning.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional


def _finite_float(value: Any) -> float | None:
    """Coerce to float, rejecting NaN and infinities."""
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _safe_float(value: Any, default: float = 0.0) -> float:
    numeric = _finite_float(value)
    if numeric is not None:
        return numeric
    fallback = _finite_float(default)
    return fallback if fallback is not None else 0.0


def _safe_int(value: Any, default: int = 0) -> int:
    numeric = _finite_float(value)
    if numeric is not None:
        return int(numeric)
    fallback = _finite_float(default)
    return int(fallback if fallback is not None else 0)


#: Closed decision vocabulary.
OBSERVATION_DECISIONS: tuple[str, ...] = ("observe", "promote", "halt")


@dataclass(slots=True)
class ObservationLifecycleDecision:
    """A decision plus the evidence that produced it."""

    decision: str
    reasons: list[str]
    regime_evaluated: bool = False
    regime_negative_labels: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "regime_evaluated": bool(self.regime_evaluated),
            "regime_negative_labels": list(self.regime_negative_labels),
        }


@dataclass(slots=True)
class ObservationLifecyclePolicy:
    """Multi-dimensional retire/promote policy driven by ``skill_lcb``."""

    #: Below this sample count, always keep observing.
    min_primary_n: int = 10
    #: Recent skill_lcb below this → halt.
    halt_recent_skill_lcb: float = -0.03
    #: Stability gap above this → halt.
    halt_stability_gap: float = 0.10
    #: Coverage below this → keep observing (treated as insufficient data).
    observe_min_coverage: float = 0.25
    #: Promotion requires skill_lcb significantly positive.
    promote_skill_lcb: float = 0.02
    promote_min_coverage: float = 0.60
    promote_max_stability_gap: float = 0.05
    #: Regime gate: minimum sample per label; any qualifying label whose recent
    #: skill is <= 0 leans the decision toward halt.
    regime_min_n: int = 20
    regime_enabled: bool = False

    def decide(
        self,
        *,
        primary_skill_lcb: float,
        recent_primary_skill_lcb: float,
        stability_gap: float,
        coverage_ratio: float,
        primary_n: int,
        hit_rate_by_regime: Optional[Mapping[str, Any]] = None,
    ) -> ObservationLifecycleDecision:
        """Return a structured decision. Pure: no side effects."""
        primary_skill_lcb = _safe_float(primary_skill_lcb)
        recent_primary_skill_lcb = _safe_float(recent_primary_skill_lcb)
        stability_gap = _safe_float(stability_gap)
        coverage_ratio = _safe_float(coverage_ratio)
        primary_n = _safe_int(primary_n)

        reasons: list[str] = []

        # 1) Insufficient sample → keep observing.
        if primary_n < self.min_primary_n:
            reasons.append(f"insufficient_samples:{primary_n}<{self.min_primary_n}")
            return ObservationLifecycleDecision("observe", reasons)

        # 2) A regime dimension turned negative recently → halt.
        regime_eval = self._evaluate_regime(hit_rate_by_regime)
        if self.regime_enabled and regime_eval["evaluated"] and regime_eval["negative_labels"]:
            reasons.append(
                "regime_skill_negative:" + ",".join(regime_eval["negative_labels"])
            )
            return ObservationLifecycleDecision(
                "halt",
                reasons,
                regime_evaluated=True,
                regime_negative_labels=tuple(regime_eval["negative_labels"]),
            )

        # 3) Recent skill_lcb turned negative → halt.
        if recent_primary_skill_lcb < self.halt_recent_skill_lcb:
            reasons.append(
                f"recent_skill_lcb_negative:{recent_primary_skill_lcb:.4f}<{self.halt_recent_skill_lcb}"
            )
            return ObservationLifecycleDecision(
                "halt", reasons, regime_evaluated=regime_eval["evaluated"]
            )

        # 4) Stability broke down → halt.
        if stability_gap > self.halt_stability_gap:
            reasons.append(f"stability_gap_high:{stability_gap:.4f}>{self.halt_stability_gap}")
            return ObservationLifecycleDecision(
                "halt", reasons, regime_evaluated=regime_eval["evaluated"]
            )

        # 5) Coverage too low → keep observing.
        if coverage_ratio < self.observe_min_coverage:
            reasons.append(f"coverage_low:{coverage_ratio:.4f}<{self.observe_min_coverage}")
            return ObservationLifecycleDecision(
                "observe", reasons, regime_evaluated=regime_eval["evaluated"]
            )

        # 6) skill_lcb significantly positive + stable + enough coverage → promote.
        if (
            primary_skill_lcb > self.promote_skill_lcb
            and recent_primary_skill_lcb > 0.0
            and stability_gap <= self.promote_max_stability_gap
            and coverage_ratio >= self.promote_min_coverage
        ):
            # With the regime gate on, a negative label was already caught in
            # step 2; record that the decision cleared every regime.
            reasons.append(
                f"promote:skill_lcb={primary_skill_lcb:.4f}>{self.promote_skill_lcb}"
            )
            if regime_eval["evaluated"]:
                reasons.append("regime_cross_positive")
            return ObservationLifecycleDecision(
                "promote", reasons, regime_evaluated=regime_eval["evaluated"]
            )

        reasons.append("observe_default")
        return ObservationLifecycleDecision(
            "observe", reasons, regime_evaluated=regime_eval["evaluated"]
        )

    def _evaluate_regime(self, hit_rate_by_regime: Optional[Mapping[str, Any]]) -> dict[str, Any]:
        """Score each regime label on recent skill, falling back to overall skill."""
        source = dict(hit_rate_by_regime or {})
        evaluated_labels: list[str] = []
        negative_labels: list[str] = []
        for dimension, buckets in source.items():
            for label, stats in dict(buckets or {}).items():
                payload = dict(stats or {})
                n = _safe_int(payload.get("n") or payload.get("effective_n"))
                if n < int(self.regime_min_n):
                    continue
                recent = payload.get("recent_skill_lcb")
                skill = _safe_float(recent if recent is not None else payload.get("skill_lcb"))
                tag = f"{dimension}:{label}"
                evaluated_labels.append(tag)
                if skill <= 0.0:
                    negative_labels.append(tag)
        return {
            "evaluated": bool(evaluated_labels),
            "evaluated_labels": evaluated_labels,
            "negative_labels": negative_labels,
        }


__all__ = [
    "OBSERVATION_DECISIONS",
    "ObservationLifecycleDecision",
    "ObservationLifecyclePolicy",
]
