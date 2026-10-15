"""Runtime-contract helpers for strategy specs."""
from __future__ import annotations

# --- runtime_contracts_parts/runtime_contract_models.py ---
from typing import Any

from strategy_factory.api.semantic_contract import CANDIDATE_CONTRACT_V2

from . import constants as _constants
from . import defaults as _defaults
from . import dsl_builder as _dsl_builder
from . import normalizers as _normalizers

for _module in (
    _constants,
    _normalizers,
    _defaults,
    _dsl_builder,
):
    globals().update(
        {
            name: getattr(_module, name)
            for name in dir(_module)
            if not name.startswith("__")
        }
    )

def _default_runtime_playbook(
    strategy_type: str,
    *,
    holding_horizon: Optional[dict[str, Any]] = None,
    trade_plan: Optional[dict[str, Any]] = None,
    risk_rules: Optional[dict[str, Any]] = None,
    portfolio_spec: Optional[dict[str, Any]] = None,
    execution_assumptions: Optional[dict[str, Any]] = None,
    instrument_profile: Optional[dict[str, Any]] = None,
    backtest_metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    holding = dict(holding_horizon or {})
    plan = dict(trade_plan or {})
    rules = dict(risk_rules or {})
    portfolio = dict(portfolio_spec or {})
    execution = dict(execution_assumptions or {})
    family = _runtime_playbook_family(strategy_type)
    profile = dict(instrument_profile or {})
    stop_loss_mode = str(rules.get("stop_loss_mode") or "fixed_pct").strip().lower() or "fixed_pct"
    atr_window = max(5, _safe_int(rules.get("atr_window"), 14))
    atr_multiplier = max(0.5, _safe_float(rules.get("atr_multiplier"), 2.0))
    stop_floor_pct = max(
        0.02,
        abs(
            _safe_float(
                rules.get("stop_floor_pct")
                or rules.get("stop_loss_pct")
                or rules.get("stop_loss")
                or 0.08,
                0.08,
            )
        ),
    )
    trailing_activation_r = max(0.5, _safe_float(rules.get("trailing_activation_r"), 1.0))
    stop_rule_source = str(rules.get("stop_rule_source") or stop_loss_mode or "fixed_pct").strip().lower() or "fixed_pct"

    initial_stop_loss_pct = max(
        0.02,
        abs(_safe_float(rules.get("stop_loss_pct") or rules.get("stop_loss") or 0.08, 0.08)),
    )
    take_profit_pct = max(
        initial_stop_loss_pct,
        abs(_safe_float(rules.get("take_profit_pct") or rules.get("take_profit") or initial_stop_loss_pct * 2.0, initial_stop_loss_pct * 2.0)),
    )
    time_stop_days = max(1, _safe_int(rules.get("max_holding_days") or holding.get("max_days") or 20, 20))
    cooldown_days = max(
        1,
        _safe_int(
            rules.get("cooldown_days")
            or rules.get("cooldown_window_days")
            or plan.get("cooldown_window_days")
            or holding.get("cooldown_window_days")
            or 5,
            5,
        ),
    )
    max_position_pct = min(
        0.35,
        max(
            0.02,
            _safe_float(
                portfolio.get("max_position_pct")
                or rules.get("position_cap_pct")
                or rules.get("max_position_pct")
                or 0.18,
                0.18,
            ),
        ),
    )
    max_slippage_bps = max(
        1.0,
        _safe_float(execution.get("max_slippage_bps") or execution.get("slippage_bps") or 5.0, 5.0),
    )
    if family == "trend" and profile:
        atr14_pct = _instrument_profile_metric(
            profile,
            "atr14_pct_realized",
            "atr14_pct",
            default=(initial_stop_loss_pct / 2.0 if initial_stop_loss_pct > 0 else 0.03),
            minimum=0.01,
            maximum=0.12,
        )
        if stop_loss_mode == "atr_bucketed":
            initial_stop_loss_pct = _clip_float(
                max(stop_floor_pct, atr14_pct * atr_multiplier),
                stop_floor_pct,
                0.22,
                initial_stop_loss_pct,
            )
        else:
            initial_stop_loss_pct = _clip_float(1.8 * atr14_pct, 0.06, 0.18, initial_stop_loss_pct)
        take_profit_pct = _clip_float(max(2.0 * initial_stop_loss_pct, 0.12), 0.12, 0.35, take_profit_pct)
    elif stop_loss_mode == "atr_bucketed":
        initial_stop_loss_pct = max(stop_floor_pct, initial_stop_loss_pct)
    trailing_stop_pct = round(
        max(
            0.03,
            min(
                (
                    _clip_float(
                        1.2
                        * _instrument_profile_metric(
                            profile,
                            "atr14_pct_realized",
                            "atr14_pct",
                            default=0.03,
                            minimum=0.01,
                            maximum=0.12,
                        ),
                        0.05,
                        0.15,
                        0.08,
                    )
                    if family == "trend" and profile
                    else initial_stop_loss_pct * (0.8 if family == "trend" else 1.0)
                ),
                0.15 if family == "trend" else 0.12,
            ),
        ),
        4,
    )
    trailing_activation_profit_pct = round(max(initial_stop_loss_pct * trailing_activation_r, 0.05), 4)
    failure_exit_rule = "signal_or_time_stop"
    if family == "trend":
        failure_exit_rule = "opposite_signal_or_breakout_failure"
    elif family == "slow_factor":
        failure_exit_rule = "quality_drift_or_rank_decay"

    loss_bands = [
        {
            "threshold_pct": round(initial_stop_loss_pct * 0.5, 4),
            "action": "hold",
            "label": "soft_drawdown_watch",
        },
        {
            "threshold_pct": round(initial_stop_loss_pct, 4),
            "action": "reduce" if family == "slow_factor" else "exit",
            "label": "primary_stop_band",
        },
        {
            "threshold_pct": round(initial_stop_loss_pct * 1.2, 4),
            "action": "freeze_reentry",
            "label": "hard_stop_band",
        },
    ]

    position_policy = {
        "budget_mode": "fixed_fraction",
        "base_budget_pct": 0.04,
        "max_position_pct": round(max_position_pct, 4),
        "max_concurrent_positions": 2 if family in {"trend", "slow_factor"} else 1,
        "scale_in": {"enabled": False, "mode": "forbid"},
        "scale_out": {
            "enabled": family == "slow_factor",
            "mode": "reduce_then_exit" if family == "slow_factor" else "take_profit_or_trailing",
        },
    }

    if family == "slow_factor":
        time_stop_days = max(time_stop_days, 42)
        position_policy["base_budget_pct"] = 0.05
    elif family == "trend" and profile:
        annual_volatility = _instrument_profile_metric(
            profile,
            "annual_volatility_realized_252d",
            "annual_volatility",
            default=0.3,
            minimum=0.12,
            maximum=0.8,
        )
        gap_p95 = _instrument_profile_metric(
            profile,
            "gap_p95_realized",
            "gap_p95",
            default=0.03,
            minimum=0.005,
            maximum=0.15,
        )
        if annual_volatility >= 0.4 or gap_p95 >= 0.045:
            position_policy["max_position_pct"] = round(min(float(position_policy["max_position_pct"]), 0.22), 4)
        position_policy["base_budget_pct"] = round(min(max(float(position_policy.get("base_budget_pct") or 0.04), 0.04), 0.06), 4)

    metrics = dict(backtest_metrics or {})
    observed_trade_count = max(
        _safe_float(metrics.get("trade_count"), 0.0),
        _safe_float(metrics.get("trades_count"), 0.0),
        _safe_float(metrics.get("total_trades"), 0.0),
    )
    max_days = max(8, _safe_int(holding.get("max_days"), time_stop_days) or time_stop_days or 20)
    expected_trade_count = observed_trade_count if observed_trade_count > 0 else max(4.0, min(12.0, 252.0 / float(max_days)))
    warmup_target_signals = max(4, min(8, int(round(expected_trade_count / 2.5)) or 4))
    warmup_soft_timeout_days = max(5, min(18, int(round(max(5, warmup_target_signals * 2.0)))))
    warmup_hard_timeout_days = max(20, min(45, int(round(max(20, warmup_target_signals * 5.0)))))
    incubation_policy = {
        "warmup_target_signals": warmup_target_signals,
        "warmup_soft_timeout_days": warmup_soft_timeout_days,
        "warmup_hard_timeout_days": warmup_hard_timeout_days,
        "warmup_max_days": max(30, min(60, int(round(max(warmup_hard_timeout_days + 10, warmup_soft_timeout_days + 15))))),
    }
    if family == "trend":
        incubation_policy = _trend_runtime_warmup_policy(
            holding_horizon=holding,
            backtest_metrics=backtest_metrics,
        )
    return {
        "entry_policy": {
            "order_style": "marketable_limit",
            "signal_validity_days": max(1, min(5, max(1, time_stop_days // 5))),
            "max_slippage_bps": round(max_slippage_bps, 4),
            "tradability_guard": bool(
                execution.get("tradability_filter")
                if execution.get("tradability_filter") is not None
                else True
            ),
            "volume_confirmation": (
                {
                    "mode": "profile_percentile_or_scaled",
                    "volume_ratio_floor": round(
                        max(
                            1.05,
                            min(
                                1.8,
                                _instrument_profile_metric(
                                    profile,
                                    "volume_ratio_p80",
                                    "volume_ratio_p90",
                                    default=1.0
                                    + _instrument_profile_metric(
                                        profile,
                                        "atr14_pct_realized",
                                        "atr14_pct",
                                        default=0.03,
                                        minimum=0.01,
                                        maximum=0.12,
                                    )
                                    * 4.5,
                                    minimum=1.0,
                                    maximum=2.5,
                                ),
                            ),
                        ),
                        4,
                    ),
                    "turnover_rate_floor": round(
                        max(
                            1.02,
                            min(
                                1.7,
                                _instrument_profile_metric(
                                    profile,
                                    "turnover_rate_p80",
                                    "turnover_rate_p90",
                                    default=1.0
                                    + _instrument_profile_metric(
                                        profile,
                                        "gap_p95_realized",
                                        "gap_p95",
                                        default=0.03,
                                        minimum=0.005,
                                        maximum=0.15,
                                    )
                                    * 3.0,
                                    minimum=0.5,
                                    maximum=4.0,
                                ),
                            ),
                        ),
                        4,
                    ),
                }
                if family == "trend" and profile
                else None
            ),
        },
        "exit_policy": {
            "initial_stop_loss_pct": round(initial_stop_loss_pct, 4),
            "stop_loss_mode": stop_loss_mode,
            "atr_window": atr_window,
            "atr_multiplier": round(atr_multiplier, 4),
            "stop_floor_pct": round(stop_floor_pct, 4),
            "stop_rule_source": stop_rule_source,
            "take_profit_pct": round(take_profit_pct, 4),
            "trailing_stop_pct": trailing_stop_pct,
            "trailing_activation_profit_pct": trailing_activation_profit_pct,
            "time_stop_days": time_stop_days,
            "failure_exit_rule": failure_exit_rule,
        },
        "adverse_move_policy": {
            "loss_bands": loss_bands,
            "average_down": "forbid",
            "freeze_after_stop": True,
            "reduce_on_drawdown": family == "slow_factor",
        },
        "reentry_policy": {
            "cooldown_days": cooldown_days,
            "reclaim_condition": (
                "reclaim_fast_ma_and_break_recent_high"
                if family == "trend"
                else "recover_rank_and_trend_alignment"
                if family == "slow_factor"
                else "signal_reconfirm_after_cooldown"
            ),
            "max_retries_per_20d": 1 if family == "slow_factor" else 2,
        },
        "cooldown_by_exit_reason": {
            "time_stop": max(1, cooldown_days // 2),
            "dsl_exit": max(1, cooldown_days // 2),
            "signal_failure_exit": cooldown_days,
            "stop_loss": cooldown_days,
            "trailing_stop": max(1, cooldown_days // 2),
            "take_profit": max(1, cooldown_days // 3),
            "freeze_reentry": max(cooldown_days, cooldown_days + 2),
            "shock_exit": max(cooldown_days, cooldown_days + 4),
            "gap_through_stop": max(cooldown_days, cooldown_days + 4),
        },
        "stop_execution_mode": "gap_aware_ohlc" if family == "trend" else "close_confirmed_only",
        "position_policy": position_policy,
        "incubation_policy": incubation_policy,
    }


def _build_regime_filter_contract(
    strategy_type: str,
    *,
    market_regime_assumption: Optional[dict[str, Any]],
    instrument_profile: Optional[dict[str, Any]],
    runtime_playbook: Optional[dict[str, Any]],
) -> dict[str, Any]:
    strategy_family = _runtime_playbook_family(strategy_type)
    regime_payload = dict(market_regime_assumption or {})
    profile = dict(instrument_profile or {})
    entry_policy = dict(dict(runtime_playbook or {}).get("entry_policy") or {})
    filters: list[dict[str, Any]] = []
    trend_efficiency_floor = round(
        max(
            0.18,
            min(
                0.45,
                _instrument_profile_metric(
                    profile,
                    "trend_efficiency_60d_realized",
                    "trend_efficiency_60d",
                    default=0.28,
                    minimum=0.0,
                    maximum=0.9,
                ) * 0.9,
            ),
        ),
        4,
    )
    if strategy_family == "trend":
        filters.append(
            {
                "metric": "trend_efficiency_60d_realized",
                "op": "gte",
                "value": trend_efficiency_floor,
                "reason": "trend_family_requires_persistent_directionality",
            }
        )
        volume_confirmation = dict(entry_policy.get("volume_confirmation") or {})
        if volume_confirmation:
            if volume_confirmation.get("volume_ratio_floor") not in _EMPTY_VALUES:
                filters.append(
                    {
                        "metric": "volume_ratio",
                        "op": "gte",
                        "value": round(_safe_float(volume_confirmation.get("volume_ratio_floor"), 1.05), 4),
                        "reason": "entry_requires_participation_confirmation",
                    }
                )
            if volume_confirmation.get("turnover_rate_floor") not in _EMPTY_VALUES:
                filters.append(
                    {
                        "metric": "turnover_rate",
                        "op": "gte",
                        "value": round(_safe_float(volume_confirmation.get("turnover_rate_floor"), 1.02), 4),
                        "reason": "entry_requires_turnover_confirmation",
                    }
                )
        filters.append(
            {
                "metric": "anti_chop_cross_count_12d",
                "op": "lt",
                "value": 3,
                "reason": "avoid_repeated_short_long_cross_chop",
            }
        )
    return {
        "family": strategy_family,
        "preferred_regime": str(regime_payload.get("preferred_regime") or "").strip() or None,
        "avoid_regime": str(regime_payload.get("avoid_regime") or "").strip() or None,
        "summary": str(regime_payload.get("summary") or "").strip() or None,
        "quantified": bool(filters),
        "filters": filters,
        "measurement_source": str(profile.get("measurement_source") or "default_board_profile"),
    }


def _build_drawdown_invalidation_contract(
    strategy_type: str,
    *,
    instrument_profile: Optional[dict[str, Any]],
    runtime_playbook: Optional[dict[str, Any]],
    target_symbols: list[str],
) -> dict[str, Any]:
    profile = dict(instrument_profile or {})
    exit_policy = dict(dict(runtime_playbook or {}).get("exit_policy") or {})
    annual_volatility = _instrument_profile_metric(
        profile,
        "annual_volatility_realized_252d",
        "annual_volatility",
        default=0.3,
        minimum=0.12,
        maximum=0.8,
    )
    initial_stop = abs(_safe_float(exit_policy.get("initial_stop_loss_pct"), 0.08))
    review_threshold = round(max(0.10, min(0.22, max(initial_stop * 2.0, annual_volatility * 0.38))), 4)
    kill_threshold = round(max(review_threshold + 0.04, min(0.32, max(initial_stop * 2.8, annual_volatility * 0.55))), 4)
    applies_as_hard_gate = strategy_type in _TREND_EXECUTABLE_DSL_TYPES and len(target_symbols or []) <= 1
    return {
        "review_drawdown_pct": review_threshold,
        "kill_drawdown_pct": kill_threshold,
        "apply_as_hard_gate": applies_as_hard_gate,
        "stage_action": {
            "review_threshold": "forced_review",
            "kill_threshold": "kill_switch",
        },
        "measurement_source": str(profile.get("measurement_source") or "default_board_profile"),
    }


def _build_thesis_invalidation_contract(
    strategy_type: str,
    *,
    trade_plan: Optional[dict[str, Any]],
    runtime_playbook: Optional[dict[str, Any]],
    instrument_profile: Optional[dict[str, Any]],
    drawdown_invalidation_contract: Optional[dict[str, Any]],
) -> dict[str, Any]:
    playbook = dict(runtime_playbook or {})
    exit_policy = dict(playbook.get("exit_policy") or {})
    adverse_move_policy = dict(playbook.get("adverse_move_policy") or {})
    profile = dict(instrument_profile or {})
    invalidates_when: list[dict[str, Any]] = []
    invalidates_when.append(
        {
            "reason": "signal_failure_exit",
            "trigger": str(exit_policy.get("failure_exit_rule") or "signal_or_time_stop"),
            "source": "runtime_playbook.exit_policy.failure_exit_rule",
        }
    )
    if adverse_move_policy.get("freeze_after_stop") is not None:
        invalidates_when.append(
            {
                "reason": "adverse_move_exit",
                "trigger": "freeze_after_stop" if adverse_move_policy.get("freeze_after_stop") else "adverse_move_exit",
                "source": "runtime_playbook.adverse_move_policy.freeze_after_stop",
            }
        )
    if strategy_type in _TREND_EXECUTABLE_DSL_TYPES:
        invalidates_when.append(
            {
                "reason": "trend_efficiency_break",
                "metric": "trend_efficiency_60d_realized",
                "op": "lt",
                "value": round(
                    max(
                        0.12,
                        min(
                            0.28,
                            _instrument_profile_metric(
                                profile,
                                "trend_efficiency_60d_realized",
                                "trend_efficiency_60d",
                                default=0.24,
                                minimum=0.0,
                                maximum=0.9,
                            ) * 0.75,
                        ),
                    ),
                    4,
                ),
                "source": "instrument_profile",
            }
        )
    return {
        "strategy_family": _runtime_playbook_family(strategy_type),
        "trade_plan_entry": dict((trade_plan or {}).get("entry") or {}),
        "invalidates_when": invalidates_when,
        "drawdown_linked": dict(drawdown_invalidation_contract or {}),
    }

# --- runtime_contracts_parts/runtime_contract_builders.py ---
def _build_parameter_coherence_audit(
    strategy_type: str,
    *,
    holding_horizon: Optional[dict[str, Any]],
    rebalance_rule: Optional[dict[str, Any]],
    runtime_playbook: Optional[dict[str, Any]],
    instrument_profile: Optional[dict[str, Any]],
    backtest_metrics: Optional[dict[str, Any]],
) -> dict[str, Any]:
    holding = dict(holding_horizon or {})
    rebalance = dict(rebalance_rule or {})
    playbook = dict(runtime_playbook or {})
    entry_policy = dict(playbook.get("entry_policy") or {})
    exit_policy = dict(playbook.get("exit_policy") or {})
    reentry_policy = dict(playbook.get("reentry_policy") or {})
    incubation_policy = dict(playbook.get("incubation_policy") or {})
    profile = dict(instrument_profile or {})
    metrics = dict(backtest_metrics or {})
    issues: list[dict[str, Any]] = []

    atr14_pct = _instrument_profile_metric(
        profile,
        "atr14_pct_realized",
        "atr14_pct",
        default=0.03,
        minimum=0.01,
        maximum=0.12,
    )
    initial_stop = abs(_safe_float(exit_policy.get("initial_stop_loss_pct"), 0.0))
    if initial_stop > 0:
        stop_vs_atr = round(initial_stop / max(atr14_pct, 1e-6), 4)
        if stop_vs_atr < 1.2:
            issues.append(
                {
                    "code": "stop_vs_atr_too_tight",
                    "severity": "blocker",
                    "message": "initial stop loss is tighter than 1.2x ATR for the measured instrument profile",
                    "metric": "stop_vs_atr",
                    "value": stop_vs_atr,
                }
            )
        elif stop_vs_atr > 4.5:
            issues.append(
                {
                    "code": "stop_vs_atr_too_loose",
                    "severity": "warning",
                    "message": "initial stop loss is looser than 4.5x ATR and may dilute thesis invalidation timing",
                    "metric": "stop_vs_atr",
                    "value": stop_vs_atr,
                }
            )

    max_holding_days = max(
        _safe_int(holding.get("max_days"), 0),
        _safe_int(exit_policy.get("time_stop_days"), 0),
    )
    rebalance_interval_days = _safe_int(
        rebalance.get("frequency_days") or rebalance.get("rebalance_interval_days"),
        0,
    )
    if rebalance_interval_days > 0 and max_holding_days > 0 and rebalance_interval_days > max_holding_days:
        issues.append(
            {
                "code": "holding_horizon_shorter_than_rebalance_interval",
                "severity": "blocker",
                "message": "rebalance interval exceeds maximum holding horizon",
                "metric": "rebalance_interval_days",
                "value": rebalance_interval_days,
            }
        )

    observed_trade_count = max(
        _safe_float(metrics.get("trade_count"), 0.0),
        _safe_float(metrics.get("trades_count"), 0.0),
        _safe_float(metrics.get("total_trades"), 0.0),
    )
    expected_trade_interval_days = (
        round(252.0 / observed_trade_count, 2)
        if observed_trade_count > 0
        else float(max(6, max_holding_days or 20))
    )
    cooldown_days = _safe_int(reentry_policy.get("cooldown_days"), 0)
    if cooldown_days > 0 and expected_trade_interval_days > 0 and cooldown_days > expected_trade_interval_days * 1.5:
        issues.append(
            {
                "code": "cooldown_exceeds_expected_trade_interval",
                "severity": "warning",
                "message": "cooldown is materially longer than expected trade interval and may suppress re-entry evidence accumulation",
                "metric": "cooldown_days",
                "value": cooldown_days,
            }
        )

    warmup_target_signals = _safe_int(incubation_policy.get("warmup_target_signals"), 0)
    expected_annual_signals = max(1.0, observed_trade_count or round(252.0 / max(expected_trade_interval_days, 1.0), 2))
    if warmup_target_signals > max(8, expected_annual_signals * 1.25):
        issues.append(
            {
                "code": "warmup_target_exceeds_signal_density",
                "severity": "blocker" if strategy_type in _TREND_EXECUTABLE_DSL_TYPES else "warning",
                "message": "warmup target signals exceeds expected annual signal density and may deadlock incubation",
                "metric": "warmup_target_signals",
                "value": warmup_target_signals,
            }
        )

    volume_confirmation = dict(entry_policy.get("volume_confirmation") or {})
    volume_ratio_floor = _safe_float(volume_confirmation.get("volume_ratio_floor"), 0.0)
    volume_ratio_p90 = _instrument_profile_metric(
        profile,
        "volume_ratio_p90",
        default=1.18,
        minimum=1.0,
        maximum=3.0,
    )
    if volume_ratio_floor > 0 and volume_ratio_floor > volume_ratio_p90 + 0.05:
        issues.append(
            {
                "code": "volume_filter_exceeds_observed_distribution",
                "severity": "warning",
                "message": "volume confirmation floor is above observed p90 and may create signal vacuum",
                "metric": "volume_ratio_floor",
                "value": volume_ratio_floor,
            }
        )

    trade_density = _safe_float(metrics.get("trade_density"), 0.0)
    implementation_shortfall_proxy = _safe_float(metrics.get("implementation_shortfall_proxy"), 0.0)
    if trade_density > 0 and implementation_shortfall_proxy > 0 and trade_density * implementation_shortfall_proxy > 0.12:
        issues.append(
            {
                "code": "trade_density_cost_pressure_high",
                "severity": "warning",
                "message": "expected trade density and implementation shortfall imply elevated round-trip cost drag",
                "metric": "trade_density_cost_pressure",
                "value": round(trade_density * implementation_shortfall_proxy, 4),
            }
        )

    blockers = [issue["code"] for issue in issues if issue.get("severity") == "blocker"]
    warnings = [issue["code"] for issue in issues if issue.get("severity") == "warning"]
    return {
        "status": "failed" if blockers else "passed_with_warnings" if warnings else "passed",
        "issues": issues,
        "blockers": blockers,
        "warnings": warnings,
        "metrics": {
            "stop_vs_atr": round(initial_stop / max(atr14_pct, 1e-6), 4) if initial_stop > 0 else None,
            "expected_trade_interval_days": expected_trade_interval_days,
            "warmup_target_signals": warmup_target_signals or None,
            "expected_annual_signals": round(expected_annual_signals, 2),
            "volume_ratio_floor": round(volume_ratio_floor, 4) if volume_ratio_floor > 0 else None,
            "volume_ratio_p90": round(volume_ratio_p90, 4),
        },
    }


def _resolve_source_label(*labeled_values: tuple[str, Any]) -> str:
    for label, value in labeled_values:
        if isinstance(value, dict) and value:
            return label
        if isinstance(value, (list, tuple, set)) and value:
            return label
        if value not in (None, "", [], {}):
            return label
    return "default"


def _normalize_symbol_tokens(*values: Any) -> list[str]:
    tokens: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                token = str(item or "").strip()
                if token:
                    tokens.append(token)
        else:
            token = str(value or "").strip()
            if token:
                tokens.append(token)
    return list(dict.fromkeys(tokens))


def _merge_contract_payloads(*payloads: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            token = str(key or "").strip()
            if not token:
                continue
            if isinstance(value, dict) and isinstance(merged.get(token), dict):
                merged[token] = _merge_contract_payloads(merged[token], value)
            elif isinstance(value, dict):
                merged[token] = _merge_contract_payloads(value)
            elif isinstance(value, list):
                merged[token] = list(value)
            else:
                merged[token] = value
    return merged


def _resolve_nonmissing_field_provenance(source: Any) -> str:
    token = normalize_field_provenance_token(source)
    return token if token != "missing" else "derived"


def _should_use_510300_default_research_protocol(
    *,
    strategy_type: str,
    target_symbols: list[str],
    research_task: Optional[dict[str, Any]] = None,
    candidate_family: Any = None,
) -> bool:
    del strategy_type
    symbol_tokens = {token.lower() for token in _normalize_symbol_tokens(target_symbols)}
    if symbol_tokens.intersection({"510300", "159919", "510310"}):
        return True
    task = dict(research_task or {})
    family_tokens = {
        str(candidate_family or "").strip().lower(),
        str(task.get("candidate_family") or "").strip().lower(),
        str(task.get("family") or "").strip().lower(),
    }
    return any("510300" in token for token in family_tokens if token)


def _generic_default_research_validation_contract_payload(
    *,
    validation_profile: Optional[dict[str, Any]] = None,
    family: str | None = None,
    holding_bucket: str = "medium",
) -> dict[str, Any]:
    return {
        "contract_version": "strategy_factory.research_protocol.v2",
        "walk_forward_config": {"train_months": 60, "test_months": 12, "step_months": 12},
        "baseline_reference": {
            "name": "cn_equity_generic_baseline",
            "baseline_slippage_bps": 5.0,
            "stress_slippage_bps": 10.0,
        },
        "cash_sleeve_policy": {
            "enabled": False,
            "schedule_clock": "prev_close_signal_next_open_execute_same_close_cash_rebuild",
        },
        "cost_sensitivity_grid": {
            "base_slippage_bps": 5.0,
            "stress_slippage_bps": 10.0,
            "main_scenario_slippage_bps": 5.0,
            "control_scenario_slippage_bps": 0.0,
        },
        "capacity_execution": {
            "schedule_clock": "prev_close_signal_next_open_execute_same_close_cash_rebuild",
        },
        "multiple_testing": {
            "mode": "formal_runtime",
            "white_reality_check_enabled": True,
            "hansen_spa_enabled": True,
            "pbo_enabled": True,
        },
        "admission_thresholds": {
            "validation_profile": dict(validation_profile or {}),
        },
        "family_holding_bucket": {
            "family": str(family or "default").strip().lower() or "default",
            "holding_bucket": str(holding_bucket or "medium").strip().lower() or "medium",
            "enable_enhancements": False,
            "enforce_family_alignment": False,
            "enforce_holding_bucket_alignment": False,
        },
    }


def _default_research_validation_contract_payload(
    *,
    strategy_type: str,
    target_symbols: list[str],
    research_task: Optional[dict[str, Any]] = None,
    validation_profile: Optional[dict[str, Any]] = None,
    candidate_family: Any = None,
    holding_bucket: str = "medium",
) -> dict[str, Any]:
    fallback_reason: Optional[str] = None
    if _should_use_510300_default_research_protocol(
        strategy_type=strategy_type,
        target_symbols=target_symbols,
        research_task=research_task,
        candidate_family=candidate_family,
    ):
        try:
            # `from .research_510300_v3` resolved to a sibling inside
            # `strategy_spec/`, where no such module exists — the implementation
            # lives one level up, in `services/`. So this import always failed,
            # the bare `except Exception: pass` swallowed it, and the generic
            # protocol was always substituted: the 510300 branch was dead and its
            # business-admission requirements were never applied.
            from ..research_510300_v3 import build_default_research_validation_contract

            payload = dict(build_default_research_validation_contract() or {})
            payload.setdefault("research_protocol_source", "510300_default")
            return payload
        except Exception as exc:
            fallback_reason = f"{type(exc).__name__}: {exc}"
    payload = _generic_default_research_validation_contract_payload(
        validation_profile=validation_profile,
        family=str(candidate_family or "").strip().lower() or str(strategy_type or "").strip().lower(),
        holding_bucket=holding_bucket,
    )
    # A silent fallback is indistinguishable from a deliberate choice, so the
    # caller is told which protocol it received and why.
    payload.setdefault("research_protocol_source", "generic_fallback")
    if fallback_reason:
        payload.setdefault("research_protocol_fallback_reason", fallback_reason)
    return payload


def _classify_holding_bucket(holding_horizon: dict[str, Any]) -> str:
    max_days = _safe_int(dict(holding_horizon or {}).get("max_days"), 0)
    if max_days <= 5:
        return "short"
    if max_days <= 15:
        return "medium"
    return "long"


def _trade_plan_nodes_for_provenance(trade_plan: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def _append(node: Any, *, phase: str, index: int = 0) -> None:
        if not isinstance(node, dict):
            return
        node_id = str(
            node.get("node_id")
            or node.get("plan_node_id")
            or node.get("trade_plan_node_id")
            or node.get("trade_plan_step_id")
            or node.get("id")
            or f"{phase}_{index}"
        ).strip()
        claim_ids = [
            str(item).strip()
            for item in list(node.get("claim_ids") or [])
            if str(item).strip()
        ]
        nodes.append(
            {
                **node,
                "node_id": node_id,
                "phase": phase,
                "claim_ids": list(dict.fromkeys(claim_ids)),
            }
        )

    payload = dict(trade_plan or {})
    if isinstance(payload.get("entry"), dict):
        _append(payload.get("entry"), phase="entry")
    if isinstance(payload.get("exit"), dict):
        _append(payload.get("exit"), phase="exit")
    for phase_name in ("entries", "exits", "nodes", "steps"):
        for index, item in enumerate(list(payload.get(phase_name) or [])):
            resolved_phase = "entry" if phase_name == "entries" else "exit" if phase_name == "exits" else phase_name
            _append(item, phase=resolved_phase, index=index)
    if not nodes and payload:
        _append(payload, phase="node")
    return nodes


def _claim_ids_from_prediction_contract(prediction_contract: dict[str, Any]) -> list[str]:
    claim_ids: list[str] = []
    for item in list(dict(prediction_contract or {}).get("claims") or []):
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get("claim_id") or item.get("id") or "").strip()
        if claim_id:
            claim_ids.append(claim_id)
    return list(dict.fromkeys(claim_ids))


def _trade_step_ids_from_trade_plan_to_dsl_map(trade_plan_to_dsl_map: dict[str, Any]) -> list[str]:
    step_ids: list[str] = []
    for key in (
        "trade_step_to_dsl_sections",
        "trade_step_to_claim_ids",
    ):
        payload = dict(trade_plan_to_dsl_map.get(key) or {})
        step_ids.extend(str(item).strip() for item in payload.keys() if str(item).strip())
    return list(dict.fromkeys(step_ids))


def _claim_ids_from_claim_map(claim_to_trade_plan_map: dict[str, Any]) -> list[str]:
    payload = dict(claim_to_trade_plan_map.get("claim_to_trade_step_ids") or {})
    return list(dict.fromkeys(str(item).strip() for item in payload.keys() if str(item).strip()))


def _enrich_runtime_playbook_provenance(
    runtime_playbook: dict[str, Any],
    *,
    strategy_type: str,
    prediction_contract: dict[str, Any],
    trade_plan: dict[str, Any],
    claim_to_trade_plan_map: dict[str, Any],
    trade_plan_to_dsl_map: dict[str, Any],
    source_priority: dict[str, str],
    runtime_playbook_source: str,
) -> dict[str, Any]:
    playbook = dict(runtime_playbook or {})
    existing_provenance = dict(playbook.get("_provenance") or {})
    trade_plan_nodes = _trade_plan_nodes_for_provenance(trade_plan)
    derived_claim_ids = list(
        dict.fromkeys(
            [
                *_claim_ids_from_prediction_contract(prediction_contract),
                *_claim_ids_from_claim_map(claim_to_trade_plan_map),
                *[
                    str(claim_id).strip()
                    for node in trade_plan_nodes
                    for claim_id in list(node.get("claim_ids") or [])
                    if str(claim_id).strip()
                ],
            ]
        )
    )
    derived_trade_step_ids = list(
        dict.fromkeys(
            [
                *[
                    str(node.get("node_id") or "").strip()
                    for node in trade_plan_nodes
                    if str(node.get("node_id") or "").strip()
                ],
                *_trade_step_ids_from_trade_plan_to_dsl_map(trade_plan_to_dsl_map),
            ]
        )
    )
    source_claim_ids = list(
        dict.fromkeys(
            [
                *[
                    str(item).strip()
                    for item in list(playbook.get("source_claim_ids") or [])
                    if str(item).strip()
                ],
                *[
                    str(item).strip()
                    for item in list(existing_provenance.get("source_claim_ids") or [])
                    if str(item).strip()
                ],
                *derived_claim_ids,
            ]
        )
    )
    source_trade_step_ids = list(
        dict.fromkeys(
            [
                *[
                    str(item).strip()
                    for item in list(playbook.get("source_trade_step_ids") or [])
                    if str(item).strip()
                ],
                *[
                    str(item).strip()
                    for item in list(existing_provenance.get("source_trade_step_ids") or [])
                    if str(item).strip()
                ],
                *derived_trade_step_ids,
            ]
        )
    )
    derived_from_defaults = bool(
        existing_provenance.get("derived_from_defaults")
        if existing_provenance.get("derived_from_defaults") is not None
        else playbook.get("derived_from_defaults")
        if playbook.get("derived_from_defaults") is not None
        else runtime_playbook_source == "default"
    )
    family_label = _runtime_playbook_family(strategy_type)
    derivation_labels = list(
        dict.fromkeys(
            [
                *[
                    str(item).strip()
                    for item in list(playbook.get("derivation_labels") or [])
                    if str(item).strip()
                ],
                *[
                    str(item).strip()
                    for item in list(existing_provenance.get("derivation_labels") or [])
                    if str(item).strip()
                ],
                "default_runtime_playbook" if derived_from_defaults else "runtime_playbook_provided",
                "trade_plan_driven" if source_trade_step_ids else "trade_plan_missing",
                "claim_linked" if source_claim_ids else "claim_mapping_missing",
                f"family_template:{family_label}",
            ]
        )
    )
    provenance = {
        **existing_provenance,
        "source_claim_ids": source_claim_ids,
        "source_trade_step_ids": source_trade_step_ids,
        "derived_from_defaults": derived_from_defaults,
        "derivation_labels": derivation_labels,
        "source_priority": dict(source_priority),
        "runtime_playbook_source": runtime_playbook_source,
    }
    return {
        **playbook,
        "source_claim_ids": source_claim_ids,
        "source_trade_step_ids": source_trade_step_ids,
        "derived_from_defaults": derived_from_defaults,
        "derivation_labels": derivation_labels,
        "_provenance": provenance,
    }


def _default_validation_profile(
    strategy_type: str,
    research_task: dict[str, Any],
    task_source: str,
) -> dict[str, Any]:
    default_focus = (
        "candidate_target_only"
        if strategy_type == "event_structure_breakout"
        else "event_target_only"
        if task_source == "event_driven"
        else "candidate_target_only" if strategy_type == "quality_factor"
        else "target_plus_representative"
    )
    validation_focus = str(
        research_task.get("validation_focus") or default_focus
    ).strip().lower()
    if strategy_type == "quality_factor" and validation_focus in {
        "candidate_target_only",
        "target_only",
        "target_plus_family_peer",
    }:
        profile = "trade_rule_validation"
    elif strategy_type in _FACTOR_VALIDATION_TYPES:
        profile = "factor_rank_validation"
    elif strategy_type == "macro_timing":
        profile = "macro_regime_validation"
    elif strategy_type == "event_structure_breakout" or task_source == "event_driven" or validation_focus in {"event_target_only", "candidate_target_only"}:
        profile = "event_trade_validation"
    else:
        profile = "trade_rule_validation"
    profile_payload = {
        "profile": profile,
        "validation_focus": validation_focus,
        "primary_validation_layer": "target" if validation_focus in {"event_target_only", "candidate_target_only", "target_only"} else "combined",
    }
    if strategy_type == "event_structure_breakout":
        profile_payload.update(
            {
                "objective_profile": "high_precision",
                "trade_density_preference": "low",
                "regime_required": True,
                "cost_robust_required": True,
                "entry_selectivity": "strict_event_breakout",
                "preferred_regime": "event_follow_through_with_structure_confirmation",
                "avoid_regime": "false_breakout_or_post_event_mean_reversion",
                "event_prefilter_required": True,
                "event_prefilter_profile": "announcement_flow_sector_v1",
                "event_prefilter_min_confirmations": 1,
            }
        )
    return profile_payload

