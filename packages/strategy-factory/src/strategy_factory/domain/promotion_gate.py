"""Layer-3 promotion gate: deflated-Sharpe correction on forward returns.

Phase 3 of the extraction plan. Whether a strategy may be promoted is a
lifecycle *decision*, so it belongs to Strategy Factory. What moved is the
decision only; fetching the forward-return series stays with the data provider.

The statistics behind the decision are not reimplemented here. ``evaluate``
accepts an injectable ``dsr_fn``, and otherwise resolves
``deflated_sharpe_ratio`` from ``aiask-quant-core`` — a package Strategy Factory
already depends on, so no host is involved.

The gate is conservative by construction: insufficient samples, an unavailable
statistic, or an exception all yield ``passed=False``. A promotion is never
granted because a computation failed.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)


#: Env toggle for folding DSR correction into promotion. Default OFF, so
#: enabling the gate is an explicit operator decision.
PROMOTION_DSR_ENV = "STRATEGY_FACTORY_PROMOTION_DSR_ENABLED"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _finite_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as _swallowed:
        logger.warning("packages/strategy-factory/src/strategy_factory/domain/promotion_gate.py:41: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
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


def promotion_gate_enabled() -> bool:
    """Whether DSR correction participates in the promotion decision."""
    return str(os.getenv(PROMOTION_DSR_ENV) or "").strip().lower() in _TRUTHY


@dataclass(slots=True)
class PromotionGateVerdict:
    """The gate's verdict plus the evidence behind it.

    ``eligible`` distinguishes "evaluated and rejected" from "could not be
    evaluated", so a thin sample is not confused with a genuine failure.
    """

    passed: bool
    eligible: bool
    reasons: list[str]
    dsr: Optional[float] = None
    observed_sharpe: Optional[float] = None
    effective_trials: Optional[float] = None
    sample_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "eligible": bool(self.eligible),
            "reasons": list(self.reasons),
            "dsr": self.dsr,
            "observed_sharpe": self.observed_sharpe,
            "effective_trials": self.effective_trials,
            "sample_size": int(self.sample_size),
        }


@dataclass(slots=True)
class PromotionGate:
    """Runs DSR over a forward series, correcting for multiple testing."""

    #: Lower bound on the deflated Sharpe ratio (López de Prado / Bailey).
    dsr_min: float = 0.60
    #: Below this sample count the gate does not evaluate; let time accumulate.
    min_sample_size: int = 30
    #: Annualisation factor; forward returns are a trading-day series.
    periods_per_year: float = 252.0

    def evaluate(
        self,
        forward_returns: Sequence[float],
        *,
        n_trials: int = 1,
        benchmark_sharpe: float = 0.0,
        dsr_fn: Optional[Any] = None,
    ) -> PromotionGateVerdict:
        """Evaluate a forward-return series.

        Args:
            forward_returns: the strategy's accumulated forward returns.
            n_trials: how many strategies were screened in the same period —
                the selection-bias input DSR corrects for.
            benchmark_sharpe: benchmark Sharpe, default 0.
            dsr_fn: injectable ``deflated_sharpe_ratio``; resolved from
                quant-core when omitted.
        """
        series: list[float] = []
        for item in forward_returns or []:
            numeric = _finite_float(item)
            if numeric is not None:
                series.append(numeric)
        n = len(series)
        reasons: list[str] = []

        if n < int(self.min_sample_size):
            reasons.append(f"insufficient_forward_samples:{n}<{self.min_sample_size}")
            return PromotionGateVerdict(
                passed=False, eligible=False, reasons=reasons, sample_size=n
            )

        fn = dsr_fn or self._resolve_dsr_fn()
        if fn is None:
            reasons.append("dsr_fn_unavailable")
            return PromotionGateVerdict(
                passed=False, eligible=False, reasons=reasons, sample_size=n
            )

        try:
            import numpy as np

            result = fn(
                np.asarray(series, dtype=float),
                n_trials=max(1, _safe_int(n_trials, 1)),
                benchmark_sharpe=_safe_float(benchmark_sharpe, 0.0),
                periods_per_year=max(1.0, _safe_float(self.periods_per_year, 252.0)),
            )
        except Exception as exc:
            # Soft degrade: a statistics failure must not promote by accident.
            reasons.append(f"dsr_exception:{type(exc).__name__}")
            return PromotionGateVerdict(
                passed=False, eligible=False, reasons=reasons, sample_size=n
            )

        payload = dict(result or {})
        dsr = _safe_float(payload.get("dsr"))
        observed_sharpe = _safe_float(payload.get("observed_sharpe"))
        effective_trials = _safe_float(payload.get("effective_trials"))

        if not bool(payload.get("available")):
            reasons.append("dsr_not_available")
            return PromotionGateVerdict(
                passed=False,
                eligible=False,
                reasons=reasons,
                dsr=dsr,
                observed_sharpe=observed_sharpe,
                effective_trials=effective_trials,
                sample_size=n,
            )

        passed = dsr >= float(self.dsr_min)
        reasons.append(
            f"dsr_pass:{dsr:.4f}>={self.dsr_min}"
            if passed
            else f"dsr_below_min:{dsr:.4f}<{self.dsr_min}"
        )
        return PromotionGateVerdict(
            passed=passed,
            eligible=True,
            reasons=reasons,
            dsr=dsr,
            observed_sharpe=observed_sharpe,
            effective_trials=effective_trials,
            sample_size=n,
        )

    @staticmethod
    def _resolve_dsr_fn() -> Optional[Any]:
        """Resolve the statistic from quant-core, never from a host package."""
        try:
            from aiask_quant_core._validation_support import deflated_sharpe_ratio

            return deflated_sharpe_ratio
        except Exception as exc:  # pragma: no cover - import degradation
            logger.debug("PromotionGate: deflated_sharpe_ratio unavailable: %s", exc)
            return None


__all__ = [
    "PROMOTION_DSR_ENV",
    "PromotionGate",
    "PromotionGateVerdict",
    "promotion_gate_enabled",
]
