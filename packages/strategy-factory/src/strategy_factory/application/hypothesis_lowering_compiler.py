"""Structured hypothesis lowering for L2 candidate compilation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..domain.targets import (
    _apply_target_symbol_policy,
    _normalize_research_task_contract,
    _normalize_target_codes,
)
from .candidate_contract import resolve_candidate_validation_profile
from .precompile_contract import validate_precompile_candidate_contract
from .semantic_contract import (
    audit_candidate_semantic_contract,
    normalize_semantic_contract_fields,
)

_EMPTY_VALUES = (None, "", [], {})
_REQUIRED_HYPOTHESIS_FIELDS = (
    "alpha_hypothesis",
    "failure_mode",
    "target_universe_hypothesis",
    "family_hint",
    "holding_rationale",
    "alpha_half_life",
    "cost_sensitivity_grid",
    "position_model",
    "capacity_assumption",
    "market_regime_assumption",
    "validation_focus",
)

_FAMILY_SPECIFIC_REQUIREMENTS = {
    "momentum": (
        "trend_persistence_logic",
        "failure_scenario",
        "false_breakout_filter",
    ),
    "quality_factor": (
        "quality_metrics",
        "holding_consistency_explanation",
        "quality_drift_detection",
    ),
    "ma_cross": (
        "trend_noise_separation",
        "range_filter",
        "volume_confirmation",
    ),
}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _normalize_stock_pool(payload: Any, target_symbols: list[str]) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        source = dict(payload)
        symbols = list(
            _normalize_target_codes(
                source.get("symbols")
                or source.get("codes")
                or source.get("stock_codes")
                or target_symbols,
                limit=8,
            )
        )
        return {
            "selection_mode": _string(
                source.get("selection_mode")
                or source.get("mode")
                or ("explicit" if symbols else "screened")
            )
            or ("explicit" if symbols else "screened"),
            "symbols": symbols,
            "filters": dict(source.get("filters") or {}),
            "rationale": source.get("rationale"),
        }
    return {
        "selection_mode": "explicit" if target_symbols else "screened",
        "symbols": list(target_symbols),
        "filters": {},
        "rationale": None,
    }


def _normalize_turnover_band(value: Any) -> str:
    token = _string(value).lower()
    if token in {"very_high", "high", "medium", "low"}:
        return token
    return ""


def _derive_half_life_profile(alpha_half_life: float) -> dict[str, Any]:
    half_life = max(float(alpha_half_life or 0.0), 0.0)
    if half_life <= 0:
        return {}
    if half_life <= 3:
        band = "very_high"
        min_days = 1
        max_days = max(2, _safe_int(round(half_life * 1.5), 2))
        rebalance_interval_days = 1
        cooldown_window_days = 1
    elif half_life <= 8:
        band = "high"
        min_days = max(2, _safe_int(round(half_life * 0.75), 2))
        max_days = max(min_days + 1, _safe_int(round(half_life * 1.5), min_days + 1))
        rebalance_interval_days = max(2, _safe_int(round(half_life / 2.0), 2))
        cooldown_window_days = max(1, _safe_int(round(half_life / 3.0), 1))
    elif half_life <= 16:
        band = "medium"
        min_days = max(3, _safe_int(round(half_life * 0.8), 3))
        max_days = max(min_days + 1, _safe_int(round(half_life * 1.8), min_days + 1))
        rebalance_interval_days = max(3, _safe_int(round(half_life * 0.75), 3))
        cooldown_window_days = max(2, _safe_int(round(half_life / 2.0), 2))
    else:
        band = "low"
        min_days = max(5, _safe_int(round(half_life), 5))
        max_days = max(min_days + 1, _safe_int(round(half_life * 2.2), min_days + 1))
        rebalance_interval_days = max(5, _safe_int(round(half_life), 5))
        cooldown_window_days = max(3, _safe_int(round(half_life * 0.75), 3))
    return {
        "alpha_half_life": round(half_life, 4),
        "min_days": int(min_days),
        "max_days": int(max_days),
        "rebalance_interval_days": int(min(rebalance_interval_days, max_days)),
        "cooldown_window_days": int(min(cooldown_window_days, max_days)),
        "expected_turnover_band": band,
    }


def _resolve_capacity_bucket(
    capacity_assumption: Mapping[str, Any],
    *,
    target_symbols: list[str],
    position_model: str,
) -> str:
    explicit = _string(
        capacity_assumption.get("capacity_bucket")
        or capacity_assumption.get("bucket")
    ).lower()
    if explicit:
        return explicit
    max_position_pct = _safe_float(capacity_assumption.get("max_position_pct"), 0.0)
    participation = _safe_float(capacity_assumption.get("capacity_participation_rate"), 0.0)
    symbol_count = max(
        _safe_int(capacity_assumption.get("symbol_count"), 0),
        len(target_symbols),
    )
    normalized_model = _string(position_model).lower()
    if symbol_count <= 1 or "single" in normalized_model or max_position_pct >= 0.3 or participation >= 0.15:
        return "small"
    if symbol_count >= 8 and max_position_pct <= 0.12 and participation <= 0.08:
        return "large"
    return "mid"


def _resolve_turnover_cost_class(
    *,
    cost_sensitivity_grid: Mapping[str, Any],
    expected_turnover_band: str,
    capacity_bucket: str,
) -> str:
    base_case = _as_dict(cost_sensitivity_grid.get("base_case"))
    slippage_bps = _safe_float(base_case.get("slippage_bps"), 0.0)
    market_impact_bps = _safe_float(base_case.get("market_impact_bps"), 0.0)
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
    normalized_model = _string(position_model).lower()
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


def _coerce_market_regime_assumption(value: Any) -> Any:
    if isinstance(value, Mapping):
        payload = dict(value)
        if payload:
            return payload
    token = _string(value)
    return token or None


def _coerce_family_specific_hypothesis(
    strategy_type: str,
    hypothesis: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    family = _string(strategy_type).lower()
    required_fields = _FAMILY_SPECIFIC_REQUIREMENTS.get(family)
    payload = _as_dict(hypothesis.get("family_specific_hypothesis"))
    if not required_fields:
        return payload, []
    missing = [
        field
        for field in required_fields
        if payload.get(field) in _EMPTY_VALUES
        or (isinstance(payload.get(field), str) and not _string(payload.get(field)))
    ]
    return payload, missing


def _coerce_position_sizing(
    explicit: Optional[Mapping[str, Any]],
    *,
    position_model: str,
    target_symbols: list[str],
    capacity_bucket: str,
    expected_turnover_band: str,
) -> dict[str, Any]:
    payload = dict(explicit or {})
    if payload:
        result = dict(payload)
    else:
        normalized_model = _string(position_model).lower()
        multiple_names = len(target_symbols) > 1
        if "single" in normalized_model and not multiple_names:
            result = {
                "mode": "single_name",
                "position_assumption": "single_name_full_notional",
            }
        elif "volatility" in normalized_model:
            result = {
                "mode": "equal_weight",
                "position_assumption": "equal_weight_proxy",
                "weight_method": "volatility_budget",
            }
        else:
            result = {
                "mode": "equal_weight" if multiple_names else "single_name",
                "position_assumption": "equal_weight_proxy"
                if multiple_names
                else "single_name_full_notional",
            }
    result.setdefault("capacity_bucket", capacity_bucket or None)
    result.setdefault(
        "position_sizing_rationale",
        _resolve_position_sizing_rationale(
            position_model=position_model,
            target_symbols=target_symbols,
            capacity_bucket=capacity_bucket,
            expected_turnover_band=expected_turnover_band,
        ),
    )
    result.setdefault("expected_turnover_band", expected_turnover_band or None)
    return result


def _coerce_portfolio_spec(
    explicit: Optional[Mapping[str, Any]],
    *,
    position_sizing: Mapping[str, Any],
    capacity_assumption: Any,
    target_symbols: list[str],
    capacity_bucket: str,
    expected_turnover_band: str,
) -> dict[str, Any]:
    payload = dict(explicit or {})
    if payload:
        portfolio_spec = dict(payload)
    else:
        capacity = _as_dict(capacity_assumption)
        multiple_names = len(target_symbols) > 1
        max_position_pct = capacity.get("max_position_pct")
        if max_position_pct in _EMPTY_VALUES and target_symbols:
            max_position_pct = round(min(0.35, 1.0 / max(1, len(target_symbols))), 4)
        portfolio_spec = {
            "position_assumption": position_sizing.get("position_assumption"),
            "target_weight_scheme": "equal_weight"
            if multiple_names
            else "single_name",
        }
        if max_position_pct not in _EMPTY_VALUES:
            portfolio_spec["max_position_pct"] = _safe_float(max_position_pct)
        if capacity.get("weight_method") not in _EMPTY_VALUES:
            portfolio_spec["weight_method"] = capacity.get("weight_method")
    portfolio_spec.setdefault(
        "position_sizing_rationale",
        position_sizing.get("position_sizing_rationale"),
    )
    portfolio_spec.setdefault("capacity_bucket", capacity_bucket or None)
    portfolio_spec.setdefault("expected_turnover_band", expected_turnover_band or None)
    return portfolio_spec


def _coerce_execution_assumptions(
    explicit: Optional[Mapping[str, Any]],
    *,
    cost_sensitivity_grid: Mapping[str, Any],
    capacity_bucket: str,
    turnover_cost_class: str,
    expected_turnover_band: str,
) -> dict[str, Any]:
    payload = dict(explicit or {})
    if payload:
        execution_assumptions = dict(payload)
    else:
        base_case = _as_dict(cost_sensitivity_grid.get("base_case"))
        if not base_case:
            stress_cases = list(cost_sensitivity_grid.get("stress_cases") or [])
            if stress_cases:
                base_case = _as_dict(stress_cases[0])
        if not base_case:
            return {}
        execution_assumptions = {
            "commission_rate": _safe_float(base_case.get("commission_rate"), 0.0),
            "slippage_bps": _safe_float(base_case.get("slippage_bps"), 0.0),
            "tradability_filter": (
                True
                if base_case.get("tradability_filter") in _EMPTY_VALUES
                else bool(base_case.get("tradability_filter"))
            ),
            "slippage_model": _string(base_case.get("slippage_model")) or "fixed",
        }
    execution_assumptions.setdefault("capacity_bucket", capacity_bucket or None)
    execution_assumptions.setdefault("turnover_cost_class", turnover_cost_class or None)
    execution_assumptions.setdefault(
        "capacity_participation_rate",
        _safe_float(
            _as_dict(cost_sensitivity_grid.get("base_case")).get("capacity_participation_rate"),
            _safe_float(_as_dict(cost_sensitivity_grid.get("base_case")).get("adv_ratio_limit"), 0.0),
        ),
    )
    execution_assumptions.setdefault(
        "market_impact_bps",
        _safe_float(_as_dict(cost_sensitivity_grid.get("base_case")).get("market_impact_bps"), 0.0),
    )
    execution_assumptions.setdefault("expected_turnover_band", expected_turnover_band or None)
    return execution_assumptions


def _coerce_validation_profile(
    candidate: Optional[Mapping[str, Any]],
    *,
    research_task: Mapping[str, Any],
    validation_focus: str,
) -> dict[str, Any]:
    explicit = _as_dict((candidate or {}).get("validation_profile"))
    if explicit:
        return explicit
    return resolve_candidate_validation_profile(
        {
            **dict(candidate or {}),
            "validation_profile": {
                "validation_focus": validation_focus,
            },
        },
        research_task=research_task,
    )


def _coerce_holding_horizon(
    explicit: Optional[Mapping[str, Any]],
    *,
    holding_rationale: str,
    alpha_half_life: float,
    derived_profile: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    profile = dict(derived_profile or _derive_half_life_profile(alpha_half_life))
    payload = dict(explicit or {})
    if payload:
        result = dict(payload)
        if result.get("rationale") in _EMPTY_VALUES and holding_rationale:
            result["rationale"] = holding_rationale
        if result.get("alpha_half_life") in _EMPTY_VALUES and alpha_half_life > 0:
            result["alpha_half_life"] = alpha_half_life
        if result.get("cooldown_window_days") in _EMPTY_VALUES and profile.get("cooldown_window_days") not in _EMPTY_VALUES:
            result["cooldown_window_days"] = profile.get("cooldown_window_days")
        if result.get("expected_turnover_band") in _EMPTY_VALUES and profile.get("expected_turnover_band") not in _EMPTY_VALUES:
            result["expected_turnover_band"] = profile.get("expected_turnover_band")
        return result
    if alpha_half_life <= 0 and not profile:
        return {}
    result = {
        "min_days": max(1, _safe_int(profile.get("min_days"), max(1, _safe_int(round(alpha_half_life / 2.0), 1)))),
        "max_days": max(1, _safe_int(profile.get("max_days"), max(1, _safe_int(round(alpha_half_life), 1)))),
        "alpha_half_life": round(alpha_half_life, 4),
        "rationale": holding_rationale,
    }
    if profile.get("cooldown_window_days") not in _EMPTY_VALUES:
        result["cooldown_window_days"] = _safe_int(profile.get("cooldown_window_days"), 0)
    if profile.get("expected_turnover_band") not in _EMPTY_VALUES:
        result["expected_turnover_band"] = profile.get("expected_turnover_band")
    return result


def _coerce_rebalance_rule(
    explicit: Optional[Mapping[str, Any]],
    *,
    research_task: Mapping[str, Any],
    holding_horizon: Mapping[str, Any],
    derived_profile: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    profile = dict(derived_profile or {})
    payload = dict(explicit or {})
    if payload:
        result = dict(payload)
        if result.get("cooldown_window_days") in _EMPTY_VALUES and profile.get("cooldown_window_days") not in _EMPTY_VALUES:
            result["cooldown_window_days"] = profile.get("cooldown_window_days")
        if result.get("rebalance_interval_days") in _EMPTY_VALUES and profile.get("rebalance_interval_days") not in _EMPTY_VALUES:
            result["rebalance_interval_days"] = profile.get("rebalance_interval_days")
        if result.get("expected_turnover_band") in _EMPTY_VALUES and profile.get("expected_turnover_band") not in _EMPTY_VALUES:
            result["expected_turnover_band"] = profile.get("expected_turnover_band")
        return result
    task_source = _string(research_task.get("task_source")).lower()
    max_days = _safe_int(holding_horizon.get("max_days"), 0)
    rebalance_interval_days = max(
        1,
        _safe_int(
            profile.get("rebalance_interval_days"),
            max(1, min(max_days, max(1, max_days // 2))),
        ),
    )
    if task_source == "event_driven":
        return {
            "mode": "event_driven_hold",
            "rebalance_interval_days": rebalance_interval_days,
            "cooldown_window_days": _safe_int(profile.get("cooldown_window_days"), 0),
            "expected_turnover_band": profile.get("expected_turnover_band"),
        }
    if max_days > 0:
        return {
            "mode": "periodic_rebalance" if rebalance_interval_days >= 3 else "signal_rebalance",
            "frequency_days": max(1, min(max_days, rebalance_interval_days)),
            "rebalance_interval_days": rebalance_interval_days,
            "cooldown_window_days": _safe_int(profile.get("cooldown_window_days"), 0),
            "expected_turnover_band": profile.get("expected_turnover_band"),
        }
    return {}


@dataclass(slots=True)
class HypothesisLoweringCompileResult:
    accepted: bool
    candidate: dict[str, Any] = field(default_factory=dict)
    hypothesis_artifact: dict[str, Any] = field(default_factory=dict)
    reject_reasons: list[str] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": bool(self.accepted),
            "candidate": dict(self.candidate),
            "hypothesis_artifact": dict(self.hypothesis_artifact),
            "reject_reasons": list(self.reject_reasons),
            "audit": dict(self.audit),
        }


class HypothesisLoweringCompiler:
    """Fails closed when structured hypothesis cannot be lowered safely."""

    @classmethod
    def lower(
        cls,
        candidate: Optional[Mapping[str, Any]],
        *,
        hypothesis: Optional[Mapping[str, Any]],
        research_task: Optional[Mapping[str, Any]] = None,
        source: str = "external_llm",
    ) -> HypothesisLoweringCompileResult:
        payload = dict(candidate or {})
        normalized_task = _normalize_research_task_contract(
            research_task or payload.get("research_task") or {}
        )
        structured_hypothesis = dict(hypothesis or {})
        semantic_fields = normalize_semantic_contract_fields(payload)
        reject_reasons: list[str] = []

        if not structured_hypothesis:
            reject_reasons.append("hypothesis_missing")
        else:
            for field in _REQUIRED_HYPOTHESIS_FIELDS:
                value = structured_hypothesis.get(field)
                if value in _EMPTY_VALUES or (
                    isinstance(value, str) and not value.strip()
                ):
                    reject_reasons.append(f"hypothesis_missing:{field}")

        strategy_type = _string(payload.get("strategy_type")).lower()
        if not strategy_type:
            reject_reasons.append("strategy_type_missing")
        dsl = _as_dict(payload.get("dsl"))
        if not dsl or not dsl.get("entry") or not dsl.get("exit"):
            reject_reasons.append("dsl_missing")

        raw_targets = [
            payload.get("target_symbols"),
            payload.get("stock_pool"),
            (dsl.get("metadata") or {}).get("target_symbols"),
            (dsl.get("metadata") or {}).get("stock_pool"),
        ]
        targeted_fallback_symbols = [
            normalized_task.get("same_theme_symbols"),
            normalized_task.get("theme_members"),
            (normalized_task.get("event_context") or {}).get("same_theme_symbols"),
            (normalized_task.get("event_context") or {}).get("theme_members"),
            normalized_task.get("target_symbols"),
        ]
        broad_fallback_symbols = [
            normalized_task.get("target_symbols"),
            normalized_task.get("stock_pool"),
        ]
        targeted_task = bool(list(normalized_task.get("target_symbols") or []))
        target_resolution = _apply_target_symbol_policy(
            raw_targets,
            normalized_task,
            fallback_symbols=(
                targeted_fallback_symbols if targeted_task else broad_fallback_symbols
            ),
            limit=8,
        )
        target_symbols = list(target_resolution.get("target_symbols") or [])
        if not target_symbols:
            reject_reasons.append("target_symbols_missing_after_alignment")
        stock_pool = _normalize_stock_pool(payload.get("stock_pool"), target_symbols)
        family_specific_hypothesis, family_specific_missing_fields = _coerce_family_specific_hypothesis(
            strategy_type,
            structured_hypothesis,
        )
        for field in family_specific_missing_fields:
            reject_reasons.append(f"hypothesis_missing:family_specific_hypothesis.{field}")

        holding_rationale = _string(structured_hypothesis.get("holding_rationale"))
        alpha_half_life = _safe_float(structured_hypothesis.get("alpha_half_life"), 0.0)
        if alpha_half_life <= 0:
            reject_reasons.append("hypothesis_invalid:alpha_half_life")
        derived_holding_profile = _derive_half_life_profile(alpha_half_life)
        holding_horizon = _coerce_holding_horizon(
            payload.get("holding_horizon"),
            holding_rationale=holding_rationale,
            alpha_half_life=alpha_half_life,
            derived_profile=derived_holding_profile,
        )
        trade_plan = _as_dict(payload.get("trade_plan"))
        if not trade_plan:
            reject_reasons.append("trade_plan_missing")
        risk_rules = _as_dict(payload.get("risk_rules"))
        if not risk_rules:
            reject_reasons.append("risk_rules_missing")
        elif holding_horizon.get("max_days") not in _EMPTY_VALUES and risk_rules.get("max_holding_days") in _EMPTY_VALUES:
            risk_rules["max_holding_days"] = _safe_int(holding_horizon.get("max_days"), 0)
        if (
            risk_rules.get("cooldown_window_days") in _EMPTY_VALUES
            and derived_holding_profile.get("cooldown_window_days") not in _EMPTY_VALUES
        ):
            risk_rules["cooldown_window_days"] = _safe_int(
                derived_holding_profile.get("cooldown_window_days"),
                0,
            )

        market_regime_assumption = _coerce_market_regime_assumption(
            structured_hypothesis.get("market_regime_assumption")
        )
        if market_regime_assumption in _EMPTY_VALUES:
            reject_reasons.append("hypothesis_missing:market_regime_assumption")

        cost_sensitivity_grid = _as_dict(
            structured_hypothesis.get("cost_sensitivity_grid")
        )
        expected_turnover_band = _normalize_turnover_band(
            holding_horizon.get("expected_turnover_band")
            or derived_holding_profile.get("expected_turnover_band")
        )
        capacity_bucket = _resolve_capacity_bucket(
            _as_dict(structured_hypothesis.get("capacity_assumption")),
            target_symbols=target_symbols,
            position_model=_string(structured_hypothesis.get("position_model")),
        )
        turnover_cost_class = _resolve_turnover_cost_class(
            cost_sensitivity_grid=cost_sensitivity_grid,
            expected_turnover_band=expected_turnover_band,
            capacity_bucket=capacity_bucket,
        )

        position_sizing = _coerce_position_sizing(
            payload.get("position_sizing"),
            position_model=_string(structured_hypothesis.get("position_model")),
            target_symbols=target_symbols,
            capacity_bucket=capacity_bucket,
            expected_turnover_band=expected_turnover_band,
        )
        portfolio_spec = _coerce_portfolio_spec(
            payload.get("portfolio_spec"),
            position_sizing=position_sizing,
            capacity_assumption=structured_hypothesis.get("capacity_assumption"),
            target_symbols=target_symbols,
            capacity_bucket=capacity_bucket,
            expected_turnover_band=expected_turnover_band,
        )
        execution_assumptions = _coerce_execution_assumptions(
            payload.get("execution_assumptions"),
            cost_sensitivity_grid=cost_sensitivity_grid,
            capacity_bucket=capacity_bucket,
            turnover_cost_class=turnover_cost_class,
            expected_turnover_band=expected_turnover_band,
        )
        if not execution_assumptions:
            reject_reasons.append("execution_assumptions_missing")
        validation_focus = _string(structured_hypothesis.get("validation_focus")).lower()
        validation_profile = _coerce_validation_profile(
            payload,
            research_task=normalized_task,
            validation_focus=validation_focus,
        )
        rebalance_rule = _coerce_rebalance_rule(
            payload.get("rebalance_rule"),
            research_task=normalized_task,
            holding_horizon=holding_horizon,
            derived_profile=derived_holding_profile,
        )
        if not rebalance_rule:
            reject_reasons.append("rebalance_rule_missing")

        position_sizing_rationale = _string(
            position_sizing.get("position_sizing_rationale")
            or portfolio_spec.get("position_sizing_rationale")
        )
        if not position_sizing_rationale:
            reject_reasons.append("position_sizing_rationale_missing")
        if not expected_turnover_band:
            reject_reasons.append("expected_turnover_band_missing")
        if not capacity_bucket:
            reject_reasons.append("capacity_bucket_missing")
        if not turnover_cost_class:
            reject_reasons.append("turnover_cost_class_missing")

        trade_plan.setdefault("expected_turnover_band", expected_turnover_band or None)
        trade_plan.setdefault(
            "cooldown_window_days",
            _safe_int(holding_horizon.get("cooldown_window_days"), 0),
        )

        targeting_policy = {
            "target_symbol_policy": normalized_task.get("target_symbol_policy"),
            "universe_expansion_policy": normalized_task.get("universe_expansion_policy"),
            "validation_focus": validation_focus or normalized_task.get("validation_focus"),
        }
        candidate_payload = {
            **payload,
            "strategy_type": strategy_type,
            "research_task": dict(normalized_task),
            "target_symbols": list(target_symbols),
            "stock_pool": dict(stock_pool),
            "holding_horizon": dict(holding_horizon),
            "trade_plan": dict(trade_plan),
            "risk_rules": dict(risk_rules),
            "position_sizing": dict(position_sizing),
            "rebalance_rule": dict(rebalance_rule),
            "portfolio_spec": dict(portfolio_spec),
            "execution_assumptions": dict(execution_assumptions),
            "validation_profile": dict(validation_profile),
            "targeting_policy": dict(targeting_policy),
            "constraint_check": dict(target_resolution.get("constraint_check") or {}),
            "hypothesis": _string(structured_hypothesis.get("alpha_hypothesis")),
            "holding_rationale": holding_rationale,
            "alpha_half_life": alpha_half_life,
            "cost_sensitivity_grid": dict(cost_sensitivity_grid),
            "position_model": _string(structured_hypothesis.get("position_model")),
            "capacity_assumption": structured_hypothesis.get("capacity_assumption"),
            "market_regime_assumption": market_regime_assumption,
            "validation_focus": validation_focus,
            "family_specialization": dict(family_specific_hypothesis),
            "position_sizing_rationale": position_sizing_rationale,
            "capacity_bucket": capacity_bucket,
            "turnover_cost_class": turnover_cost_class,
            "expected_turnover_band": expected_turnover_band,
            "economic_semantics_score": _safe_int(
                structured_hypothesis.get("economic_semantics_score"),
                0,
            ),
            "economic_semantics_missing_fields": list(
                structured_hypothesis.get("economic_semantics_missing_fields") or []
            ),
            "economic_semantics_complete": bool(
                structured_hypothesis.get("economic_semantics_complete")
            ),
            "tags": list(
                dict.fromkeys(
                    [
                        *list(payload.get("tags") or []),
                        "llm_hypothesis_lowered",
                    ]
                )
            ),
        }
        for key in (
            "evidence_chain",
            "prediction_contract",
            "confidence_contract",
            "evidence_alignment_audit",
            "dsl_support_audit",
        ):
            value = semantic_fields.get(key)
            if isinstance(value, Mapping) and dict(value):
                candidate_payload[key] = dict(value)
        for key in ("legacy_semantic_contract", "contradiction_count", "proxy_dependency_score"):
            value = semantic_fields.get(key)
            if value not in _EMPTY_VALUES:
                candidate_payload[key] = value
        if reject_reasons:
            return HypothesisLoweringCompileResult(
                accepted=False,
                candidate={},
                hypothesis_artifact=dict(structured_hypothesis),
                reject_reasons=list(dict.fromkeys(reject_reasons)),
                audit={
                    "source": source,
                    "target_symbols": list(target_symbols),
                    "rejected_at": "pre_lowering",
                },
            )

        validation = validate_precompile_candidate_contract(
            candidate_payload,
            research_task=normalized_task,
            source=source,
        )
        if not validation.accepted:
            reject_reasons = list(validation.reject_reasons or [])
            return HypothesisLoweringCompileResult(
                accepted=False,
                candidate={},
                hypothesis_artifact=dict(structured_hypothesis),
                reject_reasons=reject_reasons,
                audit={
                    "source": source,
                    "target_symbols": list(target_symbols),
                    "constraint_check": dict(validation.constraint_check),
                    "precompile_validation": validation.to_dict(),
                    "rejected_at": "precompile",
                },
            )

        candidate_payload["portfolio_spec"] = dict(validation.portfolio_spec or portfolio_spec)
        candidate_payload["execution_assumptions"] = dict(
            validation.execution_assumptions or execution_assumptions
        )
        candidate_payload["validation_profile"] = dict(
            validation.validation_profile or validation_profile
        )
        candidate_payload["constraint_check"] = dict(validation.constraint_check or {})

        hypothesis_artifact = {
            **dict(structured_hypothesis),
            "source": source,
        }
        candidate_payload["hypothesis_artifact"] = hypothesis_artifact
        candidate_payload["hypothesis_artifact_id"] = hypothesis_artifact.get("artifact_id")

        semantic_audit = audit_candidate_semantic_contract(candidate_payload)
        candidate_payload["evidence_alignment_audit"] = dict(semantic_audit)
        candidate_payload["legacy_semantic_contract"] = bool(
            semantic_audit.get("legacy_semantic_contract")
        )
        candidate_payload["contradiction_count"] = _safe_int(
            semantic_audit.get("contradiction_count"),
            0,
        )
        candidate_payload["proxy_dependency_score"] = _safe_float(
            semantic_audit.get("proxy_dependency_score"),
            0.0,
        )
        if semantic_audit.get("hard_fail_reasons"):
            return HypothesisLoweringCompileResult(
                accepted=False,
                candidate={},
                hypothesis_artifact=dict(structured_hypothesis),
                reject_reasons=list(dict.fromkeys(semantic_audit.get("hard_fail_reasons") or [])),
                audit={
                    "source": source,
                    "target_symbols": list(target_symbols),
                    "constraint_check": dict(validation.constraint_check or {}),
                    "precompile_validation": validation.to_dict(),
                    "semantic_audit": dict(semantic_audit),
                    "rejected_at": "semantic_contract",
                },
            )

        candidate_provenance = dict(payload.get("candidate_provenance") or {})
        candidate_payload["candidate_provenance"] = {
            **candidate_provenance,
            "candidate_family": _string(
                candidate_provenance.get("candidate_family")
                or structured_hypothesis.get("family_hint")
                or strategy_type
            ).lower(),
            "generator_mode": _string(
                candidate_provenance.get("generator_mode")
                or "llm_hypothesis_compiler"
            ),
            "alpha_source": "llm_hypothesis",
            "holding_rationale": holding_rationale,
            "position_model": _string(structured_hypothesis.get("position_model")),
            "capacity_assumption": structured_hypothesis.get("capacity_assumption"),
            "market_regime_assumption": market_regime_assumption,
            "validation_focus": validation_focus,
            "family_specialization": dict(family_specific_hypothesis),
            "expected_turnover_band": expected_turnover_band,
            "capacity_bucket": capacity_bucket,
            "turnover_cost_class": turnover_cost_class,
        }
        return HypothesisLoweringCompileResult(
            accepted=True,
            candidate=candidate_payload,
            hypothesis_artifact=hypothesis_artifact,
            reject_reasons=[],
            audit={
                "source": source,
                "target_symbols": list(target_symbols),
                "constraint_check": dict(validation.constraint_check or {}),
                "precompile_validation": validation.to_dict(),
                "semantic_audit": dict(semantic_audit),
                "field_sources": dict(structured_hypothesis.get("field_sources") or {}),
                "family_specific_hypothesis": dict(family_specific_hypothesis),
                "derived_holding_profile": dict(derived_holding_profile),
                "position_sizing_rationale": position_sizing_rationale,
                "capacity_bucket": capacity_bucket,
                "turnover_cost_class": turnover_cost_class,
                "market_regime_assumption": market_regime_assumption,
            },
        )


__all__ = [
    "HypothesisLoweringCompileResult",
    "HypothesisLoweringCompiler",
]
