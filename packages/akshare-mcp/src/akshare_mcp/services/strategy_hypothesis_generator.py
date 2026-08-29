"""Structured hypothesis extraction for L2 strategy generation."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from strategy_factory.api.semantic_contract import normalize_research_task_contract

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


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in _EMPTY_VALUES:
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _normalize_boolish(value: Any, *, default: Optional[bool] = None) -> Optional[bool]:
    if value in _EMPTY_VALUES:
        return default
    if isinstance(value, bool):
        return value
    token = _string(value).lower()
    if token in {"1", "true", "yes", "on", "required", "must"}:
        return True
    if token in {"0", "false", "no", "off", "optional"}:
        return False
    return default


def _direction_bucket(value: Any) -> Optional[str]:
    token = _string(value).lower()
    if not token:
        return None
    if any(word in token for word in ("up", "long", "bull", "buy", "rise", "rebound")):
        return "up"
    if any(word in token for word in ("down", "short", "bear", "sell", "fall", "drop")):
        return "down"
    return None


def _normalize_conflict_resolution_rule(value: Any) -> Any:
    if isinstance(value, dict):
        payload = {
            key: item
            for key, item in dict(value).items()
            if item not in _EMPTY_VALUES
        }
        return payload or None
    token = _string(value)
    return token or None


def _validate_prediction_contract(
    prediction_contract: Any,
    evidence_chain: Any,
) -> list[str]:
    contract = _as_dict(prediction_contract)
    if not contract:
        return []
    claims = _as_list(contract.get("claims"))
    evidence_by_id = {
        _string(item.get("evidence_id") or item.get("id")): _as_dict(item)
        for item in _as_list(_as_dict(evidence_chain).get("evidences"))
        if _string(_as_dict(item).get("evidence_id") or _as_dict(item).get("id"))
    }
    default_conflict_rule = _normalize_conflict_resolution_rule(
        contract.get("conflict_resolution_rule")
    )
    reject_reasons: list[str] = []
    for index, raw_claim in enumerate(claims):
        claim = _as_dict(raw_claim)
        claim_id = _string(claim.get("claim_id") or claim.get("id")) or f"claim_{index}"
        evidence_ids = [
            _string(item)
            for item in _as_list(claim.get("evidence_ids"))
            if _string(item)
        ]
        if not evidence_ids:
            reject_reasons.append(f"prediction_contract_missing_evidence_ids:{claim_id}")
            continue
        expected_direction = _direction_bucket(claim.get("expected_move"))
        directions = [
            _direction_bucket(_as_dict(evidence_by_id.get(evidence_id)).get("direction"))
            for evidence_id in evidence_ids
            if evidence_id in evidence_by_id
        ]
        directions = [direction for direction in directions if direction]
        conflict_rule = _normalize_conflict_resolution_rule(
            claim.get("conflict_resolution_rule")
        ) or default_conflict_rule
        if expected_direction:
            has_same = any(direction == expected_direction for direction in directions)
            has_opposite = any(direction != expected_direction for direction in directions)
            if has_same and has_opposite and not conflict_rule:
                reject_reasons.append(
                    f"prediction_contract_missing_conflict_resolution_rule:{claim_id}"
                )
        elif len(set(directions)) > 1 and not conflict_rule:
            reject_reasons.append(
                f"prediction_contract_missing_conflict_resolution_rule:{claim_id}"
            )
    return reject_reasons


def _normalize_code_list(values: Any, limit: int = 12) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    def visit(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for key in ("code", "symbol", "stock_code"):
                if value.get(key) is not None:
                    visit(value.get(key))
            for key in ("codes", "symbols", "stock_codes", "target_symbols"):
                if value.get(key) is not None:
                    visit(value.get(key))
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)
            return
        raw = str(value or "").strip()
        if not raw:
            return
        if any(sep in raw for sep in [",", ";", "|", "\n", "\t", " "]):
            normalized = (
                raw.replace(";", ",")
                .replace("|", ",")
                .replace("\n", ",")
                .replace("\t", ",")
                .replace(" ", ",")
            )
            for part in normalized.split(","):
                visit(part)
            return
        code = raw.split(".")[0].strip()
        if not code or code in seen:
            return
        seen.add(code)
        codes.append(code)

    visit(values)
    return codes[: max(1, min(int(limit or 12), 40))]


def _coerce_summary_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_string(item) for item in value if _string(item)]
        return " / ".join(parts[:4])
    if isinstance(value, dict):
        for key in ("summary", "description", "rationale", "mode", "primary_failure_mode"):
            token = _string(value.get(key))
            if token:
                return token
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(value)
    return _string(value)


def _coerce_cost_grid(
    explicit_grid: Any,
    *,
    execution_assumptions: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if isinstance(explicit_grid, dict) and explicit_grid:
        return dict(explicit_grid)
    assumptions = dict(execution_assumptions or {})
    if not assumptions:
        return {}
    commission_rate = assumptions.get("commission_rate")
    slippage_bps = assumptions.get("slippage_bps")
    if commission_rate in _EMPTY_VALUES and slippage_bps in _EMPTY_VALUES:
        return {}
    base_commission = _safe_float(commission_rate)
    base_slippage = _safe_float(slippage_bps)
    if base_commission is None and base_slippage is None:
        return {}
    base_commission = float(base_commission or 0.0)
    base_slippage = float(base_slippage or 0.0)
    return {
        "base_case": {
            "commission_rate": base_commission,
            "slippage_bps": base_slippage,
            "tradability_filter": assumptions.get("tradability_filter"),
            "slippage_model": assumptions.get("slippage_model"),
        },
        "stress_cases": [
            {
                "label": "tight",
                "commission_rate": round(base_commission, 6),
                "slippage_bps": round(max(0.0, base_slippage * 0.75), 4),
            },
            {
                "label": "stressed",
                "commission_rate": round(base_commission * 1.5, 6),
                "slippage_bps": round(max(0.0, base_slippage * 1.5), 4),
            },
        ],
        "source": "execution_assumptions",
    }


def _coerce_market_regime_assumption(
    explicit_assumption: Any,
    *,
    provider_payload: Optional[dict[str, Any]] = None,
    research_task: Optional[dict[str, Any]] = None,
    strategy_type: str = "",
    validation_focus: str = "",
) -> Any:
    if explicit_assumption not in _EMPTY_VALUES:
        return explicit_assumption

    provider = dict(provider_payload or {})
    analysis = dict(provider.get("analysis") or {})
    for key in (
        "market_regime_assumption",
        "regime_assumption",
        "market_regime",
        "regime",
    ):
        value = analysis.get(key)
        if value not in _EMPTY_VALUES:
            return value

    task = dict(research_task or {})
    task_source = _string(task.get("task_source")).lower()
    family = _string(strategy_type).lower()
    focus = _string(validation_focus).lower()

    if task_source == "event_driven":
        return {
            "summary": "事件催化后短窗口延续，仅在流动性充足且主题未失效时成立。",
            "preferred_regime": "event_follow_through",
            "avoid_regime": "post_event_mean_reversion",
            "task_source": task_source,
            "validation_focus": focus or None,
        }
    if family in {"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout"}:
        return {
            "summary": "趋势扩张或龙头持续阶段更容易兑现，震荡横盘时容易失效。",
            "preferred_regime": "trend_expansion",
            "avoid_regime": "range_bound_chop",
            "task_source": task_source or None,
            "validation_focus": focus or None,
        }
    if family in {"quality_factor", "value_factor", "growth_factor", "multi_factor"}:
        return {
            "summary": "中低换手、基本面扩散稳定的慢变量阶段更有利于兑现。",
            "preferred_regime": "slow_factor_diffusion",
            "avoid_regime": "high_noise_rotation",
            "task_source": task_source or None,
            "validation_focus": focus or None,
        }
    if family in {"gap_fill", "mean_reversion_short", "rsi"}:
        return {
            "summary": "短期情绪失衡后的修复阶段更有效，强趋势单边时容易持续逆风。",
            "preferred_regime": "short_term_dislocation_repair",
            "avoid_regime": "persistent_one_way_trend",
            "task_source": task_source or None,
            "validation_focus": focus or None,
        }
    return {
        "summary": "仅在流动性正常、成本可控、目标样本与研究假设一致时成立。",
        "preferred_regime": "neutral_liquid_cn_equity",
        "avoid_regime": "illiquid_stressed_market",
        "task_source": task_source or None,
        "validation_focus": focus or None,
    }


def _coerce_objective_profile(
    explicit_profile: Any,
    *,
    provider_payload: Optional[dict[str, Any]] = None,
    research_task: Optional[dict[str, Any]] = None,
    validation_profile: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    for value in (
        explicit_profile,
        _as_dict(validation_profile).get("objective_profile"),
        _as_dict(research_task).get("objective_profile"),
        _as_dict(provider_payload).get("objective_profile"),
        _as_dict(_as_dict(provider_payload).get("analysis")).get("objective_profile"),
    ):
        token = _string(value).lower()
        if token:
            return token
    return None


def _coerce_trade_density_preference(
    explicit_preference: Any,
    *,
    objective_profile: Optional[str],
    strategy_type: str,
) -> Optional[str]:
    token = _string(explicit_preference).lower()
    if token in {"low", "medium", "high"}:
        return token
    if _string(objective_profile).lower() != "high_precision":
        return None
    family = _string(strategy_type).lower()
    if family in {"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout", "rsi", "gap_fill", "mean_reversion_short"}:
        return "low"
    return "medium"


def _coerce_entry_selectivity(
    explicit_selectivity: Any,
    *,
    objective_profile: Optional[str],
    strategy_type: str,
) -> Optional[str]:
    token = _string(explicit_selectivity).lower()
    if token:
        return token
    if _string(objective_profile).lower() != "high_precision":
        return None
    family = _string(strategy_type).lower()
    if family in {"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout"}:
        return "strict"
    if family in {"rsi", "gap_fill", "mean_reversion_short"}:
        return "selective"
    return "narrow"


def _score_economic_semantics(hypothesis: dict[str, Any]) -> tuple[int, list[str]]:
    missing_fields: list[str] = []
    weights = {
        "alpha_hypothesis": 16,
        "failure_mode": 12,
        "target_universe_hypothesis": 10,
        "family_hint": 6,
        "holding_rationale": 14,
        "alpha_half_life": 14,
        "cost_sensitivity_grid": 14,
        "position_model": 8,
        "capacity_assumption": 8,
        "market_regime_assumption": 8,
        "validation_focus": 4,
    }
    score = 0
    for field, weight in weights.items():
        value = hypothesis.get(field)
        missing = value in _EMPTY_VALUES or (isinstance(value, str) and not value.strip())
        if missing:
            missing_fields.append(field)
            continue
        if field == "alpha_half_life":
            score += weight if (_safe_float(value) or 0.0) > 0 else 0
            if (_safe_float(value) or 0.0) <= 0:
                missing_fields.append(field)
        elif field == "cost_sensitivity_grid":
            grid = _as_dict(value)
            base_case = _as_dict(grid.get("base_case"))
            if base_case.get("commission_rate") in _EMPTY_VALUES or base_case.get("slippage_bps") in _EMPTY_VALUES:
                missing_fields.append(field)
                continue
            score += weight
        elif field == "capacity_assumption":
            capacity = _as_dict(value)
            if (
                capacity.get("max_position_pct") in _EMPTY_VALUES
                and capacity.get("capacity_bucket") in _EMPTY_VALUES
                and capacity.get("symbol_count") in _EMPTY_VALUES
            ):
                missing_fields.append(field)
                continue
            score += weight
        else:
            score += weight
    return max(0, min(int(score), 100)), list(dict.fromkeys(missing_fields))


def _infer_family_specific_hypothesis(
    family_hint: str,
    *,
    explicit: Optional[dict[str, Any]] = None,
    normalized_task: Optional[dict[str, Any]] = None,
    holding_horizon: Optional[dict[str, Any]] = None,
    trade_plan: Optional[dict[str, Any]] = None,
    market_regime_assumption: Any = None,
    validation_focus: str = "",
) -> tuple[dict[str, Any], list[str]]:
    family = _string(family_hint).lower()
    if family not in _FAMILY_SPECIFIC_REQUIREMENTS:
        return ({}, [])

    payload = dict(explicit or {})
    task = dict(normalized_task or {})
    holding = dict(holding_horizon or {})
    trade = dict(trade_plan or {})
    regime = _coerce_summary_text(market_regime_assumption)
    max_days = int(_safe_float(holding.get("max_days") or 0) or 0)
    focus = _string(validation_focus).lower() or _string(task.get("validation_focus")).lower()

    if family == "momentum":
        payload.setdefault(
            "trend_persistence_logic",
            f"要求趋势至少持续 {max(8, max_days or 12)} 个交易日，并保持相对强度为正、趋势斜率不转负。"
        )
        payload.setdefault(
            "failure_scenario",
            _coerce_summary_text(
                {
                    "summary": trade.get("exit_bias") or "趋势衰减、相对强度转弱或主题扩散失败时失效。",
                    "regime": regime or None,
                }
            ),
        )
        payload.setdefault(
            "false_breakout_filter",
            "优先要求量能确认、突破后持续性与回撤受控，避免无量冲高或单日脉冲。"
        )
        if focus in {"target_only", "candidate_target_only"}:
            payload.setdefault("peer_selection_rationale", "target_only_with_dynamic_family_peers")
    elif family == "quality_factor":
        payload.setdefault(
            "quality_metrics",
            "优先考察盈利能力、现金流质量、利润率稳定性与资产负债表稳健性。"
        )
        payload.setdefault(
            "holding_consistency_explanation",
            f"质量慢变量扩散通常需要 {max(20, max_days or 30)} 个交易日左右兑现，因此更适合低频再平衡与更长持有。"
        )
        payload.setdefault(
            "quality_drift_detection",
            "若质量排名、利润率稳定性、现金流质量或价格趋势共振被破坏，则视为质量漂移。"
        )
        payload.setdefault(
            "trend_resonance_condition",
            "仅在质量指标保持稳定且价格位于中期趋势上方时提高权重。"
        )
    elif family == "ma_cross":
        payload.setdefault(
            "trend_noise_separation",
            "需要快慢线张口扩大且长期均线保持方向性，不能仅凭横盘噪声中的一次穿越入场。"
        )
        payload.setdefault(
            "range_filter",
            "当长期均线走平、振幅收敛或价格围绕均线反复摆动时，视为横盘过滤不入场。"
        )
        payload.setdefault(
            "volume_confirmation",
            "优先选择伴随量能放大或 volume_ratio 明显改善的有效金叉/死叉。"
        )
        payload.setdefault(
            "adaptive_span_logic",
            "根据趋势噪声水平与持有期调整快慢线跨度，避免过短跨度造成频繁换手。"
        )

    missing_fields = [
        field
        for field in _FAMILY_SPECIFIC_REQUIREMENTS.get(family, ())
        if payload.get(field) in _EMPTY_VALUES or (isinstance(payload.get(field), str) and not str(payload.get(field)).strip())
    ]
    return payload, missing_fields


@dataclass(slots=True)
class LLMHypothesisResult:
    accepted: bool
    hypothesis: dict[str, Any] = field(default_factory=dict)
    reject_reasons: list[str] = field(default_factory=list)
    field_sources: dict[str, str] = field(default_factory=dict)

    def to_artifact(self) -> dict[str, Any]:
        payload = dict(self.hypothesis or {})
        payload["field_sources"] = dict(self.field_sources or {})
        payload["accepted"] = bool(self.accepted)
        payload["reject_reasons"] = list(self.reject_reasons or [])
        return payload


class LLMHypothesisGenerator:
    """Promotes raw provider output into structured research hypotheses."""

    @classmethod
    def build(
        cls,
        candidate: Optional[dict[str, Any]],
        *,
        research_task: Optional[dict[str, Any]] = None,
        provider_payload: Optional[dict[str, Any]] = None,
    ) -> LLMHypothesisResult:
        payload = dict(candidate or {})
        normalized_task = normalize_research_task_contract(
            research_task or payload.get("research_task") or {}
        )
        provider = dict(provider_payload or {})
        explicit = dict(
            payload.get("hypothesis_artifact")
            or payload.get("hypothesis_structured")
            or {}
        )
        prediction_contract = _as_dict(payload.get("prediction_contract"))
        evidence_chain = _as_dict(payload.get("evidence_chain"))
        holding_horizon = _as_dict(payload.get("holding_horizon"))
        trade_plan = _as_dict(payload.get("trade_plan"))
        risk_rules = _as_dict(payload.get("risk_rules"))
        position_sizing = _as_dict(payload.get("position_sizing"))
        portfolio_spec = _as_dict(payload.get("portfolio_spec"))
        execution_assumptions = _as_dict(payload.get("execution_assumptions"))
        validation_profile = _as_dict(payload.get("validation_profile"))
        stock_pool = _as_dict(payload.get("stock_pool"))
        target_symbols = _normalize_code_list(
            [
                payload.get("target_symbols"),
                stock_pool,
                (payload.get("dsl") or {}).get("metadata"),
                normalized_task.get("target_symbols"),
            ],
            limit=8,
        )
        if target_symbols and not stock_pool:
            stock_pool = {
                "selection_mode": "explicit",
                "symbols": list(target_symbols),
            }
        field_sources: dict[str, str] = {}

        def choose(field: str, *values: tuple[str, Any]) -> Any:
            for source_name, value in values:
                if value in _EMPTY_VALUES:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                if isinstance(value, dict) and not value:
                    continue
                if isinstance(value, list) and not value:
                    continue
                field_sources[field] = source_name
                return value
            return None

        alpha_hypothesis = choose(
            "alpha_hypothesis",
            ("hypothesis_artifact", explicit.get("alpha_hypothesis")),
            ("candidate_hypothesis", payload.get("hypothesis")),
            ("candidate_rationale", payload.get("rationale")),
            ("candidate_description", payload.get("description")),
            ("provider_analysis", dict(provider.get("analysis") or {}).get("hypothesis")),
        )
        failure_mode = choose(
            "failure_mode",
            ("hypothesis_artifact", explicit.get("failure_mode")),
            ("candidate_failure_mode", payload.get("failure_mode")),
            (
                "risk_trade_plan",
                {
                    "primary_failure_mode": _string(
                        trade_plan.get("exit_bias")
                        or risk_rules.get("failure_mode")
                        or "thesis_break_or_time_stop"
                    ),
                    "stop_loss_pct": risk_rules.get("stop_loss_pct"),
                    "take_profit_pct": risk_rules.get("take_profit_pct"),
                }
                if trade_plan or risk_rules
                else None
            ),
        )
        target_universe_hypothesis = choose(
            "target_universe_hypothesis",
            ("hypothesis_artifact", explicit.get("target_universe_hypothesis")),
            (
                "candidate_targeting",
                {
                    "target_symbols": list(target_symbols),
                    "stock_pool": dict(stock_pool),
                    "target_symbol_policy": normalized_task.get("target_symbol_policy"),
                    "universe_expansion_policy": normalized_task.get("universe_expansion_policy"),
                    "validation_focus": validation_profile.get("validation_focus")
                    or normalized_task.get("validation_focus"),
                }
                if target_symbols or stock_pool
                else None
            ),
        )
        family_hint = choose(
            "family_hint",
            ("hypothesis_artifact", explicit.get("family_hint")),
            ("candidate_strategy_type", payload.get("strategy_type")),
        )
        holding_rationale = choose(
            "holding_rationale",
            ("hypothesis_artifact", explicit.get("holding_rationale")),
            ("holding_horizon", holding_horizon.get("rationale")),
            ("trade_plan", trade_plan.get("exit_bias")),
        )
        alpha_half_life = choose(
            "alpha_half_life",
            ("hypothesis_artifact", explicit.get("alpha_half_life")),
            ("candidate_alpha_half_life", payload.get("alpha_half_life")),
            ("holding_horizon", holding_horizon.get("alpha_half_life")),
            ("holding_horizon", holding_horizon.get("max_days")),
        )
        cost_sensitivity_grid = choose(
            "cost_sensitivity_grid",
            ("hypothesis_artifact", explicit.get("cost_sensitivity_grid")),
            ("candidate_cost_grid", payload.get("cost_sensitivity_grid")),
            (
                "execution_assumptions",
                _coerce_cost_grid(
                    None,
                    execution_assumptions=execution_assumptions,
                ),
            ),
        )
        position_model = choose(
            "position_model",
            ("hypothesis_artifact", explicit.get("position_model")),
            ("candidate_position_model", payload.get("position_model")),
            ("position_sizing", position_sizing.get("mode")),
            ("portfolio_spec", portfolio_spec.get("position_assumption")),
        )
        capacity_assumption = choose(
            "capacity_assumption",
            ("hypothesis_artifact", explicit.get("capacity_assumption")),
            ("candidate_capacity_assumption", payload.get("capacity_assumption")),
            (
                "portfolio_spec",
                {
                    "max_position_pct": portfolio_spec.get("max_position_pct"),
                    "target_weight_scheme": portfolio_spec.get("target_weight_scheme"),
                    "symbol_count": len(target_symbols),
                }
                if portfolio_spec or target_symbols
                else None
            ),
        )
        validation_focus = choose(
            "validation_focus",
            ("hypothesis_artifact", explicit.get("validation_focus")),
            ("validation_profile", validation_profile.get("validation_focus")),
            ("research_task", normalized_task.get("validation_focus")),
        )
        objective_profile = choose(
            "objective_profile",
            ("hypothesis_artifact", explicit.get("objective_profile")),
            ("validation_profile", validation_profile.get("objective_profile")),
            (
                "context_defaults",
                _coerce_objective_profile(
                    explicit.get("objective_profile"),
                    provider_payload=provider,
                    research_task=normalized_task,
                    validation_profile=validation_profile,
                ),
            ),
        )
        trade_density_preference = choose(
            "trade_density_preference",
            ("hypothesis_artifact", explicit.get("trade_density_preference")),
            ("validation_profile", validation_profile.get("trade_density_preference")),
            (
                "objective_profile",
                _coerce_trade_density_preference(
                    explicit.get("trade_density_preference")
                    or validation_profile.get("trade_density_preference"),
                    objective_profile=_string(objective_profile),
                    strategy_type=_string(family_hint),
                ),
            ),
        )
        entry_selectivity = choose(
            "entry_selectivity",
            ("hypothesis_artifact", explicit.get("entry_selectivity")),
            ("validation_profile", validation_profile.get("entry_selectivity")),
            (
                "objective_profile",
                _coerce_entry_selectivity(
                    explicit.get("entry_selectivity")
                    or validation_profile.get("entry_selectivity"),
                    objective_profile=_string(objective_profile),
                    strategy_type=_string(family_hint),
                ),
            ),
        )
        regime_required = choose(
            "regime_required",
            ("hypothesis_artifact", explicit.get("regime_required")),
            ("validation_profile", validation_profile.get("regime_required")),
            (
                "objective_profile",
                _normalize_boolish(
                    validation_profile.get("regime_required"),
                    default=_string(objective_profile).lower() == "high_precision",
                ),
            ),
        )
        cost_robust_required = choose(
            "cost_robust_required",
            ("hypothesis_artifact", explicit.get("cost_robust_required")),
            ("validation_profile", validation_profile.get("cost_robust_required")),
            (
                "objective_profile",
                _normalize_boolish(
                    validation_profile.get("cost_robust_required"),
                    default=_string(objective_profile).lower() == "high_precision",
                ),
            ),
        )
        market_regime_assumption = choose(
            "market_regime_assumption",
            ("hypothesis_artifact", explicit.get("market_regime_assumption")),
            ("candidate_market_regime", payload.get("market_regime_assumption")),
            (
                "provider_analysis",
                _coerce_market_regime_assumption(
                    None,
                    provider_payload=provider,
                    research_task=normalized_task,
                    strategy_type=_string(family_hint),
                    validation_focus=_string(validation_focus),
                ),
            ),
        )
        family_specific_hypothesis, family_specific_missing_fields = _infer_family_specific_hypothesis(
            _string(family_hint),
            explicit=_as_dict(explicit.get("family_specific_hypothesis")),
            normalized_task=normalized_task,
            holding_horizon=holding_horizon,
            trade_plan=trade_plan,
            market_regime_assumption=market_regime_assumption,
            validation_focus=_string(validation_focus),
        )

        normalized_hypothesis = {
            "version": "l2.hypothesis.v1",
            "generator_type": str(payload.get("generator_type") or payload.get("_engine") or "external_llm").strip(),
            "provider": provider.get("provider"),
            "model": provider.get("model"),
            "alpha_hypothesis": _coerce_summary_text(alpha_hypothesis),
            "failure_mode": failure_mode,
            "target_universe_hypothesis": target_universe_hypothesis,
            "family_hint": _string(family_hint).lower() or None,
            "holding_rationale": _coerce_summary_text(holding_rationale),
            "alpha_half_life": _safe_float(alpha_half_life),
            "cost_sensitivity_grid": dict(cost_sensitivity_grid or {}),
            "position_model": _coerce_summary_text(position_model),
            "capacity_assumption": capacity_assumption,
            "market_regime_assumption": market_regime_assumption,
            "validation_focus": _string(validation_focus).lower() or None,
            "objective_profile": _string(objective_profile).lower() or None,
            "trade_density_preference": _string(trade_density_preference).lower() or None,
            "entry_selectivity": _string(entry_selectivity).lower() or None,
            "regime_required": _normalize_boolish(regime_required, default=False),
            "cost_robust_required": _normalize_boolish(cost_robust_required, default=False),
            "family_specific_hypothesis": dict(family_specific_hypothesis or {}),
        }

        economic_semantics_score, missing_fields = _score_economic_semantics(
            normalized_hypothesis
        )
        missing_fields.extend(
            [f"family_specific_hypothesis.{field}" for field in family_specific_missing_fields]
        )
        missing_fields = list(dict.fromkeys(missing_fields))
        normalized_hypothesis["economic_semantics_score"] = economic_semantics_score
        normalized_hypothesis["economic_semantics_missing_fields"] = list(missing_fields)
        normalized_hypothesis["economic_semantics_complete"] = len(missing_fields) == 0
        normalized_hypothesis["family_specific_missing_fields"] = list(
            dict.fromkeys(family_specific_missing_fields)
        )
        normalized_hypothesis["family_specific_complete"] = len(family_specific_missing_fields) == 0

        reject_reasons = [f"hypothesis_missing:{field}" for field in missing_fields]
        reject_reasons.extend(
            _validate_prediction_contract(prediction_contract, evidence_chain)
        )

        artifact_fingerprint = hashlib.sha1(
            json.dumps(
                {
                    key: normalized_hypothesis.get(key)
                    for key in _REQUIRED_HYPOTHESIS_FIELDS
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:12]
        normalized_hypothesis["artifact_id"] = (
            _string(explicit.get("artifact_id"))
            or f"hyp_{artifact_fingerprint}"
        )

        accepted = len(reject_reasons) == 0
        return LLMHypothesisResult(
            accepted=accepted,
            hypothesis=normalized_hypothesis,
            reject_reasons=list(dict.fromkeys(reject_reasons)),
            field_sources=field_sources,
        )


__all__ = [
    "LLMHypothesisGenerator",
    "LLMHypothesisResult",
]
