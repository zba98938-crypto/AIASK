"""Strategy generators: rule-based and LLM-proxy strategy candidate generation."""
from __future__ import annotations

# --- strategy_generators_parts/context.py ---
import asyncio
import json
import logging
import os
import time
from copy import deepcopy
from typing import Any, Optional

import pandas as pd
def _get_strategy_factory_imports():
    from strategy_factory.api import (
        extract_event_context as _extract_event_context,
        preferred_strategy_types_for_factor,
    )
    from strategy_factory.api.constants import (
        CATEGORY_MINIMUMS,
        LLM_FAN_OUT_COUNT,
        PIPELINE_MODE,
        PIPELINE_STAGE_TIMEOUTS,
        PIPELINE_STAGE_TIMEOUT_SEC,
    )
    from strategy_factory.api.contracts import (
        normalize_execution_assumptions,
        normalize_strategy_preferences,
    )
    from strategy_factory.api.semantic_contract import synthesize_confidence_contract
    from strategy_factory.api.semantic_contract import apply_target_symbol_policy, normalize_research_task_contract
    return {
        "CATEGORY_MINIMUMS": CATEGORY_MINIMUMS,
        "LLM_FAN_OUT_COUNT": LLM_FAN_OUT_COUNT,
        "PIPELINE_MODE": PIPELINE_MODE,
        "PIPELINE_STAGE_TIMEOUTS": PIPELINE_STAGE_TIMEOUTS,
        "PIPELINE_STAGE_TIMEOUT_SEC": PIPELINE_STAGE_TIMEOUT_SEC,
        "extract_event_context": _extract_event_context,
        "preferred_strategy_types_for_factor": preferred_strategy_types_for_factor,
        "apply_target_symbol_policy": apply_target_symbol_policy,
        "normalize_research_task_contract": normalize_research_task_contract,
        "normalize_execution_assumptions": normalize_execution_assumptions,
        "normalize_strategy_preferences": normalize_strategy_preferences,
        "synthesize_confidence_contract": synthesize_confidence_contract,
    }


import functools as _functools


@_functools.lru_cache(maxsize=1)
def _sf():
    return _get_strategy_factory_imports()


class _LazyProxy:
    """Module-level proxy to defer strategy_factory imports until first access."""
    def __getattr__(self, name):
        return _sf()[name]

_lazy = _LazyProxy()


def __getattr__(name):
    _map = {
        "CATEGORY_MINIMUMS": "CATEGORY_MINIMUMS",
        "LLM_FAN_OUT_COUNT": "LLM_FAN_OUT_COUNT",
        "PIPELINE_MODE": "PIPELINE_MODE",
        "_extract_event_context": "extract_event_context",
        "preferred_strategy_types_for_factor": "preferred_strategy_types_for_factor",
        "apply_target_symbol_policy": "apply_target_symbol_policy",
        "normalize_research_task_contract": "normalize_research_task_contract",
        "normalize_execution_assumptions": "normalize_execution_assumptions",
        "normalize_strategy_preferences": "normalize_strategy_preferences",
        "synthesize_confidence_contract": "synthesize_confidence_contract",
    }
    if name in _map:
        return _sf()[_map[name]]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


CATEGORY_MINIMUMS = _sf()["CATEGORY_MINIMUMS"]
LLM_FAN_OUT_COUNT = _sf()["LLM_FAN_OUT_COUNT"]
PIPELINE_MODE = _sf()["PIPELINE_MODE"]
_extract_event_context = _sf()["extract_event_context"]
preferred_strategy_types_for_factor = _sf()["preferred_strategy_types_for_factor"]
apply_target_symbol_policy = _sf()["apply_target_symbol_policy"]
normalize_research_task_contract = _sf()["normalize_research_task_contract"]
normalize_execution_assumptions = _sf()["normalize_execution_assumptions"]
normalize_strategy_preferences = _sf()["normalize_strategy_preferences"]
synthesize_confidence_contract = _sf()["synthesize_confidence_contract"]

from .llm_alpha import LLMAlphaMiner
from .data_pipeline import normalize_klines
from .strategy_dsl import compile_strategy_blueprint
from .strategy_llm_provider import StrategyLLMProvider, get_strategy_llm_provider
from .strategy_pipeline import get_strategy_pipeline
from .strategy_spec import (
    DEFAULT_CODES,
    RESEARCH_CANDIDATE_POOL_LIMIT,
    RESEARCH_FINANCIAL_DETAIL_LIMIT,
    RESEARCH_KLINE_SCAN_LIMIT,
    RESEARCH_SYMBOL_DETAIL_LIMIT,
    RESEARCH_UNIVERSE_PAGE_SIZE,
    RESEARCH_UNIVERSE_SCAN_LIMIT,
    StrategySpec,
)

logger = logging.getLogger(__name__)


def _enrich_rule_template_contract(
    strategy_type: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    enriched = deepcopy(contract or {})
    if not enriched:
        return {}
    holding_horizon = dict(enriched.get("holding_horizon") or {})
    execution_assumptions = dict(enriched.get("execution_assumptions") or {})
    portfolio_spec = dict(enriched.get("portfolio_spec") or {})
    position_sizing = dict(enriched.get("position_sizing") or {})
    max_days = int(holding_horizon.get("max_days") or 10)
    alpha_half_life = float(enriched.get("alpha_half_life") or max(2, round(max_days * 0.75)))
    expected_turnover_band = str(
        holding_horizon.get("expected_turnover_band")
        or ("low" if max_days >= 24 else "medium" if max_days >= 12 else "high")
    ).strip().lower()
    execution_assumptions = normalize_execution_assumptions(
        execution_assumptions,
        portfolio_spec=portfolio_spec,
        capacity_assumption=enriched.get("capacity_assumption"),
        holding_horizon=holding_horizon,
        cost_sensitivity_grid=enriched.get("cost_sensitivity_grid"),
    )
    capacity_bucket = str(
        execution_assumptions.get("capacity_bucket")
        or ("mid" if max_days >= 10 else "small")
    ).strip().lower()
    enriched.setdefault(
        "holding_rationale",
        (
            "围绕目标信号的半衰期持有，避免在信号尚未衰减前过早换手。"
            if strategy_type in {"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout"}
            else "围绕慢变量扩散或修复窗口持有，在预期兑现后再做再平衡。"
        ),
    )
    enriched.setdefault("alpha_half_life", alpha_half_life)
    enriched.setdefault(
        "cost_sensitivity_grid",
        {
            "base_case": {
                "commission_rate": execution_assumptions.get("commission_rate", 0.00025),
                "slippage_bps": execution_assumptions.get("slippage_bps", 5),
                "tradability_filter": execution_assumptions.get("tradability_filter", True),
                "slippage_model": execution_assumptions.get("slippage_model", "fixed"),
                "market_impact_bps": execution_assumptions.get("market_impact_bps", 0.0),
            },
            "source": "rule_template_contract",
        },
    )
    enriched.setdefault(
        "position_model",
        position_sizing.get("mode")
        or portfolio_spec.get("position_assumption")
        or "equal_weight",
    )
    enriched.setdefault(
        "capacity_assumption",
        {
            "max_position_pct": portfolio_spec.get("max_position_pct"),
            "capacity_bucket": capacity_bucket,
            "symbol_count": 4,
        },
    )
    enriched.setdefault(
        "market_regime_assumption",
        {
            "summary": (
                "趋势扩张阶段更容易兑现。"
                if strategy_type in {"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout"}
                else "中低噪声、流动性正常阶段更容易兑现。"
            ),
            "preferred_regime": (
                "trend_expansion"
                if strategy_type in {"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout"}
                else "stable_liquid_cn_equity"
            ),
            "avoid_regime": "illiquid_stressed_market",
        },
    )
    holding_horizon.setdefault("alpha_half_life", alpha_half_life)
    holding_horizon.setdefault("expected_turnover_band", expected_turnover_band)
    holding_horizon.setdefault("cooldown_window_days", max(1, int(round(alpha_half_life / 3.0))))
    enriched["holding_horizon"] = holding_horizon
    position_sizing.setdefault("capacity_bucket", capacity_bucket)
    position_sizing.setdefault(
        "position_sizing_rationale",
        "equal_weight_diversified_basket"
        if position_sizing.get("mode") != "single_name"
        else "single_name_conviction_capped_by_capacity",
    )
    position_sizing.setdefault("expected_turnover_band", expected_turnover_band)
    enriched["position_sizing"] = position_sizing
    execution_assumptions.setdefault("capacity_bucket", capacity_bucket)
    execution_assumptions.setdefault("expected_turnover_band", expected_turnover_band)
    enriched["execution_assumptions"] = execution_assumptions
    portfolio_spec.setdefault(
        "position_sizing_rationale",
        position_sizing.get("position_sizing_rationale"),
    )
    portfolio_spec.setdefault("capacity_bucket", capacity_bucket)
    portfolio_spec.setdefault("expected_turnover_band", expected_turnover_band)
    enriched["portfolio_spec"] = portfolio_spec
    return enriched


def _rule_semantic_expected_move(strategy_type: str) -> str:
    return "neutral"


def _rule_failure_summary(
    trade_plan: dict[str, Any],
    risk_rules: dict[str, Any],
) -> str:
    exit_bias = str(trade_plan.get("exit_bias") or "signal_reversal_or_time_stop").strip()
    max_holding_days = int(risk_rules.get("max_holding_days") or 0)
    stop_loss_pct = float(risk_rules.get("stop_loss_pct") or risk_rules.get("stop_loss") or 0.0)
    parts = [exit_bias]
    if stop_loss_pct > 0:
        parts.append(f"stop_loss_{round(stop_loss_pct * 100.0, 2)}pct")
    if max_holding_days > 0:
        parts.append(f"time_stop_{max_holding_days}d")
    return " / ".join(parts)


def _attach_rule_trade_plan_claims(
    strategy_type: str,
    trade_plan: Optional[dict[str, Any]],
    *,
    entry_claim_id: str,
    exit_claim_id: str,
    entry_evidence_ids: list[str],
    exit_evidence_ids: list[str],
) -> dict[str, Any]:
    payload = dict(trade_plan or {})
    entry = dict(payload.get("entry") or {})
    exit_payload = dict(payload.get("exit") or {})
    entry.setdefault("node_id", "entry_step_1")
    entry.setdefault("phase", "entry")
    entry.setdefault("entry_bias", str(payload.get("entry_bias") or "").strip() or None)
    entry["claim_ids"] = [entry_claim_id]
    entry["evidence_ids"] = list(entry_evidence_ids)
    exit_payload.setdefault("node_id", "exit_step_1")
    exit_payload.setdefault("phase", "exit")
    exit_payload.setdefault("exit_bias", str(payload.get("exit_bias") or "").strip() or None)
    exit_payload["claim_ids"] = [exit_claim_id]
    exit_payload["evidence_ids"] = list(exit_evidence_ids)
    payload["entry"] = entry
    payload["exit"] = exit_payload
    if payload.get("entry_bias") in (None, "", [], {}):
        payload["entry_bias"] = entry.get("entry_bias")
    if payload.get("exit_bias") in (None, "", [], {}):
        payload["exit_bias"] = exit_payload.get("exit_bias")
    payload.setdefault("semantic_generation_mode", "rule_semantic_contract")
    payload.setdefault("strategy_type", str(strategy_type or "").strip().lower() or None)
    return payload


def _build_rule_semantic_contract_bundle(
    strategy_type: str,
    *,
    strategy_name: str,
    description: str,
    source: str,
    regime: str,
    fg: int,
    factor_summary: Optional[dict[str, Any]] = None,
    trade_plan: Optional[dict[str, Any]] = None,
    holding_horizon: Optional[dict[str, Any]] = None,
    risk_rules: Optional[dict[str, Any]] = None,
    target_symbols: Optional[list[str]] = None,
    rationale: Optional[str] = None,
    template_contract: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    strategy_key = str(strategy_type or "").strip().lower() or "rule_strategy"
    holding = dict(holding_horizon or {})
    risk = dict(risk_rules or {})
    entry_expected_move = _rule_semantic_expected_move(strategy_key)
    target_symbol_list = [
        str(symbol).strip()
        for symbol in list(target_symbols or [])
        if str(symbol).strip()
    ][:12]
    primary_horizon_days = max(
        1,
        int(
            holding.get("max_days")
            or holding.get("alpha_half_life")
            or risk.get("max_holding_days")
            or 10
        ),
    )
    entry_claim_id = f"{strategy_key}_claim_entry"
    exit_claim_id = f"{strategy_key}_claim_exit"
    entry_evidence_ids = [f"{strategy_key}_ev_context", f"{strategy_key}_ev_template"]
    exit_evidence_ids = [f"{strategy_key}_ev_invalidation"]
    normalized_trade_plan = _attach_rule_trade_plan_claims(
        strategy_key,
        trade_plan,
        entry_claim_id=entry_claim_id,
        exit_claim_id=exit_claim_id,
        entry_evidence_ids=entry_evidence_ids,
        exit_evidence_ids=exit_evidence_ids,
    )
    factor_payload = dict(factor_summary or {})
    template_payload = dict(template_contract or {})
    factor_names = [
        str(item).strip()
        for item in list(factor_payload.get("top_factor_names") or [])
        if str(item).strip()
    ][:3]
    rationale_text = str(rationale or description or strategy_name or strategy_key).strip()
    failure_summary = _rule_failure_summary(normalized_trade_plan, risk)
    evidence_chain = {
        "thesis": rationale_text,
        "generation_mode": "rule_semantic_contract",
        "evidences": [
            {
                "evidence_id": entry_evidence_ids[0],
                "source_type": "template_fallback",
                "direction": "neutral",
                "summary": (
                    f"{source} 认为 {strategy_key} 与 {regime} 环境匹配"
                    + (f"，top_factors={factor_names}" if factor_names else "")
                    + f"，fear_greed_index={int(fg)}。"
                ),
                "proxy_only": True,
                "target_symbols": list(target_symbol_list),
                "horizon_days": primary_horizon_days,
                "claim_ids": [entry_claim_id],
            },
            {
                "evidence_id": entry_evidence_ids[1],
                "source_type": "rule_template_contract",
                "direction": "neutral",
                "summary": (
                    f"{strategy_name} 使用模板化 trade plan / risk rules / horizon。"
                    f" entry_bias={normalized_trade_plan.get('entry_bias')},"
                    f" exit_bias={normalized_trade_plan.get('exit_bias')}。"
                ),
                "proxy_only": True,
                "target_symbols": list(target_symbol_list),
                "horizon_days": primary_horizon_days,
                "claim_ids": [entry_claim_id],
                "support_metric": {
                    "template_generation_profile": template_payload.get("template_generation_profile"),
                },
            },
            {
                "evidence_id": exit_evidence_ids[0],
                "source_type": "risk_template",
                "direction": "neutral",
                "summary": f"退出/失效条件由模板风控定义：{failure_summary}。",
                "proxy_only": True,
                "target_symbols": list(target_symbol_list),
                "horizon_days": min(primary_horizon_days, max(1, primary_horizon_days // 2)),
                "claim_ids": [exit_claim_id],
            },
        ],
    }
    prediction_contract = {
        "generation_mode": "rule_semantic_contract",
        "primary_horizon_days": primary_horizon_days,
        "target": "forward_return_neutral",
        "direction": "neutral",
        "template_fallback_used": True,
        "direction_source": "rule_template_diagnostic_fallback",
        "conflict_resolution_rule": {
            "policy": "prefer_invalidation_when_exit_evidence_present",
            "tie_breaker": "risk_first",
        },
        "claims": [
            {
                "claim_id": entry_claim_id,
                "claim_type": "entry",
                "summary": rationale_text,
                "expected_move": entry_expected_move,
                "expected_horizon": primary_horizon_days,
                "evidence_ids": list(entry_evidence_ids),
                "failure_condition": failure_summary,
                "conflict_resolution_rule": {"policy": "prefer_invalidation_when_exit_evidence_present"},
                "target_symbols": list(target_symbol_list),
            },
            {
                "claim_id": exit_claim_id,
                "claim_type": "exit",
                "summary": f"当 {normalized_trade_plan.get('exit_bias') or 'risk_rule'} 出现时退出。",
                "expected_move": "neutral",
                "expected_horizon": min(primary_horizon_days, max(1, primary_horizon_days // 2)),
                "evidence_ids": list(exit_evidence_ids),
                "failure_condition": "entry thesis restored",
                "conflict_resolution_rule": {"policy": "risk_first"},
                "target_symbols": list(target_symbol_list),
            },
        ],
    }
    confidence_contract = synthesize_confidence_contract(
        {
            "strategy_type": strategy_key,
            "evidence_chain": dict(evidence_chain),
            "prediction_contract": dict(prediction_contract),
        }
    )
    claim_to_trade_plan_map = {
        "claim_to_trade_step_ids": {
            entry_claim_id: [str(dict(normalized_trade_plan.get("entry") or {}).get("node_id") or "entry_step_1")],
            exit_claim_id: [str(dict(normalized_trade_plan.get("exit") or {}).get("node_id") or "exit_step_1")],
        },
        "trade_step_to_claim_ids": {
            str(dict(normalized_trade_plan.get("entry") or {}).get("node_id") or "entry_step_1"): [entry_claim_id],
            str(dict(normalized_trade_plan.get("exit") or {}).get("node_id") or "exit_step_1"): [exit_claim_id],
        },
    }
    return {
        "trade_plan": normalized_trade_plan,
        "evidence_chain": evidence_chain,
        "prediction_contract": prediction_contract,
        "confidence_contract": confidence_contract,
        "claim_to_trade_plan_map": claim_to_trade_plan_map,
    }

# --- strategy_generators_parts/specs.py ---
def _rule_template_contract(strategy_type: str) -> dict[str, Any]:
    contracts: dict[str, dict[str, Any]] = {
        'momentum': {
            'template_generation_profile': 'conservative_momentum',
            'holding_horizon': {'min_days': 14, 'max_days': 42},
            'trade_plan': {'entry_bias': 'trend_persistence_confirmation', 'exit_bias': 'false_breakout_or_momentum_decay'},
            'risk_rules': {'stop_loss_pct': 0.07, 'take_profit_pct': 0.2, 'max_holding_days': 42, 'max_position_pct': 0.16},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'periodic_rebalance', 'frequency_days': 14},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'trend_score_tilt',
                'max_position_pct': 0.16,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 5,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
            },
            'validation_profile': {
                'profile': 'trade_rule_validation',
                'validation_focus': 'target_plus_representative',
                'primary_validation_layer': 'target',
            },
            'family_specialization': {
                'trend_persistence_regime': 'trend_expansion_with_relative_strength_persistence',
                'false_breakout_filter': 'prefer_volume_confirmed_breakout_and_positive_trend_slope',
                'peer_selection_mode': 'target_plus_dynamic_family_peer',
            },
            'targeting_policy': {
                'target_symbol_policy': 'dynamic_signal_universe',
                'universe_scope': 'trend_leaders_liquid',
                'universe_expansion_policy': 'trend_leaders_only',
            },
            'rule_template_contract': {
                'template_generation_profile': 'conservative_momentum',
                'applicable_universe': {
                    'market_cap': 'mid_large',
                    'liquidity': 'high',
                    'style_bias': 'trend_expansion',
                },
                'target_layer': 'target',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.07,
                    'take_profit_pct': 0.2,
                    'max_holding_days': 36,
                    'max_position_pct': 0.16,
                },
                'portfolio_weight_method': 'trend_score_tilt',
            },
        },
        'ma_cross': {
            'template_generation_profile': 'conservative_ma_cross',
            'holding_horizon': {'min_days': 14, 'max_days': 48},
            'trade_plan': {'entry_bias': 'adaptive_cross_with_volume_confirmation', 'exit_bias': 'range_reentry_or_cross_failure'},
            'risk_rules': {'stop_loss_pct': 0.07, 'take_profit_pct': 0.18, 'max_holding_days': 48, 'max_position_pct': 0.14},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'periodic_rebalance', 'frequency_days': 12},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'cross_strength_tilt',
                'max_position_pct': 0.14,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 5,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
            },
            'validation_profile': {
                'profile': 'trade_rule_validation',
                'validation_focus': 'target_plus_representative',
                'primary_validation_layer': 'target',
            },
            'family_specialization': {
                'adaptive_span_logic': 'fast_slow_span_scaled_by_regime_and_noise_level',
                'range_filter': 'avoid_crosses_when_long_ma_is_flat_and_price_is_range_bound',
                'volume_confirmation': 'prefer_crosses_with_volume_ratio_confirmation',
            },
            'targeting_policy': {
                'target_symbol_policy': 'dynamic_signal_universe',
                'universe_scope': 'trend_followers_liquid',
                'universe_expansion_policy': 'trend_leaders_only',
            },
            'rule_template_contract': {
                'template_generation_profile': 'conservative_ma_cross',
                'applicable_universe': {
                    'market_cap': 'mid_large',
                    'liquidity': 'high',
                    'style_bias': 'trend_following',
                },
                'target_layer': 'target',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.07,
                    'take_profit_pct': 0.18,
                    'max_holding_days': 48,
                    'max_position_pct': 0.14,
                },
                'portfolio_weight_method': 'cross_strength_tilt',
            },
        },
        'rsi': {
            'template_generation_profile': 'high_precision_mean_reversion',
            'holding_horizon': {'min_days': 2, 'max_days': 8, 'expected_turnover_band': 'low'},
            'trade_plan': {'entry_bias': 'oversold_repair_with_mean_reversion_confirmation', 'exit_bias': 'mean_reversion_completion_or_rsi_reset'},
            'risk_rules': {'stop_loss_pct': 0.04, 'take_profit_pct': 0.1, 'max_holding_days': 8, 'max_position_pct': 0.12},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'signal_rebalance', 'frequency_days': 3},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'high_precision_repair_weight',
                'max_position_pct': 0.12,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 4,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
                'market_impact_bps': 1.0,
                'capacity_bucket': 'small',
            },
            'validation_profile': {
                'profile': 'trade_rule_validation',
                'validation_focus': 'target_plus_representative',
                'primary_validation_layer': 'target',
                'objective_profile': 'high_precision',
                'trade_density_preference': 'low',
                'regime_required': True,
                'cost_robust_required': True,
                'entry_selectivity': 'strict',
                'preferred_regime': 'oversold_repair_with_stabilizing_liquidity',
                'avoid_regime': 'downtrend_acceleration_or_noise_chop',
            },
            'family_specialization': {
                'mean_reversion_confirmation': 'require_oversold_and_price_dislocation_with_stabilization',
                'repair_confirmation': 'require_rebound_from_recent_low_and_rsi_reclaim_before_entry',
                'entry_regime_filter': 'bear_calm_or_bear_volatile_only',
                'entry_selectivity': 'strict_repair_only',
                'noise_filter': 'path_noise_ratio_ceiling_6',
                'exit_discipline': 'mean_reversion_exit_after_4_bars_or_time_stop_6_bars',
                'adverse_regime_exit': 'exit_when_range_volatile_noise_spikes_after_entry',
                'event_semantics': 'stateful_entry_exit_masks',
                'failure_mode_focus': 'catching_falling_knife_or_overtrading',
                'holding_bias': 'repair_window_hold_2_to_8_days',
                'rebalance_bias': 'signal_rebalance_every_3_days',
            },
            'targeting_policy': {
                'target_symbol_policy': 'dynamic_signal_universe',
                'universe_scope': 'liquid_repair_candidates',
                'universe_expansion_policy': 'oversold_repair_only',
            },
            'holding_rationale': '只在熊市超跌后的修复阶段参与，优先等待止跌且噪声可控的窗口，达到均值回归或时间止损即退出，避免在中性噪声阶段频繁抄底。',
            'position_model': 'equal_weight',
            'capacity_assumption': {'max_position_pct': 0.12, 'capacity_bucket': 'small', 'symbol_count': 4},
            'market_regime_assumption': {
                'summary': '超跌后的熊市修复阶段更有效，需要避免下跌加速和高噪声横盘。',
                'preferred_regime': 'oversold_repair_with_stabilizing_liquidity',
                'avoid_regime': 'downtrend_acceleration_or_noise_chop',
            },
            'failure_mode': {
                'primary_failure_mode': 'downtrend_acceleration',
                'secondary_failure_mode': 'range_bound_overtrading',
                'invalidators': ['no_stabilization_after_entry', 'cost_drag_exceeds_repair_edge'],
            },
            'rule_template_contract': {
                'template_generation_profile': 'high_precision_mean_reversion',
                'applicable_universe': {
                    'market_cap': 'all_liquid',
                    'liquidity': 'high',
                    'style_bias': 'oversold_repair',
                },
                'target_layer': 'target',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.04,
                    'take_profit_pct': 0.1,
                    'max_holding_days': 8,
                    'max_position_pct': 0.12,
                },
                'portfolio_weight_method': 'high_precision_repair_weight',
            },
        },
        'macro_timing': {
            'template_generation_profile': 'high_precision_regime_timing',
            'holding_horizon': {'min_days': 6, 'max_days': 24, 'expected_turnover_band': 'low'},
            'trade_plan': {'entry_bias': 'panic_repair_after_regime_confirmation', 'exit_bias': 'greed_extreme_or_regime_break'},
            'risk_rules': {'stop_loss_pct': 0.06, 'take_profit_pct': 0.16, 'max_holding_days': 24, 'max_position_pct': 0.14},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'regime_rebalance', 'frequency_days': 7},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'regime_conviction_tilt',
                'max_position_pct': 0.14,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 4,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
                'market_impact_bps': 1.0,
                'capacity_bucket': 'mid',
            },
            'validation_profile': {
                'profile': 'macro_regime_validation',
                'validation_focus': 'target_plus_representative',
                'primary_validation_layer': 'target',
                'objective_profile': 'high_precision',
                'trade_density_preference': 'low',
                'regime_required': True,
                'cost_robust_required': True,
                'entry_selectivity': 'strict',
                'preferred_regime': 'panic_repair_with_volatility_stabilization',
                'avoid_regime': 'mid_regime_whipsaw',
            },
            'family_specialization': {
                'regime_confirmation': 'require_fear_recovery_and_volatility_stabilization_before_entry',
                'whipsaw_filter': 'avoid_mid_regime_flip_flops_and_reenter_only_after_cooldown',
                'holding_bias': 'regime_hold_6_to_24_days',
                'rebalance_bias': 'regime_rebalance_every_7_days',
            },
            'targeting_policy': {
                'target_symbol_policy': 'dynamic_signal_universe',
                'universe_scope': 'liquid_defensive_core',
                'universe_expansion_policy': 'macro_regime_only',
            },
            'holding_rationale': '只在恐慌充分释放后等待风险偏好和波动结构同步修复再入场，中性区间不频繁切换，避免宏观噪声抽打。',
            'position_model': 'equal_weight',
            'capacity_assumption': {'max_position_pct': 0.14, 'capacity_bucket': 'mid', 'symbol_count': 4},
            'market_regime_assumption': {
                'summary': '极端恐慌后波动回落、风险偏好修复的阶段更有效，需要避免中性区间的来回抽打。',
                'preferred_regime': 'panic_repair_with_volatility_stabilization',
                'avoid_regime': 'mid_regime_whipsaw',
            },
            'failure_mode': {
                'primary_failure_mode': 'regime_whipsaw',
                'secondary_failure_mode': 'late_entry_after_fear_release',
                'invalidators': ['fear_signal_not_confirmed_by_stabilization', 'cost_drag_exceeds_regime_edge'],
            },
            'rule_template_contract': {
                'template_generation_profile': 'high_precision_regime_timing',
                'applicable_universe': {
                    'market_cap': 'mid_large',
                    'liquidity': 'high',
                    'style_bias': 'panic_repair_regime',
                },
                'target_layer': 'target',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.06,
                    'take_profit_pct': 0.16,
                    'max_holding_days': 24,
                    'max_position_pct': 0.14,
                },
                'portfolio_weight_method': 'regime_conviction_tilt',
            },
        },
        'event_structure_breakout': {
            'template_generation_profile': 'high_precision_event_structure',
            'holding_horizon': {'min_days': 3, 'max_days': 12, 'expected_turnover_band': 'low'},
            'trade_plan': {
                'entry_bias': 'event_structure_breakout_confirmation',
                'exit_bias': 'breakout_failure_or_time_stop',
            },
            'risk_rules': {'stop_loss_pct': 0.06, 'take_profit_pct': 0.16, 'max_holding_days': 12, 'max_position_pct': 0.14},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'signal_rebalance', 'frequency_days': 4},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'event_structure_breakout_weight',
                'max_position_pct': 0.14,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 5,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
                'market_impact_bps': 1.0,
                'capacity_bucket': 'mid',
            },
            'validation_profile': {
                'profile': 'event_trade_validation',
                'validation_focus': 'candidate_target_only',
                'primary_validation_layer': 'target',
                'objective_profile': 'high_precision',
                'trade_density_preference': 'low',
                'regime_required': True,
                'cost_robust_required': True,
                'entry_selectivity': 'strict_event_breakout',
                'preferred_regime': 'event_follow_through_with_structure_confirmation',
                'avoid_regime': 'false_breakout_or_post_event_mean_reversion',
                'event_prefilter_required': True,
                'event_prefilter_profile': 'announcement_flow_sector_v1',
                'event_prefilter_min_confirmations': 1,
            },
            'family_specialization': {
                'event_prefilter': 'require_announcement_fund_flow_or_sector_catalyst_before_breakout_confirmation',
                'event_impulse_confirmation': 'require_recent_impulse_before_breakout_attempt',
                'breakout_confirmation': 'require_close_above_prior_high_with_buffer',
                'liquidity_confirmation': 'require_volume_reexpansion_at_breakout',
                'structure_confirmation': 'require_high_close_location_and_positive_body',
                'adverse_breakout_exit': 'exit_on_breakout_failure_or_volume_fade',
                'holding_bias': 'event_follow_through_hold_3_to_12_days',
                'rebalance_bias': 'signal_rebalance_every_4_days',
            },
            'targeting_policy': {
                'target_symbol_policy': 'strict_intersection',
                'universe_scope': 'liquid_event_breakout_leaders',
                'universe_expansion_policy': 'forbid',
            },
            'holding_rationale': '只在催化后先走出短窗口强势，再经历缩量整理并放量突破的结构延续段参与，避免在普通噪声突破里频繁追价。',
            'position_model': 'equal_weight',
            'capacity_assumption': {'max_position_pct': 0.14, 'capacity_bucket': 'mid', 'symbol_count': 4},
            'event_prefilter': {
                'required': True,
                'profile': 'announcement_flow_sector_v1',
                'allowed_sources': ['announcement', 'fund_flow', 'sector_catalyst'],
                'observed_sources': [],
                'observed_confirmation_count': 0,
                'confirmation_count': 0,
                'min_confirmations': 1,
                'passed': False,
                'status': 'missing',
                'focus_industries': [],
                'event_anchor': {},
                'anchor_strength': None,
            },
            'market_regime_assumption': {
                'summary': '事件催化后的短窗口延续、缩量整理和放量突破共振阶段更有效，需要避免普通假突破与事件后均值回归。',
                'preferred_regime': 'event_follow_through_with_structure_confirmation',
                'avoid_regime': 'false_breakout_or_post_event_mean_reversion',
            },
            'failure_mode': {
                'primary_failure_mode': 'false_breakout_after_event_impulse',
                'secondary_failure_mode': 'cost_drag_after_chasing_extension',
                'invalidators': ['breakout_volume_not_confirmed', 'close_falls_back_into_range'],
            },
            'rule_template_contract': {
                'template_generation_profile': 'high_precision_event_structure',
                'applicable_universe': {
                    'market_cap': 'mid_large',
                    'liquidity': 'high',
                    'style_bias': 'event_structure_follow_through',
                },
                'target_layer': 'target',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.06,
                    'take_profit_pct': 0.16,
                    'max_holding_days': 12,
                    'max_position_pct': 0.14,
                },
                'portfolio_weight_method': 'event_structure_breakout_weight',
            },
        },
        'volatility_breakout': {
            'template_generation_profile': 'conservative_breakout',
            'holding_horizon': {'min_days': 3, 'max_days': 15},
            'trade_plan': {'entry_bias': 'breakout_confirmation', 'exit_bias': 'trailing_stop_or_time_stop'},
            'risk_rules': {'stop_loss_pct': 0.07, 'take_profit_pct': 0.16, 'max_holding_days': 15, 'max_position_pct': 0.18},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'signal_rebalance', 'frequency_days': 3},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'volatility_budget',
                'max_position_pct': 0.18,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 6,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
            },
            'validation_profile': {
                'profile': 'trade_rule_validation',
                'validation_focus': 'target_plus_representative',
                'primary_validation_layer': 'target',
            },
            'targeting_policy': {
                'target_symbol_policy': 'dynamic_signal_universe',
                'universe_scope': 'liquid_large_mid',
                'universe_expansion_policy': 'trend_leaders_only',
            },
            'rule_template_contract': {
                'template_generation_profile': 'conservative_breakout',
                'applicable_universe': {
                    'market_cap': 'mid_large',
                    'liquidity': 'high',
                    'style_bias': 'trend_expansion',
                },
                'target_layer': 'target',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.07,
                    'take_profit_pct': 0.16,
                    'max_holding_days': 15,
                    'max_position_pct': 0.18,
                },
                'portfolio_weight_method': 'volatility_budget',
            },
        },
        'gap_fill': {
            'template_generation_profile': 'conservative_mean_reversion',
            'holding_horizon': {'min_days': 1, 'max_days': 8},
            'trade_plan': {'entry_bias': 'gap_repair_confirmation', 'exit_bias': 'mean_reversion_completion'},
            'risk_rules': {'stop_loss_pct': 0.05, 'take_profit_pct': 0.12, 'max_holding_days': 8, 'max_position_pct': 0.14},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'signal_rebalance', 'frequency_days': 2},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'repair_equal_weight',
                'max_position_pct': 0.14,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 5,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
            },
            'validation_profile': {
                'profile': 'trade_rule_validation',
                'validation_focus': 'target_plus_representative',
                'primary_validation_layer': 'target',
            },
            'targeting_policy': {
                'target_symbol_policy': 'dynamic_signal_universe',
                'universe_scope': 'liquid_repair_candidates',
                'universe_expansion_policy': 'oversold_repair_only',
            },
            'rule_template_contract': {
                'template_generation_profile': 'conservative_mean_reversion',
                'applicable_universe': {
                    'market_cap': 'all_liquid',
                    'liquidity': 'medium_high',
                    'style_bias': 'oversold_repair',
                },
                'target_layer': 'target',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.05,
                    'take_profit_pct': 0.12,
                    'max_holding_days': 8,
                    'max_position_pct': 0.14,
                },
                'portfolio_weight_method': 'repair_equal_weight',
            },
        },
        'mean_reversion_short': {
            'template_generation_profile': 'conservative_mean_reversion',
            'holding_horizon': {'min_days': 1, 'max_days': 7},
            'trade_plan': {'entry_bias': 'short_horizon_reversal', 'exit_bias': 'time_stop_or_signal_reset'},
            'risk_rules': {'stop_loss_pct': 0.05, 'take_profit_pct': 0.1, 'max_holding_days': 7, 'max_position_pct': 0.12},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'signal_rebalance', 'frequency_days': 2},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'short_horizon_equal_weight',
                'max_position_pct': 0.12,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 5,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
            },
            'validation_profile': {
                'profile': 'trade_rule_validation',
                'validation_focus': 'target_plus_representative',
                'primary_validation_layer': 'target',
            },
            'targeting_policy': {
                'target_symbol_policy': 'dynamic_signal_universe',
                'universe_scope': 'liquid_defensive_reversion',
                'universe_expansion_policy': 'short_horizon_only',
            },
            'rule_template_contract': {
                'template_generation_profile': 'conservative_mean_reversion',
                'applicable_universe': {
                    'market_cap': 'all_liquid',
                    'liquidity': 'high',
                    'style_bias': 'defensive_mean_reversion',
                },
                'target_layer': 'target',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.05,
                    'take_profit_pct': 0.1,
                    'max_holding_days': 7,
                    'max_position_pct': 0.12,
                },
                'portfolio_weight_method': 'short_horizon_equal_weight',
            },
        },
        'sector_rotation': {
            'template_generation_profile': 'conservative_rotation',
            'holding_horizon': {'min_days': 5, 'max_days': 20},
            'trade_plan': {'entry_bias': 'relative_strength_rotation', 'exit_bias': 'leadership_decay_or_time_stop'},
            'risk_rules': {'stop_loss_pct': 0.08, 'take_profit_pct': 0.18, 'max_holding_days': 20, 'max_position_pct': 0.15},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'periodic_rebalance', 'frequency_days': 5},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'sector_score_tilt',
                'max_position_pct': 0.15,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 6,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
            },
            'validation_profile': {
                'profile': 'trade_rule_validation',
                'validation_focus': 'target_plus_representative',
                'primary_validation_layer': 'combined',
            },
            'targeting_policy': {
                'target_symbol_policy': 'sector_leader_rotation',
                'universe_scope': 'liquid_sector_leaders',
                'universe_expansion_policy': 'sector_relative_strength',
            },
            'rule_template_contract': {
                'template_generation_profile': 'conservative_rotation',
                'applicable_universe': {
                    'market_cap': 'mid_large',
                    'liquidity': 'high',
                    'style_bias': 'sector_leadership',
                },
                'target_layer': 'combined',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.08,
                    'take_profit_pct': 0.18,
                    'max_holding_days': 20,
                    'max_position_pct': 0.15,
                },
                'portfolio_weight_method': 'sector_score_tilt',
            },
        },
        'north_capital_track': {
            'template_generation_profile': 'conservative_flow',
            'holding_horizon': {'min_days': 4, 'max_days': 12},
            'trade_plan': {'entry_bias': 'capital_flow_confirmation', 'exit_bias': 'flow_reversal_or_time_stop'},
            'risk_rules': {'stop_loss_pct': 0.07, 'take_profit_pct': 0.16, 'max_holding_days': 12, 'max_position_pct': 0.16},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'periodic_rebalance', 'frequency_days': 3},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'flow_score_tilt',
                'max_position_pct': 0.16,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 6,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
            },
            'validation_profile': {
                'profile': 'trade_rule_validation',
                'validation_focus': 'target_plus_representative',
                'primary_validation_layer': 'combined',
            },
            'targeting_policy': {
                'target_symbol_policy': 'northbound_eligible_focus',
                'universe_scope': 'northbound_liquid_core',
                'universe_expansion_policy': 'flow_leaders_only',
            },
            'rule_template_contract': {
                'template_generation_profile': 'conservative_flow',
                'applicable_universe': {
                    'northbound_eligible': True,
                    'liquidity': 'high',
                    'style_bias': 'capital_flow_leaders',
                },
                'target_layer': 'combined',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.07,
                    'take_profit_pct': 0.16,
                    'max_holding_days': 12,
                    'max_position_pct': 0.16,
                },
                'portfolio_weight_method': 'flow_score_tilt',
            },
        },
        'margin_divergence': {
            'template_generation_profile': 'high_precision_flow_repair',
            'holding_horizon': {'min_days': 3, 'max_days': 10, 'expected_turnover_band': 'low'},
            'trade_plan': {
                'entry_bias': 'liquidity_divergence_repair_confirmation',
                'exit_bias': 'liquidity_break_or_time_stop',
            },
            'risk_rules': {'stop_loss_pct': 0.05, 'take_profit_pct': 0.12, 'max_holding_days': 10, 'max_position_pct': 0.12},
            'position_sizing': {'mode': 'equal_weight', 'position_assumption': 'equal_weight_proxy'},
            'rebalance_rule': {'mode': 'signal_rebalance', 'frequency_days': 3},
            'portfolio_spec': {
                'position_assumption': 'equal_weight_proxy',
                'target_weight_scheme': 'equal_weight',
                'weight_method': 'liquidity_divergence_weight',
                'max_position_pct': 0.12,
            },
            'execution_assumptions': {
                'commission_rate': 0.00025,
                'slippage_bps': 4,
                'tradability_filter': True,
                'slippage_model': 'fixed',
                'market_ruleset': 'cn_equity',
                'market_impact_bps': 1.0,
                'capacity_bucket': 'small',
            },
            'validation_profile': {
                'profile': 'trade_rule_validation',
                'validation_focus': 'candidate_target_only',
                'primary_validation_layer': 'target',
                'objective_profile': 'high_precision',
                'trade_density_preference': 'low',
                'regime_required': True,
                'cost_robust_required': True,
                'entry_selectivity': 'strict',
                'preferred_regime': 'liquidity_repair_with_volume_reexpansion',
                'avoid_regime': 'volume_vacuum_or_failed_rebound',
            },
            'family_specialization': {
                'repair_confirmation': 'require_medium_window_drawdown_plus_short_window_rebound',
                'liquidity_confirmation': 'require_pre_entry_volume_dryup_and_entry_volume_reexpansion',
                'structure_confirmation': 'require_high_close_location_and_positive_body',
                'adverse_regime_exit': 'exit_on_volume_break_or_failed_rebound',
                'holding_bias': 'repair_window_hold_3_to_10_days',
                'rebalance_bias': 'signal_rebalance_every_3_days',
            },
            'targeting_policy': {
                'target_symbol_policy': 'margin_activity_focus',
                'universe_scope': 'liquidity_repair_candidates',
                'universe_expansion_policy': 'divergence_repair_only',
            },
            'holding_rationale': '只在中期下跌后出现缩量止跌、放量修复和结构转强共振时参与，优先低频高把握，不在普通反抽里频繁交易。',
            'position_model': 'equal_weight',
            'capacity_assumption': {'max_position_pct': 0.12, 'capacity_bucket': 'small', 'symbol_count': 4},
            'market_regime_assumption': {
                'summary': '流动性收缩后出现放量修复且结构转强的阶段更有效，需要避免缩量阴跌与假修复。',
                'preferred_regime': 'liquidity_repair_with_volume_reexpansion',
                'avoid_regime': 'volume_vacuum_or_failed_rebound',
            },
            'failure_mode': {
                'primary_failure_mode': 'failed_rebound_after_liquidity_dryup',
                'secondary_failure_mode': 'cost_fragility_after_false_reexpansion',
                'invalidators': ['entry_volume_not_confirmed', 'structure_break_after_entry'],
            },
            'rule_template_contract': {
                'template_generation_profile': 'high_precision_flow_repair',
                'applicable_universe': {
                    'margin_active': True,
                    'liquidity': 'high',
                    'style_bias': 'liquidity_divergence_repair',
                },
                'target_layer': 'target',
                'default_risk_constraints': {
                    'stop_loss_pct': 0.05,
                    'take_profit_pct': 0.12,
                    'max_holding_days': 10,
                    'max_position_pct': 0.12,
                },
                'portfolio_weight_method': 'liquidity_divergence_weight',
            },
        },
    }
    strategy_key = str(strategy_type or '').strip().lower()
    return _enrich_rule_template_contract(
        strategy_key,
        deepcopy(contracts.get(strategy_key) or {}),
    )

# --- strategy_generators_parts/runtime.py ---
class RuleStrategyGenerator:
    @staticmethod
    def _factor_research_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
        factor_research = dict(snapshot.get('factor_research') or {})
        summary = dict(factor_research.get('summary') or {})
        return {
            'top_factor_names': list(summary.get('top_factor_names') or factor_research.get('active_factors') or [])[:3],
            'preferred_strategy_types': [
                str(item).strip()
                for item in list(factor_research.get('preferred_strategy_types') or [])
                if str(item).strip()
            ][:4],
            'degraded': bool(factor_research.get('degraded')),
        }

    @classmethod
    def _build_rule_spec(
        cls,
        strategy_type: str,
        *,
        fg: int,
        regime: str,
        source: str,
        factor_summary: dict[str, Any],
    ) -> Optional[StrategySpec]:
        templates: dict[str, dict[str, Any]] = {
            'momentum': {
                'params': {'lookback': 20, 'threshold': 0.013},
                'name': 'AI 动量强化',
                'description': '趋势持续且量价确认阶段偏向动量追随，并过滤假突破。',
            },
            'ma_cross': {
                'params': {'short_period': 8, 'long_period': 34},
                'name': 'AI 均线趋势',
                'description': '趋势确认阶段用自适应均线跨度、横盘过滤和量能确认过滤噪音。',
            },
            'rsi': {
                'params': {
                    'rsi_period': 12,
                    'oversold': 18,
                    'overbought': 64,
                    'regime_filter_enabled': True,
                    'allowed_entry_regimes': ['bear_calm', 'bear_volatile'],
                    'noise_filter_enabled': True,
                    'noise_window': 6,
                    'noise_ceiling': 6.0,
                    'regime_break_threshold': 0.015,
                    'repair_confirmation_enabled': True,
                    'repair_confirmation_window': 6,
                    'repair_confirmation_rebound_pct': 0.008,
                    'repair_confirmation_rsi_reclaim': 24.0,
                    'liquidity_confirmation_enabled': True,
                    'liquidity_window': 8,
                    'liquidity_volume_floor_ratio': 0.8,
                    'structure_confirmation_enabled': True,
                    'structure_window': 4,
                    'structure_close_location_min': 0.55,
                    'structure_body_return_min': 0.0015,
                    'max_active_symbols': 2,
                    'universe_selection_profile': 'repair_liquidity_fit_v1',
                    'mean_reversion_exit_min_hold_bars': 4,
                    'mean_reversion_exit_buffer': -0.002,
                    'max_hold_bars': 6,
                    'adverse_regime_exit_enabled': True,
                    'adverse_exit_regimes': ['range_volatile'],
                    'adverse_noise_ceiling': 6.0,
                },
                'name': 'AI RSI 反转',
                'description': '仅在熊市超跌后的止跌修复阶段参与高精度反转，并过滤高噪声触发。',
            },
            'value_factor': {
                'params': {'lookback': 60, 'buy_quantile': 0.8, 'sell_quantile': 0.2},
                'name': 'AI 价值回归',
                'description': '估值修复阶段偏向价值/反转组合。',
            },
            'quality_factor': {
                'params': {'lookback': 72, 'buy_quantile': 0.82, 'sell_quantile': 0.18},
                'name': 'AI 质量精选',
                'description': '质量因子占优阶段偏向低频再平衡、质量稳定与价格趋势共振筛选。',
            },
            'growth_factor': {
                'params': {'lookback': 40, 'buy_quantile': 0.78, 'sell_quantile': 0.22},
                'name': 'AI 成长加速',
                'description': '成长因子活跃阶段偏向高景气扩张。',
            },
            'multi_factor': {
                'params': {'factor_weights': {'value': 0.4, 'quality': 0.35, 'momentum': 0.25}},
                'name': 'AI 多因子平衡',
                'description': '因子共振时优先使用多因子组合。',
            },
            'macro_timing': {
                'params': {'fear_threshold': 24, 'greed_threshold': 74, 'lookback': 36},
                'name': 'AI 宏观择时',
                'description': '只在恐慌修复与波动稳定共振时参与宏观择时。',
            },
            'event_structure_breakout': {
                'params': {
                    'breakout_window': 12,
                    'breakout_buffer_pct': 0.002,
                    'contraction_window': 5,
                    'contraction_max_range_ratio': 0.06,
                    'volume_window': 8,
                    'breakout_volume_ratio_min': 1.0,
                    'structure_window': 4,
                    'structure_close_location_min': 0.62,
                    'structure_body_return_min': 0.003,
                    'event_impulse_window': 5,
                    'event_impulse_threshold': 0.015,
                    'max_hold_bars': 8,
                    'breakout_failure_close_buffer': -0.012,
                    'adverse_volume_ratio_max': 0.85,
                    'event_prefilter_enabled': True,
                    'event_prefilter_profile': 'announcement_flow_sector_v1',
                    'event_prefilter_min_confirmations': 1,
                },
                'name': 'AI 事件结构突破',
                'description': '只做催化后缩量整理再放量突破的结构延续，优先低频高把握。',
            },
            'volatility_breakout': {
                'params': {'lookback': 20, 'threshold': 0.025},
                'name': 'AI 波动突破',
                'description': '趋势扩张与波动放大阶段偏向波动率突破。',
            },
            'gap_fill': {
                'params': {'gap_threshold': 0.02, 'rsi_period': 5, 'oversold': 24, 'overbought': 58},
                'name': 'AI 跳空回补',
                'description': '情绪错杀或事件冲击后偏向短线回补机会。',
            },
            'mean_reversion_short': {
                'params': {'rsi_period': 6, 'oversold': 26, 'overbought': 62},
                'name': 'AI 短线回归',
                'description': '震荡与防御环境下偏向短周期均值回归。',
            },
            'sector_rotation': {
                'params': {'lookback': 20, 'factor_weights': {'momentum': 0.45, 'quality': 0.30, 'value': 0.25}},
                'name': 'AI 行业轮动',
                'description': '主题扩散与风格切换阶段偏向行业轮动打分。',
            },
            'north_capital_track': {
                'params': {'lookback': 15, 'threshold': 0.015},
                'name': 'AI 北向跟踪',
                'description': '资金偏好明确时偏向价量共振的北向跟踪。',
            },
            'margin_divergence': {
                'params': {
                    'fear_threshold': 43,
                    'greed_threshold': 60,
                    'lookback': 12,
                    'rebound_window': 3,
                    'repair_drawdown_floor': -0.06,
                    'repair_rebound_pct': 0.012,
                    'dryup_window': 3,
                    'dryup_max_ratio': 0.9,
                    'liquidity_window': 8,
                    'entry_volume_floor_ratio': 1.0,
                    'structure_window': 4,
                    'structure_close_location_min': 0.58,
                    'structure_body_return_min': 0.002,
                    'max_hold_bars': 8,
                    'adverse_volume_break_ratio': 0.72,
                    'adverse_close_break_pct': -0.012,
                    'max_active_symbols': 2,
                    'universe_selection_profile': 'liquidity_divergence_fit_v1',
                },
                'name': 'AI 流动性背离修复',
                'description': '只在缩量止跌后出现放量修复和结构转强时参与高精度修复。',
            },
        }
        template = templates.get(strategy_type)
        if template is None:
            return None
        template_contract = _rule_template_contract(strategy_type)
        semantic_contract_bundle = _build_rule_semantic_contract_bundle(
            strategy_type,
            strategy_name=str(template["name"]),
            description=str(template["description"]),
            source=source,
            regime=regime,
            fg=fg,
            factor_summary=factor_summary,
            trade_plan=dict(template_contract.get("trade_plan") or {}),
            holding_horizon=dict(template_contract.get("holding_horizon") or {}),
            risk_rules=dict(template_contract.get("risk_rules") or {}),
            template_contract=template_contract,
        )
        metadata = {
            'generator_type': 'rule',
            'generation_reason': {
                'source': source,
                'fg': fg,
                'regime': regime,
                'factor_research': factor_summary,
                'template_generation_profile': template_contract.get('template_generation_profile'),
                'rule_template_contract': dict(template_contract.get('rule_template_contract') or {}),
            },
        }
        for key in (
            'holding_horizon',
            'trade_plan',
            'risk_rules',
            'position_sizing',
            'rebalance_rule',
            'portfolio_spec',
            'execution_assumptions',
            'validation_profile',
            'targeting_policy',
            'family_specialization',
            'holding_rationale',
            'alpha_half_life',
            'cost_sensitivity_grid',
            'position_model',
            'capacity_assumption',
            'market_regime_assumption',
            'failure_mode',
            'rule_template_contract',
        ):
            value = template_contract.get(key)
            if value:
                metadata[key] = deepcopy(value)
        for key in (
            "trade_plan",
            "evidence_chain",
            "prediction_contract",
            "confidence_contract",
            "claim_to_trade_plan_map",
        ):
            value = semantic_contract_bundle.get(key)
            if value:
                metadata[key] = deepcopy(value)
        return StrategySpec(
            strategy_type=strategy_type,
            params=dict(template['params']),
            name=str(template['name']),
            description=str(template['description']),
            tags=['rule', 'factor_research' if source == 'factor_research' else 'fear_greed'],
            metadata=metadata,
        )

    def generate(
        self,
        snapshot: dict,
        limit: int = 2,
        *,
        preferred_types: Optional[list[str]] = None,
    ) -> list[StrategySpec]:
        fg = int(snapshot.get('fear_greed_index') or 50)
        regime = 'greed' if fg >= 60 else ('fear' if fg < 45 else 'neutral')
        factor_summary = self._factor_research_summary(snapshot)
        factor_preferred_types = [
            item for item in factor_summary.get('preferred_strategy_types') or []
            if item in CATEGORY_MINIMUMS
        ]
        requested_types = [
            str(item).strip()
            for item in list(preferred_types or [])
            if str(item).strip() in CATEGORY_MINIMUMS
        ]
        if not factor_preferred_types:
            for factor_name in list(factor_summary.get('top_factor_names') or []):
                for strategy_type in preferred_strategy_types_for_factor(factor_name):
                    if strategy_type in CATEGORY_MINIMUMS and strategy_type not in factor_preferred_types:
                        factor_preferred_types.append(strategy_type)
        regime_defaults = (
            ['momentum', 'volatility_breakout', 'north_capital_track', 'ma_cross', 'quality_factor']
            if regime == 'greed'
            else ['value_factor', 'quality_factor', 'mean_reversion_short', 'gap_fill', 'rsi']
        )
        preferred_anchor = requested_types or factor_preferred_types
        strategy_order = list(dict.fromkeys([*requested_types, *factor_preferred_types, *regime_defaults]))
        specs: list[StrategySpec] = []
        for index, strategy_type in enumerate(strategy_order):
            source = 'factor_research' if index < len(preferred_anchor) and preferred_anchor else 'fear_greed'
            spec = self._build_rule_spec(
                strategy_type,
                fg=fg,
                regime=regime,
                source=source,
                factor_summary=factor_summary,
            )
            if spec is not None:
                specs.append(spec)
        return specs[: max(1, min(int(limit or 2), 10))]

from ._strategy_generators_external import _LLMProxyStrategyGeneratorExternalMixin
from ._strategy_generators_context import _LLMProxyStrategyGeneratorContextMixin
from ._strategy_generators_specs import _LLMProxyStrategyGeneratorSpecsMixin
from ._strategy_generators_generate import _LLMProxyStrategyGeneratorGenerateMixin


class LLMProxyStrategyGenerator(_LLMProxyStrategyGeneratorExternalMixin, _LLMProxyStrategyGeneratorContextMixin, _LLMProxyStrategyGeneratorSpecsMixin, _LLMProxyStrategyGeneratorGenerateMixin):
        def __init__(self):
            self.miner = LLMAlphaMiner()
            self.external_provider = get_strategy_llm_provider()
            self.last_report: dict[str, Any] = {}

        def get_last_report(self) -> dict[str, Any]:
            return dict(self.last_report)

