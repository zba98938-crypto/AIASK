"""Fail-closed A-share strategy tradeability certificates.

The legacy factory gates are candidate screens, not certifications.  This
module keeps tradeability decisions separate and only advances a candidate
when the exact strategy, execution contract, data snapshot and raw evidence
are bound together.  Missing or derived evidence is reported as a reason; it
is never coerced into a passing numeric value.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


TRADEABILITY_PROFILE_VERSION = "strategy_factory.a_share_daily_tradeability.v1"
TRADEABILITY_CERTIFICATE_VERSION = "strategy_factory.tradeability_certificate.v1"
TRADEABILITY_GATE_VERSION = "strategy_factory.tradeability_gate.v1"

CERTIFICATION_STATUSES = frozenset(
    {
        "research_only",
        "backtest_candidate",
        "paper_trial",
        "paper_validated",
        "rejected",
        "expired",
    }
)

# Frozen v1 decision profile.  These are deliberately independent of candidate
# parameters and legacy backtest thresholds.  Changes require a new gate version.
TRADEABILITY_THRESHOLDS: dict[str, Any] = {
    "profile_version": "a_share_live_admission_seed_2026_10_v1",
    "post_cost_sharpe_min": 0.35,
    "max_drawdown_max": 0.25,
    "min_oos_folds": 3,
    "min_effective_oos_round_trips": 30,
    "min_oos_trading_days": 60,
    "net_return_block_bootstrap_lcb_min": 0.0,
    "min_family_trials": 2,
    "deflated_sharpe_ratio_min": 0.10,
    "pbo_max": 0.35,
    "white_reality_check_pvalue_max": 0.10,
    "hansen_spa_pvalue_max": 0.10,
    "paper_min_trading_days": 60,
    "paper_min_closed_round_trips": 30,
    "paper_min_sharpe": 0.50,
    "paper_min_profit_factor": 1.50,
    "paper_max_drawdown": 0.25,
    "paper_net_return_block_bootstrap_lcb_min": 0.0,
    "conservative_cost_multiple_min": 2.0,
    "certificate_validity_days": 90,
}

_PROFILE_FIELDS: tuple[str, ...] = (
    "market",
    "bar_frequency",
    "signal_timing",
    "execution",
    "universe",
    "holding_exit",
    "position_sizing",
    "risk_limits",
    "cost_model",
    "liquidity_capacity",
    "validation",
)


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tradeability_execution_hash(candidate: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[str, str]:
    from .candidate_contract import build_execution_contract_hash

    base_hash = str(build_execution_contract_hash(candidate) or "")
    composite_hash = _canonical_hash(
        {
            "base_execution_contract_hash": base_hash,
            "execution": _as_mapping(profile.get("execution")),
            "holding_exit": _as_mapping(profile.get("holding_exit")),
            "position_sizing": profile.get("position_sizing"),
            "risk_limits": profile.get("risk_limits"),
            "cost_model": _as_mapping(profile.get("cost_model")),
            "liquidity_capacity": _as_mapping(profile.get("liquidity_capacity")),
        }
    )
    return base_hash, composite_hash


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def build_a_share_tradeability_profile(candidate: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return an explicit but intentionally incomplete v1 profile skeleton.

    The function does not invent PIT membership, costs, liquidity evidence,
    risk limits, or validation design.  Unknown values remain ``None`` and
    keep the strategy in research-only until sourced and reviewed.
    """
    payload = _as_mapping(candidate)
    params = _as_mapping(payload.get("params"))
    symbols = payload.get("target_symbols") or params.get("target_symbols") or []
    if isinstance(symbols, str):
        symbols = [symbols]
    return {
        "profile_version": TRADEABILITY_PROFILE_VERSION,
        "market": "A_share",
        "bar_frequency": "daily",
        "signal_timing": "after_close",
        "execution": {
            "timing": "next_tradable_session",
            "price_policy": "next_session_open",
            "order_policy": None,
            "missing_bar_policy": "reject_and_record",
            "partial_fill_policy": None,
        },
        "universe": {
            "symbols": sorted({str(item).strip() for item in symbols if str(item).strip()}),
            "membership_policy": None,
            "point_in_time_source": None,
            "delisted_security_coverage": None,
        },
        "holding_exit": {
            "holding_rule": _as_mapping(payload.get("holding_horizon")) or None,
            "entry_rule": payload.get("entry_logic") or None,
            "exit_rule": payload.get("exit_logic") or None,
            "corporate_action_policy": None,
        },
        "position_sizing": _as_mapping(payload.get("portfolio_spec")) or None,
        "risk_limits": _as_mapping(payload.get("risk_rules")) or None,
        "cost_model": {
            "version": None,
            "commission_rate": None,
            "sell_tax_rate": None,
            "slippage_model": None,
            "slippage_bps": None,
            "market_impact_model": None,
            "market_impact_bps": None,
            "evidence_refs": [],
        },
        "liquidity_capacity": {
            "adv_source": None,
            "adv_window_days": None,
            "max_adv_participation": None,
            "capacity_notional": None,
            "evidence_refs": [],
        },
        "validation": {
            "train_window": None,
            "purge_days": None,
            "embargo_days": None,
            "oos_fold_count": 3,
            "holdout_start": None,
            "holdout_end": None,
            "holdout_sealed": False,
            "holdout_excluded_from_selection": False,
        },
    }


def validate_tradeability_profile(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the mandatory facts and evidence for an A-share daily profile."""
    data = _as_mapping(profile)
    missing: list[str] = []
    for key in _PROFILE_FIELDS:
        if not _is_present(data.get(key)):
            missing.append(f"profile_missing_{key}")
    if data.get("profile_version") != TRADEABILITY_PROFILE_VERSION:
        missing.append("profile_version_missing_or_unsupported")

    if data.get("market") != "A_share":
        missing.append("unsupported_market_profile")
    if data.get("bar_frequency") != "daily":
        missing.append("unsupported_bar_frequency")
    if data.get("signal_timing") != "after_close":
        missing.append("signal_timing_not_after_close")

    execution = _as_mapping(data.get("execution"))
    if execution.get("timing") != "next_tradable_session":
        missing.append("execution_not_next_tradable_session")
    if execution.get("price_policy") != "next_session_open":
        missing.append("execution_price_policy_not_next_open")
    for key in ("order_policy", "partial_fill_policy", "missing_bar_policy"):
        if not _is_present(execution.get(key)):
            missing.append(f"execution_{key}_missing")

    universe = _as_mapping(data.get("universe"))
    if not universe.get("symbols"):
        missing.append("target_universe_missing")
    for key in ("membership_policy", "point_in_time_source", "delisted_security_coverage"):
        if not _is_present(universe.get(key)):
            missing.append(f"universe_{key}_missing")

    holding_exit = _as_mapping(data.get("holding_exit"))
    for key in ("holding_rule", "entry_rule", "exit_rule", "corporate_action_policy"):
        if not _is_present(holding_exit.get(key)):
            missing.append(f"holding_exit_{key}_missing")
    if not _is_present(data.get("position_sizing")):
        missing.append("position_sizing_missing")
    if not _is_present(data.get("risk_limits")):
        missing.append("risk_limits_missing")

    cost = _as_mapping(data.get("cost_model"))
    for key in (
        "version",
        "commission_rate",
        "sell_tax_rate",
        "slippage_model",
        "slippage_bps",
        "market_impact_model",
        "market_impact_bps",
    ):
        if cost.get(key) is None or (isinstance(cost.get(key), str) and not cost[key].strip()):
            missing.append(f"cost_{key}_missing")
    if not cost.get("evidence_refs"):
        missing.append("cost_evidence_missing")

    liquidity = _as_mapping(data.get("liquidity_capacity"))
    for key in ("adv_source", "adv_window_days", "max_adv_participation", "capacity_notional"):
        if not _is_present(liquidity.get(key)):
            missing.append(f"liquidity_{key}_missing")
    if not liquidity.get("evidence_refs"):
        missing.append("liquidity_evidence_missing")

    validation = _as_mapping(data.get("validation"))
    for key in ("train_window", "purge_days", "embargo_days", "holdout_start", "holdout_end"):
        if not _is_present(validation.get(key)):
            missing.append(f"validation_{key}_missing")
    try:
        declared_fold_count = int(validation.get("oos_fold_count") or 0)
    except (TypeError, ValueError, OverflowError):
        declared_fold_count = 0
    if declared_fold_count < int(TRADEABILITY_THRESHOLDS["min_oos_folds"]):
        missing.append("validation_oos_folds_below_minimum")
    if validation.get("holdout_sealed") is not True:
        missing.append("holdout_not_sealed")
    if validation.get("holdout_excluded_from_selection") is not True:
        missing.append("holdout_used_or_not_proven_excluded")

    return {
        "valid": not bool(missing),
        "missing_fields": _unique(missing),
        "profile_hash": _canonical_hash(data) if data else None,
    }


def block_bootstrap_mean_interval(
    returns: Sequence[Any],
    *,
    block_length: int,
    iterations: int = 2000,
    confidence: float = 0.95,
    seed: int = 1729,
) -> dict[str, Any]:
    """Circular moving-block bootstrap CI for a finite return series."""
    values = [_number(item) for item in list(returns or [])]
    series = [float(item) for item in values if item is not None]
    n = len(series)
    block = max(1, int(block_length or 0))
    if n < max(8, block * 2):
        return {
            "available": False,
            "reason": "insufficient_block_bootstrap_sample",
            "sample_size": n,
            "block_length": block,
            "ci_lower": None,
            "ci_upper": None,
            "derived": False,
            "method": "circular_moving_block_bootstrap",
        }
    import numpy as np

    rng = np.random.default_rng(seed)
    arr = np.asarray(series, dtype=np.float64)
    draws = max(500, int(iterations or 0))
    block_count = int(math.ceil(n / block))
    means = np.empty(draws, dtype=np.float64)
    for draw in range(draws):
        starts = rng.integers(0, n, size=block_count)
        sample = np.concatenate(
            [arr[(start + np.arange(block)) % n] for start in starts]
        )[:n]
        means[draw] = float(np.mean(sample))
    alpha = (1.0 - float(confidence)) / 2.0
    return {
        "available": True,
        "sample_size": n,
        "block_length": block,
        "iterations": draws,
        "confidence": float(confidence),
        "mean": float(np.mean(arr)),
        "ci_lower": float(np.quantile(means, alpha)),
        "ci_upper": float(np.quantile(means, 1.0 - alpha)),
        "derived": False,
        "method": "circular_moving_block_bootstrap",
    }


def _trade_metrics(daily_returns: Sequence[Any], round_trip_returns: Sequence[Any]) -> dict[str, Any]:
    import numpy as np

    daily = np.asarray([_number(item) for item in list(daily_returns or [])], dtype=object)
    daily = np.asarray([float(item) for item in daily if item is not None], dtype=np.float64)
    trades = [_number(item) for item in list(round_trip_returns or [])]
    trips = [float(item) for item in trades if item is not None]
    if daily.size < 2:
        return {"available": False, "reason": "oos_daily_return_series_missing", "derived": False}
    std = float(np.std(daily, ddof=1))
    sharpe = float(np.mean(daily) / std * math.sqrt(252.0)) if std > 0 else None
    cumulative = np.cumprod(1.0 + daily)
    running_peak = np.maximum.accumulate(np.concatenate(([1.0], cumulative)))
    drawdown_curve = np.concatenate(([1.0], cumulative)) / running_peak - 1.0
    gains = sum(value for value in trips if value > 0)
    losses = -sum(value for value in trips if value < 0)
    profit_factor = gains / losses if losses > 0 else (None if gains > 0 else None)
    return {
        "available": bool(trips),
        "daily_return_count": int(daily.size),
        "round_trip_count": len(trips),
        "post_cost_sharpe": sharpe,
        "max_drawdown": float(abs(np.min(drawdown_curve))) if drawdown_curve.size else None,
        "net_cumulative_return": float(cumulative[-1] - 1.0) if cumulative.size else None,
        "profit_factor": profit_factor,
        "round_trip_returns": trips,
        "daily_returns": daily.tolist(),
        "derived": False,
    }


def _validate_oos_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    raw = _as_mapping(evidence)
    reasons: list[str] = []
    folds = [dict(item) for item in (raw.get("oos_folds") or []) if isinstance(item, Mapping)]
    if len(folds) < int(TRADEABILITY_THRESHOLDS["min_oos_folds"]):
        reasons.append("oos_folds_below_minimum")

    prior_test_end: int | None = None
    purge = max(0, int(raw.get("purge_days") or 0))
    embargo = max(0, int(raw.get("embargo_days") or 0))
    all_daily_returns: list[float] = []
    all_trade_returns: list[float] = []
    oos_days: set[str] = set()
    seen_oos_days: set[str] = set()
    seen_trade_ids: set[str] = set()
    for index, fold in enumerate(folds):
        if fold.get("derived") is True:
            reasons.append("derived_oos_fold")
        start = fold.get("test_start_index")
        end = fold.get("test_end_index")
        train_end = fold.get("train_end_index")
        if not all(isinstance(item, int) and not isinstance(item, bool) for item in (start, end, train_end)):
            reasons.append("oos_fold_interval_missing")
            continue
        if int(end) <= int(start):
            reasons.append("oos_fold_interval_invalid")
        if int(start) < int(train_end) + purge + embargo:
            reasons.append("oos_fold_purge_or_embargo_violation")
        if prior_test_end is not None and int(start) < prior_test_end:
            reasons.append("oos_test_windows_overlap_or_out_of_order")
        prior_test_end = int(end)
        fold_returns = fold.get("daily_net_returns")
        if not isinstance(fold_returns, (list, tuple)) or not fold_returns:
            reasons.append("oos_fold_raw_daily_returns_missing")
        else:
            for value in fold_returns:
                number = _number(value)
                if number is None:
                    reasons.append("oos_return_non_finite")
                else:
                    all_daily_returns.append(number)
        dates = [str(item) for item in list(fold.get("trading_dates") or []) if str(item).strip()]
        if not dates:
            reasons.append("oos_trading_dates_missing")
        for day in dates:
            if day in seen_oos_days:
                reasons.append("overlapping_oos_trading_dates")
            seen_oos_days.add(day)
            oos_days.add(day)
        trade_returns = fold.get("round_trip_net_returns")
        trade_ids = fold.get("independent_trade_ids")
        if not isinstance(trade_returns, (list, tuple)) or not isinstance(trade_ids, (list, tuple)):
            reasons.append("oos_round_trip_evidence_missing")
        else:
            if len(trade_returns) != len(trade_ids):
                reasons.append("oos_trade_ids_return_count_mismatch")
            for trade_id, value in zip(trade_ids, trade_returns):
                token = str(trade_id or "").strip()
                number = _number(value)
                if not token or number is None:
                    reasons.append("oos_trade_identity_or_return_missing")
                    continue
                if token in seen_trade_ids:
                    reasons.append("overlapping_round_trip_counted_twice")
                    continue
                seen_trade_ids.add(token)
                all_trade_returns.append(number)
    if len(seen_trade_ids) < int(TRADEABILITY_THRESHOLDS["min_effective_oos_round_trips"]):
        reasons.append("effective_oos_round_trips_below_minimum")
    if len(oos_days) < int(TRADEABILITY_THRESHOLDS["min_oos_trading_days"]):
        reasons.append("oos_trading_days_below_minimum")

    holdout = _as_mapping(raw.get("final_holdout"))
    if holdout.get("sealed") is not True or holdout.get("excluded_from_selection") is not True:
        reasons.append("final_holdout_unsealed_or_used_for_selection")
    if holdout.get("opened_for_candidate_selection") is True:
        reasons.append("final_holdout_leakage")
    if not _is_present(holdout.get("start")) or not _is_present(holdout.get("end")):
        reasons.append("final_holdout_window_missing")
    holdout_start_index = holdout.get("start_index")
    holdout_end_index = holdout.get("end_index")
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in (holdout_start_index, holdout_end_index)):
        reasons.append("final_holdout_index_bounds_missing")
    elif prior_test_end is not None and int(holdout_start_index) < prior_test_end:
        reasons.append("final_holdout_overlaps_oos_test_windows")
    elif int(holdout_end_index) <= int(holdout_start_index):
        reasons.append("final_holdout_interval_invalid")

    metrics = _trade_metrics(all_daily_returns, all_trade_returns)
    if metrics.get("available") is not True:
        reasons.append(str(metrics.get("reason") or "oos_trade_metrics_unavailable"))
    if metrics.get("post_cost_sharpe") is None or metrics.get("max_drawdown") is None:
        reasons.append("oos_sharpe_or_drawdown_unmeasurable")
    if metrics.get("profit_factor") is None:
        reasons.append("oos_profit_factor_unmeasurable")
    bootstrap = block_bootstrap_mean_interval(
        all_daily_returns,
        block_length=max(1, int(raw.get("bootstrap_block_length") or 5)),
        seed=int(raw.get("bootstrap_seed") or 1729),
    )
    if bootstrap.get("available") is not True:
        reasons.append(str(bootstrap.get("reason") or "oos_block_bootstrap_unavailable"))

    return {
        "available": not bool(reasons),
        "reason_codes": _unique(reasons),
        "fold_count": len(folds),
        "effective_round_trip_count": len(seen_trade_ids),
        "oos_trading_day_count": len(oos_days),
        "oos_metrics": metrics,
        "net_return_block_bootstrap": bootstrap,
        "final_holdout": {
            "sealed": holdout.get("sealed") is True,
            "excluded_from_selection": holdout.get("excluded_from_selection") is True,
            "start": holdout.get("start"),
            "end": holdout.get("end"),
        },
    }


def _validate_multiple_testing(raw: Mapping[str, Any], returns: Sequence[float]) -> dict[str, Any]:
    multiple = _as_mapping(raw.get("multiple_testing"))
    n_trials = multiple.get("n_trials")
    family_returns = multiple.get("family_net_returns")
    if not isinstance(n_trials, int) or isinstance(n_trials, bool) or n_trials < int(TRADEABILITY_THRESHOLDS["min_family_trials"]):
        return {"available": False, "reason_codes": ["multiple_testing_trial_count_missing_or_too_small"]}
    if not isinstance(family_returns, (list, tuple)) or not family_returns:
        return {"available": False, "reason_codes": ["multiple_testing_family_returns_missing"]}
    if multiple.get("derived") is True:
        return {"available": False, "reason_codes": ["multiple_testing_evidence_derived"]}
    try:
        import numpy as np
        from aiask_quant_core.validation import (
            deflated_sharpe_ratio,
            hansen_spa_test,
            probability_of_backtest_overfitting,
            white_reality_check,
        )

        matrix = np.asarray(family_returns, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != n_trials or matrix.shape[0] != len(returns):
            return {"available": False, "reason_codes": ["multiple_testing_family_shape_mismatch"]}
        if not np.isfinite(matrix).all():
            return {"available": False, "reason_codes": ["multiple_testing_family_returns_non_finite"]}
        strategy_returns = np.asarray(returns, dtype=np.float64)
        dsr = deflated_sharpe_ratio(strategy_returns, n_trials=n_trials, periods_per_year=252.0)
        pbo = probability_of_backtest_overfitting(matrix, n_splits=8, periods_per_year=252.0)
        white = white_reality_check(matrix, n_bootstrap=1000, seed=1729)
        spa = hansen_spa_test(matrix, n_bootstrap=1000, seed=1729)
        checks = {
            "dsr": dsr.get("available") is True
            and _number(dsr.get("dsr")) is not None
            and float(dsr["dsr"]) >= float(TRADEABILITY_THRESHOLDS["deflated_sharpe_ratio_min"]),
            "pbo": pbo.get("available") is True
            and _number(pbo.get("pbo")) is not None
            and float(pbo["pbo"]) <= float(TRADEABILITY_THRESHOLDS["pbo_max"]),
            "white_reality_check": white.get("available") is True
            and _number(white.get("p_value")) is not None
            and float(white["p_value"]) <= float(TRADEABILITY_THRESHOLDS["white_reality_check_pvalue_max"]),
            "hansen_spa": spa.get("available") is True
            and _number(spa.get("p_value")) is not None
            and float(spa["p_value"]) <= float(TRADEABILITY_THRESHOLDS["hansen_spa_pvalue_max"]),
        }
        calculation_complete = all(
            result.get("available") is True
            for result in (dsr, pbo, white, spa)
        )
        reasons = [f"multiple_testing_{key}_failed_or_unavailable" for key, passed in checks.items() if not passed]
        return {
            "available": not bool(reasons),
            "complete": calculation_complete,
            "reason_codes": reasons,
            "n_trials": n_trials,
            "checks": checks,
            "deflated_sharpe": dsr,
            "pbo": pbo,
            "white_reality_check": white,
            "hansen_spa": spa,
            "derived": False,
        }
    except Exception as exc:  # the certificate must fail closed if tests cannot run
        return {
            "available": False,
            "reason_codes": [f"multiple_testing_calculation_failed:{type(exc).__name__}"],
        }


def _validate_execution_and_cost_evidence(raw: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    execution = _as_mapping(raw.get("execution"))
    if execution.get("derived") is True:
        reasons.append("execution_evidence_derived")
    if execution.get("matches_profile") is not True:
        reasons.append("execution_profile_equivalence_unproven")
    if execution.get("data_coverage_complete") is not True:
        reasons.append("execution_market_data_coverage_incomplete")
    if execution.get("unmeasurable_fields"):
        reasons.append("execution_required_fields_unmeasurable")
    events = [dict(item) for item in (execution.get("order_fill_events") or []) if isinstance(item, Mapping)]
    if not events:
        reasons.append("execution_order_fill_trace_missing")
    for event in events:
        if event.get("derived") is True:
            reasons.append("execution_order_fill_trace_derived")
        signal_day = str(event.get("signal_date") or "")
        execution_day = str(event.get("execution_date") or "")
        if not signal_day or not execution_day or execution_day <= signal_day:
            reasons.append("same_day_or_unordered_execution")
        if event.get("status") == "filled" and event.get("price_source") != "next_session_open":
            reasons.append("fill_not_priced_at_next_session_open")
        if event.get("status") == "filled":
            if _number(event.get("fill_price")) is None:
                reasons.append("fill_price_missing")
            if not isinstance(event.get("fee_breakdown"), Mapping) or not event.get("fee_breakdown"):
                reasons.append("fill_fee_breakdown_missing")
        if event.get("status") in {"rejected", "partial"} and not _is_present(event.get("reason")):
            reasons.append("unfilled_order_reason_missing")

    costs = _as_mapping(raw.get("cost_scenarios"))
    base = _as_mapping(costs.get("base"))
    conservative = _as_mapping(costs.get("conservative"))
    if costs.get("derived") is True:
        reasons.append("cost_scenarios_derived")
    if _number(base.get("total_cost")) is None or not _is_present(base.get("evidence_ref")):
        reasons.append("base_cost_evidence_missing")
    factor = _number(conservative.get("cost_multiple"))
    if factor is None or factor < float(TRADEABILITY_THRESHOLDS["conservative_cost_multiple_min"]):
        reasons.append("conservative_cost_stress_below_minimum")
    base_total_cost = _number(base.get("total_cost"))
    conservative_total_cost = _number(conservative.get("total_cost"))
    if base_total_cost is None or conservative_total_cost is None:
        reasons.append("conservative_cost_total_missing")
    elif conservative_total_cost + 1e-12 < base_total_cost * float(TRADEABILITY_THRESHOLDS["conservative_cost_multiple_min"]):
        reasons.append("conservative_cost_total_below_twice_base")
    stress_returns = conservative.get("daily_net_returns")
    if not isinstance(stress_returns, (list, tuple)) or not stress_returns:
        stress_net_return = None
    else:
        numeric_stress_returns = [_number(value) for value in stress_returns]
        if any(value is None for value in numeric_stress_returns):
            stress_net_return = None
        else:
            import numpy as np
            stress_net_return = float(np.prod(1.0 + np.asarray(numeric_stress_returns, dtype=np.float64)) - 1.0)
    if stress_net_return is None:
        reasons.append("conservative_cost_net_return_missing")
    elif stress_net_return <= 0:
        reasons.append("conservative_cost_net_advantage_disappears")
    return {
        "available": not bool(reasons),
        "reason_codes": _unique(reasons),
        "execution_event_count": len(events),
        "base_cost": base,
        "conservative_cost": conservative,
    }


def _evaluate_backtest(raw: Mapping[str, Any]) -> dict[str, Any]:
    oos = _validate_oos_evidence(raw)
    execution = _validate_execution_and_cost_evidence(raw)
    multiple = _validate_multiple_testing(raw, list(oos.get("oos_metrics", {}).get("daily_returns") or []))
    metrics = _as_mapping(oos.get("oos_metrics"))
    bootstrap = _as_mapping(oos.get("net_return_block_bootstrap"))
    thresholds = TRADEABILITY_THRESHOLDS
    metric_checks = {
        "post_cost_sharpe": _number(metrics.get("post_cost_sharpe")) is not None
        and float(metrics["post_cost_sharpe"]) >= float(thresholds["post_cost_sharpe_min"]),
        "max_drawdown": _number(metrics.get("max_drawdown")) is not None
        and float(metrics["max_drawdown"]) <= float(thresholds["max_drawdown_max"]),
        "net_return_block_bootstrap": _number(bootstrap.get("ci_lower")) is not None
        and float(bootstrap["ci_lower"]) > float(thresholds["net_return_block_bootstrap_lcb_min"]),
        "profit_factor": _number(metrics.get("profit_factor")) is not None
        and float(metrics["profit_factor"]) > 1.0,
    }
    metric_reasons = [f"oos_{key}_failed_or_unavailable" for key, passed in metric_checks.items() if not passed]
    missing = bool(
        oos.get("reason_codes")
        or execution.get("reason_codes")
        or multiple.get("reason_codes")
    )
    failed = bool(metric_reasons)
    return {
        "available": not missing and not failed,
        "complete": bool(
            oos.get("available")
            and execution.get("available")
            and multiple.get("complete")
            and _number(metrics.get("post_cost_sharpe")) is not None
            and _number(metrics.get("max_drawdown")) is not None
            and _number(metrics.get("profit_factor")) is not None
            and bootstrap.get("available") is True
        ),
        "failed": bool(
            oos.get("available")
            and execution.get("available")
            and multiple.get("complete")
            and (failed or not multiple.get("available"))
        ),
        "reason_codes": _unique(
            [
                *list(oos.get("reason_codes") or []),
                *list(execution.get("reason_codes") or []),
                *list(multiple.get("reason_codes") or []),
                *metric_reasons,
            ]
        ),
        "oos": oos,
        "execution_and_cost": execution,
        "multiple_testing": multiple,
        "metric_checks": metric_checks,
    }


def _evaluate_paper(paper: Mapping[str, Any]) -> dict[str, Any]:
    raw = _as_mapping(paper)
    reasons: list[str] = []
    if raw.get("trial_started") is not True:
        reasons.append("paper_trial_not_started")
    if raw.get("evidence_track", "real") != "real":
        reasons.append("paper_evidence_not_real")
    if raw.get("derived") is True:
        reasons.append("paper_metrics_derived")
    if raw.get("candidate_hash_bound") is not True:
        reasons.append("paper_candidate_hash_not_bound")
    if not raw.get("evidence_refs"):
        reasons.append("paper_evidence_references_missing")

    nav_by_date: dict[str, float] = {}
    for observation in (raw.get("nav_observations") or []):
        if not isinstance(observation, Mapping):
            reasons.append("paper_nav_observation_invalid")
            continue
        if observation.get("derived") is True:
            reasons.append("paper_nav_evidence_derived")
            continue
        day = str(observation.get("date") or "").strip()
        nav = _number(observation.get("nav"))
        if not day or nav is None or nav <= 0:
            reasons.append("paper_nav_observation_invalid")
            continue
        if day in nav_by_date:
            reasons.append("paper_duplicate_nav_date")
            continue
        nav_by_date[day] = nav
    ordered_nav = [nav_by_date[day] for day in sorted(nav_by_date)]
    trading_days = len(nav_by_date)
    daily_returns = [ordered_nav[i] / ordered_nav[i - 1] - 1.0 for i in range(1, len(ordered_nav))]
    if trading_days < int(TRADEABILITY_THRESHOLDS["paper_min_trading_days"]):
        reasons.append("paper_valid_trading_days_below_minimum")

    round_trip_ids: set[str] = set()
    round_trip_returns: list[float] = []
    round_trip_pnls: list[float] = []
    for trip in (raw.get("closed_round_trips") or []):
        if not isinstance(trip, Mapping):
            reasons.append("paper_closed_round_trip_evidence_invalid")
            continue
        position_id = str(trip.get("position_id") or "").strip()
        pnl = _number(trip.get("net_pnl"))
        net_return = _number(trip.get("net_return"))
        if trip.get("status", "closed") != "closed" or not position_id or pnl is None or net_return is None:
            reasons.append("paper_closed_round_trip_evidence_invalid")
            continue
        if trip.get("derived") is True:
            reasons.append("paper_round_trip_evidence_derived")
            continue
        if position_id in round_trip_ids:
            reasons.append("paper_round_trip_identity_duplicate")
            continue
        round_trip_ids.add(position_id)
        round_trip_pnls.append(pnl)
        round_trip_returns.append(net_return)
    closed_trips = len(round_trip_ids)
    if closed_trips < int(TRADEABILITY_THRESHOLDS["paper_min_closed_round_trips"]):
        reasons.append("paper_closed_round_trips_below_minimum")

    import numpy as np
    if len(daily_returns) > 1:
        daily_std = float(np.std(np.asarray(daily_returns), ddof=1))
        sharpe = float(np.mean(daily_returns) / daily_std * math.sqrt(252.0)) if daily_std > 0 else None
    else:
        sharpe = None
    nav_array = np.asarray(ordered_nav, dtype=np.float64)
    drawdown = (
        float(np.max((np.maximum.accumulate(nav_array) - nav_array) / np.maximum(np.maximum.accumulate(nav_array), 1e-12)))
        if nav_array.size > 1
        else None
    )
    gross_profit = sum(value for value in round_trip_pnls if value > 0)
    gross_loss = -sum(value for value in round_trip_pnls if value < 0)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    win_rate = sum(value > 0 for value in round_trip_pnls) / len(round_trip_pnls) if round_trip_pnls else None
    if sharpe is None:
        reasons.append("paper_net_sharpe_unmeasurable")
    elif sharpe < float(TRADEABILITY_THRESHOLDS["paper_min_sharpe"]):
        reasons.append("paper_net_sharpe_below_threshold")
    if profit_factor is None:
        reasons.append("paper_profit_factor_unmeasurable")
    elif profit_factor < float(TRADEABILITY_THRESHOLDS["paper_min_profit_factor"]):
        reasons.append("paper_profit_factor_below_threshold")
    if drawdown is None:
        reasons.append("paper_drawdown_unmeasurable")
    elif drawdown > float(TRADEABILITY_THRESHOLDS["paper_max_drawdown"]):
        reasons.append("paper_drawdown_above_threshold")
    if win_rate is None:
        reasons.append("paper_win_rate_unmeasurable")
    ci = block_bootstrap_mean_interval(round_trip_returns, block_length=1)
    if ci.get("derived") is True or ci.get("available") is not True:
        reasons.append("paper_net_return_block_bootstrap_unavailable_or_derived")
    lcb = _number(ci.get("ci_lower"))
    if lcb is None:
        reasons.append("paper_net_return_lcb_unmeasurable")
    elif lcb <= float(TRADEABILITY_THRESHOLDS["paper_net_return_block_bootstrap_lcb_min"]):
        reasons.append("paper_net_return_lcb_not_positive")
    return {
        "available": not bool(reasons),
        "reason_codes": _unique(reasons),
        "valid_trading_days": trading_days if isinstance(trading_days, int) and not isinstance(trading_days, bool) else None,
        "closed_round_trip_count": closed_trips if isinstance(closed_trips, int) and not isinstance(closed_trips, bool) else None,
        "nav_observation_count": len(nav_by_date),
        "net_sharpe": sharpe,
        "profit_factor": profit_factor,
        "max_drawdown": drawdown,
        "win_rate": win_rate,
        "net_return_block_bootstrap": ci,
    }


def build_tradeability_certificate(
    candidate: Mapping[str, Any] | None,
    *,
    profile: Mapping[str, Any] | None = None,
    data_snapshot: Mapping[str, Any] | None = None,
    backtest_evidence: Mapping[str, Any] | None = None,
    paper_evidence: Mapping[str, Any] | None = None,
    issued_at: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Create a reproducible certificate; no candidate-supplied status is trusted."""
    from .candidate_contract import build_execution_contract_hash, build_tested_object_hash

    item = _as_mapping(candidate)
    tradeability_profile = _as_mapping(profile or item.get("tradeability_profile"))
    data_ref = _as_mapping(data_snapshot)
    bt = _as_mapping(backtest_evidence)
    if not bt:
        bt = _as_mapping(item.get("tradeability_backtest_evidence"))
    paper = _as_mapping(paper_evidence)
    computed_tested_hash = str(build_tested_object_hash(item) or "")
    declared_execution_base_hash, computed_execution_hash = _tradeability_execution_hash(item, tradeability_profile)
    declared_tested_hash = str(item.get("tested_object_hash") or "")
    declared_execution_hash = str(item.get("execution_contract_hash") or "")
    tested_hash = computed_tested_hash
    execution_hash = computed_execution_hash
    profile_validation = validate_tradeability_profile(tradeability_profile)
    profile_hash = str(profile_validation.get("profile_hash") or _canonical_hash(tradeability_profile))
    data_hash = str(data_ref.get("snapshot_hash") or data_ref.get("content_hash") or "")
    cost_version = str(_as_mapping(tradeability_profile.get("cost_model")).get("version") or "")
    backtest_evidence_hash = _canonical_hash(bt) if bt else None
    paper_evidence_hash = _canonical_hash(paper) if paper else None
    identity_payload = {
        "tested_object_hash": tested_hash,
        "execution_contract_hash": execution_hash,
        "tradeability_profile_hash": profile_hash,
        "data_snapshot_hash": data_hash,
        "cost_model_version": cost_version,
        "gate_version": TRADEABILITY_GATE_VERSION,
    }
    tested_identity_valid = bool(tested_hash and execution_hash) and not bool(
        item.get("tested_object_backfill_incomplete") or item.get("legacy_identity_partial")
    )
    identity_reasons = [] if tested_identity_valid else ["candidate_identity_incomplete"]
    if declared_tested_hash and declared_tested_hash != computed_tested_hash:
        identity_reasons.append("candidate_tested_object_hash_mismatch")
        tested_identity_valid = False
    if declared_execution_hash and declared_execution_hash != declared_execution_base_hash:
        identity_reasons.append("candidate_execution_contract_hash_mismatch")
        tested_identity_valid = False
    if not data_hash:
        identity_reasons.append("data_snapshot_hash_missing")
    if not _is_present(data_ref.get("as_of")):
        identity_reasons.append("data_snapshot_as_of_missing")

    backtest = _evaluate_backtest(bt) if bt else {
        "available": False,
        "complete": False,
        "failed": False,
        "reason_codes": ["real_oos_backtest_evidence_missing"],
        "oos": {},
        "execution_and_cost": {},
        "multiple_testing": {},
        "metric_checks": {},
    }
    paper_result = _evaluate_paper(paper) if paper else {
        "available": False,
        "reason_codes": ["paper_trial_not_started"],
    }

    blocking_reasons = _unique(
        [
            *profile_validation["missing_fields"],
            *identity_reasons,
            *list(backtest.get("reason_codes") or []),
        ]
    )
    # A completed test that falsifies a frozen economic/statistical criterion is
    # rejected. Missing/incomplete evidence stays research_only and can be rerun.
    issued = issued_at or datetime.now(timezone.utc).isoformat()
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            now = datetime.fromisoformat(datetime.now(timezone.utc).isoformat())
            status = "expired" if now > expiry else "research_only"
        except ValueError:
            status = "research_only"
            blocking_reasons.append("certificate_expiry_invalid")
    else:
        try:
            issued_dt = datetime.fromisoformat(str(issued).replace("Z", "+00:00"))
            expires_at = (issued_dt + timedelta(days=int(TRADEABILITY_THRESHOLDS["certificate_validity_days"]))).isoformat()
            status = "research_only"
        except (TypeError, ValueError, OverflowError):
            status = "research_only"
            blocking_reasons.append("certificate_issued_at_invalid")

    if status != "expired" and profile_validation.get("valid") and tested_identity_valid:
        if backtest.get("complete") and backtest.get("failed"):
            status = "rejected"
        elif backtest.get("available"):
            status = "backtest_candidate"
            if paper.get("trial_started") is True:
                status = "paper_validated" if paper_result.get("available") else "paper_trial"
        else:
            status = "research_only"
    if status == "paper_trial":
        blocking_reasons = _unique([*list(paper_result.get("reason_codes") or [])])
    if status == "paper_validated":
        blocking_reasons = []

    payload = {
        "certificate_version": TRADEABILITY_CERTIFICATE_VERSION,
        "gate_version": TRADEABILITY_GATE_VERSION,
        "candidate_id": str(item.get("candidate_id") or item.get("id") or "") or None,
        "strategy_id": str(item.get("strategy_id") or item.get("id") or "") or None,
        "tested_object_hash": tested_hash or None,
        "execution_contract_hash": execution_hash or None,
        "candidate_hash": _canonical_hash(identity_payload),
        "tradeability_profile_hash": profile_hash,
        "tradeability_profile": tradeability_profile,
        "data_snapshot": {
            "snapshot_hash": data_hash or None,
            "snapshot_ref": data_ref.get("snapshot_ref") or data_ref.get("source_ref"),
            "as_of": data_ref.get("as_of"),
        },
        "cost_model_version": cost_version or None,
        "backtest_evidence_ref": bt.get("evidence_ref") or None,
        "backtest_evidence_hash": backtest_evidence_hash,
        "paper_evidence_ref": paper.get("evidence_ref") or None,
        "paper_evidence_hash": paper_evidence_hash,
        "threshold_profile": dict(TRADEABILITY_THRESHOLDS),
        "profile_validation": profile_validation,
        "backtest": backtest,
        "paper": paper_result,
        "derived": False,
        "status": status,
        "reason_codes": blocking_reasons,
        "issued_at": issued,
        "expires_at": expires_at,
    }
    payload["certificate_hash"] = _canonical_hash(payload)
    payload["certificate_id"] = f"tradeability:{payload['certificate_hash'][:24]}"
    return payload


def validate_tradeability_certificate_freshness(
    certificate: Mapping[str, Any],
    candidate: Mapping[str, Any],
    profile: Mapping[str, Any] | None = None,
    *,
    data_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify that edits to strategy, execution, profile, data or costs invalidate it."""
    cert = _as_mapping(certificate)
    item = _as_mapping(candidate)
    current_profile = _as_mapping(profile or item.get("tradeability_profile"))
    from .candidate_contract import build_execution_contract_hash, build_tested_object_hash

    current_tested_hash = str(build_tested_object_hash(item) or "")
    _, current_execution_hash = _tradeability_execution_hash(item, current_profile)
    current_profile_hash = _canonical_hash(current_profile)
    snapshot = _as_mapping(data_snapshot)
    current_data_hash = str(snapshot.get("snapshot_hash") or snapshot.get("content_hash") or "")
    expected = {
        "tested_object_hash": current_tested_hash,
        "execution_contract_hash": current_execution_hash,
        "tradeability_profile_hash": current_profile_hash,
        "snapshot_hash": current_data_hash or None,
        "cost_model_version": str(_as_mapping(current_profile.get("cost_model")).get("version") or "") or None,
    }
    actual_snapshot = _as_mapping(cert.get("data_snapshot"))
    actual = {
        "tested_object_hash": cert.get("tested_object_hash"),
        "execution_contract_hash": cert.get("execution_contract_hash"),
        "tradeability_profile_hash": cert.get("tradeability_profile_hash"),
        "snapshot_hash": actual_snapshot.get("snapshot_hash"),
        "cost_model_version": cert.get("cost_model_version"),
    }
    mismatches = [key for key, value in expected.items() if value != actual.get(key)]
    return {
        "valid": not bool(mismatches),
        "reason_codes": [f"certificate_stale_{key}" for key in mismatches],
        "expected": expected,
        "actual": actual,
    }


__all__ = [
    "CERTIFICATION_STATUSES",
    "TRADEABILITY_CERTIFICATE_VERSION",
    "TRADEABILITY_GATE_VERSION",
    "TRADEABILITY_PROFILE_VERSION",
    "TRADEABILITY_THRESHOLDS",
    "block_bootstrap_mean_interval",
    "build_a_share_tradeability_profile",
    "build_tradeability_certificate",
    "validate_tradeability_certificate_freshness",
    "validate_tradeability_profile",
]
