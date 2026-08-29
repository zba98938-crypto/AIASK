"""Shared portfolio candidate contract helpers."""
from __future__ import annotations

# --- candidate_contract_parts/normalizers.py ---
import hashlib
import json
from typing import Any, Mapping, Optional

from ..api.contracts import FactoryBacktestAssumptions, normalize_execution_assumptions
from ..domain.strategy_profile import infer_candidate_strategy_profile
from ..domain.targets import (
    _apply_target_symbol_policy,
    _build_task_signature,
    _extract_candidate_origin_target_codes,
    _extract_target_codes_from_payload,
    _normalize_target_codes,
    _normalize_research_task_contract,
)

_EMPTY_VALUES = (None, "", [], {})
_FACTOR_VALIDATION_TYPES = {"value_factor", "quality_factor", "growth_factor", "multi_factor"}
_TARGET_SYMBOL_KEYS = frozenset({
    "target_symbols",
    "symbols",
    "codes",
    "stock_codes",
    "same_theme_symbols",
    "theme_members",
})
_DYNAMIC_TARGETING_KEYS = frozenset({
    "coverage_ratio",
    "intersection_ratio",
    "target_overlap_count",
    "constraint_check",
})
_LOGIC_PARAM_SKIP_KEYS = frozenset({
    "candidate_contract_hash",
    "candidate_contract_snapshot",
    "candidate_identity_signature",
    "candidate_local_attempt_count",
    "candidate_local_selected_count",
    "candidate_lineage_contract",
    "candidate_provenance",
    "confidence_contract",
    "constraint_check",
    "contradiction_count",
    "evidence_alignment_audit",
    "evidence_chain",
    "event_context",
    "execution_contract_hash",
    "execution_assumptions",
    "factor_signature",
    "factory_attempt_count",
    "factory_global_attempt_count",
    "factory_global_selected_count",
    "factory_selected_count",
    "had_explicit_research_task",
    "holding_horizon",
    "legacy_semantic_contract",
    "logic_signature",
    "lineage",
    "lineage_id",
    "parent_candidate_id",
    "parent_candidate_ids",
    "parent_strategy_id",
    "parent_strategy_ids",
    "portfolio_spec",
    "prediction_contract",
    "proxy_dependency_score",
    "rebalance_rule",
    "request_target_symbols",
    "requested_target_symbols",
    "research_task",
    "resolved_candidate_envelope",
    "risk_rules",
    "runtime_playbook",
    "stock_pool",
    "strategy_profile",
    "target_pool_id",
    "target_symbols",
    "targeting_policy",
    "task_signature",
    "task_local_attempt_count",
    "task_local_selected_count",
    "tested_object_hash",
    "trade_plan",
    "trade_prediction_contract",
    "trade_prediction_contract_hash",
    "trade_prediction_contract_missing_fields",
    "trade_prediction_contract_reject_reasons",
    "trade_prediction_contract_status",
    "validation_profile",
    "dsl_signature",
    "entry_exit_signature",
    "legacy_identity_partial",
    "tested_object_backfill_incomplete",
})
_LOGIC_METADATA_SKIP_KEYS = frozenset({
    "candidate_contract_hash",
    "candidate_contract_snapshot",
    "candidate_family",
    "candidate_family_id",
    "candidate_identity_signature",
    "candidate_lineage_contract",
    "candidate_provenance",
    "confidence_contract",
    "codes",
    "contradiction_count",
    "dsl_signature",
    "evidence_alignment_audit",
    "evidence_chain",
    "entry_exit_signature",
    "event_context",
    "execution_contract_hash",
    "factor_signature",
    "lineage",
    "lineage_id",
    "logic_signature",
    "parent_candidate_id",
    "parent_candidate_ids",
    "parent_strategy_id",
    "parent_strategy_ids",
    "pool_id",
    "prediction_contract",
    "proxy_dependency_score",
    "research_task",
    "run_id",
    "runtime_playbook",
    "same_theme_symbols",
    "stock_codes",
    "strategy_profile",
    "strategy_tags",
    "symbols",
    "target_pool_id",
    "target_symbols",
    "task_signature",
    "tested_object_hash",
    "tested_object_backfill_incomplete",
    "trade_prediction_contract",
    "trade_prediction_contract_hash",
    "trade_prediction_contract_missing_fields",
    "trade_prediction_contract_reject_reasons",
    "trade_prediction_contract_status",
    "theme_code",
    "theme_id",
    "theme_members",
    "legacy_semantic_contract",
    "legacy_identity_partial",
})
_TOP_LEVEL_LOGIC_KEYS = (
    "alpha_formula",
    "entry_logic",
    "exit_logic",
    "factor_weights",
    "formula",
    "ranking_logic",
    "selection_logic",
    "signal_formula",
)
_FACTOR_SIGNATURE_LOGIC_KEYS = frozenset({
    "alpha_formula",
    "factor_weights",
    "formula",
    "ranking_logic",
    "selection_logic",
    "signal_formula",
})
_FACTOR_SIGNATURE_PARAM_TOKENS = ("alpha", "factor", "formula", "rank", "score", "select", "signal", "weight")
_ENTRY_EXIT_LOGIC_KEYS = frozenset({
    "entry_logic",
    "exit_logic",
})


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


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _normalize_string_list(*values: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, (list, tuple, set)):
            candidates = list(value)
        elif value in _EMPTY_VALUES:
            candidates = []
        else:
            candidates = [value]
        for item in candidates:
            token = _string(item)
            if not token or token in seen:
                continue
            seen.add(token)
            ordered.append(token)
    return ordered


def _canonical_target_symbols(values: Any, *, limit: int = 40) -> list[str]:
    return sorted(_normalize_target_codes(values, limit=limit))


def _canonicalize_target_pool_id(pool_id: Any, *, target_symbols: Optional[list[str]] = None) -> Optional[str]:
    normalized_pool_id = _string(pool_id)
    if not normalized_pool_id:
        return None
    canonical_targets = list(target_symbols or [])
    if not canonical_targets or ":" not in normalized_pool_id:
        return normalized_pool_id
    prefix, suffix = normalized_pool_id.split(":", 1)
    if not prefix or not suffix:
        return normalized_pool_id
    suffix_symbols = _canonical_target_symbols(suffix)
    if suffix_symbols and suffix_symbols == canonical_targets:
        return f"{prefix}:{','.join(canonical_targets)}"
    return normalized_pool_id


def _canonicalize_contract_value(value: Any, *, key: Optional[str] = None) -> Any:
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for child_key, child_value in value.items():
            normalized_key = str(child_key)
            if key == "targeting" and normalized_key in _DYNAMIC_TARGETING_KEYS:
                continue
            canonical[normalized_key] = _canonicalize_contract_value(child_value, key=normalized_key)
        target_symbols = _canonical_target_symbols(canonical.get("target_symbols")) if "target_symbols" in canonical else []
        if target_symbols:
            canonical["target_symbols"] = target_symbols
        if "target_pool_id" in canonical:
            canonical["target_pool_id"] = _canonicalize_target_pool_id(
                canonical.get("target_pool_id"),
                target_symbols=target_symbols,
            )
        return canonical
    if key in _TARGET_SYMBOL_KEYS:
        return _canonical_target_symbols(value)
    if isinstance(value, (list, tuple, set)):
        return [_canonicalize_contract_value(item) for item in value]
    return value


def _compact_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for key, child in value.items():
            compact_child = _compact_payload(child)
            if compact_child in _EMPTY_VALUES:
                continue
            compact[str(key)] = compact_child
        return compact
    if isinstance(value, (list, tuple, set)):
        compact_items = [_compact_payload(item) for item in value]
        return [item for item in compact_items if item not in _EMPTY_VALUES]
    return value


def _canonicalize_logic_value(value: Any, *, key: Optional[str] = None) -> Any:
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for child_key, child_value in value.items():
            normalized_key = str(child_key)
            if normalized_key in _LOGIC_METADATA_SKIP_KEYS:
                continue
            canonical_value = _canonicalize_logic_value(child_value, key=normalized_key)
            if canonical_value in _EMPTY_VALUES:
                continue
            canonical[normalized_key] = canonical_value
        return canonical
    if key in _TARGET_SYMBOL_KEYS:
        return []
    if isinstance(value, (list, tuple, set)):
        return [_canonicalize_logic_value(item) for item in value]
    return value


def _build_candidate_logic_payload(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(candidate or {})
    params = _as_dict(payload.get("params"))
    dsl = _as_dict(params.get("dsl") or payload.get("dsl"))
    if dsl:
        dsl = {
            str(key): _canonicalize_logic_value(value, key=str(key))
            for key, value in dsl.items()
            if str(key) != "metadata"
        }
    alpha_params = {
        str(key): _canonicalize_logic_value(value, key=str(key))
        for key, value in params.items()
        if str(key) not in _LOGIC_PARAM_SKIP_KEYS and str(key) not in {"dsl", "factor_weights", "selection_logic"}
    }
    top_level_logic = {
        key: _canonicalize_logic_value(payload.get(key), key=key)
        for key in _TOP_LEVEL_LOGIC_KEYS
        if payload.get(key) not in _EMPTY_VALUES
    }
    if "factor_weights" not in top_level_logic:
        factor_weights = _canonicalize_logic_value(params.get("factor_weights"), key="factor_weights")
        if factor_weights not in _EMPTY_VALUES:
            top_level_logic["factor_weights"] = factor_weights
    if "selection_logic" not in top_level_logic:
        selection_logic = _canonicalize_logic_value(params.get("selection_logic"), key="selection_logic")
        if selection_logic not in _EMPTY_VALUES:
            top_level_logic["selection_logic"] = selection_logic
    return _compact_payload({
        "strategy_type": _string(payload.get("strategy_type")).lower() or "unknown",
        "dsl": dsl,
        "logic_fields": top_level_logic,
        "alpha_params": alpha_params,
    })


def _hash_payload(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()


def _has_material_logic_payload(payload: Optional[Mapping[str, Any]]) -> bool:
    item = dict(payload or {})
    return bool(
        item.get("dsl")
        or item.get("logic_fields")
        or item.get("alpha_params")
    )


def _should_include_factor_param(strategy_type: str, key: str) -> bool:
    normalized_key = _string(key).lower()
    if not normalized_key:
        return False
    if strategy_type in _FACTOR_VALIDATION_TYPES:
        return True
    return any(token in normalized_key for token in _FACTOR_SIGNATURE_PARAM_TOKENS)


def _build_dsl_signature_payload(logic_payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(logic_payload or {})
    return _compact_payload({
        "strategy_type": payload.get("strategy_type"),
        "dsl": dict(payload.get("dsl") or {}),
    })


def _build_factor_signature_payload(logic_payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(logic_payload or {})
    strategy_type = _string(payload.get("strategy_type")).lower() or "unknown"
    logic_fields = {
        str(key): value
        for key, value in dict(payload.get("logic_fields") or {}).items()
        if str(key) in _FACTOR_SIGNATURE_LOGIC_KEYS
    }
    alpha_params = {
        str(key): value
        for key, value in dict(payload.get("alpha_params") or {}).items()
        if _should_include_factor_param(strategy_type, str(key))
    }
    return _compact_payload({
        "strategy_type": strategy_type,
        "logic_fields": logic_fields,
        "alpha_params": alpha_params,
    })


def _build_entry_exit_signature_payload(logic_payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(logic_payload or {})
    dsl = {
        str(key): value
        for key, value in dict(payload.get("dsl") or {}).items()
        if "entry" in str(key).lower() or "exit" in str(key).lower()
    }
    logic_fields = {
        str(key): value
        for key, value in dict(payload.get("logic_fields") or {}).items()
        if str(key) in _ENTRY_EXIT_LOGIC_KEYS
    }
    return _compact_payload({
        "strategy_type": payload.get("strategy_type"),
        "dsl": dsl,
        "logic_fields": logic_fields,
    })


def build_logic_signature(candidate: Optional[Mapping[str, Any]]) -> str:
    return _hash_payload(_build_candidate_logic_payload(candidate))


def build_dsl_signature(candidate: Optional[Mapping[str, Any]]) -> Optional[str]:
    payload = _build_dsl_signature_payload(_build_candidate_logic_payload(candidate))
    if not dict(payload).get("dsl"):
        return None
    return _hash_payload(payload)


def build_factor_signature(candidate: Optional[Mapping[str, Any]]) -> Optional[str]:
    payload = _build_factor_signature_payload(_build_candidate_logic_payload(candidate))
    if not dict(payload).get("logic_fields") and not dict(payload).get("alpha_params"):
        return None
    return _hash_payload(payload)


def build_entry_exit_signature(candidate: Optional[Mapping[str, Any]]) -> Optional[str]:
    payload = _build_entry_exit_signature_payload(_build_candidate_logic_payload(candidate))
    if not dict(payload).get("dsl") and not dict(payload).get("logic_fields"):
        return None
    return _hash_payload(payload)


def build_alpha_identity_components(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    logic_payload = _build_candidate_logic_payload(candidate)
    dsl_payload = _build_dsl_signature_payload(logic_payload)
    factor_payload = _build_factor_signature_payload(logic_payload)
    entry_exit_payload = _build_entry_exit_signature_payload(logic_payload)
    return {
        "strategy_type": logic_payload.get("strategy_type"),
        "has_material_logic": _has_material_logic_payload(logic_payload),
        "logic_signature": _hash_payload(logic_payload),
        "dsl_signature": _hash_payload(dsl_payload) if dict(dsl_payload).get("dsl") else None,
        "factor_signature": (
            _hash_payload(factor_payload)
            if dict(factor_payload).get("logic_fields") or dict(factor_payload).get("alpha_params")
            else None
        ),
        "entry_exit_signature": (
            _hash_payload(entry_exit_payload)
            if dict(entry_exit_payload).get("dsl") or dict(entry_exit_payload).get("logic_fields")
            else None
        ),
    }


def _semantic_contract_payload(contract: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _canonicalize_contract_value(contract)
    if not isinstance(canonical, dict):
        canonical = dict(contract)
    canonical.pop("candidate_id", None)
    canonical.pop("name", None)
    lineage = dict(canonical.get("lineage") or {})
    lineage.pop("run_id", None)
    lineage.pop("parent_strategy_ids", None)
    canonical["lineage"] = lineage
    return canonical

# --- candidate_contract_parts/policy.py ---
def candidate_contract_value(candidate: Optional[Mapping[str, Any]], key: str, default: Any = None) -> Any:
    payload = dict(candidate or {})
    params = _as_dict(payload.get("params"))
    research_task = _as_dict(payload.get("research_task"))
    lineage = _as_dict(payload.get("lineage"))
    provenance = _as_dict(payload.get("candidate_provenance"))
    for source in (payload, params, lineage, provenance, research_task):
        if key in source and source.get(key) not in _EMPTY_VALUES:
            return source.get(key)
    return default


def resolve_candidate_validation_profile(
    candidate: Optional[Mapping[str, Any]],
    *,
    research_task: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(candidate or {})
    normalized_task = _normalize_research_task_contract(research_task or payload.get("research_task") or {})
    strategy_type = _string(payload.get("strategy_type")).lower()
    explicit_profile = {}
    for source in (
        payload,
        _as_dict(payload.get("params")),
        _as_dict(payload.get("lineage")),
        _as_dict(payload.get("candidate_provenance")),
    ):
        explicit_profile = _as_dict(source.get("validation_profile"))
        if explicit_profile:
            break
    task_validation_profile = _as_dict(normalized_task.get("validation_profile"))
    use_task_validation_profile = bool(
        task_validation_profile
        and (
            bool(candidate_contract_value(payload, "had_explicit_research_task", False))
            or _string(normalized_task.get("task_source")).lower() in {"bulk_stock_matrix", "event_driven"}
            or _string(task_validation_profile.get("validation_focus")).lower()
            in {"candidate_target_only", "event_target_only", "target_only"}
        )
    )
    if not explicit_profile and use_task_validation_profile:
        explicit_profile = dict(task_validation_profile)
    profile_name = _string(explicit_profile.get("profile")).lower()
    validation_focus = _string(
        explicit_profile.get("validation_focus")
        or normalized_task.get("validation_focus")
        or ("event_target_only" if normalized_task.get("task_source") == "event_driven" else "target_plus_representative")
    ).lower() or "target_plus_representative"
    if not profile_name:
        if strategy_type in _FACTOR_VALIDATION_TYPES:
            profile_name = "factor_rank_validation"
        elif strategy_type == "macro_timing":
            profile_name = "macro_regime_validation"
        elif normalized_task.get("task_source") == "event_driven" or validation_focus == "event_target_only":
            profile_name = "event_trade_validation"
        else:
            profile_name = "trade_rule_validation"
    primary_validation_layer = _string(explicit_profile.get("primary_validation_layer")).lower()
    if not primary_validation_layer:
        if validation_focus == "event_target_only":
            primary_validation_layer = "target"
        elif validation_focus == "broad_generalization" or profile_name == "factor_rank_validation":
            primary_validation_layer = "combined"
        else:
            primary_validation_layer = "target"
    return {
        **explicit_profile,
        "profile": profile_name,
        "validation_focus": validation_focus,
        "primary_validation_layer": primary_validation_layer,
    }


def resolve_candidate_targeting_policy(
    candidate: Optional[Mapping[str, Any]],
    *,
    research_task: Optional[Mapping[str, Any]] = None,
    validation_profile: Optional[Mapping[str, Any]] = None,
    constraint_check: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(candidate or {})
    normalized_task = _normalize_research_task_contract(research_task or payload.get("research_task") or {})
    resolved_validation_profile = dict(
        validation_profile
        or resolve_candidate_validation_profile(payload, research_task=normalized_task)
    )
    explicit_policy = _as_dict(candidate_contract_value(payload, "targeting_policy", {}))
    resolved_constraint_check = _as_dict(
        constraint_check
        if constraint_check is not None
        else candidate_contract_value(payload, "constraint_check", {})
    )
    coverage_ratio_raw = explicit_policy.get("coverage_ratio")
    if coverage_ratio_raw is None:
        coverage_ratio_raw = resolved_constraint_check.get("coverage_ratio")
    intersection_ratio_raw = explicit_policy.get("intersection_ratio")
    if intersection_ratio_raw is None:
        intersection_ratio_raw = resolved_constraint_check.get("intersection_ratio")
    constraint_violation = explicit_policy.get("constraint_violation")
    if constraint_violation is None:
        constraint_violation = (
            resolved_constraint_check.get("constraint_violation")
            or resolved_constraint_check.get("alignment_contract_violation")
        )
    return {
        **explicit_policy,
        "target_symbol_policy": _string(
            explicit_policy.get("target_symbol_policy")
            or normalized_task.get("target_symbol_policy")
            or "prefer_intersection"
        ) or "prefer_intersection",
        "universe_expansion_policy": _string(
            explicit_policy.get("universe_expansion_policy")
            or normalized_task.get("universe_expansion_policy")
            or "allow_market_fallback"
        ) or "allow_market_fallback",
        "validation_focus": _string(
            explicit_policy.get("validation_focus")
            or resolved_validation_profile.get("validation_focus")
            or normalized_task.get("validation_focus")
            or "target_plus_representative"
        ) or "target_plus_representative",
        "constraint_violation": bool(constraint_violation),
        "expansion_applied": bool(
            explicit_policy.get("expansion_applied")
            or resolved_constraint_check.get("expansion_applied")
        ),
        "expansion_reason": (
            explicit_policy.get("expansion_reason")
            or resolved_constraint_check.get("expansion_reason")
        ),
        "expansion_source": (
            explicit_policy.get("expansion_source")
            or resolved_constraint_check.get("expansion_source")
        ),
        "coverage_ratio": round(_safe_float(coverage_ratio_raw, 0.0), 4),
        "intersection_ratio": round(_safe_float(intersection_ratio_raw, 0.0), 4),
    }


def _resolve_target_pool_id(
    candidate: Optional[Mapping[str, Any]],
    *,
    research_task: Optional[Mapping[str, Any]] = None,
    target_symbols: Optional[list[str]] = None,
) -> Optional[str]:
    payload = dict(candidate or {})
    params = _as_dict(payload.get("params"))
    normalized_task = _normalize_research_task_contract(research_task or payload.get("research_task") or {})
    stock_pool = _as_dict(candidate_contract_value(payload, "stock_pool", {}))
    task_stock_pool = _as_dict(normalized_task.get("stock_pool"))
    provenance = _as_dict(candidate_contract_value(payload, "candidate_provenance", {}))
    resolved_symbols = list(target_symbols or _extract_target_codes_from_payload(payload, limit=12))
    canonical_symbols = _canonical_target_symbols(resolved_symbols)
    for source in (payload, params, stock_pool, task_stock_pool, provenance, normalized_task):
        for key in ("target_pool_id", "pool_id", "active_pool_id", "candidate_pool_id", "theme_code", "theme_id"):
            value = _string(source.get(key))
            if value:
                return value
    selection_mode = _string(stock_pool.get("selection_mode") or task_stock_pool.get("selection_mode")).lower()
    if selection_mode and canonical_symbols:
        return f"{selection_mode}:{','.join(canonical_symbols)}"
    if canonical_symbols:
        return f"symbols:{','.join(canonical_symbols)}"
    return None


def _resolve_lineage(
    candidate: Optional[Mapping[str, Any]],
    *,
    research_task: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(candidate or {})
    normalized_task = _normalize_research_task_contract(research_task or payload.get("research_task") or {})
    explicit = _as_dict(candidate_contract_value(payload, "lineage", {}))
    event_context = _as_dict(payload.get("event_context") or normalized_task.get("event_context"))
    task_signature = _string(
        explicit.get("task_signature")
        or candidate_contract_value(payload, "task_signature")
        or normalized_task.get("task_signature")
        or _build_task_signature({**normalized_task, **event_context})
    )
    lineage_id = _string(
        explicit.get("lineage_id")
        or candidate_contract_value(payload, "lineage_id")
        or task_signature
    )
    run_id = _string(
        explicit.get("run_id")
        or candidate_contract_value(payload, "run_id")
        or candidate_contract_value(payload, "factory_run_id")
    )
    parent_strategy_ids = _normalize_string_list(
        explicit.get("parent_strategy_ids"),
        explicit.get("parent_candidate_ids"),
        candidate_contract_value(payload, "parent_strategy_ids"),
        candidate_contract_value(payload, "parent_candidate_ids"),
        candidate_contract_value(payload, "parent_strategy_id"),
        candidate_contract_value(payload, "parent_candidate_id"),
    )
    return {
        "lineage_id": lineage_id or None,
        "run_id": run_id or None,
        "task_signature": task_signature or None,
        "parent_strategy_ids": parent_strategy_ids,
    }


def _derive_turnover_cost_class(
    *,
    expected_turnover_band: str,
    capacity_bucket: str,
    slippage_bps: float,
    market_impact_bps: float,
) -> Optional[str]:
    if expected_turnover_band == "very_high" or slippage_bps >= 10.0 or market_impact_bps >= 4.0:
        return "high_touch"
    if expected_turnover_band == "high" or slippage_bps >= 5.0 or capacity_bucket == "small":
        return "medium_touch"
    if expected_turnover_band in {"medium", "low"} or capacity_bucket:
        return "low_touch"
    return None


def _resolve_economic_semantics(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(candidate or {})
    holding_horizon = _as_dict(candidate_contract_value(payload, "holding_horizon", {}))
    rebalance_rule = _as_dict(candidate_contract_value(payload, "rebalance_rule", {}))
    portfolio_spec = _as_dict(candidate_contract_value(payload, "portfolio_spec", {}))
    execution_assumptions = _as_dict(candidate_contract_value(payload, "execution_assumptions", {}))
    position_sizing = _as_dict(candidate_contract_value(payload, "position_sizing", {}))
    capacity_assumption = _as_dict(candidate_contract_value(payload, "capacity_assumption", {}))
    cost_sensitivity_grid = _as_dict(candidate_contract_value(payload, "cost_sensitivity_grid", {}))
    cost_base_case = _as_dict(cost_sensitivity_grid.get("base_case"))

    expected_turnover_band = _string(
        candidate_contract_value(payload, "expected_turnover_band")
        or execution_assumptions.get("expected_turnover_band")
        or portfolio_spec.get("expected_turnover_band")
        or holding_horizon.get("expected_turnover_band")
        or rebalance_rule.get("expected_turnover_band")
        or position_sizing.get("expected_turnover_band")
    ).lower()
    capacity_bucket = _string(
        candidate_contract_value(payload, "capacity_bucket")
        or execution_assumptions.get("capacity_bucket")
        or portfolio_spec.get("capacity_bucket")
        or capacity_assumption.get("capacity_bucket")
        or capacity_assumption.get("bucket")
    ).lower()
    slippage_bps = _safe_float(
        execution_assumptions.get("slippage_bps"),
        _safe_float(cost_base_case.get("slippage_bps"), 0.0),
    )
    market_impact_bps = _safe_float(
        execution_assumptions.get("market_impact_bps"),
        _safe_float(cost_base_case.get("market_impact_bps"), 0.0),
    )
    position_model = _string(
        candidate_contract_value(payload, "position_model")
        or position_sizing.get("mode")
        or portfolio_spec.get("position_assumption")
    )
    return {
        "holding_rationale": _string(
            candidate_contract_value(payload, "holding_rationale")
            or holding_horizon.get("rationale")
        ) or None,
        "cost_sensitivity_grid": dict(cost_sensitivity_grid),
        "position_model": position_model or None,
        "capacity_assumption": dict(capacity_assumption),
        "market_regime_assumption": (
            candidate_contract_value(payload, "market_regime_assumption")
            if candidate_contract_value(payload, "market_regime_assumption") not in _EMPTY_VALUES
            else None
        ),
        "expected_turnover_band": expected_turnover_band or None,
        "capacity_bucket": capacity_bucket or None,
        "position_sizing_rationale": _string(
            portfolio_spec.get("position_sizing_rationale")
            or execution_assumptions.get("position_sizing_rationale")
            or position_sizing.get("position_sizing_rationale")
            or candidate_contract_value(payload, "position_sizing_rationale")
        ) or None,
        "turnover_cost_class": _string(
            execution_assumptions.get("turnover_cost_class")
            or _derive_turnover_cost_class(
                expected_turnover_band=expected_turnover_band,
                capacity_bucket=capacity_bucket,
                slippage_bps=slippage_bps,
                market_impact_bps=market_impact_bps,
            )
        ) or None,
        "slippage_bps": slippage_bps,
        "market_impact_bps": market_impact_bps,
        "capacity_participation_rate": _safe_float(
            execution_assumptions.get("capacity_participation_rate"),
            _safe_float(
                cost_base_case.get("capacity_participation_rate"),
                _safe_float(capacity_assumption.get("capacity_participation_rate"), 0.0),
            ),
        ),
        "adv_ratio_limit": _safe_float(
            execution_assumptions.get("adv_ratio_limit"),
            _safe_float(
                cost_base_case.get("adv_ratio_limit"),
                _safe_float(capacity_assumption.get("adv_ratio_limit"), 0.0),
            ),
        ),
        "max_position_pct": (
            _safe_float(portfolio_spec.get("max_position_pct"))
            if portfolio_spec.get("max_position_pct") is not None
            else (
                _safe_float(capacity_assumption.get("max_position_pct"))
                if capacity_assumption.get("max_position_pct") is not None
                else None
            )
        ),
    }


def _repair_allowed_strategy_types_for_candidate(
    candidate: Optional[Mapping[str, Any]],
    normalized_task: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    payload = dict(candidate or {})
    task = dict(normalized_task or {})
    strategy_type = _string(payload.get("strategy_type")).lower()
    allowed_strategy_types = _normalize_string_list(task.get("allowed_strategy_types"))
    if not allowed_strategy_types or not strategy_type:
        return task

    allowed_strategy_type_set = {
        _string(item).lower()
        for item in allowed_strategy_types
        if _string(item)
    }
    if strategy_type in allowed_strategy_type_set:
        return task

    strategy_profile = infer_candidate_strategy_profile(payload, research_task=task)
    preferred_strategy_types = {
        _string(item).lower()
        for item in _normalize_string_list(
            task.get("preferred_strategy_types"),
            task.get("strategy_preferences"),
        )
        if _string(item)
    }
    if (
        _string(task.get("task_source")).lower() == "snapshot"
        and strategy_type == "momentum"
        and _string(strategy_profile.get("generator_mode")).lower() == "rl_bandit"
        and strategy_type in preferred_strategy_types
    ):
        return {
            **task,
            "allowed_strategy_types": _normalize_string_list(
                task.get("allowed_strategy_types"),
                strategy_type,
            ),
        }
    return task


def _alignment_violation_from_metrics(
    target_symbols: list[str],
    research_symbols: list[str],
    target_alignment_contract: dict[str, Any],
) -> Optional[str]:
    overlap_count = len(set(target_symbols).intersection(research_symbols))
    coverage_ratio = round(overlap_count / max(1, len(target_symbols)), 4) if target_symbols else 0.0
    intersection_ratio = (
        round(overlap_count / max(1, len(research_symbols)), 4)
        if research_symbols
        else None
    )
    min_coverage_ratio = _safe_float(target_alignment_contract.get("min_coverage_ratio"), 0.0)
    min_intersection_ratio = (
        None
        if target_alignment_contract.get("min_intersection_ratio") is None
        else _safe_float(target_alignment_contract.get("min_intersection_ratio"), 0.0)
    )
    min_required_overlap_count = _safe_int(target_alignment_contract.get("min_required_overlap_count"), 0)
    if not target_symbols and research_symbols:
        return "empty_target_symbols_after_alignment"
    if coverage_ratio < min_coverage_ratio:
        return "coverage_ratio_below_contract"
    if intersection_ratio is not None and min_intersection_ratio is not None and intersection_ratio < min_intersection_ratio:
        return "intersection_ratio_below_contract"
    if min_required_overlap_count > 0 and overlap_count < min_required_overlap_count:
        return "target_overlap_count_below_contract"
    return None


def _refresh_constraint_check_from_targets(
    target_symbols: list[str],
    normalized_task: Optional[Mapping[str, Any]],
    existing_constraint_check: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    task = dict(normalized_task or {})
    existing = _as_dict(existing_constraint_check)
    research_symbols = list(task.get("target_symbols") or [])
    target_alignment_contract = dict(task.get("target_alignment_contract") or {})
    overlap_count = len(set(target_symbols).intersection(research_symbols))
    coverage_ratio = round(overlap_count / max(1, len(target_symbols)), 4) if target_symbols else 0.0
    intersection_ratio = (
        round(overlap_count / max(1, len(research_symbols)), 4)
        if research_symbols
        else None
    )
    alignment_violation = None
    alignment_ok = True
    if target_alignment_contract.get("quality_gate_enabled"):
        alignment_violation = _alignment_violation_from_metrics(
            target_symbols,
            research_symbols,
            target_alignment_contract,
        )
        alignment_ok = alignment_violation is None
    return {
        **existing,
        "target_symbols_before_normalize": list(existing.get("target_symbols_before_normalize") or target_symbols),
        "target_symbols_after_normalize": list(target_symbols),
        "research_target_symbols": list(research_symbols),
        "target_symbol_policy": _string(existing.get("target_symbol_policy") or task.get("target_symbol_policy")) or None,
        "universe_expansion_policy": _string(
            existing.get("universe_expansion_policy") or task.get("universe_expansion_policy")
        ) or None,
        "expansion_applied": bool(existing.get("expansion_applied")),
        "expansion_reason": existing.get("expansion_reason"),
        "expansion_source": existing.get("expansion_source"),
        "constraint_violation": existing.get("constraint_violation"),
        "expansion_blocked_reason": existing.get("expansion_blocked_reason"),
        "coverage_ratio": coverage_ratio,
        "intersection_ratio": intersection_ratio,
        "target_overlap_count": int(overlap_count),
        "alignment_contract_ok": alignment_ok,
        "alignment_contract_violation": alignment_violation,
        "target_alignment_contract": dict(target_alignment_contract),
    }


def _constraint_check_refresh_required(
    target_symbols: list[str],
    normalized_task: Optional[Mapping[str, Any]],
    existing_constraint_check: Optional[Mapping[str, Any]] = None,
) -> bool:
    task = dict(normalized_task or {})
    target_alignment_contract = dict(task.get("target_alignment_contract") or {})
    if not target_alignment_contract.get("quality_gate_enabled"):
        return False

    existing = _as_dict(existing_constraint_check)
    if not existing:
        return True

    refreshed = _refresh_constraint_check_from_targets(
        target_symbols,
        task,
        existing_constraint_check=existing,
    )
    for key in ("coverage_ratio", "intersection_ratio"):
        lhs = existing.get(key)
        rhs = refreshed.get(key)
        if lhs is None and rhs is None:
            continue
        if lhs is None or rhs is None:
            return True
        if abs(_safe_float(lhs) - _safe_float(rhs)) > 1e-4:
            return True
    if _safe_int(existing.get("target_overlap_count"), -1) != _safe_int(refreshed.get("target_overlap_count"), -1):
        return True
    if _string(existing.get("alignment_contract_violation")) != _string(refreshed.get("alignment_contract_violation")):
        return True
    return False

# --- candidate_contract_parts/evaluation.py ---
from strategy_factory.application.trade_prediction_contract import freeze_trade_prediction_contract


def _should_trim_candidate_targets_by_alignment_policy(
    candidate: Optional[Mapping[str, Any]],
    normalized_task: Optional[Mapping[str, Any]],
    requested_target_symbols: list[str],
) -> bool:
    payload = dict(candidate or {})
    task = dict(normalized_task or {})
    research_symbols = set(task.get("target_symbols") or [])
    if not requested_target_symbols or not research_symbols:
        return False
    requested_set = set(requested_target_symbols)
    if requested_set.issubset(research_symbols):
        return False
    strategy_type = _string(payload.get("strategy_type")).lower()
    strategy_profile = infer_candidate_strategy_profile(payload, research_task=task)
    return (
        _string(task.get("task_source")).lower() == "snapshot"
        and strategy_type == "momentum"
        and _string(strategy_profile.get("generator_mode")).lower() == "rl_bandit"
    )


def build_portfolio_candidate_contract(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(candidate or {})
    normalized_task = _normalize_research_task_contract(candidate_contract_value(payload, "research_task", {}) or {})
    strategy_profile = infer_candidate_strategy_profile(payload, research_task=normalized_task)
    provenance = _as_dict(candidate_contract_value(payload, "candidate_provenance", {}))
    target_symbols = _extract_target_codes_from_payload(payload, limit=12)
    constraint_check = _as_dict(candidate_contract_value(payload, "constraint_check", {}))
    validation_profile = resolve_candidate_validation_profile(payload, research_task=normalized_task)
    candidate_family = _string(
        candidate_contract_value(payload, "candidate_family")
        or provenance.get("candidate_family")
        or strategy_profile.get("strategy_family")
        or payload.get("strategy_type")
    ).lower()
    candidate_family_id = _string(
        candidate_contract_value(payload, "candidate_family_id")
        or provenance.get("candidate_family_id")
        or strategy_profile.get("candidate_family_id")
    )
    stock_pool = _as_dict(candidate_contract_value(payload, "stock_pool", {}))
    target_source = _string(
        candidate_contract_value(payload, "target_source")
        or stock_pool.get("selection_mode")
        or normalized_task.get("task_source")
        or payload.get("source")
    ).lower()
    economic_semantics = _resolve_economic_semantics(payload)
    targeting = {
        "target_symbols": list(target_symbols),
        "stock_pool": stock_pool,
        "target_pool_id": _resolve_target_pool_id(payload, research_task=normalized_task, target_symbols=target_symbols),
        "coverage_ratio": None
        if constraint_check.get("coverage_ratio") is None
        else round(_safe_float(constraint_check.get("coverage_ratio")), 4),
        "intersection_ratio": None
        if constraint_check.get("intersection_ratio") is None
        else round(_safe_float(constraint_check.get("intersection_ratio")), 4),
        "target_overlap_count": _safe_int(constraint_check.get("target_overlap_count")),
        "target_source": target_source or None,
        "constraint_check": constraint_check,
    }
    contract = {
        "candidate_id": _string(payload.get("candidate_id") or payload.get("id")) or None,
        "name": _string(payload.get("name")) or None,
        "strategy_type": _string(payload.get("strategy_type")).lower() or "unknown",
        "generator_mode": _string(payload.get("generator_mode") or strategy_profile.get("generator_mode")).lower() or None,
        "candidate_family": candidate_family or None,
        "candidate_family_id": candidate_family_id or None,
        "targeting": targeting,
        "research_task": normalized_task,
        "holding_horizon": _as_dict(candidate_contract_value(payload, "holding_horizon", {})),
        "trade_plan": _as_dict(candidate_contract_value(payload, "trade_plan", {})),
        "risk_rules": _as_dict(candidate_contract_value(payload, "risk_rules", {})),
        "runtime_playbook": _as_dict(candidate_contract_value(payload, "runtime_playbook", {})),
        "rebalance_rule": candidate_contract_value(payload, "rebalance_rule", {}),
        "portfolio_spec": _as_dict(candidate_contract_value(payload, "portfolio_spec", {})),
        "execution_assumptions": _as_dict(candidate_contract_value(payload, "execution_assumptions", {})),
        "instrument_profile": _as_dict(candidate_contract_value(payload, "instrument_profile", {})),
        "economic_semantics": economic_semantics,
        "validation_profile": validation_profile,
        "lineage": _resolve_lineage(payload, research_task=normalized_task),
    }
    for field_name in ("evidence_chain", "prediction_contract", "confidence_contract"):
        value = _as_dict(candidate_contract_value(payload, field_name, {}))
        if value:
            contract[field_name] = value
    return contract


def build_resolved_candidate_envelope(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(candidate or {})
    existing_envelope = _as_dict(payload.get("resolved_candidate_envelope"))
    params = _as_dict(payload.get("params"))
    if "had_explicit_research_task" in existing_envelope:
        had_explicit_research_task = bool(existing_envelope.get("had_explicit_research_task"))
    else:
        had_explicit_research_task = False
        for raw_task in (payload.get("research_task"), params.get("research_task")):
            task_payload = dict(raw_task or {}) if isinstance(raw_task, Mapping) else {}
            if not task_payload:
                continue
            if bool(task_payload.get("synthetic_local_spawn")):
                continue
            had_explicit_research_task = True
            break
    normalized_task = _normalize_research_task_contract(
        existing_envelope.get("normalized_research_task")
        or candidate_contract_value(payload, "research_task", {})
        or {}
    )
    normalized_task = _repair_allowed_strategy_types_for_candidate(payload, normalized_task)
    requested_target_symbols = _normalize_target_codes(
        candidate_contract_value(payload, "requested_target_symbols")
        or existing_envelope.get("requested_target_symbols")
        or _extract_candidate_origin_target_codes(payload, limit=12)
        or _extract_target_codes_from_payload(payload, limit=12),
        limit=12,
    )
    raw_constraint_check = _as_dict(candidate_contract_value(payload, "constraint_check", {}))
    should_trim_targets = _should_trim_candidate_targets_by_alignment_policy(
        payload,
        normalized_task,
        requested_target_symbols,
    )
    if should_trim_targets:
        aligned_targeting = _apply_target_symbol_policy(
            requested_target_symbols,
            normalized_task,
            fallback_symbols=[
                normalized_task.get("target_symbols"),
                normalized_task.get("stock_pool"),
            ],
            limit=12,
        )
        resolved_target_symbols = _canonical_target_symbols(
            aligned_targeting.get("target_symbols"),
            limit=12,
        )
        resolved_constraint_check = {
            **raw_constraint_check,
            **_as_dict(aligned_targeting.get("constraint_check")),
        }
    else:
        resolved_target_symbols = _canonical_target_symbols(
            requested_target_symbols,
            limit=12,
        )
        resolved_constraint_check = dict(raw_constraint_check)
    if _constraint_check_refresh_required(
        resolved_target_symbols,
        normalized_task,
        resolved_constraint_check,
    ):
        resolved_constraint_check = _refresh_constraint_check_from_targets(
            resolved_target_symbols,
            normalized_task,
            existing_constraint_check=resolved_constraint_check,
        )
    resolved_stock_pool = _as_dict(
        existing_envelope.get("resolved_stock_pool")
        or candidate_contract_value(payload, "stock_pool", {})
        or normalized_task.get("stock_pool")
        or {}
    )
    if resolved_target_symbols:
        resolved_stock_pool = {
            **resolved_stock_pool,
            "selection_mode": _string(resolved_stock_pool.get("selection_mode") or "explicit") or "explicit",
            "symbols": list(resolved_target_symbols),
        }

    resolved_payload = {
        **payload,
        "research_task": normalized_task,
        "target_symbols": list(resolved_target_symbols),
        "stock_pool": dict(resolved_stock_pool),
        "constraint_check": dict(resolved_constraint_check),
        "params": {
            **params,
            "research_task": dict(normalized_task),
            "requested_target_symbols": list(requested_target_symbols),
            "target_symbols": list(resolved_target_symbols),
            "stock_pool": dict(resolved_stock_pool),
            "constraint_check": dict(resolved_constraint_check),
        },
    }
    contract_snapshot = build_portfolio_candidate_contract(resolved_payload)
    resolved_target_symbols = list((contract_snapshot.get("targeting") or {}).get("target_symbols") or resolved_target_symbols)
    resolved_stock_pool = _as_dict((contract_snapshot.get("targeting") or {}).get("stock_pool") or resolved_stock_pool)
    resolved_constraint_check = _as_dict((contract_snapshot.get("targeting") or {}).get("constraint_check") or resolved_constraint_check)
    resolved_holding_horizon = _as_dict(contract_snapshot.get("holding_horizon") or {})
    resolved_trade_plan = _as_dict(contract_snapshot.get("trade_plan") or {})
    resolved_risk_rules = _as_dict(contract_snapshot.get("risk_rules") or {})
    resolved_runtime_playbook = _as_dict(contract_snapshot.get("runtime_playbook") or {})
    resolved_rebalance_rule = _as_dict(contract_snapshot.get("rebalance_rule") or {})
    resolved_portfolio_spec = _as_dict(contract_snapshot.get("portfolio_spec") or {})
    resolved_execution_assumptions = normalize_execution_assumptions(
        contract_snapshot.get("execution_assumptions"),
        portfolio_spec=resolved_portfolio_spec,
        capacity_assumption=candidate_contract_value(resolved_payload, "capacity_assumption", {}),
        holding_horizon=resolved_holding_horizon,
        cost_sensitivity_grid=candidate_contract_value(resolved_payload, "cost_sensitivity_grid", {}),
    )
    resolved_validation_profile = dict(
        contract_snapshot.get("validation_profile")
        or resolve_candidate_validation_profile(resolved_payload, research_task=normalized_task)
    )
    resolved_targeting_policy = resolve_candidate_targeting_policy(
        resolved_payload,
        research_task=normalized_task,
        validation_profile=resolved_validation_profile,
        constraint_check=resolved_constraint_check,
    )
    alpha_identity_components = build_alpha_identity_components(resolved_payload)
    execution_contract_hash = build_execution_contract_hash(contract=contract_snapshot)
    candidate_contract_hash = execution_contract_hash
    tested_object_hash = build_tested_object_hash(resolved_payload)
    candidate_identity_signature = build_candidate_identity_signature(resolved_payload)
    candidate_lineage_contract = dict(contract_snapshot.get("lineage") or {})
    return {
        "had_explicit_research_task": bool(had_explicit_research_task),
        "normalized_research_task": normalized_task,
        "requested_target_symbols": list(requested_target_symbols),
        "resolved_target_symbols": list(resolved_target_symbols),
        "resolved_stock_pool": dict(resolved_stock_pool),
        "resolved_constraint_check": dict(resolved_constraint_check),
        "resolved_holding_horizon": dict(resolved_holding_horizon),
        "resolved_trade_plan": dict(resolved_trade_plan),
        "resolved_risk_rules": dict(resolved_risk_rules),
        "resolved_rebalance_rule": dict(resolved_rebalance_rule),
        "resolved_portfolio_spec": dict(resolved_portfolio_spec),
        "resolved_execution_assumptions": dict(resolved_execution_assumptions),
        "resolved_runtime_playbook": dict(resolved_runtime_playbook),
        "resolved_validation_profile": dict(resolved_validation_profile),
        "resolved_targeting_policy": dict(resolved_targeting_policy),
        "candidate_contract_snapshot": contract_snapshot,
        "candidate_contract_hash": candidate_contract_hash,
        "execution_contract_hash": execution_contract_hash,
        "tested_object_hash": tested_object_hash,
        "candidate_identity_signature": candidate_identity_signature,
        "candidate_lineage_contract": candidate_lineage_contract,
        "logic_signature": alpha_identity_components.get("logic_signature"),
        "dsl_signature": alpha_identity_components.get("dsl_signature"),
        "factor_signature": alpha_identity_components.get("factor_signature"),
        "entry_exit_signature": alpha_identity_components.get("entry_exit_signature"),
    }


def apply_resolved_candidate_envelope(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    payload = dict(candidate or {})
    envelope = build_resolved_candidate_envelope(payload)
    trade_prediction_seed_params = {
        **_as_dict(payload.get("params")),
        "research_task": dict(envelope.get("normalized_research_task") or {}),
        "target_symbols": list(envelope.get("resolved_target_symbols") or []),
        "stock_pool": dict(envelope.get("resolved_stock_pool") or {}),
        "holding_horizon": dict(envelope.get("resolved_holding_horizon") or {}),
        "trade_plan": dict(envelope.get("resolved_trade_plan") or {}),
    }
    trade_prediction_source = {
        **payload,
        "research_task": dict(envelope.get("normalized_research_task") or {}),
        "target_symbols": list(envelope.get("resolved_target_symbols") or []),
        "stock_pool": dict(envelope.get("resolved_stock_pool") or {}),
        "holding_horizon": dict(envelope.get("resolved_holding_horizon") or {}),
        "trade_plan": dict(envelope.get("resolved_trade_plan") or {}),
        "params": trade_prediction_seed_params,
    }
    trade_prediction = freeze_trade_prediction_contract(trade_prediction_source)
    trade_prediction_contract = dict(trade_prediction.get("contract") or {})
    trade_prediction_status = str(trade_prediction.get("status") or "")
    trade_prediction_hash = trade_prediction.get("contract_hash")
    trade_prediction_missing_fields = list(trade_prediction.get("missing_fields") or [])
    trade_prediction_reject_reasons = list(trade_prediction.get("reject_reasons") or [])
    resolved_validation_profile = dict(envelope.get("resolved_validation_profile") or {})
    resolved_targeting_policy = dict(envelope.get("resolved_targeting_policy") or {})
    params = {
        **_as_dict(payload.get("params")),
        "had_explicit_research_task": bool(envelope.get("had_explicit_research_task")),
        "research_task": dict(envelope.get("normalized_research_task") or {}),
        "requested_target_symbols": list(envelope.get("requested_target_symbols") or []),
        "target_symbols": list(envelope.get("resolved_target_symbols") or []),
        "stock_pool": dict(envelope.get("resolved_stock_pool") or {}),
        "constraint_check": dict(envelope.get("resolved_constraint_check") or {}),
        "holding_horizon": dict(envelope.get("resolved_holding_horizon") or {}),
        "trade_plan": dict(envelope.get("resolved_trade_plan") or {}),
        "risk_rules": dict(envelope.get("resolved_risk_rules") or {}),
        "rebalance_rule": dict(envelope.get("resolved_rebalance_rule") or {}),
        "portfolio_spec": dict(envelope.get("resolved_portfolio_spec") or {}),
        "execution_assumptions": dict(envelope.get("resolved_execution_assumptions") or {}),
        "runtime_playbook": dict(envelope.get("resolved_runtime_playbook") or {}),
        "validation_profile": resolved_validation_profile,
        "targeting_policy": resolved_targeting_policy,
        "candidate_contract_snapshot": dict(envelope.get("candidate_contract_snapshot") or {}),
        "candidate_contract_hash": envelope.get("candidate_contract_hash"),
        "execution_contract_hash": envelope.get("execution_contract_hash"),
        "tested_object_hash": envelope.get("tested_object_hash"),
        "candidate_identity_signature": envelope.get("candidate_identity_signature"),
        "candidate_lineage_contract": dict(envelope.get("candidate_lineage_contract") or {}),
        "logic_signature": envelope.get("logic_signature"),
        "dsl_signature": envelope.get("dsl_signature"),
        "factor_signature": envelope.get("factor_signature"),
        "entry_exit_signature": envelope.get("entry_exit_signature"),
        "trade_prediction_contract": trade_prediction_contract,
        "trade_prediction_contract_status": trade_prediction_status,
        "trade_prediction_contract_hash": trade_prediction_hash,
        "trade_prediction_contract_missing_fields": trade_prediction_missing_fields,
        "trade_prediction_contract_reject_reasons": trade_prediction_reject_reasons,
        "candidate_origin": candidate_contract_value(payload, "candidate_origin"),
        "event_anchor": _as_dict(candidate_contract_value(payload, "event_anchor", {})),
        "target_pool_source": candidate_contract_value(payload, "target_pool_source"),
        "resolved_candidate_envelope": envelope,
    }
    return {
        **payload,
        "params": params,
        "had_explicit_research_task": bool(envelope.get("had_explicit_research_task")),
        "research_task": dict(envelope.get("normalized_research_task") or {}),
        "requested_target_symbols": list(envelope.get("requested_target_symbols") or []),
        "target_symbols": list(envelope.get("resolved_target_symbols") or []),
        "stock_pool": dict(envelope.get("resolved_stock_pool") or {}),
        "constraint_check": dict(envelope.get("resolved_constraint_check") or {}),
        "holding_horizon": dict(envelope.get("resolved_holding_horizon") or {}),
        "trade_plan": dict(envelope.get("resolved_trade_plan") or {}),
        "risk_rules": dict(envelope.get("resolved_risk_rules") or {}),
        "rebalance_rule": dict(envelope.get("resolved_rebalance_rule") or {}),
        "portfolio_spec": dict(envelope.get("resolved_portfolio_spec") or {}),
        "execution_assumptions": dict(envelope.get("resolved_execution_assumptions") or {}),
        "runtime_playbook": dict(envelope.get("resolved_runtime_playbook") or {}),
        "validation_profile": resolved_validation_profile,
        "targeting_policy": resolved_targeting_policy,
        "candidate_contract_snapshot": dict(envelope.get("candidate_contract_snapshot") or {}),
        "candidate_contract_hash": str(envelope.get("candidate_contract_hash") or ""),
        "execution_contract_hash": str(envelope.get("execution_contract_hash") or ""),
        "tested_object_hash": str(envelope.get("tested_object_hash") or ""),
        "candidate_identity_signature": str(envelope.get("candidate_identity_signature") or ""),
        "candidate_lineage_contract": dict(envelope.get("candidate_lineage_contract") or {}),
        "logic_signature": str(envelope.get("logic_signature") or ""),
        "dsl_signature": str(envelope.get("dsl_signature") or ""),
        "factor_signature": str(envelope.get("factor_signature") or ""),
        "entry_exit_signature": str(envelope.get("entry_exit_signature") or ""),
        "trade_prediction_contract": trade_prediction_contract,
        "trade_prediction_contract_status": trade_prediction_status,
        "trade_prediction_contract_hash": str(trade_prediction_hash or ""),
        "trade_prediction_contract_missing_fields": trade_prediction_missing_fields,
        "trade_prediction_contract_reject_reasons": trade_prediction_reject_reasons,
        "candidate_origin": candidate_contract_value(payload, "candidate_origin"),
        "event_anchor": _as_dict(candidate_contract_value(payload, "event_anchor", {})),
        "target_pool_source": candidate_contract_value(payload, "target_pool_source"),
        "resolved_candidate_envelope": envelope,
    }


def build_execution_contract_hash(
    candidate: Optional[Mapping[str, Any]] = None,
    *,
    contract: Optional[Mapping[str, Any]] = None,
) -> str:
    payload = dict(contract or build_portfolio_candidate_contract(candidate))
    semantic_payload = _semantic_contract_payload(payload)
    return _hash_payload(semantic_payload)


def build_candidate_contract_hash(
    candidate: Optional[Mapping[str, Any]] = None,
    *,
    contract: Optional[Mapping[str, Any]] = None,
) -> str:
    return build_execution_contract_hash(candidate, contract=contract)


def build_tested_object_hash(candidate: Optional[Mapping[str, Any]]) -> str:
    alpha_identity = build_alpha_identity_components(candidate)
    tested_object_payload = {
        "strategy_type": alpha_identity.get("strategy_type"),
        "logic_signature": alpha_identity.get("logic_signature"),
        "dsl_signature": alpha_identity.get("dsl_signature"),
        "factor_signature": alpha_identity.get("factor_signature"),
        "entry_exit_signature": alpha_identity.get("entry_exit_signature"),
    }
    return _hash_payload(tested_object_payload)


def build_candidate_identity_signature(candidate: Optional[Mapping[str, Any]]) -> str:
    contract = _semantic_contract_payload(build_portfolio_candidate_contract(candidate))
    targeting = dict(contract.get("targeting") or {})
    lineage = dict(contract.get("lineage") or {})
    execution_contract_hash = build_execution_contract_hash(contract=contract)
    identity_payload = {
        "strategy_type": contract.get("strategy_type"),
        "candidate_family_id": contract.get("candidate_family_id"),
        "execution_contract_hash": execution_contract_hash,
        "tested_object_hash": build_tested_object_hash(candidate),
        "validation_profile": dict(contract.get("validation_profile") or {}),
        "targeting": {
            "target_pool_id": targeting.get("target_pool_id"),
            "target_symbols": list(targeting.get("target_symbols") or []),
        },
        "holding_horizon": dict(contract.get("holding_horizon") or {}),
        "trade_plan": dict(contract.get("trade_plan") or {}),
        "risk_rules": dict(contract.get("risk_rules") or {}),
        "runtime_playbook": dict(contract.get("runtime_playbook") or {}),
        "rebalance_rule": contract.get("rebalance_rule"),
        "portfolio_spec": dict(contract.get("portfolio_spec") or {}),
        "execution_assumptions": dict(contract.get("execution_assumptions") or {}),
        "lineage_id": lineage.get("lineage_id"),
        "task_signature": lineage.get("task_signature"),
    }
    return _hash_payload(identity_payload)


def build_candidate_contract_backfill(candidate: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    resolved = apply_resolved_candidate_envelope(candidate)
    contract_snapshot = dict(resolved.get("candidate_contract_snapshot") or build_portfolio_candidate_contract(resolved))
    targeting = dict(contract_snapshot.get("targeting") or {})
    params = _as_dict(resolved.get("params"))
    alpha_identity = build_alpha_identity_components(resolved)
    has_material_logic = bool(alpha_identity.get("has_material_logic"))
    legacy_identity_partial = not has_material_logic
    tested_object_backfill_incomplete = not has_material_logic
    return {
        "candidate_contract_snapshot": contract_snapshot,
        "candidate_contract_hash": str(resolved.get("candidate_contract_hash") or build_candidate_contract_hash(contract=contract_snapshot)),
        "execution_contract_hash": str(resolved.get("execution_contract_hash") or build_execution_contract_hash(contract=contract_snapshot)),
        "candidate_identity_signature": str(
            resolved.get("candidate_identity_signature") or build_candidate_identity_signature(resolved)
        ),
        "tested_object_hash": str(resolved.get("tested_object_hash") or build_tested_object_hash(resolved)),
        "candidate_lineage_contract": dict(
            resolved.get("candidate_lineage_contract") or contract_snapshot.get("lineage") or {}
        ),
        "target_pool_id": targeting.get("target_pool_id"),
        "logic_signature": str(alpha_identity.get("logic_signature") or params.get("logic_signature") or ""),
        "dsl_signature": str(alpha_identity.get("dsl_signature") or params.get("dsl_signature") or ""),
        "factor_signature": str(alpha_identity.get("factor_signature") or params.get("factor_signature") or ""),
        "entry_exit_signature": str(alpha_identity.get("entry_exit_signature") or params.get("entry_exit_signature") or ""),
        "legacy_identity_partial": legacy_identity_partial,
        "tested_object_backfill_incomplete": tested_object_backfill_incomplete,
    }


def build_factory_backtest_assumptions(candidate: Optional[Mapping[str, Any]]) -> FactoryBacktestAssumptions:
    contract = build_portfolio_candidate_contract(candidate)
    execution_assumptions = normalize_execution_assumptions(
        contract.get("execution_assumptions"),
        portfolio_spec=contract.get("portfolio_spec"),
        capacity_assumption=candidate_contract_value(candidate, "capacity_assumption", {}),
        holding_horizon=contract.get("holding_horizon"),
        cost_sensitivity_grid=candidate_contract_value(candidate, "cost_sensitivity_grid", {}),
    )
    portfolio_spec = dict(contract.get("portfolio_spec") or {})
    validation_profile = dict(contract.get("validation_profile") or {})
    economic_semantics = dict(contract.get("economic_semantics") or {})
    target_symbols = list((contract.get("targeting") or {}).get("target_symbols") or [])
    target_weight_scheme = _string(
        portfolio_spec.get("target_weight_scheme")
        or ("equal_weight" if len(target_symbols) > 1 else "single_name")
    ) or ("equal_weight" if len(target_symbols) > 1 else "single_name")
    return FactoryBacktestAssumptions(
        initial_capital=_safe_float(execution_assumptions.get("initial_capital"), 100000.0),
        commission_rate=_safe_float(execution_assumptions.get("commission_rate"), 0.00025),
        slippage_bps=_safe_float(
            execution_assumptions.get("slippage_bps"),
            _safe_float(
                execution_assumptions.get("slippage"),
                _safe_float(economic_semantics.get("slippage_bps"), 0.0) / 10000.0,
            ) * 10000.0,
        ),
        market_impact_bps=_safe_float(
            execution_assumptions.get("market_impact_bps"),
            _safe_float(economic_semantics.get("market_impact_bps"), 0.0),
        ),
        arrival_price_policy=_string(execution_assumptions.get("arrival_price_policy") or "next_open_proxy") or "next_open_proxy",
        implementation_shortfall_proxy=_safe_float(execution_assumptions.get("implementation_shortfall_proxy"), 0.0),
        tradability_filter=_safe_bool(execution_assumptions.get("tradability_filter"), True),
        slippage_model=_string(execution_assumptions.get("slippage_model") or "fixed") or "fixed",
        max_position_pct=(
            _safe_float(portfolio_spec.get("max_position_pct"))
            if portfolio_spec.get("max_position_pct") is not None
            else (
                _safe_float(economic_semantics.get("max_position_pct"))
                if economic_semantics.get("max_position_pct") is not None
                else None
            )
        ),
        capacity_participation_rate=_safe_float(
            execution_assumptions.get("capacity_participation_rate"),
            _safe_float(economic_semantics.get("capacity_participation_rate"), 0.0),
        ),
        adv_ratio_limit=_safe_float(
            execution_assumptions.get("adv_ratio_limit"),
            _safe_float(economic_semantics.get("adv_ratio_limit"), 0.0),
        ),
        capacity_bucket=_string(
            execution_assumptions.get("capacity_bucket")
            or economic_semantics.get("capacity_bucket")
        ) or None,
        margin_rate=(
            _safe_float(execution_assumptions.get("margin_rate"))
            if execution_assumptions.get("margin_rate") is not None
            else None
        ),
        contract_multiplier=(
            _safe_int(execution_assumptions.get("contract_multiplier"))
            if execution_assumptions.get("contract_multiplier") is not None
            else None
        ),
        liquidity_bucket=_string(execution_assumptions.get("liquidity_bucket")) or None,
        max_contracts_per_rebalance=(
            _safe_int(execution_assumptions.get("max_contracts_per_rebalance"))
            if execution_assumptions.get("max_contracts_per_rebalance") is not None
            else None
        ),
        position_assumption=_string(
            portfolio_spec.get("position_assumption")
            or economic_semantics.get("position_model")
            or ("equal_weight_proxy" if len(target_symbols) > 1 else "single_name_full_notional")
        )
        or ("equal_weight_proxy" if len(target_symbols) > 1 else "single_name_full_notional"),
        target_weight_scheme=target_weight_scheme,
        target_weight_map=_as_dict(portfolio_spec.get("target_weight_map")),
        turnover_cost_class=_string(
            execution_assumptions.get("turnover_cost_class")
            or economic_semantics.get("turnover_cost_class")
        ) or None,
        position_sizing_rationale=_string(
            portfolio_spec.get("position_sizing_rationale")
            or execution_assumptions.get("position_sizing_rationale")
            or economic_semantics.get("position_sizing_rationale")
            or candidate_contract_value(candidate, "position_sizing_rationale")
        )
        or None,
        expected_turnover_band=_string(
            execution_assumptions.get("expected_turnover_band")
            or portfolio_spec.get("expected_turnover_band")
            or economic_semantics.get("expected_turnover_band")
            or dict(contract.get("holding_horizon") or {}).get("expected_turnover_band")
            or candidate_contract_value(candidate, "expected_turnover_band")
        )
        or None,
        market_regime_assumption=(
            economic_semantics.get("market_regime_assumption")
            if economic_semantics.get("market_regime_assumption") not in _EMPTY_VALUES
            else None
        ),
        market_ruleset=_string(execution_assumptions.get("market_ruleset") or "cn_equity") or "cn_equity",
        sell_tax_rate=_safe_float(execution_assumptions.get("sell_tax_rate"), 0.001),
        min_trade_lot=max(1, _safe_int(execution_assumptions.get("min_trade_lot"), 100)),
        t_plus_one=_safe_bool(execution_assumptions.get("t_plus_one"), True),
        validation_focus=_string(validation_profile.get("validation_focus") or "target_plus_representative")
        or "target_plus_representative",
    )

# --- candidate_contract_parts/reporting.py ---
__all__ = [
    "apply_resolved_candidate_envelope",
    "build_alpha_identity_components",
    "build_candidate_contract_hash",
    "build_candidate_contract_backfill",
    "build_candidate_identity_signature",
    "build_dsl_signature",
    "build_entry_exit_signature",
    "build_execution_contract_hash",
    "build_factor_signature",
    "build_factory_backtest_assumptions",
    "build_logic_signature",
    "build_portfolio_candidate_contract",
    "build_resolved_candidate_envelope",
    "build_tested_object_hash",
    "candidate_contract_value",
    "resolve_candidate_targeting_policy",
    "resolve_candidate_validation_profile",
]

