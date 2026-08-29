"""Adaptive parameter registry (方案 C).

Uses factor IC history and historical experiment results to generate
strategy parameters that are more likely to pass Gate-2 in the current
market environment.

Instead of fixed defaults like MA(5,20), this module finds the momentum
window with the highest IC and generates parameters accordingly.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Seed factor → momentum window mapping
_MOMENTUM_SEED_WINDOWS = {
    "factor_candidate:factor_memory_seed_mom_1d": 1,
    "factor_candidate:factor_memory_seed_mom_5d": 5,
    "factor_candidate:factor_memory_seed_mom_10d": 10,
    "factor_candidate:factor_memory_seed_mom_60d": 60,
}

# Seed factor → volatility window mapping
_VOLATILITY_SEED_WINDOWS = {
    "factor_candidate:factor_memory_seed_vol_5d": 5,
    "factor_candidate:factor_memory_seed_vol_10d": 10,
    "factor_candidate:factor_memory_seed_vol_60d": 60,
}


async def find_best_momentum_window(db: Any, default: int = 20) -> int:
    """Find the momentum lookback window with highest recent IC."""
    best_ic = -999.0
    best_window = default

    for seed_name, window in _MOMENTUM_SEED_WINDOWS.items():
        try:
            rows = await db.get_factor_ic_history(seed_name, "20", 10)
            if rows:
                avg_ic = sum(float(r.get("ic_value") or 0) for r in rows) / len(rows)
                if avg_ic > best_ic:
                    best_ic = avg_ic
                    best_window = window
        except Exception:
            continue

    return best_window


async def find_best_volatility_window(db: Any, default: int = 20) -> int:
    """Find the volatility window with highest recent IC."""
    best_ic = -999.0
    best_window = default

    for seed_name, window in _VOLATILITY_SEED_WINDOWS.items():
        try:
            rows = await db.get_factor_ic_history(seed_name, "20", 10)
            if rows:
                avg_ic = sum(float(r.get("ic_value") or 0) for r in rows) / len(rows)
                if avg_ic > best_ic:
                    best_ic = avg_ic
                    best_window = window
        except Exception:
            continue

    return best_window


async def generate_adaptive_params(db: Any, strategy_type: str, snapshot: dict[str, Any] = None) -> list[dict[str, Any]]:
    """Generate adaptive parameters for a strategy type based on current factor signals.

    Returns a list of parameter dicts (multiple variants to try).
    """
    if strategy_type == "ma_cross":
        best_mom = await find_best_momentum_window(db, default=20)
        short = max(3, best_mom // 3)
        return [
            {"short_period": short, "long_period": best_mom, "_adaptive": True, "_source": f"mom_window={best_mom}"},
            {"short_period": max(3, best_mom // 4), "long_period": best_mom, "_adaptive": True},
            {"short_period": short, "long_period": int(best_mom * 1.5), "_adaptive": True},
        ]

    if strategy_type == "momentum":
        best_mom = await find_best_momentum_window(db, default=20)
        return [
            {"lookback": best_mom, "hold_days": max(3, best_mom // 4), "_adaptive": True, "_source": f"mom_window={best_mom}"},
            {"lookback": best_mom, "hold_days": max(2, best_mom // 6), "_adaptive": True},
        ]

    if strategy_type == "rsi":
        best_mom = await find_best_momentum_window(db, default=14)
        period = max(6, min(30, best_mom))
        return [
            {"period": period, "oversold": 30, "overbought": 70, "_adaptive": True},
            {"period": period, "oversold": 25, "overbought": 75, "_adaptive": True},
        ]

    if strategy_type == "volatility_breakout":
        best_vol = await find_best_volatility_window(db, default=20)
        return [
            {"vol_window": best_vol, "breakout_mult": 2.0, "hold_days": max(3, best_vol // 4), "_adaptive": True},
            {"vol_window": best_vol, "breakout_mult": 1.5, "hold_days": max(2, best_vol // 5), "_adaptive": True},
        ]

    if strategy_type == "mean_reversion_short":
        return [
            {"lookback": 20, "entry_zscore": -2.0, "exit_zscore": 0.0, "hold_max": 5, "_adaptive": True},
            {"lookback": 10, "entry_zscore": -1.5, "exit_zscore": 0.5, "hold_max": 3, "_adaptive": True},
        ]

    if strategy_type in ("value_factor", "quality_factor", "growth_factor", "multi_factor"):
        # Factor strategies use the factor values directly, params are less critical
        return [
            {"rebalance_days": 20, "top_n": 10, "_adaptive": True},
            {"rebalance_days": 10, "top_n": 5, "_adaptive": True},
        ]

    # Default: no adaptive params available
    return []


async def get_adaptive_candidates(db: Any, strategy_types: list[str], snapshot: dict[str, Any] = None) -> list[dict[str, Any]]:
    """Generate adaptive candidates for multiple strategy types.

    Returns candidates in the same format as StrategySpawner.spawn().
    """
    candidates = []

    for strategy_type in strategy_types:
        param_variants = await generate_adaptive_params(db, strategy_type, snapshot)
        for params in param_variants:
            candidates.append({
                "strategy_type": strategy_type,
                "params": params,
                "source": "adaptive_parameter_registry",
                "tags": ["adaptive", f"type_{strategy_type}"],
                "spawn_reason": "adaptive_factor_driven",
            })

    return candidates


__all__ = [
    "find_best_momentum_window",
    "find_best_volatility_window",
    "generate_adaptive_params",
    "get_adaptive_candidates",
]
