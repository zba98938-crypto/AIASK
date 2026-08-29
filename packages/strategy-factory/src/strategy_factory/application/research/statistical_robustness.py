"""Statistical robustness post-processor (P4).

This module enriches a ``validation_report`` with derived statistical
metrics that Gate-3 reads. It runs as a thin post-processor *after* the
upstream validation gateway, so it doesn't have to know about the
backtest engine internals.

What this module does today (P4 stop-gap, all results marked
``derived=true``):

    - ``param_sensitivity_derived``: when the upstream report exposes
      ``parameter_perturbation_trade_stability`` we surface
      ``1 - stability`` as a per-candidate sensitivity proxy. (Same
      logic as the Gate-3 inverse-of-stability path; we surface it
      explicitly here so the post-processor can be sampled.)
    - ``wf_ic_ir_derived``: when ``walk_forward.oos_rank_ic_mean`` and
      ``oos_rank_ic_std`` are both present and non-zero, we derive
      ``IR = mean / std`` (clipped). When ``oos_rank_ic_ir`` is already
      populated by upstream, we leave it alone and don't derive.
    - ``bootstrap_ci_lower_derived``: when ``bootstrap_ci.ic_mean`` and
      ``bootstrap_ci.se`` are both present and non-zero, we derive
      ``ci_lower = ic_mean - 1.96 * se``.
    - ``period_robustness_derived``: when ``walk_forward`` exposes
      ``is_ic_mean`` and ``oos_ic_mean``, we surface them as a
      first/second-half pair (``derived=true``).

What this module does NOT do (R8 long-term work):

    - Run real parameter-perturbation backtests (±10% / ±20%).
    - Run real train+oos / oos1+oos2 segmented backtests.
    - Replace ``derived=true`` with real ``derived=false`` values.

The P4-prep landing path:
    - Gate-3 keeps using the existing fields. The new ``*_derived``
      fields under ``validation_report.statistical_metrics`` are exposed
      for downstream readers / dashboards but do not yet override
      Gate-3's primary fields.
    - When a real perturbation backtest is implemented, the writer just
      replaces ``derived=true`` with ``derived=false`` in the same field
      and switches Gate-3 to read the new field; nothing else changes.

Public API:
    ``enrich_validation_report_with_robustness_derivations(...)``
        Add the derived statistical metrics block in-place. Idempotent:
        safe to call multiple times on the same report.

    ``RobustnessSamplingPolicy``
        Decide whether a given candidate index gets full robustness
        treatment or only the cheap derivations, given a budget cap.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional


_DEFAULT_PER_CANDIDATE_BUDGET_SEC = 30.0
_DEFAULT_FULL_SAMPLE_SIZE = 4


def _resolve_per_candidate_budget_sec() -> float:
    """Read the env-driven per-candidate backtest budget."""
    raw = os.getenv(
        "STRATEGY_FACTORY_BACKTEST_PER_CANDIDATE_BUDGET_SEC",
        str(_DEFAULT_PER_CANDIDATE_BUDGET_SEC),
    )
    try:
        v = float(raw)
    except Exception:
        return _DEFAULT_PER_CANDIDATE_BUDGET_SEC
    if v <= 0.0:
        return _DEFAULT_PER_CANDIDATE_BUDGET_SEC
    return v


def _resolve_full_sample_size() -> int:
    """Number of leading candidates that get 'full' robustness treatment.

    Beyond this index, ``RobustnessSamplingPolicy`` returns ``"degraded"``
    and only cheap derivations run.
    """
    raw = os.getenv(
        "STRATEGY_FACTORY_BACKTEST_FULL_SAMPLE_SIZE",
        str(_DEFAULT_FULL_SAMPLE_SIZE),
    )
    try:
        v = int(raw)
    except Exception:
        return _DEFAULT_FULL_SAMPLE_SIZE
    if v <= 0:
        return _DEFAULT_FULL_SAMPLE_SIZE
    return v


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except Exception:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _classify_real(value: Optional[float]) -> bool:
    """Mirror of ``_classify_gate3_metric_value(...) == "present_real"``
    but inlined here to avoid a cross-package import.

    Treats None / NaN / inf / |x| < 1e-12 as not-real.
    """
    if value is None:
        return False
    if not math.isfinite(value):
        return False
    if abs(value) < 1e-12:
        return False
    return True


@dataclass(frozen=True)
class RobustnessSample:
    """Per-candidate decision returned by ``RobustnessSamplingPolicy``."""
    candidate_index: int
    sampling: str        # "full" | "degraded"
    budget_sec: float
    full_sample_size: int


class RobustnessSamplingPolicy:
    """Decide which candidates get full robustness backtests vs cheap
    derivations only.

    Default policy: the first ``full_sample_size`` candidates run the
    full perturbation + segmentation backtest (when implemented). The
    remainder fall back to cheap derivations from the existing
    validation_report fields.

    The ``decision_for(...)`` method is pure and side-effect free; it's
    the caller's job to actually allocate compute. The post-processor
    here emits ``sampling="full"`` or ``sampling="degraded"`` so
    dashboards can tell which candidates got the full treatment.
    """

    def __init__(
        self,
        *,
        full_sample_size: Optional[int] = None,
        budget_sec: Optional[float] = None,
    ) -> None:
        self._full_sample_size = (
            int(full_sample_size)
            if full_sample_size is not None
            else _resolve_full_sample_size()
        )
        self._budget_sec = (
            float(budget_sec)
            if budget_sec is not None
            else _resolve_per_candidate_budget_sec()
        )

    @property
    def full_sample_size(self) -> int:
        return self._full_sample_size

    @property
    def budget_sec(self) -> float:
        return self._budget_sec

    def decision_for(self, candidate_index: int) -> RobustnessSample:
        sampling = "full" if int(candidate_index) < self._full_sample_size else "degraded"
        return RobustnessSample(
            candidate_index=int(candidate_index),
            sampling=sampling,
            budget_sec=self._budget_sec,
            full_sample_size=self._full_sample_size,
        )


def _derive_wf_ic_ir(walk_forward: Mapping[str, Any]) -> Optional[float]:
    """Compute IR = mean / std from the walk-forward block when both are
    present-real and IR isn't already populated."""
    existing = _safe_float(walk_forward.get("oos_rank_ic_ir") or
                           walk_forward.get("oos_ic_ir"))
    if _classify_real(existing):
        return None  # don't override real upstream value

    mean = _safe_float(walk_forward.get("oos_rank_ic_mean") or
                       walk_forward.get("oos_ic_mean"))
    std = _safe_float(walk_forward.get("oos_rank_ic_std") or
                      walk_forward.get("oos_ic_std"))
    if not _classify_real(mean) or not _classify_real(std):
        return None
    if std is None or mean is None or std == 0:
        return None
    ir = mean / std
    if not math.isfinite(ir):
        return None
    # Clip to a sensible band; IR > 5 is rare and almost always overfit.
    return max(-10.0, min(10.0, ir))


def _derive_bootstrap_ci_lower(bootstrap_ci: Mapping[str, Any]) -> Optional[float]:
    existing = _safe_float(bootstrap_ci.get("ci_lower"))
    if _classify_real(existing):
        return None  # respect real upstream value
    mean = _safe_float(bootstrap_ci.get("ic_mean") or bootstrap_ci.get("mean"))
    se = _safe_float(bootstrap_ci.get("se") or bootstrap_ci.get("std"))
    if not _classify_real(mean) or not _classify_real(se):
        return None
    return float(mean) - 1.96 * float(se)


def _derive_period_robustness(
    walk_forward: Mapping[str, Any]
) -> Optional[dict[str, float]]:
    """Surface walk-forward IS / OOS IC as a first/second-half pair."""
    is_ic = _safe_float(walk_forward.get("is_ic_mean"))
    oos_ic = _safe_float(walk_forward.get("oos_ic_mean") or
                          walk_forward.get("oos_rank_ic_mean"))
    if not _classify_real(is_ic) or not _classify_real(oos_ic):
        return None
    return {"first_half_ic": is_ic, "second_half_ic": oos_ic}


def _derive_param_sensitivity(
    backtest_metrics: Mapping[str, Any]
) -> Optional[float]:
    stability = _safe_float(
        backtest_metrics.get("parameter_perturbation_trade_stability")
    )
    if not _classify_real(stability):
        return None
    if stability is None:
        return None
    return max(0.0, min(1.0, 1.0 - float(stability)))


def enrich_validation_report_with_robustness_derivations(
    validation_report: Optional[dict[str, Any]],
    *,
    backtest_metrics: Optional[Mapping[str, Any]] = None,
    candidate_index: int = 0,
    policy: Optional[RobustnessSamplingPolicy] = None,
) -> dict[str, Any]:
    """Enrich ``validation_report`` with a derived statistical_metrics block.

    Returns the (possibly mutated) report. If ``validation_report`` is
    None, returns a minimal new dict so callers don't need to check.

    All derived fields carry ``derived=True`` so dashboards and Gate-3's
    derived-metric audit can flag them. The legacy fields under
    ``validation_report.walk_forward`` / ``.bootstrap_ci`` /
    ``.period_robustness`` are NOT overwritten — only added to.
    """
    report = dict(validation_report or {})
    metrics = report.setdefault("statistical_metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
        report["statistical_metrics"] = metrics

    eff_policy = policy or RobustnessSamplingPolicy()
    sample = eff_policy.decision_for(candidate_index)

    walk_forward = dict(report.get("walk_forward") or {})
    bootstrap_ci = dict(report.get("bootstrap_ci") or {})
    bt = dict(backtest_metrics or {})

    # wf_ic_ir
    wf_ir = _derive_wf_ic_ir(walk_forward)
    if wf_ir is not None and "wf_ic_ir" not in metrics:
        metrics["wf_ic_ir"] = {
            "value": float(wf_ir),
            "derived": True,
            "source": "derived_from_walk_forward_mean_std",
        }

    # bootstrap_ci_lower
    ci_lower = _derive_bootstrap_ci_lower(bootstrap_ci)
    if ci_lower is not None and "bootstrap_ci_lower" not in metrics:
        metrics["bootstrap_ci_lower"] = {
            "value": float(ci_lower),
            "derived": True,
            "source": "derived_from_bootstrap_mean_se",
        }

    # period_robustness
    pr = _derive_period_robustness(walk_forward)
    if pr is not None and "period_robustness" not in metrics:
        metrics["period_robustness"] = {
            "value": pr,
            "derived": True,
            "source": "derived_from_walk_forward_is_oos",
        }

    # param_sensitivity
    ps = _derive_param_sensitivity(bt)
    if ps is not None and "param_sensitivity" not in metrics:
        metrics["param_sensitivity"] = {
            "value": float(ps),
            "derived": True,
            "source": "derived_from_parameter_perturbation_stability",
        }

    metrics["sampling"] = sample.sampling
    metrics["sampling_policy"] = {
        "candidate_index": sample.candidate_index,
        "full_sample_size": sample.full_sample_size,
        "budget_sec": sample.budget_sec,
    }
    return report


__all__ = [
    "RobustnessSample",
    "RobustnessSamplingPolicy",
    "enrich_validation_report_with_robustness_derivations",
]
