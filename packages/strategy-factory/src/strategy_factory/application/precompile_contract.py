"""Shared lightweight contract validation for generator precompile checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..api.contracts import normalize_execution_assumptions
from ..domain.targets import _build_target_alignment_contract, _normalize_research_task_contract
from .candidate_contract import (
    build_portfolio_candidate_contract,
    candidate_contract_value,
    resolve_candidate_validation_profile,
)

_EMPTY_VALUES = (None, "", [], {})
_FACTOR_VALIDATION_TYPES = {"value_factor", "quality_factor", "growth_factor", "multi_factor"}
_REQUIRED_CONTRACT_FIELDS: dict[str, tuple[str, ...]] = {
    "portfolio_spec": ("position_assumption", "target_weight_scheme"),
    "execution_assumptions": ("commission_rate", "slippage_bps", "tradability_filter", "slippage_model"),
    "validation_profile": ("profile", "validation_focus", "primary_validation_layer"),
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _dedup_reasons(values: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = _string(value)
        if not token or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def _contract_value_missing(value: Any) -> bool:
    return value in _EMPTY_VALUES


def _require_explicit_contract_dict(
    candidate: Optional[Mapping[str, Any]],
    field: str,
    *,
    required_keys: tuple[str, ...],
) -> tuple[dict[str, Any], list[str]]:
    payload = candidate_contract_value(candidate, field, {})
    if not isinstance(payload, Mapping) or not payload:
        return {}, [f"{field}_missing"]
    normalized = dict(payload)
    missing_keys = [
        key
        for key in required_keys
        if _contract_value_missing(normalized.get(key))
    ]
    if missing_keys:
        return normalized, [f"{field}_missing_keys:{','.join(missing_keys)}"]
    return normalized, []


def _allowed_validation_layers(validation_focus: str) -> set[str]:
    if validation_focus == "event_target_only":
        return {"target"}
    if validation_focus == "broad_generalization":
        return {"combined"}
    return {"target", "combined"}


@dataclass(slots=True)
class PrecompileContractValidationResult:
    accepted: bool
    reject_reasons: list[str] = field(default_factory=list)
    source: Optional[str] = None
    strategy_type: Optional[str] = None
    normalized_research_task: dict[str, Any] = field(default_factory=dict)
    target_symbols: list[str] = field(default_factory=list)
    stock_pool: dict[str, Any] = field(default_factory=dict)
    target_alignment_contract: dict[str, Any] = field(default_factory=dict)
    constraint_check: dict[str, Any] = field(default_factory=dict)
    portfolio_spec: dict[str, Any] = field(default_factory=dict)
    execution_assumptions: dict[str, Any] = field(default_factory=dict)
    validation_profile: dict[str, Any] = field(default_factory=dict)
    expected_validation_profile: dict[str, Any] = field(default_factory=dict)
    allowed_strategy_types_ok: bool = True
    validation_profile_ok: bool = True
    target_alignment_ok: bool = True
    contract_completeness_ok: bool = True
    alignment_reject_reasons: list[str] = field(default_factory=list)
    validation_profile_reject_reasons: list[str] = field(default_factory=list)
    contract_reject_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": bool(self.accepted),
            "source": self.source,
            "strategy_type": self.strategy_type,
            "reject_reasons": list(self.reject_reasons),
            "generator_precompile_reject_reason": self.reject_reasons[0] if self.reject_reasons else None,
            "allowed_strategy_types_ok": bool(self.allowed_strategy_types_ok),
            "validation_profile_ok": bool(self.validation_profile_ok),
            "target_alignment_ok": bool(self.target_alignment_ok),
            "contract_completeness_ok": bool(self.contract_completeness_ok),
            "alignment_reject_reasons": list(self.alignment_reject_reasons),
            "validation_profile_reject_reasons": list(self.validation_profile_reject_reasons),
            "contract_reject_reasons": list(self.contract_reject_reasons),
            "normalized_research_task": dict(self.normalized_research_task),
            "target_symbols": list(self.target_symbols),
            "stock_pool": dict(self.stock_pool),
            "target_alignment_contract": dict(self.target_alignment_contract),
            "constraint_check": dict(self.constraint_check),
            "validation_profile": dict(self.validation_profile),
            "expected_validation_profile": dict(self.expected_validation_profile),
        }


def validate_precompile_candidate_contract(
    candidate: Optional[Mapping[str, Any]],
    *,
    research_task: Optional[Mapping[str, Any]] = None,
    source: Optional[str] = None,
) -> PrecompileContractValidationResult:
    payload = dict(candidate or {})
    normalized_research_task = _normalize_research_task_contract(
        research_task or payload.get("research_task") or {}
    )
    payload.setdefault("research_task", dict(normalized_research_task))
    contract_snapshot = build_portfolio_candidate_contract(payload)
    targeting = dict(contract_snapshot.get("targeting") or {})
    target_symbols = list(targeting.get("target_symbols") or [])
    stock_pool = dict(
        targeting.get("stock_pool")
        or candidate_contract_value(payload, "stock_pool", {})
        or normalized_research_task.get("stock_pool")
        or {}
    )
    if target_symbols and not stock_pool:
        stock_pool = {"selection_mode": "explicit", "symbols": list(target_symbols)}

    strategy_type = _string(
        contract_snapshot.get("strategy_type")
        or candidate_contract_value(payload, "strategy_type")
    ).lower() or None
    allowed_strategy_types = {
        _string(item).lower()
        for item in list(normalized_research_task.get("allowed_strategy_types") or [])
        if _string(item)
    }
    allowed_strategy_types_ok = not (
        allowed_strategy_types and strategy_type and strategy_type not in allowed_strategy_types
    )

    target_alignment_contract = dict(
        normalized_research_task.get("target_alignment_contract")
        or _build_target_alignment_contract(normalized_research_task, candidate=payload)
    )
    constraint_check = dict(candidate_contract_value(payload, "constraint_check", {}) or {})
    research_symbols = list(normalized_research_task.get("target_symbols") or [])
    overlap_count = len(set(target_symbols).intersection(research_symbols))
    coverage_ratio = (
        round(_safe_float(constraint_check.get("coverage_ratio"), overlap_count / max(1, len(target_symbols))), 4)
        if target_symbols
        else 0.0
    )
    intersection_ratio: Optional[float]
    if research_symbols:
        intersection_ratio = round(
            _safe_float(constraint_check.get("intersection_ratio"), overlap_count / max(1, len(research_symbols))),
            4,
        )
    else:
        intersection_ratio = None if constraint_check.get("intersection_ratio") is None else round(
            _safe_float(constraint_check.get("intersection_ratio"), 0.0),
            4,
        )
    alignment_reject_reasons: list[str] = []
    if target_alignment_contract.get("quality_gate_enabled"):
        min_coverage_ratio = float(target_alignment_contract.get("min_coverage_ratio") or 0.0)
        min_intersection_ratio = (
            None
            if target_alignment_contract.get("min_intersection_ratio") is None
            else float(target_alignment_contract.get("min_intersection_ratio") or 0.0)
        )
        min_required_overlap_count = int(target_alignment_contract.get("min_required_overlap_count") or 0)
        if not target_symbols and research_symbols:
            alignment_reject_reasons.append("empty_target_symbols_after_alignment")
        if coverage_ratio < min_coverage_ratio:
            alignment_reject_reasons.append("coverage_ratio_below_contract")
        if intersection_ratio is not None and min_intersection_ratio is not None and intersection_ratio < min_intersection_ratio:
            alignment_reject_reasons.append("intersection_ratio_below_contract")
        if min_required_overlap_count > 0 and overlap_count < min_required_overlap_count:
            alignment_reject_reasons.append("target_overlap_count_below_contract")
    alignment_reject_reasons = _dedup_reasons(alignment_reject_reasons)
    target_alignment_ok = len(alignment_reject_reasons) == 0

    constraint_check = {
        **constraint_check,
        "target_symbols_after_normalize": list(target_symbols),
        "coverage_ratio": coverage_ratio,
        "intersection_ratio": intersection_ratio,
        "target_overlap_count": int(overlap_count),
        "alignment_contract_ok": bool(target_alignment_ok),
        "alignment_contract_violation": alignment_reject_reasons[0] if alignment_reject_reasons else None,
        "alignment_contract_reject_reasons": list(alignment_reject_reasons),
        "target_alignment_contract": dict(target_alignment_contract),
    }

    portfolio_spec, portfolio_reject_reasons = _require_explicit_contract_dict(
        payload,
        "portfolio_spec",
        required_keys=_REQUIRED_CONTRACT_FIELDS["portfolio_spec"],
    )
    raw_execution_assumptions, execution_reject_reasons = _require_explicit_contract_dict(
        payload,
        "execution_assumptions",
        required_keys=_REQUIRED_CONTRACT_FIELDS["execution_assumptions"],
    )
    execution_assumptions = normalize_execution_assumptions(
        raw_execution_assumptions,
        portfolio_spec=portfolio_spec or contract_snapshot.get("portfolio_spec"),
        capacity_assumption=candidate_contract_value(payload, "capacity_assumption", {}),
        holding_horizon=contract_snapshot.get("holding_horizon"),
        cost_sensitivity_grid=candidate_contract_value(payload, "cost_sensitivity_grid", {}),
    )
    validation_profile, validation_reject_reasons = _require_explicit_contract_dict(
        payload,
        "validation_profile",
        required_keys=_REQUIRED_CONTRACT_FIELDS["validation_profile"],
    )
    contract_reject_reasons = _dedup_reasons(
        [
            *portfolio_reject_reasons,
            *execution_reject_reasons,
            *validation_reject_reasons,
        ]
    )
    contract_completeness_ok = len(contract_reject_reasons) == 0

    resolved_validation_profile = dict(contract_snapshot.get("validation_profile") or {})
    expected_validation_profile = resolve_candidate_validation_profile(
        {
            "strategy_type": strategy_type,
            "research_task": normalized_research_task,
        },
        research_task=normalized_research_task,
    )
    validation_profile_reject_reasons: list[str] = []
    if validation_profile:
        if _string(resolved_validation_profile.get("profile")).lower() != _string(expected_validation_profile.get("profile")).lower():
            validation_profile_reject_reasons.append("validation_profile_profile_mismatch")
        if _string(resolved_validation_profile.get("validation_focus")).lower() != _string(expected_validation_profile.get("validation_focus")).lower():
            validation_profile_reject_reasons.append("validation_profile_focus_mismatch")
        actual_layer = _string(resolved_validation_profile.get("primary_validation_layer")).lower()
        if actual_layer not in _allowed_validation_layers(
            _string(expected_validation_profile.get("validation_focus")).lower()
        ):
            validation_profile_reject_reasons.append("validation_profile_layer_mismatch")
    validation_profile_reject_reasons = _dedup_reasons(validation_profile_reject_reasons)
    validation_profile_ok = len(validation_profile_reject_reasons) == 0

    reject_reasons = _dedup_reasons(
        [
            "outside_allowed_strategy_types" if not allowed_strategy_types_ok else "",
            *validation_profile_reject_reasons,
            "target_universe_alignment_too_low" if not target_alignment_ok else "",
            *contract_reject_reasons,
        ]
    )
    return PrecompileContractValidationResult(
        accepted=len(reject_reasons) == 0,
        reject_reasons=reject_reasons,
        source=source,
        strategy_type=strategy_type,
        normalized_research_task=dict(normalized_research_task),
        target_symbols=list(target_symbols),
        stock_pool=dict(stock_pool),
        target_alignment_contract=dict(target_alignment_contract),
        constraint_check=dict(constraint_check),
        portfolio_spec=dict(portfolio_spec),
        execution_assumptions=dict(execution_assumptions),
        validation_profile=dict(validation_profile or resolved_validation_profile),
        expected_validation_profile=dict(expected_validation_profile),
        allowed_strategy_types_ok=allowed_strategy_types_ok,
        validation_profile_ok=validation_profile_ok,
        target_alignment_ok=target_alignment_ok,
        contract_completeness_ok=contract_completeness_ok,
        alignment_reject_reasons=list(alignment_reject_reasons),
        validation_profile_reject_reasons=list(validation_profile_reject_reasons),
        contract_reject_reasons=list(contract_reject_reasons),
    )


__all__ = [
    "PrecompileContractValidationResult",
    "validate_precompile_candidate_contract",
]
