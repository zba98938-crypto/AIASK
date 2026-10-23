"""Feature toggles for the prediction-hitrate programme (WP-A/B/C/D).

All default to **off**. Each one is enabled only after the gate named in
``docs/PREDICTION_HITRATE_85_PLAN.md`` (v3 §9.1) reports its precondition.

The existing ``STRATEGY_FACTORY_DIRECTION_GATE_ENABLED`` (default **on**) is a
different thing: it admits a stock direction into the factory. Naming the
hit-rate gate ``..._PREDICTION_HITRATE_GATE_ENABLED`` keeps the two apart —
the v2 plan's ``..._DIRECTION_GATE_ENABLED`` was two letters away from a live
variable with the opposite default.
"""

from __future__ import annotations

import os


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def daily_prediction_writing_enabled() -> bool:
    """WP-0.5: write one prediction per (strategy, symbol) per closed session."""
    return _env_bool("STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED", default=False)


def horizon_scoring_enabled() -> bool:
    """WP-A: score pending predictions on the net-of-cost horizon."""
    return _env_bool("STRATEGY_FACTORY_HORIZON_SCORING_ENABLED", default=False)


def calibration_enabled() -> bool:
    """WP-B: evaluate B1/B2/B3 and fit a model only when they pass."""
    return _env_bool("STRATEGY_FACTORY_CALIBRATION_ENABLED", default=False)


def selective_gate_enabled() -> bool:
    """WP-C: route paper orders through the calibrated-confidence gate."""
    return _env_bool("STRATEGY_FACTORY_SELECTIVE_GATE_ENABLED", default=False)


def prediction_hitrate_gate_enabled() -> bool:
    """Hard 85% gate. Only meaningful after n_trading_days >= 250."""
    return _env_bool("STRATEGY_FACTORY_PREDICTION_HITRATE_GATE_ENABLED", default=False)


def selective_gate_threshold() -> float:
    raw = os.getenv("STRATEGY_FACTORY_SELECTIVE_GATE_THRESHOLD", "0.85")
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.85
    return max(0.5, min(value, 0.995))


__all__ = [
    "calibration_enabled",
    "daily_prediction_writing_enabled",
    "horizon_scoring_enabled",
    "prediction_hitrate_gate_enabled",
    "selective_gate_enabled",
    "selective_gate_threshold",
]
