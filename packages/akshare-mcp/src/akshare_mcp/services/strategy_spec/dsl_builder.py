"""DSL and execution-semantic builders for strategy specs."""

from __future__ import annotations

from typing import Any

from . import constants as _constants
from . import defaults as _defaults
from . import normalizers as _normalizers

for _module in (
    _constants,
    _normalizers,
    _defaults,
):
    globals().update(
        {
            name: getattr(_module, name)
            for name in dir(_module)
            if not name.startswith("__")
        }
    )

def _trend_strategy_requires_compiled_dsl(strategy_type: str, target_symbols: list[str]) -> bool:
    return str(strategy_type or "").strip().lower() in _TREND_EXECUTABLE_DSL_TYPES and len(list(target_symbols or [])) == 1


def _has_nonempty_mapping(value: Any) -> bool:
    return isinstance(value, dict) and bool(value)


def _semantic_contract_missing_fields(
    strategy_type: str,
    *,
    evidence_chain: Optional[dict[str, Any]],
    prediction_contract: Optional[dict[str, Any]],
    confidence_contract: Optional[dict[str, Any]],
) -> list[str]:
    normalized_strategy_type = str(strategy_type or "").strip().lower()
    if normalized_strategy_type not in (_TREND_EXECUTABLE_DSL_TYPES | _PROXY_RUNTIME_FACTOR_TYPES):
        return []
    missing: list[str] = []
    if not _has_nonempty_mapping(evidence_chain):
        missing.append("evidence_chain")
    if not _has_nonempty_mapping(prediction_contract):
        missing.append("prediction_contract")
    if not _has_nonempty_mapping(confidence_contract):
        missing.append("confidence_contract")
    return missing


def _has_true_fundamental_runtime(
    strategy_type: str,
    *,
    params: Optional[dict[str, Any]] = None,
    runtime_playbook: Optional[dict[str, Any]] = None,
) -> bool:
    normalized_strategy_type = str(strategy_type or "").strip().lower()
    if normalized_strategy_type not in _PROXY_RUNTIME_FACTOR_TYPES:
        return False
    payload = dict(params or {})
    playbook = dict(runtime_playbook or {})
    explicit_mode = str(
        payload.get("runtime_family_data_source")
        or playbook.get("runtime_family_data_source")
        or payload.get("factor_runtime_mode")
        or ""
    ).strip().lower()
    if explicit_mode in {"fundamental_runtime", "fundamental_cached_runtime", "fundamental"}:
        return True
    factor_runtime_contract = dict(
        payload.get("fundamental_runtime_contract")
        or payload.get("factor_runtime_contract")
        or {}
    )
    return bool(factor_runtime_contract)


def _resolve_runtime_semantic_diagnostics(
    *,
    strategy_type: str,
    params: Optional[dict[str, Any]],
    target_symbols: list[str],
    instrument_profile: Optional[dict[str, Any]],
    runtime_playbook: Optional[dict[str, Any]],
    evidence_chain: Optional[dict[str, Any]],
    prediction_contract: Optional[dict[str, Any]],
    confidence_contract: Optional[dict[str, Any]],
    execution_semantic_contract: Optional[dict[str, Any]],
) -> dict[str, Any]:
    normalized_strategy_type = str(strategy_type or "").strip().lower()
    profile = dict(instrument_profile or {})
    playbook = dict(runtime_playbook or {})
    execution_contract = dict(execution_semantic_contract or {})
    single_name_trend = _trend_strategy_requires_compiled_dsl(strategy_type, target_symbols)
    semantic_contract_missing_fields = _semantic_contract_missing_fields(
        normalized_strategy_type,
        evidence_chain=evidence_chain,
        prediction_contract=prediction_contract,
        confidence_contract=confidence_contract,
    )
    measurement_source = str(profile.get("measurement_source") or "default_board_profile").strip().lower() or "default_board_profile"
    measured_profile_complete = bool(profile.get("measured_profile_complete"))
    execution_semantic_mode = str(
        execution_contract.get("execution_semantic_mode")
        or dict(params or {}).get("execution_semantic_mode")
        or ""
    ).strip().lower()
    dsl_compiled = bool(
        execution_contract.get("dsl_compiled")
        or dict(params or {}).get("dsl_compiled")
        or dict(params or {}).get("dsl")
    )
    proxy_runtime_used = False
    runtime_family_data_source = "market_data_runtime"
    if normalized_strategy_type in _PROXY_RUNTIME_FACTOR_TYPES:
        runtime_family_data_source = (
            "fundamental_runtime"
            if _has_true_fundamental_runtime(
                normalized_strategy_type,
                params=params,
                runtime_playbook=playbook,
            )
            else "price_proxy_runtime"
        )
        proxy_runtime_used = runtime_family_data_source != "fundamental_runtime"

    semantic_runtime_match = True
    if normalized_strategy_type in _PROXY_RUNTIME_FACTOR_TYPES and proxy_runtime_used:
        semantic_runtime_match = False
    elif single_name_trend and (execution_semantic_mode != "compiled_dsl" or not dsl_compiled):
        semantic_runtime_match = False

    execution_semantic_gap_reasons = [
        str(item).strip()
        for item in list(execution_contract.get("execution_semantic_gap_reasons") or [])
        if str(item).strip()
    ]
    diagnostic_reasons: list[str] = []
    if semantic_contract_missing_fields:
        diagnostic_reasons.append("final_strategy_missing_semantic_contract")
    if normalized_strategy_type in _PROXY_RUNTIME_FACTOR_TYPES and proxy_runtime_used:
        diagnostic_reasons.extend(
            [
                "runtime_family_semantic_mismatch",
                "proxy_runtime_not_allowed_for_formal_incubation",
            ]
        )
    if single_name_trend and measurement_source == "default_board_profile":
        diagnostic_reasons.append("default_profile_not_allowed_for_single_name_runtime")
    elif single_name_trend and not measured_profile_complete:
        diagnostic_reasons.append("measured_profile_incomplete")

    merged_gap_reasons = list(dict.fromkeys([*execution_semantic_gap_reasons, *diagnostic_reasons]))
    diagnostic_only = bool(
        merged_gap_reasons
        or not semantic_runtime_match
    )
    if single_name_trend and (execution_semantic_mode != "compiled_dsl" or not dsl_compiled):
        execution_readiness_tier = "missing_executable_contract"
    elif diagnostic_only:
        execution_readiness_tier = "observe_diagnostic_only"
    else:
        execution_readiness_tier = "formal_runtime_ready"
    return {
        "semantic_runtime_match": semantic_runtime_match,
        "runtime_family_data_source": runtime_family_data_source,
        "proxy_runtime_used": proxy_runtime_used,
        "diagnostic_only": diagnostic_only,
        "execution_readiness_tier": execution_readiness_tier,
        "semantic_contract_missing_fields": semantic_contract_missing_fields,
        "execution_semantic_gap_reasons": merged_gap_reasons,
    }


def _ensure_trade_plan_execution_nodes(strategy_type: str, trade_plan: dict[str, Any]) -> dict[str, Any]:
    payload = dict(trade_plan or {})
    entry_bias = str(payload.get("entry_bias") or "").strip()
    exit_bias = str(payload.get("exit_bias") or "").strip()
    if not entry_bias or not exit_bias:
        defaults = _default_trade_plan(str(strategy_type or "").strip().lower(), "snapshot")
        entry_bias = entry_bias or str(defaults.get("entry_bias") or "").strip()
        exit_bias = exit_bias or str(defaults.get("exit_bias") or "").strip()
    entry_node = dict(payload.get("entry") or {})
    exit_node = dict(payload.get("exit") or {})
    entry_node.setdefault("node_id", "entry_step_1")
    entry_node.setdefault("phase", "entry")
    entry_node.setdefault("entry_bias", entry_bias or None)
    exit_node.setdefault("node_id", "exit_step_1")
    exit_node.setdefault("phase", "exit")
    exit_node.setdefault("exit_bias", exit_bias or None)
    payload["entry_bias"] = entry_bias or None
    payload["exit_bias"] = exit_bias or None
    payload["entry"] = entry_node
    payload["exit"] = exit_node
    return payload


def _trend_runtime_warmup_policy(
    *,
    holding_horizon: dict[str, Any],
    backtest_metrics: Optional[dict[str, Any]],
) -> dict[str, Any]:
    metrics = dict(backtest_metrics or {})
    observed_trade_count = max(
        _safe_float(metrics.get("trade_count"), 0.0),
        _safe_float(metrics.get("trades_count"), 0.0),
        _safe_float(metrics.get("total_trades"), 0.0),
    )
    max_days = max(8, _safe_int(dict(holding_horizon or {}).get("max_days"), 20) or 20)
    expected_trade_count = observed_trade_count if observed_trade_count > 0 else max(4.0, min(12.0, 252.0 / float(max_days)))
    warmup_target_signals = max(4, min(8, int(round(expected_trade_count / 2.5)) or 4))
    warmup_soft_timeout_days = max(5, min(18, int(round(max(5, warmup_target_signals * 2.0)))))
    warmup_hard_timeout_days = max(20, min(45, int(round(max(20, warmup_target_signals * 5.0)))))
    warmup_max_days = max(30, min(60, int(round(max(warmup_hard_timeout_days + 10, warmup_soft_timeout_days + 15)))))
    return {
        "warmup_target_signals": warmup_target_signals,
        "warmup_soft_timeout_days": warmup_soft_timeout_days,
        "warmup_hard_timeout_days": warmup_hard_timeout_days,
        "warmup_max_days": warmup_max_days,
    }


def _build_single_name_trend_dsl(
    strategy_type: str,
    *,
    params: dict[str, Any],
    trade_plan: dict[str, Any],
    holding_horizon: dict[str, Any],
    instrument_profile: dict[str, Any],
    risk_rules: dict[str, Any],
) -> dict[str, Any]:
    family = str(strategy_type or "").strip().lower()
    entry_node_id = str(dict(trade_plan.get("entry") or {}).get("node_id") or "entry_step_1").strip() or "entry_step_1"
    exit_node_id = str(dict(trade_plan.get("exit") or {}).get("node_id") or "exit_step_1").strip() or "exit_step_1"
    atr14_pct = _instrument_profile_metric(
        instrument_profile,
        "atr14_pct_realized",
        "atr14_pct",
        default=0.03,
        minimum=0.01,
        maximum=0.12,
    )
    trend_efficiency = _instrument_profile_metric(
        instrument_profile,
        "trend_efficiency_60d_realized",
        "trend_efficiency_60d",
        default=0.3,
        minimum=0.0,
        maximum=0.9,
    )
    gap_p95 = _instrument_profile_metric(
        instrument_profile,
        "gap_p95_realized",
        "gap_p95",
        default=0.03,
        minimum=0.005,
        maximum=0.15,
    )
    intraday_range_p90 = _instrument_profile_metric(
        instrument_profile,
        "intraday_range_p90",
        default=max(atr14_pct * 1.5, gap_p95),
        minimum=0.01,
        maximum=0.20,
    )
    volume_ratio_floor = round(
        max(
            1.05,
            min(
                1.8,
                _instrument_profile_metric(
                    instrument_profile,
                    "volume_ratio_p80",
                    "volume_ratio_p90",
                    default=1.0 + atr14_pct * 4.5,
                    minimum=1.0,
                    maximum=2.5,
                ),
            ),
        ),
        4,
    )
    turnover_rate_floor = round(
        max(
            1.02,
            min(
                1.7,
                _instrument_profile_metric(
                    instrument_profile,
                    "turnover_rate_p80",
                    "turnover_rate_p90",
                    default=1.0 + gap_p95 * 3.0,
                    minimum=0.5,
                    maximum=4.0,
                ),
            ),
        ),
        4,
    )
    adx_floor = round(max(16.0, min(30.0, 18.0 + max(0.0, (trend_efficiency - 0.2) * 25.0))), 4)
    upper_shadow_ratio_max = round(
        max(
            0.22,
            min(
                0.48,
                0.42 - max(0.0, trend_efficiency - 0.2) * 0.2 + max(0.0, intraday_range_p90 - atr14_pct) * 0.2,
            ),
        ),
        4,
    )
    anti_chop_count_threshold = 3.0
    long_shadow_window = 5
    max_days = max(5, _safe_int(holding_horizon.get("max_days"), 20) or 20)
    metadata = {
        "strategy_profile": {
            "family": family,
            "execution_semantic_mode": "compiled_dsl",
        },
        "target_symbols": list(params.get("target_symbols") or []),
        "holding_horizon": dict(holding_horizon or {}),
        "holding_horizon_days": max_days,
        "instrument_profile": dict(instrument_profile or {}),
    }

    def _tag(condition: dict[str, Any], node_id: str) -> dict[str, Any]:
        return {**condition, "trade_plan_node_id": node_id}

    if family == "ma_cross":
        short_period = max(3, _safe_int(params.get("short_period"), 5) or 5)
        long_period = max(short_period + 2, _safe_int(params.get("long_period"), 20) or 20)
        slope_lookback = max(3, min(10, long_period // 4 or 3))
        anti_chop_window = max(8, min(15, long_period // 2 or 8))
        entry = {
            "all": [
                _tag(
                    {
                        "op": "cross_above",
                        "left": {"indicator": "sma", "field": "close", "window": short_period},
                        "right": {"indicator": "sma", "field": "close", "window": long_period},
                    },
                    entry_node_id,
                ),
                _tag(
                    {
                        "op": "gt",
                        "left": {"indicator": "slope", "field": "close", "window": long_period, "lookback": slope_lookback},
                        "right": {"value": 0.0},
                    },
                    entry_node_id,
                ),
                _tag(
                    {
                        "op": "gte",
                        "left": {"indicator": "adx", "window": max(7, min(20, long_period // 2 or 7))},
                        "right": {"value": adx_floor},
                    },
                    entry_node_id,
                ),
                {
                    "any": [
                        _tag(
                            {
                                "op": "gte",
                                "left": {"indicator": "volume_ratio", "window": 20},
                                "right": {"value": volume_ratio_floor},
                            },
                            entry_node_id,
                        ),
                        _tag(
                            {
                                "op": "gte",
                                "left": {"indicator": "turnover_rate", "window": 20},
                                "right": {"value": turnover_rate_floor},
                            },
                            entry_node_id,
                        ),
                    ]
                },
                {
                    "not": _tag(
                        {
                            "op": "gte",
                            "left": {
                                "indicator": "rolling_count",
                                "window": long_shadow_window,
                                "condition": {
                                    "all": [
                                        {
                                            "op": "gte",
                                            "left": {"indicator": "upper_shadow_ratio"},
                                            "right": {"value": upper_shadow_ratio_max},
                                        },
                                        {
                                            "op": "gte",
                                            "left": {"indicator": "volume_ratio", "window": 20},
                                            "right": {"value": max(1.0, round(volume_ratio_floor - 0.1, 4))},
                                        },
                                    ]
                                },
                            },
                            "right": {"value": 1.0},
                        },
                        entry_node_id,
                    )
                },
                {
                    "not": _tag(
                        {
                            "op": "gte",
                            "left": {
                                "indicator": "rolling_count",
                                "window": anti_chop_window,
                                "condition": {
                                    "any": [
                                        {
                                            "op": "cross_above",
                                            "left": {"indicator": "sma", "field": "close", "window": short_period},
                                            "right": {"indicator": "sma", "field": "close", "window": long_period},
                                        },
                                        {
                                            "op": "cross_below",
                                            "left": {"indicator": "sma", "field": "close", "window": short_period},
                                            "right": {"indicator": "sma", "field": "close", "window": long_period},
                                        },
                                    ]
                                },
                            },
                            "right": {"value": anti_chop_count_threshold},
                        },
                        entry_node_id,
                    )
                },
            ]
        }
        exit_rule = {
            "any": [
                _tag(
                    {
                        "op": "cross_below",
                        "left": {"indicator": "sma", "field": "close", "window": short_period},
                        "right": {"indicator": "sma", "field": "close", "window": long_period},
                    },
                    exit_node_id,
                ),
                _tag(
                    {
                        "op": "lte",
                        "left": {"indicator": "slope", "field": "close", "window": long_period, "lookback": slope_lookback},
                        "right": {"value": 0.0},
                    },
                    exit_node_id,
                ),
                _tag(
                    {
                        "op": "lt",
                        "left": {"indicator": "roc", "field": "close", "window": max(3, short_period)},
                        "right": {"value": -round(max(0.02, min(0.12, atr14_pct * 1.2)), 4)},
                    },
                    exit_node_id,
                ),
            ]
        }
    elif family == "momentum":
        lookback = max(3, _safe_int(params.get("lookback") or params.get("period"), 8) or 8)
        threshold = round(max(0.01, min(0.15, abs(_safe_float(params.get("threshold"), 0.02)))), 4)
        trend_window = max(10, min(30, lookback * 2))
        entry = {
            "all": [
                _tag(
                    {
                        "op": "gt",
                        "left": {"indicator": "roc", "field": "close", "window": lookback},
                        "right": {"value": threshold},
                    },
                    entry_node_id,
                ),
                _tag(
                    {
                        "op": "gt",
                        "left": {"field": "close"},
                        "right": {"indicator": "sma", "field": "close", "window": trend_window},
                    },
                    entry_node_id,
                ),
                _tag(
                    {
                        "op": "gte",
                        "left": {"indicator": "adx", "window": max(7, min(20, trend_window // 2))},
                        "right": {"value": adx_floor},
                    },
                    entry_node_id,
                ),
                _tag(
                    {
                        "op": "gte",
                        "left": {"indicator": "volume_ratio", "window": 20},
                        "right": {"value": max(1.0, round(volume_ratio_floor - 0.05, 4))},
                    },
                    entry_node_id,
                ),
            ]
        }
        exit_rule = {
            "any": [
                _tag(
                    {
                        "op": "lt",
                        "left": {"indicator": "roc", "field": "close", "window": lookback},
                        "right": {"value": round(-threshold * 0.25, 4)},
                    },
                    exit_node_id,
                ),
                _tag(
                    {
                        "op": "lt",
                        "left": {"field": "close"},
                        "right": {"indicator": "sma", "field": "close", "window": trend_window},
                    },
                    exit_node_id,
                ),
                _tag(
                    {
                        "op": "lte",
                        "left": {"indicator": "slope", "field": "close", "window": trend_window, "lookback": max(3, lookback // 2)},
                        "right": {"value": 0.0},
                    },
                    exit_node_id,
                ),
            ]
        }
    else:
        breakout_window = max(10, _safe_int(params.get("breakout_window"), 20) or 20)
        trend_window = max(10, min(30, breakout_window))
        atr_multiple = round(max(0.5, min(2.0, 1.0 + atr14_pct * 10.0)), 4)
        entry = {
            "all": [
                _tag(
                    {
                        "op": "gte",
                        "left": {"field": "close"},
                        "right": {
                            "binary": {
                                "op": "sub",
                                "left": {"indicator": "highest", "field": "high", "window": breakout_window},
                                "right": {
                                    "binary": {
                                        "op": "mul",
                                        "left": {"indicator": "atr", "window": 14},
                                        "right": {"value": atr_multiple},
                                    }
                                },
                            }
                        },
                    },
                    entry_node_id,
                ),
                _tag(
                    {
                        "op": "gte",
                        "left": {"indicator": "adx", "window": max(7, min(20, breakout_window // 2))},
                        "right": {"value": adx_floor},
                    },
                    entry_node_id,
                ),
                _tag(
                    {
                        "op": "gte",
                        "left": {"indicator": "volume_ratio", "window": 20},
                        "right": {"value": volume_ratio_floor},
                    },
                    entry_node_id,
                ),
            ]
        }
        exit_rule = {
            "any": [
                _tag(
                    {
                        "op": "lt",
                        "left": {"field": "close"},
                        "right": {"indicator": "lowest", "field": "low", "window": max(5, breakout_window // 2)},
                    },
                    exit_node_id,
                ),
                _tag(
                    {
                        "op": "lt",
                        "left": {"field": "close"},
                        "right": {"indicator": "sma", "field": "close", "window": trend_window},
                    },
                    exit_node_id,
                ),
                _tag(
                    {
                        "op": "lte",
                        "left": {"indicator": "slope", "field": "close", "window": trend_window, "lookback": max(3, trend_window // 4)},
                        "right": {"value": 0.0},
                    },
                    exit_node_id,
                ),
            ]
        }
    return {
        "version": "1.0",
        "timeframe": "daily",
        "entry": entry,
        "exit": exit_rule,
        "metadata": metadata,
        "risk_rules": dict(risk_rules or {}),
    }


def _resolve_execution_semantic_contract(
    *,
    strategy_type: str,
    params: dict[str, Any],
    target_symbols: list[str],
    trade_plan: dict[str, Any],
    holding_horizon: dict[str, Any],
    risk_rules: dict[str, Any],
    position_sizing: dict[str, Any],
    stock_pool: dict[str, Any],
    prediction_contract: dict[str, Any],
    instrument_profile: dict[str, Any],
    explicit_dsl: dict[str, Any],
    existing_claim_to_trade_plan_map: dict[str, Any],
    existing_trade_plan_to_dsl_map: dict[str, Any],
    existing_dsl_support_audit: dict[str, Any],
) -> dict[str, Any]:
    requires_dsl = _trend_strategy_requires_compiled_dsl(strategy_type, target_symbols)
    dsl_payload = dict(explicit_dsl or {})
    compile_failure_reasons: list[str] = []
    if not dsl_payload and requires_dsl:
        dsl_payload = _build_single_name_trend_dsl(
            strategy_type,
            params=params,
            trade_plan=trade_plan,
            holding_horizon=holding_horizon,
            instrument_profile=instrument_profile,
            risk_rules=risk_rules,
        )
        if not dsl_payload:
            compile_failure_reasons.append("trend_family_dsl_synthesis_failed")

    compiled_dsl: dict[str, Any] = {}
    dsl_support_audit = dict(existing_dsl_support_audit or {})
    claim_to_trade_plan_map = dict(existing_claim_to_trade_plan_map or {})
    trade_plan_to_dsl_map = dict(existing_trade_plan_to_dsl_map or {})

    if dsl_payload:
        try:
            from ..strategy_dsl import compile_strategy_blueprint

            compiled = compile_strategy_blueprint(
                {
                    "name": f"{strategy_type}_compiled_execution_contract",
                    "strategy_type": strategy_type,
                    "target_symbols": list(target_symbols),
                    "stock_pool": dict(stock_pool or {}),
                    "holding_horizon": dict(holding_horizon or {}),
                    "trade_plan": dict(trade_plan or {}),
                    "prediction_contract": dict(prediction_contract or {}),
                    "position_sizing": dict(position_sizing or {}),
                    "risk_rules": dict(risk_rules or {}),
                    "dsl": dict(dsl_payload or {}),
                },
                tune_for_factory=False,
            )
            compiled_dsl = dict((compiled.get("params") or {}).get("dsl") or {})
            compiled_meta = dict(compiled.get("metadata") or {})
            if not dsl_support_audit:
                dsl_support_audit = dict(compiled_meta.get("dsl_support_audit") or {})
            if not claim_to_trade_plan_map or not dict(claim_to_trade_plan_map).get("claim_to_trade_step_ids"):
                claim_to_trade_plan_map = dict(compiled_meta.get("claim_to_trade_plan_map") or {})
            if not trade_plan_to_dsl_map or int(dict(trade_plan_to_dsl_map).get("mapped_trade_step_count") or 0) <= 0:
                trade_plan_to_dsl_map = dict(compiled_meta.get("trade_plan_to_dsl_map") or {})
        except Exception as exc:
            _exc_detail = getattr(exc, "name", None) or str(exc) or ""
            _exc_suffix = f":{_exc_detail}" if _exc_detail else ""
            compile_failure_reasons.append(f"dsl_compile_failed:{type(exc).__name__}{_exc_suffix}")

    dsl_compiled = bool(compiled_dsl)
    mapped_trade_step_count = int(dict(trade_plan_to_dsl_map or {}).get("mapped_trade_step_count") or 0)
    execution_semantic_gap_reasons: list[str] = []
    if requires_dsl and not dsl_compiled:
        execution_semantic_gap_reasons.append("compiled_dsl_missing_for_single_name_trend_strategy")
    if requires_dsl and mapped_trade_step_count <= 0:
        execution_semantic_gap_reasons.append("trade_plan_to_dsl_map_missing_for_single_name_trend_strategy")
    execution_semantic_gap_reasons.extend(
        reason for reason in compile_failure_reasons if reason not in execution_semantic_gap_reasons
    )
    execution_semantic_mode = (
        "compiled_dsl"
        if dsl_compiled
        else "missing_executable_contract"
        if requires_dsl
        else "builtin_legacy"
    )
    return {
        "dsl": compiled_dsl,
        "dsl_support_audit": dsl_support_audit,
        "claim_to_trade_plan_map": claim_to_trade_plan_map,
        "trade_plan_to_dsl_map": trade_plan_to_dsl_map,
        "dsl_required": requires_dsl,
        "dsl_compiled": dsl_compiled,
        "execution_semantic_mode": execution_semantic_mode,
        "execution_semantic_gap": bool(execution_semantic_gap_reasons),
        "execution_semantic_gap_reasons": execution_semantic_gap_reasons,
        "dsl_compile_failure_reasons": compile_failure_reasons,
    }

