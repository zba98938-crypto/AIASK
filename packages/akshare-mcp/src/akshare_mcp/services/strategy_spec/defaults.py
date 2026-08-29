"""Default payload builders for strategy-spec generation."""

from __future__ import annotations

from typing import Any, Optional

from .constants import _FACTOR_VALIDATION_TYPES
from .normalizers import (
    _normalize_code_list,
    _normalize_turnover_band,
    _safe_float,
    _safe_int,
    _safe_normalize_research_task,
)

def _derive_half_life_semantics(alpha_half_life: Any) -> dict[str, Any]:
    half_life = _safe_float(alpha_half_life, 0.0)
    if half_life <= 0:
        return {}
    if half_life <= 3:
        return {
            "min_days": 1,
            "max_days": max(2, _safe_int(round(half_life * 1.5), 2)),
            "rebalance_interval_days": 1,
            "cooldown_window_days": 1,
            "expected_turnover_band": "very_high",
        }
    if half_life <= 8:
        min_days = max(2, _safe_int(round(half_life * 0.75), 2))
        max_days = max(min_days + 1, _safe_int(round(half_life * 1.5), min_days + 1))
        return {
            "min_days": min_days,
            "max_days": max_days,
            "rebalance_interval_days": max(2, _safe_int(round(half_life / 2.0), 2)),
            "cooldown_window_days": max(1, _safe_int(round(half_life / 3.0), 1)),
            "expected_turnover_band": "high",
        }
    if half_life <= 16:
        min_days = max(3, _safe_int(round(half_life * 0.8), 3))
        max_days = max(min_days + 1, _safe_int(round(half_life * 1.8), min_days + 1))
        return {
            "min_days": min_days,
            "max_days": max_days,
            "rebalance_interval_days": max(3, _safe_int(round(half_life * 0.75), 3)),
            "cooldown_window_days": max(2, _safe_int(round(half_life / 2.0), 2)),
            "expected_turnover_band": "medium",
        }
    min_days = max(5, _safe_int(round(half_life), 5))
    max_days = max(min_days + 1, _safe_int(round(half_life * 2.2), min_days + 1))
    return {
        "min_days": min_days,
        "max_days": max_days,
        "rebalance_interval_days": max(5, _safe_int(round(half_life), 5)),
        "cooldown_window_days": max(3, _safe_int(round(half_life * 0.75), 3)),
        "expected_turnover_band": "low",
    }


def _merge_holding_semantics(
    holding_horizon: dict[str, Any],
    *,
    holding_rationale: Any = None,
    alpha_half_life: Any = None,
) -> dict[str, Any]:
    result = dict(holding_horizon or {})
    derived = _derive_half_life_semantics(alpha_half_life)
    if result.get("rationale") in (None, "", [], {}) and holding_rationale not in (None, "", [], {}):
        result["rationale"] = holding_rationale
    if result.get("alpha_half_life") in (None, "", [], {}) and alpha_half_life not in (None, "", [], {}):
        result["alpha_half_life"] = _safe_float(alpha_half_life)
    for key in ("min_days", "max_days", "cooldown_window_days", "expected_turnover_band"):
        if result.get(key) in (None, "", [], {}) and derived.get(key) not in (None, "", [], {}):
            result[key] = derived.get(key)
    return result


def _merge_rebalance_semantics(
    rebalance_rule: dict[str, Any],
    *,
    task_source: str,
    holding_horizon: dict[str, Any],
    alpha_half_life: Any = None,
) -> dict[str, Any]:
    result = dict(rebalance_rule or {})
    derived = _derive_half_life_semantics(alpha_half_life)
    max_days = _safe_int(holding_horizon.get("max_days"), 0)
    rebalance_interval_days = max(
        1,
        _safe_int(
            result.get("rebalance_interval_days") or derived.get("rebalance_interval_days"),
            max(1, min(max_days or 10, max(1, (max_days or 10) // 2))),
        ),
    )
    if result.get("mode") in (None, "", [], {}):
        result["mode"] = (
            "event_driven_hold"
            if task_source == "event_driven"
            else ("periodic_rebalance" if rebalance_interval_days >= 3 else "signal_rebalance")
        )
    if task_source != "event_driven":
        result.setdefault("frequency_days", max(1, min(max_days or rebalance_interval_days, rebalance_interval_days)))
    result.setdefault("rebalance_interval_days", rebalance_interval_days)
    if result.get("cooldown_window_days") in (None, "", [], {}):
        result["cooldown_window_days"] = _safe_int(
            holding_horizon.get("cooldown_window_days") or derived.get("cooldown_window_days"),
            0,
        )
    if result.get("expected_turnover_band") in (None, "", [], {}):
        result["expected_turnover_band"] = (
            _normalize_turnover_band(holding_horizon.get("expected_turnover_band"))
            or derived.get("expected_turnover_band")
        )
    return result


def _resolve_capacity_bucket(
    capacity_assumption: dict[str, Any],
    *,
    target_symbols: list[str],
    position_model: str,
) -> str:
    explicit = str(
        capacity_assumption.get("capacity_bucket")
        or capacity_assumption.get("bucket")
        or ""
    ).strip().lower()
    if explicit:
        return explicit
    max_position_pct = _safe_float(capacity_assumption.get("max_position_pct"), 0.0)
    participation = _safe_float(capacity_assumption.get("capacity_participation_rate"), 0.0)
    symbol_count = max(_safe_int(capacity_assumption.get("symbol_count"), 0), len(target_symbols))
    normalized_model = str(position_model or "").strip().lower()
    if symbol_count <= 1 or "single" in normalized_model or max_position_pct >= 0.3 or participation >= 0.15:
        return "small"
    if symbol_count >= 8 and max_position_pct <= 0.12 and participation <= 0.08:
        return "large"
    return "mid"


def _resolve_turnover_cost_class(
    *,
    execution_assumptions: dict[str, Any],
    expected_turnover_band: str,
    capacity_bucket: str,
) -> str:
    slippage_bps = _safe_float(execution_assumptions.get("slippage_bps"), 0.0)
    market_impact_bps = _safe_float(execution_assumptions.get("market_impact_bps"), 0.0)
    if expected_turnover_band == "very_high" or slippage_bps >= 10 or market_impact_bps >= 4:
        return "high_touch"
    if expected_turnover_band == "high" or slippage_bps >= 5 or capacity_bucket == "small":
        return "medium_touch"
    return "low_touch"


def _resolve_position_sizing_rationale(
    *,
    position_model: str,
    target_symbols: list[str],
    capacity_bucket: str,
    expected_turnover_band: str,
) -> str:
    normalized_model = str(position_model or "").strip().lower()
    if "volatility" in normalized_model:
        return "volatility_budgeted_across_target_basket"
    if "single" in normalized_model or len(target_symbols) <= 1:
        return (
            "single_name_conviction_capped_by_capacity"
            if capacity_bucket in {"small", "mid"}
            else "single_name_conviction_with_liquidity_buffer"
        )
    if expected_turnover_band in {"high", "very_high"}:
        return "equal_weight_diversified_basket_to_limit_turnover_drag"
    return "equal_weight_diversified_basket"


def _default_holding_horizon(
    strategy_type: str,
    research_task: dict[str, Any],
    task_source: str,
    *,
    alpha_half_life: Any = None,
) -> dict[str, Any]:
    holding_window = dict(research_task.get("holding_window") or {})
    if holding_window:
        return _merge_holding_semantics(
            holding_window,
            alpha_half_life=alpha_half_life,
        )
    derived = _derive_half_life_semantics(alpha_half_life)
    if derived:
        return _merge_holding_semantics(derived, alpha_half_life=alpha_half_life)
    if task_source == "event_driven":
        return {"max_days": 10}
    if strategy_type == "quality_factor":
        return {"min_days": 30, "max_days": 84}
    if strategy_type in _FACTOR_VALIDATION_TYPES or strategy_type in {"macro_timing", "sector_rotation"}:
        return {"min_days": 5, "max_days": 24}
    if task_source in {"snapshot", "bulk_stock_matrix"}:
        if strategy_type == "momentum":
            return {"min_days": 14, "max_days": 42}
        if strategy_type == "event_structure_breakout":
            return {"min_days": 4, "max_days": 16}
        if strategy_type in {"ma_cross", "volatility_breakout", "north_capital_track", "margin_divergence"}:
            return {"min_days": 14, "max_days": 48}
        if strategy_type in {"gap_fill", "mean_reversion_short", "rsi"}:
            return {"min_days": 3, "max_days": 12}
        return {"min_days": 4, "max_days": 15}
    return {"max_days": 10}


def _default_trade_plan(strategy_type: str, task_source: str) -> dict[str, Any]:
    if task_source == "event_driven":
        return {
            "entry_bias": "event_follow_through",
            "exit_bias": "time_stop_or_signal_reversal",
        }
    if strategy_type in _FACTOR_VALIDATION_TYPES:
        return {
            "entry_bias": "cross_sectional_rank",
            "exit_bias": "rank_decay_or_periodic_rebalance",
        }
    if strategy_type == "momentum":
        return {
            "entry_bias": "trend_persistence_confirmation",
            "exit_bias": "false_breakout_or_momentum_decay",
        }
    if strategy_type == "quality_factor":
        return {
            "entry_bias": "quality_stability_with_trend_confirmation",
            "exit_bias": "quality_drift_or_rank_decay",
        }
    if strategy_type == "ma_cross":
        return {
            "entry_bias": "adaptive_cross_with_volume_confirmation",
            "exit_bias": "range_reentry_or_cross_failure",
        }
    if strategy_type == "rsi":
        return {
            "entry_bias": "oversold_repair_with_mean_reversion_confirmation",
            "exit_bias": "mean_reversion_completion_or_rsi_reset",
        }
    if strategy_type == "macro_timing":
        return {
            "entry_bias": "panic_repair_after_regime_confirmation",
            "exit_bias": "greed_extreme_or_regime_break",
        }
    if strategy_type == "margin_divergence":
        return {
            "entry_bias": "liquidity_divergence_repair_confirmation",
            "exit_bias": "liquidity_break_or_time_stop",
        }
    if strategy_type == "event_structure_breakout":
        return {
            "entry_bias": "event_structure_breakout_confirmation",
            "exit_bias": "breakout_failure_or_time_stop",
        }
    return {
        "entry_bias": "signal_confirmed",
        "exit_bias": "signal_or_time_stop",
    }


def _default_market_regime_assumption(strategy_type: str, task_source: str) -> dict[str, Any]:
    if task_source == "event_driven":
        return {
            "summary": "事件催化后的短窗口延续阶段更有效。",
            "preferred_regime": "event_follow_through",
            "avoid_regime": "post_event_mean_reversion",
        }
    if strategy_type == "momentum":
        return {
            "summary": "趋势扩张且龙头相对强度保持的阶段更有效，需要避免无量假突破与快速反抽。",
            "preferred_regime": "trend_expansion_with_persistence",
            "avoid_regime": "false_breakout_range_reversion",
        }
    if strategy_type == "ma_cross":
        return {
            "summary": "需要均线张口扩大并伴随量能确认，横盘噪声区间的频繁穿越应过滤。",
            "preferred_regime": "trend_expansion_with_volume_confirmation",
            "avoid_regime": "range_bound_chop",
        }
    if strategy_type in {"volatility_breakout"}:
        return {
            "summary": "趋势扩张或强势股持续领跑阶段更有效。",
            "preferred_regime": "trend_expansion",
            "avoid_regime": "range_bound_chop",
        }
    if strategy_type == "rsi":
        return {
            "summary": "熊市超跌后的修复阶段更有效，需要避免下跌加速和高噪声横盘。",
            "preferred_regime": "oversold_repair_with_stabilizing_liquidity",
            "avoid_regime": "downtrend_acceleration_or_noise_chop",
        }
    if strategy_type == "macro_timing":
        return {
            "summary": "极端恐慌后波动回落、风险偏好修复的阶段更有效，需要避免中性区间的来回抽打。",
            "preferred_regime": "panic_repair_with_volatility_stabilization",
            "avoid_regime": "mid_regime_whipsaw",
        }
    if strategy_type == "margin_divergence":
        return {
            "summary": "缩量止跌后出现放量修复且结构转强的阶段更有效，需要避免缩量阴跌与假修复。",
            "preferred_regime": "liquidity_repair_with_volume_reexpansion",
            "avoid_regime": "volume_vacuum_or_failed_rebound",
        }
    if strategy_type == "event_structure_breakout":
        return {
            "summary": "事件催化后的短窗口延续、缩量整理和放量突破共振阶段更有效，需要避免普通假突破与事件后均值回归。",
            "preferred_regime": "event_follow_through_with_structure_confirmation",
            "avoid_regime": "false_breakout_or_post_event_mean_reversion",
        }
    if strategy_type == "quality_factor":
        return {
            "summary": "基本面稳定扩散并与中期价格趋势共振时更有效，风格急切换和质量漂移阶段要回避。",
            "preferred_regime": "quality_stability_with_trend_resonance",
            "avoid_regime": "quality_drift_high_noise_rotation",
        }
    if strategy_type in {"value_factor", "growth_factor", "multi_factor"}:
        return {
            "summary": "慢变量扩散、基本面驱动占优的稳定阶段更有效。",
            "preferred_regime": "slow_factor_diffusion",
            "avoid_regime": "high_noise_rotation",
        }
    return {
        "summary": "流动性正常、成本可控的中性市场环境更有效。",
        "preferred_regime": "neutral_liquid_cn_equity",
        "avoid_regime": "illiquid_stressed_market",
    }


def _default_risk_rules(task_source: str, holding_horizon: dict[str, Any]) -> dict[str, Any]:
    max_holding_days = int(holding_horizon.get("max_days") or 0)
    return {
        "stop_loss_pct": 0.08 if task_source == "event_driven" else 0.1,
        "take_profit_pct": 0.18 if task_source == "event_driven" else 0.2,
        "max_holding_days": max_holding_days or (10 if task_source == "event_driven" else 20),
    }


def _default_position_sizing(target_symbols: list[str]) -> dict[str, Any]:
    multiple_names = len(target_symbols) > 1
    return {
        "mode": "equal_weight" if multiple_names else "single_name",
        "position_assumption": "equal_weight_proxy" if multiple_names else "single_name_full_notional",
    }


def _default_event_prefilter(
    strategy_type: str,
    validation_profile: dict[str, Any],
) -> dict[str, Any]:
    profile = dict(validation_profile or {})
    required = bool(
        profile.get("event_prefilter_required")
        or str(strategy_type or "").strip().lower() == "event_structure_breakout"
    )
    prefilter_profile = str(
        profile.get("event_prefilter_profile")
        or ("announcement_flow_sector_v1" if required else "")
    ).strip() or ("announcement_flow_sector_v1" if required else "")
    min_confirmations = max(
        1,
        _safe_int(profile.get("event_prefilter_min_confirmations"), 1),
    ) if required else 0
    return {
        "required": required,
        "profile": prefilter_profile or None,
        "allowed_sources": ["announcement", "fund_flow", "sector_catalyst"] if required else [],
        "observed_sources": [],
        "observed_confirmation_count": 0,
        "confirmation_count": 0,
        "min_confirmations": min_confirmations,
        "passed": not required,
        "status": "missing" if required else "not_required",
        "focus_industries": [],
        "event_anchor": {},
        "anchor_strength": None,
    }


def _default_targeting_policy(research_task: dict[str, Any]) -> dict[str, Any]:
    task = _safe_normalize_research_task(research_task)
    task_source = str(task.get("task_source") or "snapshot").strip().lower() or "snapshot"
    target_symbols = _normalize_code_list(
        task.get("target_symbols"),
        task.get("stock_pool"),
        limit=12,
    )
    stock_pool = dict(task.get("stock_pool") or {})
    target_alignment_contract = dict(task.get("target_alignment_contract") or {})
    selection_mode = str(
        stock_pool.get("selection_mode")
        or ("explicit" if target_symbols else "screened")
    ).strip().lower() or ("explicit" if target_symbols else "screened")
    universe_scope = str(
        stock_pool.get("universe_scope")
        or task.get("universe_scope")
        or ("task_target_only" if target_symbols else "market_screened")
    ).strip().lower() or ("task_target_only" if target_symbols else "market_screened")
    max_target_symbols = max(
        1,
        _safe_int(
            target_alignment_contract.get("max_candidate_target_symbols"),
            len(target_symbols) or 8,
        ),
    )
    mode = "targeted" if target_symbols else ("event_driven" if task_source == "event_driven" else "screened")
    payload: dict[str, Any] = {
        "mode": mode,
        "selection_mode": selection_mode,
        "source": str(task.get("generator_mode") or task.get("generator_type") or task_source or "research_task"),
        "reason": (
            "target_symbols_from_research_task"
            if target_symbols
            else "fallback_to_screened_universe"
        ),
        "task_source": task_source,
        "target_symbol_policy": str(
            task.get("target_symbol_policy")
            or ("strict_intersection" if task_source == "event_driven" else "prefer_intersection")
        ).strip().lower(),
        "universe_expansion_policy": str(
            task.get("universe_expansion_policy")
            or ("forbid" if task_source == "event_driven" else "allow_market_fallback")
        ).strip().lower(),
        "universe_scope": universe_scope,
        "max_target_symbols": max_target_symbols,
    }
    if target_symbols:
        payload["target_symbols"] = list(target_symbols[:max_target_symbols])
        payload["symbols"] = list(target_symbols[:max_target_symbols])
    return payload


def _default_constraint_check(
    *,
    target_symbols: list[str],
    research_task: dict[str, Any],
    targeting_policy: dict[str, Any],
) -> dict[str, Any]:
    task = _safe_normalize_research_task(research_task)
    research_symbols = _normalize_code_list(
        task.get("target_symbols"),
        task.get("stock_pool"),
        limit=12,
    )
    candidate_before = _normalize_code_list(target_symbols, limit=12)
    target_alignment_contract = dict(task.get("target_alignment_contract") or {})
    policy = str(
        dict(targeting_policy or {}).get("target_symbol_policy")
        or task.get("target_symbol_policy")
        or ("strict_intersection" if str(task.get("task_source") or "").strip().lower() == "event_driven" else "prefer_intersection")
    ).strip().lower()
    expansion_policy = str(
        dict(targeting_policy or {}).get("universe_expansion_policy")
        or task.get("universe_expansion_policy")
        or ("forbid" if str(task.get("task_source") or "").strip().lower() == "event_driven" else "allow_market_fallback")
    ).strip().lower()

    resolved = list(candidate_before)
    expansion_applied = False
    expansion_reason = ""
    expansion_source = ""
    constraint_violation = None

    contract_target_cap = _safe_int(
        target_alignment_contract.get("max_candidate_target_symbols"),
        _safe_int(dict(targeting_policy or {}).get("max_target_symbols"), len(resolved) or len(research_symbols) or 8),
    )
    resolved_limit = max(1, min(contract_target_cap or 8, 40))

    if research_symbols:
        intersection = [code for code in resolved if code in set(research_symbols)]
        if policy == "strict_intersection":
            if intersection:
                if intersection != resolved:
                    expansion_applied = True
                    expansion_reason = "strict_intersection_trimmed"
                    expansion_source = "research_task.target_symbols"
                resolved = list(intersection)
            else:
                resolved = list(research_symbols[:resolved_limit])
                expansion_applied = True
                expansion_reason = "fallback_research_symbols"
                expansion_source = "research_task.target_symbols"
                constraint_violation = "strict_intersection_empty"
        elif not resolved:
            resolved = list(research_symbols[:resolved_limit])
            expansion_applied = True
            expansion_reason = "fallback_research_symbols"
            expansion_source = "research_task.target_symbols"

    resolved = list(resolved[:resolved_limit])
    overlap_count = len(set(resolved).intersection(research_symbols))
    coverage_ratio = round(overlap_count / max(1, len(resolved)), 4) if resolved else 0.0
    intersection_ratio = round(overlap_count / max(1, len(research_symbols)), 4) if research_symbols else None

    alignment_contract_ok = True
    alignment_contract_violation = None
    if target_alignment_contract.get("quality_gate_enabled"):
        min_coverage_ratio = _safe_float(target_alignment_contract.get("min_coverage_ratio"), 0.0)
        min_intersection_ratio = (
            None
            if target_alignment_contract.get("min_intersection_ratio") is None
            else _safe_float(target_alignment_contract.get("min_intersection_ratio"), 0.0)
        )
        min_required_overlap_count = _safe_int(target_alignment_contract.get("min_required_overlap_count"), 0)
        if not resolved and research_symbols:
            alignment_contract_ok = False
            alignment_contract_violation = "empty_target_symbols_after_alignment"
        elif coverage_ratio < min_coverage_ratio:
            alignment_contract_ok = False
            alignment_contract_violation = "coverage_ratio_below_contract"
        elif min_intersection_ratio is not None and (intersection_ratio or 0.0) < min_intersection_ratio:
            alignment_contract_ok = False
            alignment_contract_violation = "intersection_ratio_below_contract"
        elif min_required_overlap_count > 0 and overlap_count < min_required_overlap_count:
            alignment_contract_ok = False
            alignment_contract_violation = "target_overlap_count_below_contract"

    return {
        "target_symbols_before_normalize": list(candidate_before),
        "target_symbols_after_normalize": list(resolved),
        "research_target_symbols": list(research_symbols),
        "same_theme_symbols": [],
        "target_symbol_policy": policy or None,
        "universe_expansion_policy": expansion_policy or None,
        "expansion_applied": expansion_applied,
        "expansion_reason": expansion_reason or None,
        "expansion_source": expansion_source or None,
        "constraint_violation": constraint_violation,
        "expansion_blocked_reason": None,
        "coverage_ratio": coverage_ratio,
        "intersection_ratio": intersection_ratio,
        "target_overlap_count": int(overlap_count),
        "alignment_contract_ok": alignment_contract_ok,
        "alignment_contract_violation": alignment_contract_violation,
        "target_alignment_contract": dict(target_alignment_contract),
    }


def _default_rebalance_rule(
    strategy_type: str,
    task_source: str,
    *,
    holding_horizon: Optional[dict[str, Any]] = None,
    alpha_half_life: Any = None,
) -> dict[str, Any]:
    derived = _derive_half_life_semantics(alpha_half_life)
    if derived:
        return _merge_rebalance_semantics(
            {},
            task_source=task_source,
            holding_horizon=_merge_holding_semantics(dict(holding_horizon or {}), alpha_half_life=alpha_half_life),
            alpha_half_life=alpha_half_life,
        )
    if task_source == "event_driven":
        return {"mode": "event_driven_hold"}
    if strategy_type == "event_structure_breakout":
        return {"mode": "signal_rebalance", "frequency_days": 4}
    if strategy_type == "quality_factor":
        return {"mode": "periodic_rebalance", "frequency_days": 28}
    if strategy_type in _FACTOR_VALIDATION_TYPES or strategy_type == "sector_rotation":
        return {"mode": "periodic_rebalance", "frequency_days": 10}
    if strategy_type == "macro_timing":
        return {"mode": "regime_rebalance", "frequency_days": 10}
    if task_source in {"snapshot", "bulk_stock_matrix"}:
        if strategy_type == "momentum":
            return {"mode": "periodic_rebalance", "frequency_days": 14}
        if strategy_type in {"ma_cross", "volatility_breakout", "north_capital_track", "margin_divergence"}:
            return {"mode": "periodic_rebalance", "frequency_days": 12}
        if strategy_type in {"gap_fill", "mean_reversion_short", "rsi"}:
            return {"mode": "periodic_rebalance", "frequency_days": 4}
    return {"mode": "signal_rebalance"}


def _default_family_specialization(
    strategy_type: str,
    task_source: str,
    *,
    holding_horizon: Optional[dict[str, Any]] = None,
    rebalance_rule: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    family = str(strategy_type or "").strip().lower()
    holding = dict(holding_horizon or {})
    rebalance = dict(rebalance_rule or {})
    max_days = max(1, _safe_int(holding.get("max_days"), 0) or 1)
    frequency_days = max(1, _safe_int(rebalance.get("frequency_days"), 0) or max(1, max_days // 2))
    if family == "momentum":
        return {
            "trend_persistence_regime": "trend_expansion_with_relative_strength_persistence",
            "false_breakout_filter": "prefer_volume_confirmed_breakout_and_positive_trend_slope",
            "peer_selection_mode": "target_plus_dynamic_family_peer",
            "holding_bias": f"hold_for_{max_days}_days_or_until_momentum_decay",
            "rebalance_bias": f"periodic_rebalance_every_{frequency_days}_days",
        }
    if family == "quality_factor":
        return {
            "rebalance_bias": "low_frequency_quality_refresh",
            "quality_trend_resonance": "require_fundamental_stability_and_price_trend_alignment",
            "quality_drift_detection": "monitor_rank_margin_cashflow_stability_deterioration",
            "peer_selection_mode": "target_plus_dynamic_family_peer",
            "compounding_window": "prefer_slow_compounding_validation_window",
            "holding_bias": f"slow_factor_diffusion_hold_{max_days}_days",
            "task_source": task_source or None,
        }
    if family == "ma_cross":
        return {
            "adaptive_span_logic": "fast_slow_span_scaled_by_regime_and_noise_level",
            "range_filter": "avoid_crosses_when_long_ma_is_flat_and_price_is_range_bound",
            "volume_confirmation": "prefer_crosses_with_volume_ratio_confirmation",
            "holding_bias": f"trend_follow_hold_{max_days}_days",
            "rebalance_bias": f"periodic_rebalance_every_{frequency_days}_days",
        }
    if family == "rsi":
        return {
            "mean_reversion_confirmation": "require_oversold_and_price_dislocation_with_stabilization",
            "repair_confirmation": "require_rebound_from_recent_low_and_rsi_reclaim_before_entry",
            "entry_regime_filter": "bear_calm_or_bear_volatile_only",
            "entry_selectivity": "strict_repair_only",
            "noise_filter": "path_noise_ratio_ceiling_6",
            "exit_discipline": "mean_reversion_exit_after_4_bars_or_time_stop_6_bars",
            "adverse_regime_exit": "exit_when_range_volatile_noise_spikes_after_entry",
            "event_semantics": "stateful_entry_exit_masks",
            "failure_mode_focus": "catching_falling_knife_or_overtrading",
            "holding_bias": f"repair_window_hold_{max_days}_days_or_until_mean_reversion",
            "rebalance_bias": f"signal_rebalance_every_{frequency_days}_days",
        }
    if family == "macro_timing":
        return {
            "regime_confirmation": "require_fear_recovery_and_volatility_stabilization_before_entry",
            "whipsaw_filter": "avoid_mid_regime_flip_flops_and_reenter_only_after_cooldown",
            "holding_bias": f"regime_hold_{max_days}_days_or_until_regime_break",
            "rebalance_bias": f"regime_rebalance_every_{frequency_days}_days",
        }
    if family == "event_structure_breakout":
        return {
            "event_impulse_confirmation": "require_recent_impulse_before_breakout_attempt",
            "breakout_confirmation": "require_close_above_prior_high_with_buffer",
            "liquidity_confirmation": "require_volume_reexpansion_at_breakout",
            "structure_confirmation": "require_high_close_location_and_positive_body",
            "adverse_breakout_exit": "exit_on_breakout_failure_or_volume_fade",
            "holding_bias": f"event_follow_through_hold_{max_days}_days",
            "rebalance_bias": f"signal_rebalance_every_{frequency_days}_days",
        }
    if family == "margin_divergence":
        return {
            "repair_confirmation": "require_medium_window_drawdown_and_short_window_rebound",
            "liquidity_confirmation": "require_pre_entry_volume_dryup_and_entry_volume_reexpansion",
            "structure_confirmation": "require_high_close_location_and_positive_body",
            "adverse_regime_exit": "exit_on_volume_break_or_failed_rebound",
            "holding_bias": f"liquidity_repair_hold_{max_days}_days_or_until_break",
            "rebalance_bias": f"signal_rebalance_every_{frequency_days}_days",
        }
    return {}


def _default_portfolio_spec(target_symbols: list[str]) -> dict[str, Any]:
    multiple_names = len(target_symbols) > 1
    return {
        "position_assumption": "equal_weight_proxy" if multiple_names else "single_name_full_notional",
        "target_weight_scheme": "equal_weight" if multiple_names else "single_name",
    }


def _default_execution_assumptions(task_source: str) -> dict[str, Any]:
    return {
        "commission_rate": 0.00025,
        "slippage_bps": 8 if task_source == "event_driven" else 5,
        "tradability_filter": True,
        "slippage_model": "fixed",
    }


def _runtime_playbook_family(strategy_type: str) -> str:
    family = str(strategy_type or "").strip().lower()
    if family in {"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout"}:
        return "trend"
    if family in {"quality_factor", "value_factor"}:
        return "slow_factor"
    return "default"
