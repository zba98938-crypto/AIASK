"""Shared submission-stage quality gate evaluation.

This module centralizes the Gate-3 quality evaluation used by both
strategy_manager submit/recheck flows and strategy_factory submitter.
"""
from __future__ import annotations

# --- runner_parts/normalizers.py ---
import math
import logging
from typing import Any, Dict, Optional

import numpy as np

from ...domain.constants import (
    INCUBATION_ADMISSION_THRESHOLDS,
    LIVE_ADMISSION_THRESHOLDS,
    QUALITY_GATE_THRESHOLDS,
    RESEARCH_ADMISSION_THRESHOLDS,
    STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE,
    TRADE_GATE_PROFILE_THRESHOLDS,
)
from ...domain.strategy_profile import infer_candidate_strategy_profile
from ...domain.targets import (
    _build_task_signature,
    _extract_target_codes_from_payload,
    _normalize_research_task_contract,
    _resolve_validation_focus_layer,
)
from ...infrastructure.mcp_services import get_normalize_klines, get_strategy_registry, get_validation_runtime
from ..candidate_contract import (
    apply_resolved_candidate_envelope,
    build_candidate_contract_hash,
    build_candidate_identity_signature,
    build_dsl_signature,
    build_entry_exit_signature,
    build_execution_contract_hash,
    build_factor_signature,
    build_logic_signature,
    build_portfolio_candidate_contract,
    build_tested_object_hash,
    resolve_candidate_validation_profile,
)
from ..research_protocol_contract import (
    adapt_research_validation_contract_for_submission,
    evaluate_research_validation_contract_admission,
)
from ..quality_reporting import maybe_grant_provisional_incubation, normalize_quality_gate_result, safe_metric_value


logger = logging.getLogger(__name__)

_FACTOR_VALIDATION_TYPES = {"value_factor", "quality_factor", "growth_factor", "multi_factor"}
_TRADE_PRIMARY_PROFILES = {"trade_rule_validation", "event_trade_validation", "macro_regime_validation"}
_ADMISSION_LEVEL_ORDER = ("research", "incubation", "live")
_ADMISSION_THRESHOLD_SETS = {
    "research": RESEARCH_ADMISSION_THRESHOLDS,
    "incubation": INCUBATION_ADMISSION_THRESHOLDS,
    "live": LIVE_ADMISSION_THRESHOLDS,
}
_SUPPLEMENTAL_STATISTICAL_FIELDS = {
    "wf_ic_ir",
    "pkf_ic",
    "bootstrap_ci_lower",
    "param_sensitivity",
    "period_robustness",
    "run_correction_mode",
    "raw_sharpe_proxy",
    "deflated_sharpe_proxy",
    "pbo_proxy",
    "reality_check_pvalue_proxy",
    "spa_pvalue_proxy",
    "multiple_testing_mode",
    "deflated_sharpe_ratio",
    "deflated_sharpe_reference_sharpe",
    "deflated_sharpe_effective_trials",
    "pbo",
    "white_reality_check_pvalue",
    "hansen_spa_pvalue",
    "multiple_testing",
    "multiple_testing_cohort_mode",
    "multiple_testing_panel_symbols",
    "multiple_testing_panel_size",
    "cohort_effective_trials",
    "batch_correlation_mode",
    "batch_correlation_multiplier",
    "batch_correlation_sibling_count",
}
_TARGET_ONLY_VALIDATION_FOCUSES = {"candidate_target_only", "event_target_only", "target_only"}
_NON_PROMOTABLE_VALIDATION_GRADES = {"D"}
_NON_PROMOTABLE_REVIEW_DECISIONS = {"reject", "revise", "retire", "drop", "defer", "watch", "observe", "paper"}
_NON_PROMOTABLE_REVIEW_RECOMMENDATIONS = {"reject", "revise", "defer", "observe_only", "paper_only"}
_LLM_CORRELATED_GENERATOR_MODES = {"external_llm", "pipeline_staged", "llm_proxy", "llm_proxy_fallback"}

# === DEV-V1 P3: D→C 升级 family 集合 toggle 化 ===
# 基础集合(永远启用)
_BASE_TRADE_AWARE_FAMILIES = frozenset({"momentum", "ma_cross", "quality_factor"})
# 候选扩展集合 — env STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES 灰度推时,
# 只有在白名单内的 family 会被实际加入,防止误配置(如 reverse-only 类型)。
_CANDIDATE_EXTRA_FAMILIES = frozenset({
    "volatility_breakout",      # 占 48% 候选,优先验证
    "value_factor",
    "sector_rotation",
    "macro_timing",
    "growth_factor",
    "north_capital_track",
    "event_structure_breakout",
})


def _resolve_trade_aware_families() -> frozenset:
    """运行时解析 D→C 升级允许的 family 集合,惰性 import 避免 fragment 顶部 import 复杂化。"""
    try:
        from strategy_factory.application._runtime_toggles import trade_aware_extra_families
        extras_raw = trade_aware_extra_families()
    except Exception:
        extras_raw = frozenset()
    extras_safe = extras_raw & _CANDIDATE_EXTRA_FAMILIES
    return _BASE_TRADE_AWARE_FAMILIES | extras_safe


class _TradeAwareFamiliesProxy:
    """模拟 frozenset 接口,但每次访问都重新解析 toggle。

    现有调用点 (`x in _TRADE_AWARE_VALIDATION_GRADE_FAMILIES`) 零修改即可适配。
    """

    __slots__ = ()

    def __contains__(self, item) -> bool:
        return item in _resolve_trade_aware_families()

    def __iter__(self):
        return iter(_resolve_trade_aware_families())

    def __len__(self) -> int:
        return len(_resolve_trade_aware_families())

    def __repr__(self) -> str:
        return f"_TradeAwareFamiliesProxy({set(_resolve_trade_aware_families())!r})"

    def __eq__(self, other) -> bool:
        return _resolve_trade_aware_families() == other

    def __hash__(self) -> int:  # 防止 mutable warning,通常 set 不被当成 dict key 用
        return hash(_BASE_TRADE_AWARE_FAMILIES)


_TRADE_AWARE_VALIDATION_GRADE_FAMILIES = _TradeAwareFamiliesProxy()
_INCUBATION_OBSERVE_TARGET_LAYER_OOS_SOFT_BAND = 0.03
_INCUBATION_OBSERVE_MDD_SOFT_BAND = 0.03
_INCUBATION_OBSERVE_POST_COST_SHARPE_FLOOR = 0.55
_INCUBATION_OBSERVE_TRADE_COUNT_FLOOR = 8.0
_TREND_EXECUTABLE_DSL_TYPES = {"ma_cross", "momentum", "volatility_breakout", "event_structure_breakout"}
_PROXY_RUNTIME_FACTOR_TYPES = {"quality_factor", "value_factor", "growth_factor"}
_CORE_BACKTEST_METRIC_FIELDS = (
    "sharpe_ratio",
    "post_cost_sharpe",
    "total_return",
    "target_layer_oos_return",
    "target_layer_abnormal_return",
    "trade_count",
    "trades_count",
    "max_drawdown",
    "win_rate",
    "profit_factor",
    "expectancy",
    "expectancy_return",
    "avg_win",
    "avg_loss",
    "payoff_ratio",
    "breakeven_win_rate",
    "gross_profit",
    "gross_loss",
    "avg_holding_days",
    "turnover_proxy",
    "event_window_hit_ratio",
    "post_event_decay",
    "trade_density",
    "parameter_perturbation_trade_stability",
    "trade_distribution",
    "trade_profile_available",
    "trade_profile_source",
    "trade_profile_aggregation",
    "validation_focus",
    "primary_validation_layer",
    "code_source",
    "target_codes",
)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_trade_prediction_contract_observation_gap(
    *,
    status: str,
    reject_reasons: list[str],
    contract: Optional[dict[str, Any]] = None,
) -> bool:
    """Return True when a derived prediction contract is simply too sparse for admission.

    Explicit malformed contracts remain hard rejects. Derived contracts often have
    no direction/confidence yet; those should enter paper observation and gather
    forward evidence instead of blocking G3.
    """

    normalized_status = str(status or "").strip().lower()
    contract_payload = dict(contract or {})
    if normalized_status == "ready":
        return False
    if normalized_status in {"", "missing"} or not contract_payload:
        return False
    normalized_reasons = [str(item or "").strip().lower() for item in (reject_reasons or []) if str(item or "").strip()]
    if not normalized_reasons:
        return False
    source = str(contract_payload.get("contract_source") or "").strip().lower()
    if source == "explicit":
        return False
    invalid_reasons = [reason for reason in normalized_reasons if reason.startswith("invalid:")]
    derived_missing_invalid = {"invalid:direction", "invalid:confidence"}
    if any(reason not in derived_missing_invalid for reason in invalid_reasons):
        return False
    derived_missing_fields = {"missing:direction", "missing:confidence"}
    missing_reasons = [reason for reason in normalized_reasons if reason.startswith("missing:")]
    if any(reason not in derived_missing_fields for reason in missing_reasons):
        return False
    return bool(
        any(reason in derived_missing_fields for reason in normalized_reasons)
        or any(reason in derived_missing_invalid for reason in normalized_reasons)
    )


def _normalize_symbol_list(*values: Any, limit: int = 12) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    stack = list(values)
    while stack:
        value = stack.pop(0)
        if isinstance(value, (list, tuple, set)):
            stack[:0] = list(value)
            continue
        token = str(value or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
        if len(ordered) >= limit:
            break
    return ordered


def _normalize_event_anchor_payload(*values: Any) -> dict[str, Any]:
    anchor: dict[str, Any] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        payload = dict(value or {})
        if not payload:
            continue
        anchor = {
            "source": _normalize_text(payload.get("source")) or None,
            "id": _strip_text(payload.get("id")) or None,
            "type": _normalize_text(payload.get("type")) or None,
            "strength": payload.get("strength"),
            "theme_code": _strip_text(payload.get("theme_code")) or None,
            "focus_industries": _normalize_string_list(payload.get("focus_industries"), limit=6),
            "target_symbols": _normalize_symbol_list(payload.get("target_symbols"), limit=12),
        }
        if any(anchor.get(key) not in (None, "", [], {}) for key in ("source", "id", "type", "theme_code", "target_symbols")):
            return anchor
    return {}


def _metric_value_present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _set_metric_from_source(
    target: dict[str, Any],
    audit: dict[str, Any],
    field_name: str,
    value: Any,
    source: str,
    *,
    override: bool = False,
) -> None:
    if not _metric_value_present(value):
        return
    if not override and _metric_value_present(target.get(field_name)):
        return
    target[field_name] = value
    audit[field_name] = source


def _materialize_backtest_metrics_contract(backtest_metrics: Optional[dict[str, Any]]) -> dict[str, Any]:
    metrics = dict(backtest_metrics or {})
    contract = dict(metrics.get("backtest_metrics_contract") or {})
    contract_status = _normalize_text(
        metrics.get("backtest_metrics_contract_status") or contract.get("status")
    ) or ("present" if contract else "missing")
    metric_source_audit = dict(metrics.get("metric_source_audit") or {})
    flattened = dict(metrics)
    flattened["backtest_metrics_contract"] = contract
    flattened["backtest_metrics_contract_status"] = contract_status
    for field_name in _CORE_BACKTEST_METRIC_FIELDS:
        if _metric_value_present(flattened.get(field_name)):
            metric_source_audit.setdefault(field_name, "backtest_metrics")

    for source_name, source_payload in (
        ("backtest_metrics_contract", contract),
        ("event_window_metrics", dict(metrics.get("event_window_metrics") or {})),
        ("target_layer_metrics", dict(metrics.get("target_layer_metrics") or {})),
        ("combined_layer_metrics", dict(metrics.get("combined_layer_metrics") or {})),
    ):
        for field_name in _CORE_BACKTEST_METRIC_FIELDS:
            _set_metric_from_source(
                flattened,
                metric_source_audit,
                field_name,
                source_payload.get(field_name),
                source_name,
            )

    if not _metric_value_present(flattened.get("trade_count")) and _metric_value_present(flattened.get("trades_count")):
        flattened["trade_count"] = flattened.get("trades_count")
        metric_source_audit["trade_count"] = metric_source_audit.get("trades_count", "trades_count_alias")
    if not _metric_value_present(flattened.get("trades_count")) and _metric_value_present(flattened.get("trade_count")):
        flattened["trades_count"] = flattened.get("trade_count")
        metric_source_audit["trades_count"] = metric_source_audit.get("trade_count", "trade_count_alias")

    flattened["metric_source_audit"] = metric_source_audit
    return flattened


def _merge_authoritative_backtest_metrics(
    gate: Optional[dict[str, Any]],
    backtest_metrics: Optional[dict[str, Any]],
) -> dict[str, Any]:
    gate_payload = dict(gate or {})
    materialized = _materialize_backtest_metrics_contract(backtest_metrics)
    metric_source_audit = {
        **dict(materialized.get("metric_source_audit") or {}),
        **dict(gate_payload.get("metric_source_audit") or {}),
    }
    merged = dict(gate_payload)
    for field_name in _CORE_BACKTEST_METRIC_FIELDS:
        if _metric_value_present(materialized.get(field_name)):
            merged[field_name] = materialized.get(field_name)
            metric_source_audit[field_name] = (
                dict(materialized.get("metric_source_audit") or {}).get(field_name)
                or "materialized_backtest_metrics"
            )
        elif _metric_value_present(gate_payload.get(field_name)):
            metric_source_audit.setdefault(field_name, "gate_payload")
    merged["backtest_metrics_contract"] = dict(materialized.get("backtest_metrics_contract") or {})
    merged["backtest_metrics_contract_status"] = materialized.get("backtest_metrics_contract_status")
    merged["metric_source_audit"] = metric_source_audit
    return merged


def _resolve_submission_generator_mode(
    strategy: dict,
    *,
    research_task: Optional[dict[str, Any]] = None,
    contract_snapshot: Optional[dict[str, Any]] = None,
) -> str | None:
    normalized_task = _normalize_research_task_contract(
        dict(
            research_task
            or _strategy_payload_value(strategy, "research_task")
            or strategy.get("research_task")
            or {}
        )
    )
    candidate_provenance = dict(
        _strategy_payload_value(strategy, "candidate_provenance")
        or strategy.get("candidate_provenance")
        or {}
    )
    snapshot = dict(
        contract_snapshot
        or _strategy_payload_value(strategy, "candidate_contract_snapshot")
        or strategy.get("candidate_contract_snapshot")
        or {}
    )
    strategy_profile = dict(snapshot.get("strategy_profile") or {})
    if not strategy_profile:
        try:
            strategy_profile = infer_candidate_strategy_profile(strategy, research_task=normalized_task)
        except Exception:
            strategy_profile = {}
    explicit_generator_mode = str(
        _strategy_payload_value(strategy, "generator_mode")
        or strategy.get("generator_mode")
        or candidate_provenance.get("generator_mode")
        or normalized_task.get("generator_mode")
        or normalized_task.get("generator_type")
        or ""
    ).strip().lower()
    had_explicit_research_task = bool(
        _strategy_payload_value(strategy, "had_explicit_research_task", strategy.get("research_task"))
    )
    generator_mode = explicit_generator_mode or str(strategy_profile.get("generator_mode") or "").strip().lower() or None
    if generator_mode == "snapshot" and not explicit_generator_mode and not had_explicit_research_task:
        generator_mode = "rule"
    return generator_mode


def _strategy_payload_value(strategy: dict, key: str, default: Any = None) -> Any:
    if key in strategy and strategy.get(key) is not None:
        return strategy.get(key)
    params = dict(strategy.get("params") or {})
    if key in params and params.get(key) is not None:
        return params.get(key)
    return default


def _resolve_semantic_runtime_context(strategy: dict, gate: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    target_codes = _extract_target_codes_from_payload(payload)
    single_name_trend = strategy_type in _TREND_EXECUTABLE_DSL_TYPES and len(target_codes) == 1
    evidence_chain = dict(_strategy_payload_value(payload, "evidence_chain") or {})
    prediction_contract = dict(_strategy_payload_value(payload, "prediction_contract") or {})
    confidence_contract = dict(_strategy_payload_value(payload, "confidence_contract") or {})
    trade_prediction_contract_status = str(
        _strategy_payload_value(payload, "trade_prediction_contract_status") or ""
    ).strip().lower()
    trade_prediction_contract_missing_fields = _normalize_string_list(
        _strategy_payload_value(payload, "trade_prediction_contract_missing_fields", []),
        limit=16,
    )
    trade_prediction_contract_reject_reasons = _normalize_string_list(
        _strategy_payload_value(payload, "trade_prediction_contract_reject_reasons", []),
        limit=32,
    )
    trade_prediction_contract = dict(_strategy_payload_value(payload, "trade_prediction_contract", {}) or {})
    trade_prediction_contract_hash = str(
        _strategy_payload_value(payload, "trade_prediction_contract_hash") or ""
    ).strip()
    trade_prediction_contract_observation_gap = _is_trade_prediction_contract_observation_gap(
        status=trade_prediction_contract_status or "missing",
        reject_reasons=trade_prediction_contract_reject_reasons,
        contract=trade_prediction_contract,
    )
    semantic_contract_missing_fields: list[str] = []
    if strategy_type in (_TREND_EXECUTABLE_DSL_TYPES | _PROXY_RUNTIME_FACTOR_TYPES):
        if not evidence_chain:
            semantic_contract_missing_fields.append("evidence_chain")
        if not prediction_contract:
            semantic_contract_missing_fields.append("prediction_contract")
        if not confidence_contract:
            semantic_contract_missing_fields.append("confidence_contract")
    instrument_profile = dict(_strategy_payload_value(payload, "instrument_profile") or {})
    measurement_source = str(
        instrument_profile.get("measurement_source") or "default_board_profile"
    ).strip().lower() or "default_board_profile"
    measured_profile_complete = bool(instrument_profile.get("measured_profile_complete"))
    runtime_family_data_source = str(
        _strategy_payload_value(payload, "runtime_family_data_source")
        or ("price_proxy_runtime" if strategy_type in _PROXY_RUNTIME_FACTOR_TYPES else "market_data_runtime")
    ).strip().lower() or None
    proxy_runtime_used = bool(
        _strategy_payload_value(payload, "proxy_runtime_used")
    ) or (
        strategy_type in _PROXY_RUNTIME_FACTOR_TYPES
        and runtime_family_data_source != "fundamental_runtime"
    )
    semantic_runtime_match = (
        bool(_strategy_payload_value(payload, "semantic_runtime_match"))
        if _strategy_payload_value(payload, "semantic_runtime_match") is not None
        else not proxy_runtime_used
    )
    default_profile_not_allowed = single_name_trend and (
        measurement_source == "default_board_profile" or not measured_profile_complete
    )
    execution_readiness_tier = str(
        _strategy_payload_value(payload, "execution_readiness_tier")
        or (
            "missing_executable_contract"
            if single_name_trend and str(_strategy_payload_value(payload, "execution_semantic_mode") or "").strip().lower() != "compiled_dsl"
            else "observe_diagnostic_only"
            if proxy_runtime_used or default_profile_not_allowed or semantic_contract_missing_fields
            else "formal_runtime_ready"
        )
    ).strip().lower() or None
    diagnostic_only = bool(
        _strategy_payload_value(payload, "diagnostic_only")
    ) or bool(
        proxy_runtime_used
        or default_profile_not_allowed
        or semantic_contract_missing_fields
        or execution_readiness_tier == "observe_diagnostic_only"
    )
    hard_fail_reasons: list[str] = []
    if semantic_contract_missing_fields:
        hard_fail_reasons.append("final_strategy_missing_semantic_contract")
    if proxy_runtime_used:
        hard_fail_reasons.extend(
            [
                "runtime_family_semantic_mismatch",
                "proxy_runtime_not_allowed_for_formal_incubation",
            ]
        )
    if default_profile_not_allowed:
        hard_fail_reasons.append("default_profile_not_allowed_for_single_name_runtime")
    if not semantic_runtime_match and "runtime_family_semantic_mismatch" not in hard_fail_reasons:
        hard_fail_reasons.append("runtime_family_semantic_mismatch")
    if bool(dict(gate or {}).get("execution_semantic_gap")):
        hard_fail_reasons.append("execution_semantic_gap")
    if trade_prediction_contract_status != "ready" and not trade_prediction_contract_observation_gap:
        hard_fail_reasons.append("trade_prediction_contract_not_ready")
    return {
        "semantic_contract_missing_fields": semantic_contract_missing_fields,
        "semantic_runtime_match": semantic_runtime_match,
        "runtime_family_data_source": runtime_family_data_source,
        "proxy_runtime_used": proxy_runtime_used,
        "diagnostic_only": diagnostic_only,
        "execution_readiness_tier": execution_readiness_tier,
        "measurement_source": measurement_source,
        "measured_profile_complete": measured_profile_complete,
        "default_profile_not_allowed": default_profile_not_allowed,
        "trade_prediction_contract_status": trade_prediction_contract_status or "missing",
        "trade_prediction_contract_missing_fields": trade_prediction_contract_missing_fields,
        "trade_prediction_contract_reject_reasons": trade_prediction_contract_reject_reasons,
        "trade_prediction_contract_observation_gap": trade_prediction_contract_observation_gap,
        "trade_prediction_contract_hash": trade_prediction_contract_hash or None,
        "hard_fail_reasons": list(dict.fromkeys(hard_fail_reasons)),
    }


def _should_route_single_target_bulk_factor_to_trade_profile(
    strategy: dict,
    *,
    research_task: Optional[dict[str, Any]] = None,
    validation_focus: Optional[str] = None,
) -> bool:
    strategy_type = str(strategy.get("strategy_type") or "").strip().lower()
    if strategy_type not in _FACTOR_VALIDATION_TYPES:
        return False
    raw_research_task = (
        research_task
        or _strategy_payload_value(strategy, "research_task")
        or strategy.get("research_task")
        or {}
    )
    if not raw_research_task:
        return False
    normalized_task = _normalize_research_task_contract(
        raw_research_task
    )
    resolved_validation_focus = str(
        validation_focus if validation_focus is not None else normalized_task.get("validation_focus") or ""
    ).strip().lower()
    target_codes = _extract_target_codes_from_payload(strategy)
    return (
        str(normalized_task.get("task_source") or "").strip().lower() == "bulk_stock_matrix"
        and resolved_validation_focus == "candidate_target_only"
        and len(target_codes) == 1
    )


def _resolve_validation_profile(strategy: dict) -> dict[str, Any]:
    research_task = _normalize_research_task_contract(
        _strategy_payload_value(strategy, "research_task") or strategy.get("research_task") or {}
    )
    raw_validation_profile = dict(
        _strategy_payload_value(strategy, "validation_profile")
        or strategy.get("validation_profile")
        or {}
    )
    research_protocol_adapter = dict(
        _strategy_payload_value(strategy, "research_validation_contract_submission_adapter")
        or strategy.get("research_validation_contract_submission_adapter")
        or {}
    )
    adapter_profile = dict(research_protocol_adapter.get("validation_profile") or {})
    resolved_profile = resolve_candidate_validation_profile(strategy, research_task=research_task)
    profile_name = str(resolved_profile.get("profile") or adapter_profile.get("profile") or "").strip().lower()
    validation_focus = str(
        resolved_profile.get("validation_focus") or adapter_profile.get("validation_focus") or ""
    ).strip().lower()
    primary_validation_layer = str(
        resolved_profile.get("primary_validation_layer") or adapter_profile.get("primary_validation_layer") or ""
    ).strip().lower() or "target"
    if profile_name == "factor_rank_validation" and _should_route_single_target_bulk_factor_to_trade_profile(
        strategy,
        research_task=research_task,
        validation_focus=validation_focus,
    ):
        profile_name = "trade_rule_validation"
        primary_validation_layer = "target"
    return {
        "profile": profile_name,
        "validation_focus": validation_focus,
        "primary_validation_layer": primary_validation_layer,
        "research_task": research_task,
        "objective_profile": str(
            raw_validation_profile.get("objective_profile")
            or adapter_profile.get("objective_profile")
            or research_task.get("objective_profile")
            or ""
        ).strip().lower() or None,
        "trade_density_preference": str(
            raw_validation_profile.get("trade_density_preference")
            or adapter_profile.get("trade_density_preference")
            or research_task.get("trade_density_preference")
            or ""
        ).strip().lower() or None,
        "entry_selectivity": str(
            raw_validation_profile.get("entry_selectivity")
            or adapter_profile.get("entry_selectivity")
            or ""
        ).strip().lower() or None,
        "regime_required": raw_validation_profile.get("regime_required")
        if raw_validation_profile.get("regime_required") is not None
        else adapter_profile.get("regime_required"),
        "cost_robust_required": raw_validation_profile.get("cost_robust_required")
        if raw_validation_profile.get("cost_robust_required") is not None
        else adapter_profile.get("cost_robust_required"),
    }


def _resolve_research_protocol_submission_adapter(strategy: dict) -> dict[str, Any]:
    adapter = dict(
        _strategy_payload_value(strategy, "research_validation_contract_submission_adapter")
        or strategy.get("research_validation_contract_submission_adapter")
        or {}
    )
    if adapter:
        return adapter
    contract = dict(
        _strategy_payload_value(strategy, "research_validation_contract")
        or strategy.get("research_validation_contract")
        or {}
    )
    if contract:
        return adapt_research_validation_contract_for_submission(contract)
    return {}


def _normalize_boolish(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    token = _normalize_text(value)
    if token in {"1", "true", "yes", "on", "required", "must"}:
        return True
    if token in {"0", "false", "no", "off", "optional"}:
        return False
    return bool(default)


def _normalize_string_list(*values: Any, limit: int = 8) -> list[str]:
    items: list[str] = []

    def visit(value: Any) -> None:
        if value in (None, "", [], {}):
            return
        if isinstance(value, dict):
            for key in ("source", "name", "value"):
                if value.get(key) not in (None, "", [], {}):
                    visit(value.get(key))
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)
            return
        token = str(value or "").strip()
        if not token:
            return
        if token not in items:
            items.append(token)

    for value in values:
        visit(value)
    return items[: max(1, int(limit or 8))]


def _strip_text(value: Any) -> str:
    return str(value or "").strip()


def _review_decision_rank(value: Any) -> int:
    token = _normalize_text(value)
    if token == "reject":
        return 3
    if token == "revise":
        return 2
    if token == "pending":
        return 1
    return 0


def _merge_review_decision(*values: Any) -> str:
    resolved = "pass"
    rank = -1
    for value in values:
        token = _normalize_text(value) or "pass"
        token_rank = _review_decision_rank(token)
        if token_rank > rank:
            resolved = token
            rank = token_rank
    return resolved

# --- runner_parts/semantic_context.py ---
def _resolve_high_precision_context(strategy: dict, profile: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    validation_profile = dict(
        _strategy_payload_value(payload, "validation_profile")
        or payload.get("validation_profile")
        or {}
    )
    research_task = dict(
        _strategy_payload_value(payload, "research_task")
        or payload.get("research_task")
        or {}
    )
    research_context = dict(
        _strategy_payload_value(payload, "research_context")
        or payload.get("research_context")
        or {}
    )
    event_context = dict(
        _strategy_payload_value(payload, "event_context")
        or payload.get("event_context")
        or {}
    )
    hypothesis_artifact = dict(
        _strategy_payload_value(payload, "hypothesis_artifact")
        or payload.get("hypothesis_artifact")
        or {}
    )
    generation_reason = dict(
        _strategy_payload_value(payload, "generation_reason")
        or payload.get("generation_reason")
        or {}
    )
    trigger_signal = dict(
        generation_reason.get("trigger_signal")
        or _strategy_payload_value(payload, "trigger_signal")
        or payload.get("trigger_signal")
        or {}
    )
    market_regime_assumption = dict(
        hypothesis_artifact.get("market_regime_assumption")
        or _strategy_payload_value(payload, "market_regime_assumption")
        or payload.get("market_regime_assumption")
        or research_context.get("market_regime_assumption")
        or {}
    )
    event_prefilter = dict(
        _strategy_payload_value(payload, "event_prefilter")
        or payload.get("event_prefilter")
        or hypothesis_artifact.get("event_prefilter")
        or validation_profile.get("event_prefilter")
        or research_context.get("event_prefilter")
        or research_task.get("event_prefilter")
        or {}
    )
    event_anchor = _normalize_event_anchor_payload(
        event_prefilter.get("event_anchor"),
        _strategy_payload_value(payload, "event_anchor"),
        payload.get("event_anchor"),
        hypothesis_artifact.get("event_anchor"),
        research_task.get("event_anchor"),
        research_context.get("event_anchor"),
        event_context.get("event_anchor"),
    )
    objective_profile = _normalize_text(
        (profile or {}).get("objective_profile")
        or validation_profile.get("objective_profile")
        or hypothesis_artifact.get("objective_profile")
        or research_task.get("objective_profile")
        or research_context.get("objective_profile")
    ) or None
    if not objective_profile and str(payload.get("generator_mode") or "").strip().lower() == "futures_calendar_research_adapter":
        objective_profile = "high_precision"
    trade_density_preference = _normalize_text(
        (profile or {}).get("trade_density_preference")
        or validation_profile.get("trade_density_preference")
        or hypothesis_artifact.get("trade_density_preference")
        or research_task.get("trade_density_preference")
    ) or ("low" if objective_profile == "high_precision" else None)
    entry_selectivity = _normalize_text(
        (profile or {}).get("entry_selectivity")
        or validation_profile.get("entry_selectivity")
        or hypothesis_artifact.get("entry_selectivity")
    ) or None
    strategy_type = _normalize_text(payload.get("strategy_type") or params.get("strategy_type"))
    event_prefilter_required = _normalize_boolish(
        event_prefilter.get("required")
        if event_prefilter.get("required") is not None
        else (profile or {}).get("event_prefilter_required")
        if (profile or {}).get("event_prefilter_required") is not None
        else validation_profile.get("event_prefilter_required"),
        default=objective_profile == "high_precision" and strategy_type == "event_structure_breakout",
    )
    event_prefilter_profile = _strip_text(
        event_prefilter.get("profile")
        or (profile or {}).get("event_prefilter_profile")
        or validation_profile.get("event_prefilter_profile")
    ) or ("announcement_flow_sector_v1" if event_prefilter_required else None)
    try:
        event_prefilter_min_confirmations = max(
            1,
            int(
                event_prefilter.get("min_confirmations")
                or (profile or {}).get("event_prefilter_min_confirmations")
                or validation_profile.get("event_prefilter_min_confirmations")
                or 1
            ),
        )
    except Exception:
        event_prefilter_min_confirmations = 1
    observed_sources = _normalize_string_list(
        event_prefilter.get("observed_sources"),
        event_prefilter.get("catalyst_sources"),
        event_prefilter.get("source_types"),
    )
    event_id = _strip_text(
        event_prefilter.get("event_id")
        or research_task.get("event_id")
        or event_context.get("event_id")
        or research_context.get("event_id")
    ) or None
    announcement_id = _strip_text(
        event_prefilter.get("announcement_id")
        or research_task.get("announcement_id")
        or event_context.get("announcement_id")
        or research_context.get("announcement_id")
    ) or None
    announcement_ids = _normalize_string_list(
        event_prefilter.get("announcement_ids"),
        research_task.get("announcement_ids"),
        event_context.get("announcement_ids"),
        research_context.get("announcement_ids"),
    )
    event_type = _normalize_text(
        event_prefilter.get("event_type")
        or research_task.get("event_type")
        or event_context.get("event_type")
        or research_context.get("event_type")
        or event_prefilter.get("catalyst_type")
        or research_task.get("catalyst_type")
        or event_context.get("catalyst_type")
        or research_context.get("catalyst_type")
    )
    theme_code = _strip_text(
        event_prefilter.get("theme_code")
        or research_task.get("theme_code")
        or event_context.get("theme_code")
        or research_context.get("theme_code")
    ) or None
    evidence_summary = _strip_text(
        event_prefilter.get("evidence_summary")
        or research_task.get("evidence_summary")
        or event_context.get("evidence_summary")
        or research_context.get("evidence_summary")
    ) or None
    focus_industries = _normalize_string_list(
        event_prefilter.get("focus_industries"),
        research_task.get("focus_industries"),
        research_task.get("hot_sectors"),
        research_context.get("focus_industries"),
        research_context.get("hot_sectors"),
    )
    generation_source = _normalize_text(generation_reason.get("source"))
    trigger_field = _normalize_text(trigger_signal.get("field"))
    has_announcement_anchor = bool(
        _normalize_text(event_anchor.get("source")) == "announcement"
        or event_id
        or announcement_id
        or announcement_ids
        or event_type in {"announcement", "earnings", "filing", "news"}
        or generation_source == "announcement"
    )
    if not event_anchor:
        if has_announcement_anchor:
            event_anchor = {
                "source": "announcement",
                "id": event_id or announcement_id or (announcement_ids[0] if announcement_ids else None) or event_type or "announcement",
                "type": event_type or "announcement",
                "strength": event_prefilter.get("anchor_strength"),
                "theme_code": theme_code,
                "focus_industries": focus_industries,
                "target_symbols": _normalize_symbol_list(
                    research_task.get("target_symbols"),
                    event_context.get("target_symbols"),
                    research_context.get("target_symbols"),
                    limit=12,
                ),
            }
        elif generation_source == "fund_flow" or trigger_field in {"north_fund_3d_net", "margin_5d_change_pct"}:
            event_anchor = {
                "source": "fund_flow",
                "id": trigger_field or generation_source or "fund_flow",
                "type": "fund_flow",
                "strength": trigger_signal.get("value"),
                "theme_code": theme_code,
                "focus_industries": focus_industries,
                "target_symbols": _normalize_symbol_list(
                    research_task.get("target_symbols"),
                    event_context.get("target_symbols"),
                    research_context.get("target_symbols"),
                    limit=12,
                ),
            }
    anchor_source = _normalize_text(event_anchor.get("source"))
    if has_announcement_anchor and "announcement" not in observed_sources:
        observed_sources.append("announcement")
    if anchor_source == "sector_catalyst" and "sector_catalyst" not in observed_sources:
        observed_sources.append("sector_catalyst")
    if anchor_source == "fund_flow" or generation_source == "fund_flow" or trigger_field in {"north_fund_3d_net", "margin_5d_change_pct"}:
        if "fund_flow" not in observed_sources:
            observed_sources.append("fund_flow")
    allowed_sources = _normalize_string_list(
        event_prefilter.get("allowed_sources"),
        ["announcement", "fund_flow", "sector_catalyst"] if event_prefilter_required else [],
    )
    matched_sources = [item for item in observed_sources if not allowed_sources or item in allowed_sources]
    confirmation_count = max(
        int(event_prefilter.get("confirmation_count") or 0),
        len(matched_sources),
    )
    event_prefilter_passed = (not event_prefilter_required) or (bool(event_anchor) and confirmation_count >= event_prefilter_min_confirmations)
    event_prefilter_summary = {
        "available": bool(event_prefilter_required or event_prefilter or observed_sources),
        "required": event_prefilter_required,
        "profile": event_prefilter_profile,
        "allowed_sources": allowed_sources,
        "observed_sources": matched_sources,
        "observed_confirmation_count": len(matched_sources),
        "confirmation_count": confirmation_count,
        "min_confirmations": event_prefilter_min_confirmations,
        "passed": event_prefilter_passed,
        "event_id": event_id,
        "announcement_id": announcement_id,
        "announcement_ids": announcement_ids,
        "theme_code": theme_code,
        "focus_industries": focus_industries,
        "evidence_summary": evidence_summary,
        "event_anchor": event_anchor,
        "anchor_strength": event_prefilter.get("anchor_strength")
        if event_prefilter.get("anchor_strength") is not None
        else event_anchor.get("strength"),
        "event_type": event_type or None,
        "trigger_field": trigger_field or None,
        "generation_source": generation_source or None,
        "status": (
            "passed"
            if event_prefilter_passed
            else ("missing" if not event_anchor else "insufficient_confirmations")
        ),
    }
    return {
        "objective_profile": objective_profile,
        "trade_density_preference": trade_density_preference,
        "entry_selectivity": entry_selectivity,
        "regime_required": _normalize_boolish(
            (profile or {}).get("regime_required")
            if (profile or {}).get("regime_required") is not None
            else validation_profile.get("regime_required")
            if validation_profile.get("regime_required") is not None
            else hypothesis_artifact.get("regime_required"),
            default=objective_profile == "high_precision",
        ),
        "cost_robust_required": _normalize_boolish(
            (profile or {}).get("cost_robust_required")
            if (profile or {}).get("cost_robust_required") is not None
            else validation_profile.get("cost_robust_required")
            if validation_profile.get("cost_robust_required") is not None
            else hypothesis_artifact.get("cost_robust_required"),
            default=objective_profile == "high_precision",
        ),
        "preferred_regime": _strip_text(
            market_regime_assumption.get("preferred_regime")
            or validation_profile.get("preferred_regime")
        ) or None,
        "avoid_regime": _strip_text(
            market_regime_assumption.get("avoid_regime")
            or validation_profile.get("avoid_regime")
        ) or None,
        "holding_rationale": _strip_text(
            hypothesis_artifact.get("holding_rationale")
            or _strategy_payload_value(payload, "holding_rationale")
            or payload.get("holding_rationale")
        ) or None,
        "failure_mode": hypothesis_artifact.get("failure_mode")
        or _strategy_payload_value(payload, "failure_mode")
        or payload.get("failure_mode"),
        "cost_sensitivity_grid": dict(
            hypothesis_artifact.get("cost_sensitivity_grid")
            or _strategy_payload_value(payload, "cost_sensitivity_grid")
            or payload.get("cost_sensitivity_grid")
            or {}
        ),
        "event_prefilter_summary": event_prefilter_summary,
    }


def _evaluate_high_precision_admission(
    strategy: dict,
    profile: dict[str, Any],
    gate_payload: Optional[dict[str, Any]],
    *,
    backtest_metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    context = _resolve_high_precision_context(strategy, profile)
    objective_profile = _normalize_text(context.get("objective_profile"))
    if objective_profile != "high_precision":
        return {
            "available": False,
            "decision": "pass",
            "blocking_reasons": [],
            "warnings": [],
            "precision_readiness": None,
            "regime_validation_summary": {},
            "cost_robustness_summary": {},
            "trade_density_summary": {},
            "objective_profile": None,
        }

    metrics = dict(gate_payload or {})
    if backtest_metrics:
        metrics = {**dict(backtest_metrics or {}), **metrics}
    blocking_reasons: list[str] = []
    warnings: list[str] = []
    preferred_regime = _strip_text(context.get("preferred_regime")) or None
    avoid_regime = _strip_text(context.get("avoid_regime")) or None
    holding_rationale = _strip_text(context.get("holding_rationale")) or None
    failure_mode = context.get("failure_mode")
    cost_sensitivity_grid = dict(context.get("cost_sensitivity_grid") or {})
    event_prefilter_summary = dict(context.get("event_prefilter_summary") or {})
    event_anchor_summary = dict(event_prefilter_summary.get("event_anchor") or {})
    trade_density_preference = _normalize_text(context.get("trade_density_preference")) or "low"
    entry_selectivity = _normalize_text(context.get("entry_selectivity")) or None
    regime_required = bool(context.get("regime_required"))
    cost_robust_required = bool(context.get("cost_robust_required"))
    trade_density = safe_metric_value(metrics, "trade_density")
    density_limit = 0.75 if trade_density_preference == "low" else 1.05
    density_passed = trade_density <= density_limit if trade_density > 0 else bool(entry_selectivity)

    if not holding_rationale:
        blocking_reasons.append("high_precision_missing_holding_rationale")
    if failure_mode in (None, "", [], {}):
        blocking_reasons.append("high_precision_missing_failure_mode")
    if regime_required:
        if not preferred_regime:
            blocking_reasons.append("high_precision_missing_preferred_regime")
        if not avoid_regime:
            blocking_reasons.append("high_precision_missing_avoid_regime")
    if not entry_selectivity:
        blocking_reasons.append("high_precision_missing_entry_selectivity")
    if trade_density > 0 and not density_passed:
        blocking_reasons.append("high_precision_trade_density_exceeds_preference")
    if cost_robust_required and not cost_sensitivity_grid:
        blocking_reasons.append("high_precision_missing_cost_sensitivity_grid")
    if event_prefilter_summary.get("required"):
        if not event_anchor_summary:
            blocking_reasons.append("high_precision_missing_event_prefilter_signal")
        elif not bool(event_prefilter_summary.get("passed")):
            blocking_reasons.append("high_precision_event_prefilter_not_confirmed")

    cost_scenarios = dict(metrics.get("cost_sensitivity_results") or metrics.get("cost_scenarios") or {})
    observed_bps: list[float] = []
    stress_post_cost_sharpe = None
    if cost_scenarios:
        for raw_key, raw_value in cost_scenarios.items():
            row = dict(raw_value or {})
            parsed_bps = safe_metric_value({"bps": raw_key}, "bps")
            if parsed_bps <= 0 and row.get("slippage_bps") is not None:
                parsed_bps = safe_metric_value(row, "slippage_bps")
            if parsed_bps > 0 and parsed_bps not in observed_bps:
                observed_bps.append(parsed_bps)
        if observed_bps:
            max_bps = max(observed_bps)
            stress_row = {}
            for raw_value in cost_scenarios.values():
                row = dict(raw_value or {})
                if safe_metric_value(row, "slippage_bps") == max_bps:
                    stress_row = row
                    break
            stress_post_cost_sharpe = safe_metric_value(stress_row, "post_cost_sharpe")
    if cost_robust_required and cost_sensitivity_grid and not observed_bps:
        warnings.append("high_precision_cost_observation_pending")
    if cost_robust_required and stress_post_cost_sharpe is not None and stress_post_cost_sharpe <= 0.0:
        blocking_reasons.append("high_precision_cost_fragility")

    post_cost_sharpe = safe_metric_value(metrics, "post_cost_sharpe")
    trade_count = safe_metric_value(metrics, "trade_count", "trades_count")
    regime_validation_summary = {
        "available": True,
        "objective_profile": objective_profile,
        "preferred_regime": preferred_regime,
        "avoid_regime": avoid_regime,
        "regime_required": regime_required,
        "passed": (not regime_required) or bool(preferred_regime and avoid_regime),
    }
    cost_robustness_summary = {
        "available": bool(cost_sensitivity_grid),
        "required": cost_robust_required,
        "observed_bps": sorted(observed_bps),
        "stress_post_cost_sharpe": round(float(stress_post_cost_sharpe), 4)
        if stress_post_cost_sharpe is not None
        else None,
        "passed": bool(cost_sensitivity_grid) and (stress_post_cost_sharpe is None or stress_post_cost_sharpe > 0.0),
    }
    trade_density_summary = {
        "available": True,
        "preference": trade_density_preference,
        "entry_selectivity": entry_selectivity,
        "max_trade_density": round(density_limit, 4),
        "observed_trade_density": round(trade_density, 4) if trade_density > 0 else None,
        "passed": bool(density_passed),
    }
    if blocking_reasons:
        decision = "reject" if "high_precision_trade_density_exceeds_preference" in blocking_reasons and trade_density > density_limit * 1.5 else "revise"
    else:
        decision = "pass"
    if decision != "pass":
        precision_readiness = decision
    elif post_cost_sharpe >= 0.8 and trade_count >= 6 and density_passed:
        precision_readiness = "strong"
    elif post_cost_sharpe >= 0.45 and trade_count >= 3:
        precision_readiness = "candidate"
    else:
        precision_readiness = "observe"
    return {
        "available": True,
        "decision": decision,
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "precision_readiness": precision_readiness,
        "regime_validation_summary": regime_validation_summary,
        "cost_robustness_summary": cost_robustness_summary,
        "trade_density_summary": trade_density_summary,
        "event_prefilter_summary": event_prefilter_summary,
        "event_anchor_summary": event_anchor_summary,
        "objective_profile": objective_profile,
    }


def _resolve_research_protocol_observed_payload(
    strategy: dict,
    *,
    backtest_metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    metrics = dict(backtest_metrics or {})
    params = dict(strategy.get("params") or {})
    candidate_provenance = dict(
        _strategy_payload_value(strategy, "candidate_provenance")
        or strategy.get("candidate_provenance")
        or {}
    )
    contract_snapshot = dict(
        _strategy_payload_value(strategy, "candidate_contract_snapshot")
        or strategy.get("candidate_contract_snapshot")
        or {}
    )
    strategy_profile = dict(contract_snapshot.get("strategy_profile") or {})
    cost_assumptions = dict(metrics.get("cost_assumptions") or {})
    implementation_shortfall = dict(metrics.get("implementation_shortfall_components") or {})
    cash_sleeve = dict(
        metrics.get("cash_sleeve")
        or _strategy_payload_value(strategy, "cash_sleeve")
        or _strategy_payload_value(strategy, "cash_sleeve_policy")
        or {}
    )
    return {
        "oos_cagr": (
            metrics.get("oos_cagr")
            if metrics.get("oos_cagr") is not None
            else metrics.get("target_layer_oos_return")
        ),
        "benchmark_oos_cagr": (
            metrics.get("benchmark_oos_cagr")
            if metrics.get("benchmark_oos_cagr") is not None
            else metrics.get("benchmark_target_layer_oos_return")
        ),
        "oos_max_drawdown": (
            metrics.get("oos_max_drawdown")
            if metrics.get("oos_max_drawdown") is not None
            else metrics.get("max_drawdown")
        ),
        "benchmark_oos_max_drawdown": (
            metrics.get("benchmark_oos_max_drawdown")
            if metrics.get("benchmark_oos_max_drawdown") is not None
            else metrics.get("benchmark_max_drawdown")
        ),
        "total_return": metrics.get("total_return"),
        "post_cost_sharpe": metrics.get("post_cost_sharpe"),
        "effective_total_bps": (
            implementation_shortfall.get("effective_total_bps")
            if implementation_shortfall.get("effective_total_bps") is not None
            else cost_assumptions.get("slippage_bps")
        ),
        "cost_sensitivity_results": dict(
            metrics.get("cost_sensitivity_results")
            or metrics.get("cost_scenarios")
            or metrics.get("cost_sensitivity_grid_results")
            or {}
        ),
        "cash_sleeve": cash_sleeve,
        "family": (
            candidate_provenance.get("candidate_family")
            or candidate_provenance.get("candidate_family_id")
            or strategy_profile.get("candidate_family")
            or strategy.get("strategy_type")
        ),
        "holding_bucket": (
            candidate_provenance.get("holding_period_bucket")
            or strategy_profile.get("holding_period_bucket")
            or _strategy_payload_value(strategy, "holding_period_bucket")
        ),
        "artifact_ids": _normalize_symbol_list(
            _strategy_payload_value(strategy, "artifact_ids"),
            candidate_provenance.get("artifact_ids"),
            params.get("artifact_ids"),
            limit=16,
        ),
        "retrieval_context_ids": _normalize_symbol_list(
            _strategy_payload_value(strategy, "retrieval_context_ids"),
            candidate_provenance.get("retrieval_context_ids"),
            params.get("retrieval_context_ids"),
            limit=16,
        ),
        "prediction_trace_id": (
            _strategy_payload_value(strategy, "prediction_trace_id")
            or _strategy_payload_value(strategy, "trace_id")
            or params.get("prediction_trace_id")
            or params.get("trace_id")
        ),
        "trace_id": (
            _strategy_payload_value(strategy, "trace_id")
            or _strategy_payload_value(strategy, "prediction_trace_id")
            or params.get("trace_id")
            or params.get("prediction_trace_id")
        ),
    }


def _build_attempt_adjustment(strategy: dict) -> dict[str, Any]:
    candidate_local_attempt_count = int(_strategy_payload_value(strategy, "candidate_local_attempt_count", 0) or 0)
    candidate_local_selected_count = int(_strategy_payload_value(strategy, "candidate_local_selected_count", 0) or 0)
    task_local_attempt_count = int(_strategy_payload_value(strategy, "task_local_attempt_count", 0) or 0)
    task_local_selected_count = int(_strategy_payload_value(strategy, "task_local_selected_count", 0) or 0)
    factory_global_attempt_count = int(_strategy_payload_value(strategy, "factory_global_attempt_count", 0) or 0)
    factory_global_selected_count = int(_strategy_payload_value(strategy, "factory_global_selected_count", 0) or 0)
    factory_attempt_count = int(_strategy_payload_value(strategy, "factory_attempt_count", 0) or 0)
    factory_selected_count = int(_strategy_payload_value(strategy, "factory_selected_count", 0) or 0)
    task_attempt_count = int(_strategy_payload_value(strategy, "task_attempt_count", 0) or 0)
    task_selected_count = int(_strategy_payload_value(strategy, "task_selected_count", 0) or 0)
    external_attempt_count = int(_strategy_payload_value(strategy, "external_llm_attempt_count", 0) or 0)
    external_selected_count = int(_strategy_payload_value(strategy, "external_llm_selected_count", 0) or 0)
    attempt_count = max(
        candidate_local_attempt_count,
        task_local_attempt_count,
        task_attempt_count,
        external_attempt_count,
        1,
    )
    selected_count = max(
        candidate_local_selected_count,
        task_local_selected_count,
        task_selected_count,
        external_selected_count,
        0,
    )
    selection_ratio = selected_count / max(attempt_count, 1)
    penalty = 0.0
    if attempt_count >= 10:
        penalty += 0.03
    if attempt_count >= 25:
        penalty += 0.05
    if attempt_count >= 50:
        penalty += 0.05
    if selected_count > 0 and selection_ratio < 0.2:
        penalty += 0.03
    adjustment = {
        "attempt_count": attempt_count,
        "selected_count": selected_count,
        "selection_ratio": round(selection_ratio, 4),
        "penalty": round(penalty, 4),
        "applied": penalty > 0,
        "candidate_local_attempt_count": candidate_local_attempt_count,
        "candidate_local_selected_count": candidate_local_selected_count,
        "task_local_attempt_count": task_local_attempt_count,
        "task_local_selected_count": task_local_selected_count,
        "factory_global_attempt_count": max(factory_global_attempt_count, factory_attempt_count, 0),
        "factory_global_selected_count": max(factory_global_selected_count, factory_selected_count, 0),
        "legacy_factory_attempt_count": factory_attempt_count,
        "legacy_task_attempt_count": task_attempt_count,
        "legacy_external_llm_attempt_count": external_attempt_count,
    }
    adjustment.update(_estimate_batch_correlation_adjustment(strategy, adjustment))
    return adjustment


def _estimate_batch_correlation_adjustment(
    strategy: dict,
    attempt_adjustment: dict[str, Any],
) -> dict[str, Any]:
    generator_mode = _resolve_submission_generator_mode(strategy)
    attempt_count = max(1, int(attempt_adjustment.get("attempt_count") or 1))
    selected_count = max(0, int(attempt_adjustment.get("selected_count") or 0))
    task_local_selected_count = max(
        0,
        int(
            attempt_adjustment.get("task_local_selected_count")
            or _strategy_payload_value(strategy, "task_local_selected_count", 0)
            or 0
        ),
    )
    candidate_local_attempt_count = max(
        0,
        int(
            attempt_adjustment.get("candidate_local_attempt_count")
            or _strategy_payload_value(strategy, "candidate_local_attempt_count", 0)
            or 0
        ),
    )
    sibling_count = max(task_local_selected_count, selected_count, 1)
    if generator_mode not in _LLM_CORRELATED_GENERATOR_MODES:
        return {
            "batch_correlation_mode": "independent_local_trials",
            "batch_correlation_multiplier": 1.0,
            "batch_correlation_sibling_count": sibling_count,
            "batch_correlation_generator_mode": generator_mode,
            "cohort_effective_trials": round(float(attempt_count), 4),
        }
    if sibling_count <= 1:
        return {
            "batch_correlation_mode": "llm_single_candidate_batch",
            "batch_correlation_multiplier": 1.0,
            "batch_correlation_sibling_count": sibling_count,
            "batch_correlation_generator_mode": generator_mode,
            "cohort_effective_trials": round(float(attempt_count), 4),
        }
    sibling_multiplier = math.sqrt(float(sibling_count))
    effective_multiplier = 1.0 + (
        (sibling_multiplier - 1.0)
        * (max(candidate_local_attempt_count, 1) / max(float(attempt_count), 1.0))
    )
    effective_trials = max(float(attempt_count), float(attempt_count) * effective_multiplier)
    return {
        "batch_correlation_mode": "llm_same_batch_sibling_proxy",
        "batch_correlation_multiplier": round(float(effective_multiplier), 4),
        "batch_correlation_sibling_count": sibling_count,
        "batch_correlation_generator_mode": generator_mode,
        "cohort_effective_trials": round(float(effective_trials), 4),
    }

# --- runner_parts/attempt_adjustment.py ---
def resolve_attempt_adjustment(
    strategy: dict,
    *,
    gate: Optional[dict[str, Any]] = None,
    attempt_adjustment: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if attempt_adjustment not in (None, {}, ""):
        return dict(
            normalize_quality_gate_result({"attempt_adjustment": attempt_adjustment}).get("attempt_adjustment") or {}
        )
    if gate:
        normalized_gate = normalize_quality_gate_result(gate)
        resolved = dict(normalized_gate.get("attempt_adjustment") or {})
        if resolved:
            return resolved
    return dict(
        normalize_quality_gate_result({"attempt_adjustment": _build_attempt_adjustment(strategy)}).get("attempt_adjustment")
        or {}
    )


def _build_multiple_testing_registry(
    strategy: dict,
    profile: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    normalized_gate = normalize_quality_gate_result(gate)
    research_task = dict(profile.get("research_task") or {})
    generation_reason = dict(
        _strategy_payload_value(strategy, "generation_reason")
        or strategy.get("generation_reason")
        or {}
    )
    target_codes = _extract_target_codes_from_payload(strategy)
    candidate_provenance = dict(
        _strategy_payload_value(strategy, "candidate_provenance")
        or strategy.get("candidate_provenance")
        or {}
    )
    dedup_result = dict(
        _strategy_payload_value(strategy, "dedup_result")
        or strategy.get("dedup_result")
        or {}
    )
    attempt_adjustment = resolve_attempt_adjustment(strategy, gate=normalized_gate)
    multiple_testing = dict(normalized_gate.get("multiple_testing") or {})
    contract_snapshot = dict(
        _strategy_payload_value(strategy, "candidate_contract_snapshot")
        or strategy.get("candidate_contract_snapshot")
        or {}
    )
    if not contract_snapshot:
        try:
            contract_snapshot = build_portfolio_candidate_contract(strategy)
        except Exception:
            contract_snapshot = {}
    targeting = dict(contract_snapshot.get("targeting") or {})
    lineage = dict(contract_snapshot.get("lineage") or {})
    task_signature = str(
        _strategy_payload_value(strategy, "task_signature")
        or research_task.get("task_signature")
        or lineage.get("task_signature")
        or _build_task_signature(research_task)
    ).strip()
    target_symbols_signature = ",".join(sorted(dict.fromkeys(target_codes)))
    strategy_family = str(
        candidate_provenance.get("candidate_family")
        or _strategy_payload_value(strategy, "candidate_family")
        or strategy.get("strategy_type")
        or "unknown"
    ).strip().lower()
    family_matrix_artifact_id = str(
        candidate_provenance.get("source_generation_artifact_id")
        or candidate_provenance.get("source_validation_artifact_id")
        or _strategy_payload_value(strategy, "source_generation_artifact_id")
        or _strategy_payload_value(strategy, "source_validation_artifact_id")
        or ""
    ).strip() or None
    strategy_type = str(strategy.get("strategy_type") or "").strip().lower() or "unknown"
    strategy_family_id = str(
        candidate_provenance.get("candidate_family_id")
        or _strategy_payload_value(strategy, "candidate_family_id")
        or strategy_family
        or ""
    ).strip() or None
    candidate_contract_hash = str(
        _strategy_payload_value(strategy, "candidate_contract_hash")
        or strategy.get("candidate_contract_hash")
        or ""
    ).strip()
    if not candidate_contract_hash:
        candidate_contract_hash = (
            build_candidate_contract_hash(contract=contract_snapshot)
            if contract_snapshot
            else build_candidate_contract_hash(strategy)
        )
    execution_contract_hash = str(
        _strategy_payload_value(strategy, "execution_contract_hash")
        or strategy.get("execution_contract_hash")
        or ""
    ).strip()
    if not execution_contract_hash:
        execution_contract_hash = (
            build_execution_contract_hash(contract=contract_snapshot)
            if contract_snapshot
            else build_execution_contract_hash(strategy)
        )
    candidate_identity_signature = str(
        _strategy_payload_value(strategy, "candidate_identity_signature")
        or strategy.get("candidate_identity_signature")
        or ""
    ).strip()
    if not candidate_identity_signature:
        candidate_identity_signature = build_candidate_identity_signature(strategy)
    tested_object_hash = str(
        _strategy_payload_value(strategy, "tested_object_hash")
        or strategy.get("tested_object_hash")
        or ""
    ).strip()
    if not tested_object_hash:
        tested_object_hash = build_tested_object_hash(strategy)
    logic_signature = str(
        _strategy_payload_value(strategy, "logic_signature")
        or strategy.get("logic_signature")
        or build_logic_signature(strategy)
        or ""
    ).strip() or None
    dsl_signature = str(
        _strategy_payload_value(strategy, "dsl_signature")
        or strategy.get("dsl_signature")
        or build_dsl_signature(strategy)
        or ""
    ).strip() or None
    factor_signature = str(
        _strategy_payload_value(strategy, "factor_signature")
        or strategy.get("factor_signature")
        or build_factor_signature(strategy)
        or ""
    ).strip() or None
    entry_exit_signature = str(
        _strategy_payload_value(strategy, "entry_exit_signature")
        or strategy.get("entry_exit_signature")
        or build_entry_exit_signature(strategy)
        or ""
    ).strip() or None
    lineage_id = str(
        lineage.get("lineage_id")
        or _strategy_payload_value(strategy, "lineage_id")
        or task_signature
        or ""
    ).strip() or None
    target_pool_id = str(
        targeting.get("target_pool_id")
        or _strategy_payload_value(strategy, "target_pool_id")
        or ""
    ).strip() or None
    template_generation_profile = str(
        research_task.get("template_generation_profile")
        or generation_reason.get("template_generation_profile")
        or dict(generation_reason.get("rule_template_contract") or {}).get("template_generation_profile")
        or candidate_provenance.get("template_generation_profile")
        or _strategy_payload_value(strategy, "template_generation_profile")
        or ""
    ).strip().lower() or None
    refresh_mode = str(
        dedup_result.get("refresh_mode")
        or _strategy_payload_value(strategy, "refresh_mode")
        or candidate_provenance.get("refresh_mode")
        or ""
    ).strip().lower() or None
    revision_mode = str(
        _strategy_payload_value(strategy, "revision_mode")
        or research_task.get("revision_mode")
        or candidate_provenance.get("revision_mode")
        or ("spawn_revision_from_existing" if refresh_mode == "spawn_revision_from_existing" else "baseline")
    ).strip().lower() or None
    formal_coverage = all(
        normalized_gate.get(field) is not None
        for field in (
            "deflated_sharpe_ratio",
            "pbo",
            "white_reality_check_pvalue",
            "hansen_spa_pvalue",
        )
    )
    strategy_profile = dict(contract_snapshot.get("strategy_profile") or {})
    if not strategy_profile:
        strategy_profile = infer_candidate_strategy_profile(strategy, research_task=research_task)
    holding_period_bucket = str(
        strategy_profile.get("holding_period_bucket")
        or _strategy_payload_value(strategy, "holding_period_bucket")
        or strategy.get("holding_period_bucket")
        or ""
    ).strip().lower() or None
    generator_mode = _resolve_submission_generator_mode(
        strategy,
        research_task=research_task,
        contract_snapshot=contract_snapshot,
    )
    validation_profile_name = str(
        dict(contract_snapshot.get("validation_profile") or {}).get("profile")
        or strategy_profile.get("validation_profile")
        or profile.get("profile")
        or ""
    ).strip().lower() or None
    validation_focus = str(profile.get("validation_focus") or "").strip().lower() or None
    primary_validation_layer = str(profile.get("primary_validation_layer") or "").strip().lower() or None

    def _axis_key(prefix: str, *parts: Any, fallback: str = "unknown") -> str:
        tokens = [str(part).strip() for part in parts if str(part or "").strip()]
        return f"{prefix}|{'|'.join(tokens)}" if tokens else f"{prefix}|{fallback}"

    task_key = _axis_key("task", task_signature)
    family_key = _axis_key("family", strategy_family_id or strategy_family, strategy_type)
    universe_key = _axis_key("universe", target_pool_id, target_symbols_signature)
    holding_key = _axis_key("holding", holding_period_bucket)
    generator_key = _axis_key("generator", generator_mode)
    validation_key = _axis_key("validation", validation_profile_name, validation_focus, primary_validation_layer)
    template_key = _axis_key(
        "template",
        template_generation_profile,
        str(profile.get("profile") or "").strip().lower(),
        strategy_type,
    )
    revision_key = _axis_key("revision", lineage_id, revision_mode, refresh_mode)
    tested_object_key = _axis_key("tested", tested_object_hash)
    registry_key = "|".join(
        (
            task_key,
            family_key,
            universe_key,
            holding_key,
            generator_key,
            validation_key,
            template_key,
            revision_key,
            tested_object_key,
        )
    )
    cohort_effective_trials = float(
        normalized_gate.get("deflated_sharpe_effective_trials")
        or normalized_gate.get("cohort_effective_trials")
        or attempt_adjustment.get("cohort_effective_trials")
        or dict(multiple_testing.get("deflated_sharpe") or {}).get("effective_trials")
        or attempt_adjustment.get("attempt_count")
        or 1.0
    )
    batch_correlation_mode = str(
        normalized_gate.get("batch_correlation_mode")
        or attempt_adjustment.get("batch_correlation_mode")
        or ""
    ).strip().lower() or None
    batch_correlation_multiplier = float(
        normalized_gate.get("batch_correlation_multiplier")
        or attempt_adjustment.get("batch_correlation_multiplier")
        or 1.0
    )
    batch_correlation_sibling_count = int(
        normalized_gate.get("batch_correlation_sibling_count")
        or attempt_adjustment.get("batch_correlation_sibling_count")
        or 0
    )

    return {
        "registry_key": registry_key,
        "task_signature": task_signature,
        "task_key": task_key,
        "strategy_family": strategy_family,
        "strategy_family_id": strategy_family_id,
        "family_key": family_key,
        "strategy_type": strategy_type,
        "target_symbols_signature": target_symbols_signature,
        "target_pool_id": target_pool_id,
        "universe_key": universe_key,
        "holding_period_bucket": holding_period_bucket,
        "holding_key": holding_key,
        "generator_mode": generator_mode,
        "generator_key": generator_key,
        "validation_profile": validation_profile_name,
        "validation_focus": validation_focus,
        "primary_validation_layer": primary_validation_layer,
        "validation_key": validation_key,
        "template_generation_profile": template_generation_profile,
        "template_key": template_key,
        "lineage_id": lineage_id,
        "revision_mode": revision_mode,
        "refresh_mode": refresh_mode,
        "revision_key": revision_key,
        "tested_object_key": tested_object_key,
        "candidate_contract_hash": candidate_contract_hash or None,
        "execution_contract_hash": execution_contract_hash or None,
        "tested_object_hash": tested_object_hash or None,
        "candidate_identity_signature": candidate_identity_signature or None,
        "logic_signature": logic_signature,
        "dsl_signature": dsl_signature,
        "factor_signature": factor_signature,
        "entry_exit_signature": entry_exit_signature,
        "attempt_count": int(attempt_adjustment.get("attempt_count") or 1),
        "selected_count": int(attempt_adjustment.get("selected_count") or 0),
        "selection_ratio": float(attempt_adjustment.get("selection_ratio") or 0.0),
        "candidate_local_attempt_count": int(
            attempt_adjustment.get("candidate_local_attempt_count")
            or _strategy_payload_value(strategy, "candidate_local_attempt_count", 0)
            or 0
        ),
        "task_local_attempt_count": int(
            attempt_adjustment.get("task_local_attempt_count")
            or _strategy_payload_value(strategy, "task_local_attempt_count", 0)
            or 0
        ),
        "factory_global_attempt_count": int(
            attempt_adjustment.get("factory_global_attempt_count")
            or _strategy_payload_value(strategy, "factory_global_attempt_count", 0)
            or 0
        ),
        "factory_attempt_count": int(_strategy_payload_value(strategy, "factory_attempt_count", 0) or 0),
        "task_attempt_count": int(_strategy_payload_value(strategy, "task_attempt_count", 0) or 0),
        "external_llm_attempt_count": int(_strategy_payload_value(strategy, "external_llm_attempt_count", 0) or 0),
        "family_matrix_artifact_id": family_matrix_artifact_id,
        "formal_coverage": formal_coverage,
        "formal_runtime_ready": formal_coverage and str(normalized_gate.get("multiple_testing_mode") or "").strip().lower() == "formal_runtime",
        "multiple_testing_mode": normalized_gate.get("multiple_testing_mode"),
        "multiple_testing_cohort_mode": normalized_gate.get("multiple_testing_cohort_mode"),
        "multiple_testing_panel_size": int(normalized_gate.get("multiple_testing_panel_size") or 0),
        "multiple_testing_panel_symbols": list(normalized_gate.get("multiple_testing_panel_symbols") or []),
        "cohort_effective_trials": round(cohort_effective_trials, 4),
        "batch_correlation_mode": batch_correlation_mode,
        "batch_correlation_multiplier": round(batch_correlation_multiplier, 4),
        "batch_correlation_sibling_count": batch_correlation_sibling_count,
        "registry_axes": {
            "task": task_key,
            "family": family_key,
            "universe": universe_key,
            "holding": holding_key,
            "generator": generator_key,
            "validation": validation_key,
            "template": template_key,
            "revision": revision_key,
            "tested_object": tested_object_key,
        },
        "multiple_testing": {
            "deflated_sharpe": dict(multiple_testing.get("deflated_sharpe") or {}),
            "pbo": dict(multiple_testing.get("pbo") or {}),
            "white_reality_check": dict(multiple_testing.get("white_reality_check") or {}),
            "hansen_spa": dict(multiple_testing.get("hansen_spa") or {}),
            "deflated_sharpe_ratio": normalized_gate.get("deflated_sharpe_ratio"),
            "pbo_value": normalized_gate.get("pbo"),
            "white_reality_check_pvalue": normalized_gate.get("white_reality_check_pvalue"),
            "hansen_spa_pvalue": normalized_gate.get("hansen_spa_pvalue"),
            "deflated_sharpe_proxy": normalized_gate.get("deflated_sharpe_proxy"),
            "pbo_proxy": normalized_gate.get("pbo_proxy"),
            "reality_check_pvalue_proxy": normalized_gate.get("reality_check_pvalue_proxy"),
            "spa_pvalue_proxy": normalized_gate.get("spa_pvalue_proxy"),
        },
    }


def _admission_threshold_bundle(admission_level: str) -> dict[str, Any]:
    normalized = str(admission_level or "incubation").strip().lower()
    return dict(_ADMISSION_THRESHOLD_SETS.get(normalized) or INCUBATION_ADMISSION_THRESHOLDS)


def _multiple_testing_thresholds(admission_level: str) -> dict[str, float]:
    base = dict(_admission_threshold_bundle(admission_level).get("multiple_testing") or {})
    return {
        "deflated_sharpe_ratio_min": float(base.get("deflated_sharpe_ratio_min", -0.10)),
        "pbo_max": float(base.get("pbo_max", 0.75)),
        "white_reality_check_pvalue_max": float(base.get("white_reality_check_pvalue_max", 0.35)),
        "hansen_spa_pvalue_max": float(base.get("hansen_spa_pvalue_max", 0.35)),
    }


def _review_gate_thresholds(admission_level: str) -> dict[str, float]:
    base = dict(_admission_threshold_bundle(admission_level).get("review") or {})
    return {
        "committee_final_score_min": float(base.get("committee_final_score_min", 0.0)),
        "promotion_review_score_min": float(base.get("promotion_review_score_min", 0.0)),
    }


def _statistical_gate_thresholds(
    attempt_adjustment: dict[str, Any],
    *,
    admission_level: str = "incubation",
) -> dict[str, float]:
    penalty = float(attempt_adjustment.get("penalty") or 0.0)
    base = dict(_admission_threshold_bundle(admission_level).get("statistical_validation") or QUALITY_GATE_THRESHOLDS)
    return {
        "walk_forward_ic_ir_min": float(base.get("walk_forward_ic_ir_min", 0.30)) + penalty,
        "purged_kfold_ic_min": float(base.get("purged_kfold_ic_min", 0.02)) + penalty / 2.0,
        "bootstrap_ci_lower_min": float(base.get("bootstrap_ci_lower_min", 0.0)) + penalty / 3.0,
        "param_sensitivity_max": float(base.get("param_sensitivity_max", 0.30)),
    }


def _first_float_value(payload: Optional[dict], *keys: str) -> Optional[float]:
    data = dict(payload or {})
    for key in keys:
        if key not in data or data.get(key) is None:
            continue
        try:
            return float(data.get(key) or 0.0)
        except Exception:
            continue
    return None


def _live_multiple_testing_reasons(payload: Optional[dict], thresholds: dict[str, float]) -> list[str]:
    normalized = dict(payload or {})
    reasons: list[str] = []
    multiple_testing_mode = str(normalized.get("multiple_testing_mode") or "").strip().lower()
    if multiple_testing_mode != "formal_runtime":
        reasons.append("formal_multiple_testing_mode_required_for_live_admission")

    deflated_sharpe = _first_float_value(normalized, "deflated_sharpe_ratio")
    pbo = _first_float_value(normalized, "pbo")
    white_rc = _first_float_value(normalized, "white_reality_check_pvalue")
    spa_pvalue = _first_float_value(normalized, "hansen_spa_pvalue")

    if deflated_sharpe is None:
        reasons.append("deflated_sharpe_missing_for_live_admission")
    elif deflated_sharpe < thresholds["deflated_sharpe_ratio_min"]:
        reasons.append(
            f"deflated_sharpe {deflated_sharpe:.3f} < {thresholds['deflated_sharpe_ratio_min']:.3f}"
        )
    if pbo is None:
        reasons.append("pbo_missing_for_live_admission")
    elif pbo > thresholds["pbo_max"]:
        reasons.append(f"pbo {pbo:.3f} > {thresholds['pbo_max']:.3f}")
    if white_rc is None:
        reasons.append("white_reality_check_missing_for_live_admission")
    elif white_rc > thresholds["white_reality_check_pvalue_max"]:
        reasons.append(
            "white_reality_check_pvalue "
            f"{white_rc:.3f} > {thresholds['white_reality_check_pvalue_max']:.3f}"
        )
    if spa_pvalue is None:
        reasons.append("hansen_spa_missing_for_live_admission")
    elif spa_pvalue > thresholds["hansen_spa_pvalue_max"]:
        reasons.append(
            f"hansen_spa_pvalue {spa_pvalue:.3f} > {thresholds['hansen_spa_pvalue_max']:.3f}"
        )
    return reasons


def _target_only_live_trade_family(
    strategy: dict,
    profile: dict[str, Any],
    payload: Optional[dict] = None,
) -> str | None:
    profile_name = _normalize_text(profile.get("profile"))
    validation_focus = _normalize_text(
        profile.get("validation_focus") or dict(payload or {}).get("validation_focus")
    )
    multiple_testing_cohort_mode = _normalize_text(dict(payload or {}).get("multiple_testing_cohort_mode"))
    if profile_name != "trade_rule_validation":
        return None
    if validation_focus not in _TARGET_ONLY_VALIDATION_FOCUSES:
        return None
    if multiple_testing_cohort_mode and multiple_testing_cohort_mode != "target_only":
        return None
    family = _normalize_text(
        _strategy_payload_value(strategy, "candidate_family")
        or _strategy_payload_value(strategy, "candidate_family_id")
        or strategy.get("strategy_type")
    )
    if family in _TRADE_AWARE_VALIDATION_GRADE_FAMILIES:
        return family
    return None


def _effective_live_multiple_testing_thresholds(
    strategy: dict,
    profile: dict[str, Any],
    payload: Optional[dict],
) -> dict[str, float]:
    thresholds = dict(_multiple_testing_thresholds("live"))
    family = _target_only_live_trade_family(strategy, profile, payload)
    if not family:
        return thresholds
    thresholds["deflated_sharpe_ratio_min"] = min(
        thresholds["deflated_sharpe_ratio_min"],
        0.0,
    )
    if family == "quality_factor":
        thresholds["pbo_max"] = max(thresholds["pbo_max"], 0.80)
        thresholds["white_reality_check_pvalue_max"] = max(
            thresholds["white_reality_check_pvalue_max"],
            0.20,
        )
        thresholds["hansen_spa_pvalue_max"] = max(
            thresholds["hansen_spa_pvalue_max"],
            0.20,
        )
    elif family == "ma_cross":
        thresholds["pbo_max"] = max(thresholds["pbo_max"], 0.75)
        thresholds["white_reality_check_pvalue_max"] = max(
            thresholds["white_reality_check_pvalue_max"],
            0.30,
        )
        thresholds["hansen_spa_pvalue_max"] = max(
            thresholds["hansen_spa_pvalue_max"],
            0.30,
        )
    elif family == "momentum":
        thresholds["pbo_max"] = max(thresholds["pbo_max"], 0.70)
        thresholds["white_reality_check_pvalue_max"] = max(
            thresholds["white_reality_check_pvalue_max"],
            0.25,
        )
        thresholds["hansen_spa_pvalue_max"] = max(
            thresholds["hansen_spa_pvalue_max"],
            0.25,
        )
    return thresholds


def _observed_sharpe_proxy(series: Optional[np.ndarray], fallback_score: float) -> float:
    arr = np.asarray(series if series is not None else [], dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size >= 3:
        std = float(np.std(arr, ddof=1))
        if std > 1e-9:
            return float(np.mean(arr) / std * np.sqrt(252.0))
    return float(fallback_score or 0.0)


def _strategy_return_series_for_params(
    klass,
    params: dict[str, Any],
    close_panels: list[np.ndarray],
    *,
    min_len: int,
) -> Optional[np.ndarray]:
    series_list: list[np.ndarray] = []
    for closes in close_panels:
        window = np.asarray(closes[:min_len], dtype=float)
        if window.size < min_len:
            continue
        instance = klass()
        instance.set_parameters(params or {})
        signals = np.asarray(instance.generate_signals(window), dtype=float)
        if signals.size < min_len:
            continue
        aligned = signals[:min_len]
        forward_returns = np.zeros(min_len, dtype=float)
        valid_prev = np.maximum(window[:-1], 1e-12)
        forward_returns[:-1] = (window[1:] - window[:-1]) / valid_prev
        series_list.append((aligned * forward_returns).astype(float))
    if not series_list:
        return None
    return np.mean(np.column_stack(series_list), axis=1).astype(float)


def _build_strategy_family_returns(
    klass,
    strategy_params: dict[str, Any],
    close_panels: list[np.ndarray],
    *,
    min_len: int,
) -> Optional[np.ndarray]:
    if min_len < 24 or not close_panels:
        return None
    family_series: list[np.ndarray] = []

    base_series = _strategy_return_series_for_params(klass, strategy_params, close_panels, min_len=min_len)
    if base_series is None:
        return None
    family_series.append(base_series)

    for key, value in sorted((strategy_params or {}).items()):
        if not isinstance(value, (int, float)) or value == 0:
            continue
        for mult in (0.8, 1.2):
            varied_params = dict(strategy_params or {})
            varied_value = float(value) * mult
            if isinstance(value, int):
                varied_params[key] = max(1, int(round(varied_value)))
            else:
                varied_params[key] = float(varied_value)
            varied_series = _strategy_return_series_for_params(klass, varied_params, close_panels, min_len=min_len)
            if varied_series is None:
                continue
            if any(np.allclose(varied_series, existing, atol=1e-9, rtol=1e-6) for existing in family_series):
                continue
            family_series.append(varied_series)

    if len(family_series) < 2:
        return np.column_stack(family_series)
    return np.column_stack(family_series)

# --- runner_parts/multiple_testing.py ---
def _estimate_run_correction_metrics(
    attempt_adjustment: dict[str, Any],
    *,
    observed_score: float,
    score_series: Optional[np.ndarray] = None,
    family_returns: Optional[np.ndarray] = None,
    validation_runtime: Any = None,
) -> dict[str, Any]:
    attempt_count = max(1, int(attempt_adjustment.get("attempt_count") or 1))
    cohort_effective_trials = max(
        1.0,
        float(attempt_adjustment.get("cohort_effective_trials") or attempt_count or 1.0),
    )
    selection_ratio = float(attempt_adjustment.get("selection_ratio") or 0.0)
    penalty = float(attempt_adjustment.get("penalty") or 0.0)
    observed_score = float(observed_score or 0.0)
    batch_correlation_mode = str(attempt_adjustment.get("batch_correlation_mode") or "").strip().lower() or None
    batch_correlation_multiplier = float(attempt_adjustment.get("batch_correlation_multiplier") or 1.0)
    batch_correlation_sibling_count = int(attempt_adjustment.get("batch_correlation_sibling_count") or 0)

    arr = np.asarray(score_series if score_series is not None else [], dtype=float)
    arr = arr[np.isfinite(arr)]
    raw_sharpe_proxy = _observed_sharpe_proxy(arr, observed_score)
    sample_size = int(arr.size)
    dsr_hurdle = (
        float(np.sqrt(max(0.0, 2.0 * np.log(max(cohort_effective_trials, 1.0)) / max(sample_size, 1))))
        if sample_size
        else penalty
    )
    deflated_sharpe_proxy = float(raw_sharpe_proxy - dsr_hurdle)

    # PBO 的正式实现需要 CSCV / family-level ranking；当前先给出 run-level proxy，
    # 明确把 selection_ratio 与 deflated score 合并为过拟合风险信号。
    logistic_term = 1.0 / (1.0 + np.exp(max(-8.0, min(8.0, deflated_sharpe_proxy * 2.0))))
    pbo_proxy = float(min(0.99, max(0.01, (1.0 - min(selection_ratio, 1.0)) * logistic_term + penalty)))

    reality_check_pvalue_proxy = float(min(0.99, max(0.01, 0.05 + penalty)))
    spa_pvalue_proxy = float(min(0.99, max(0.01, 0.05 + penalty * 0.8)))
    mode = "attempt_only_proxy"

    if sample_size >= 24:
        rng = np.random.default_rng(42)
        centered = arr - float(np.mean(arr))
        observed_mean = float(np.mean(arr))
        observed_std = float(np.std(arr, ddof=1))
        observed_t = observed_mean / (observed_std / np.sqrt(sample_size)) if observed_std > 1e-9 else 0.0
        effective_trial_count = max(1, int(math.ceil(cohort_effective_trials)))
        bootstrap_rounds = min(96, max(32, effective_trial_count * 4))
        candidate_family = min(16, max(2, effective_trial_count))
        rc_samples: list[float] = []
        spa_samples: list[float] = []
        for _ in range(bootstrap_rounds):
            max_mean = -np.inf
            max_t = -np.inf
            for _ in range(candidate_family):
                sample = centered[rng.integers(0, sample_size, size=sample_size)]
                sample_mean = float(np.mean(sample))
                sample_std = float(np.std(sample, ddof=1))
                sample_t = sample_mean / (sample_std / np.sqrt(sample_size)) if sample_std > 1e-9 else 0.0
                if sample_mean > max_mean:
                    max_mean = sample_mean
                if max(0.0, sample_t) > max_t:
                    max_t = max(0.0, sample_t)
            rc_samples.append(max_mean)
            spa_samples.append(max_t)
        rc_arr = np.asarray(rc_samples, dtype=float)
        spa_arr = np.asarray(spa_samples, dtype=float)
        reality_check_pvalue_proxy = float(np.mean(rc_arr >= observed_mean))
        spa_pvalue_proxy = float(np.mean(spa_arr >= max(0.0, observed_t)))
        mode = "bootstrap_family_proxy"

    warnings: list[str] = []
    formal_fields: dict[str, Any] = {}
    runtime_dsr = getattr(validation_runtime, "deflated_sharpe_ratio", None) if validation_runtime else None
    runtime_pbo = getattr(validation_runtime, "probability_of_backtest_overfitting", None) if validation_runtime else None
    runtime_rc = getattr(validation_runtime, "white_reality_check", None) if validation_runtime else None
    runtime_spa = getattr(validation_runtime, "hansen_spa_test", None) if validation_runtime else None

    family_arr = np.asarray(family_returns if family_returns is not None else [], dtype=float)
    if family_arr.ndim == 1 and family_arr.size:
        family_arr = family_arr.reshape(-1, 1)
    if family_arr.ndim != 2:
        family_arr = np.zeros((0, 0), dtype=float)

    if callable(runtime_dsr) and sample_size >= 3:
        try:
            trial_sharpes = None
            if family_arr.size and family_arr.shape[1] >= 1:
                trial_sharpes = np.asarray(
                    [_observed_sharpe_proxy(family_arr[:, j], 0.0) for j in range(family_arr.shape[1])],
                    dtype=float,
                )
            dsr_result = runtime_dsr(
                arr,
                observed_sharpe=raw_sharpe_proxy,
                n_trials=max(int(math.ceil(cohort_effective_trials)), int(family_arr.shape[1] or 1)),
                sharpe_trials=trial_sharpes,
                periods_per_year=252.0,
            )
            formal_fields.update(
                {
                    "multiple_testing_mode": "formal_runtime",
                    "deflated_sharpe_ratio": round(float(dsr_result.get("dsr", 0.0) or 0.0), 4),
                    "deflated_sharpe_reference_sharpe": round(float(dsr_result.get("reference_sharpe", 0.0) or 0.0), 4),
                    "deflated_sharpe_effective_trials": round(float(dsr_result.get("effective_trials", 0.0) or 0.0), 4),
                }
            )
            formal_fields.setdefault("multiple_testing", {})["deflated_sharpe"] = dict(dsr_result or {})
        except Exception as exc:
            warnings.append(f"run_correction:formal_dsr_failed:{type(exc).__name__}")

    if callable(runtime_pbo) and callable(runtime_rc) and callable(runtime_spa) and family_arr.shape[0] >= 12 and family_arr.shape[1] >= 2:
        try:
            pbo_result = runtime_pbo(family_arr, n_splits=8, metric="sharpe", periods_per_year=252.0, seed=42)
            bootstrap_trials = max(1, int(math.ceil(cohort_effective_trials)))
            rc_result = runtime_rc(
                family_arr,
                n_bootstrap=min(512, max(200, bootstrap_trials * 8)),
                stationary_bootstrap_p=0.1,
                seed=42,
            )
            spa_result = runtime_spa(
                family_arr,
                n_bootstrap=min(512, max(200, bootstrap_trials * 8)),
                stationary_bootstrap_p=0.1,
                seed=42,
                center="consistent",
            )
            formal_fields.update(
                {
                    "multiple_testing_mode": "formal_runtime",
                    "pbo": round(float(pbo_result.get("pbo", 0.0) or 0.0), 4),
                    "white_reality_check_pvalue": round(float(rc_result.get("p_value", 0.0) or 0.0), 4),
                    "hansen_spa_pvalue": round(float(spa_result.get("p_value", 0.0) or 0.0), 4),
                }
            )
            mt_bucket = formal_fields.setdefault("multiple_testing", {})
            mt_bucket["pbo"] = dict(pbo_result or {})
            mt_bucket["white_reality_check"] = dict(rc_result or {})
            mt_bucket["hansen_spa"] = dict(spa_result or {})
        except Exception as exc:
            warnings.append(f"run_correction:formal_family_tests_failed:{type(exc).__name__}")

    if deflated_sharpe_proxy < 0:
        warnings.append("run_correction:deflated_sharpe_proxy_negative")
    if pbo_proxy > 0.55:
        warnings.append("run_correction:pbo_proxy_high")
    if reality_check_pvalue_proxy > 0.2:
        warnings.append("run_correction:reality_check_pvalue_proxy_weak")
    if spa_pvalue_proxy > 0.2:
        warnings.append("run_correction:spa_pvalue_proxy_weak")

    return {
        "run_correction_mode": mode,
        "raw_sharpe_proxy": round(raw_sharpe_proxy, 4),
        "deflated_sharpe_proxy": round(deflated_sharpe_proxy, 4),
        "pbo_proxy": round(pbo_proxy, 4),
        "reality_check_pvalue_proxy": round(reality_check_pvalue_proxy, 4),
        "spa_pvalue_proxy": round(spa_pvalue_proxy, 4),
        "cohort_effective_trials": round(cohort_effective_trials, 4),
        "batch_correlation_mode": batch_correlation_mode,
        "batch_correlation_multiplier": round(batch_correlation_multiplier, 4),
        "batch_correlation_sibling_count": batch_correlation_sibling_count,
        **formal_fields,
        "warnings": warnings,
    }


# === DEV-V1 V5-PR-1: _estimate_run_correction_metrics 接入辅助 ===
# 把 dead code _estimate_run_correction_metrics 接入 submission_gate 评分流。
# 提取必要参数 (attempt_adjustment / observed_score / score_series / family_returns
# / validation_runtime),软降级处理任何缺失。
# 关联:策略工厂到孵化工厂过渡-开发方案-2026-05-26.md V5 P3 工作流深度调研
async def _build_family_returns_from_klines(
    db,
    strategy: dict,
    klass,
    *,
    min_len: int = 60,
    max_symbols: int = 6,
) -> Optional[np.ndarray]:
    """用真实 K 线 + 真实 signal 生成器合成同族收益矩阵 (打通 PBO/RC/SPA)。

    对策略参数做 ±20% 扰动生成同族变体(_build_strategy_family_returns),每个变体在真实
    K 线上跑真实 signal × forward_return 得到逐期收益序列,列拼成 (n_obs, n_models) 矩阵。
    这是 CSCV/White RC/Hansen SPA 检测参数过拟合的合法比较族,非伪造。

    取不到足够真实 K 线 (min_len<24 或无 target_symbols) 时返回 None,
    让下游 PBO/RC/SPA 诚实地维持 missing,绝不用噪声兜底。
    """
    try:
        if klass is None or db is None:
            return None
        get_klines = getattr(db, "get_klines", None)
        if not callable(get_klines):
            return None

        params = dict(strategy.get("params") or {})
        raw_symbols = (
            params.get("target_symbols")
            or strategy.get("target_symbols")
            or []
        )
        symbols = [str(s).strip() for s in raw_symbols if str(s or "").strip()][:max_symbols]
        if not symbols:
            return None

        close_panels: list[np.ndarray] = []
        for code in symbols:
            try:
                rows = await get_klines(code, limit=max(min_len * 2, 120))
            except Exception:
                continue
            closes = np.asarray(
                [float(r.get("close") or 0.0) for r in (rows or []) if r.get("close") is not None],
                dtype=float,
            )
            closes = closes[np.isfinite(closes) & (closes > 0)]
            if closes.size >= 24:
                close_panels.append(closes)

        if not close_panels:
            return None

        effective_len = int(min(min_len, min(int(c.size) for c in close_panels)))
        if effective_len < 24:
            return None

        family = _build_strategy_family_returns(
            klass,
            params,
            close_panels,
            min_len=effective_len,
        )
        if family is None:
            return None
        family = np.asarray(family, dtype=float)
        if family.ndim != 2 or family.shape[0] < 12 or family.shape[1] < 2:
            return None
        return family
    except Exception:
        return None


def _inject_run_correction_metrics(
    strategy: dict,
    profile: dict[str, Any],
    normalized: dict[str, Any],
    *,
    validation_report: Optional[dict] = None,
    backtest_metrics: Optional[dict] = None,
    family_returns_fallback: Any = None,
) -> dict[str, Any]:
    """V5-PR-1: 在 submission_gate 主流程末尾计算多重检验调整指标 (DSR/PBO/RC/SPA)。

    所有提取/计算失败软降级,返回空 dict 不打断主流程。
    成功时返回包含 multiple_testing_mode / deflated_sharpe_ratio / pbo /
    white_reality_check_pvalue / hansen_spa_pvalue 等字段的 dict,可直接合并到 normalized。

    Args:
        strategy:候选策略 dict
        profile:validation profile dict
        normalized:已经计算好的 quality_gate dict (含 attempt_adjustment / post_cost_sharpe / wf_ic_ir)
        validation_report:可选,用于提取 walk-forward 序列
        backtest_metrics:可选,用于提取 family_returns 矩阵
        family_returns_fallback:可选,调用方预先用真实 K 线 + 真实 signal 合成的同族收益矩阵
            (n_obs, n_models)。仅当 backtest_metrics 未直接携带 family_returns 时使用,
            用于打通 PBO/RC/SPA。必须是真实回测序列,不可为噪声。

    Returns:
        要合并到 normalized 的字段 dict;失败时返回空 dict 但带 warning。
    """
    try:
        attempt_adjustment = dict(normalized.get("attempt_adjustment") or {})
        if not attempt_adjustment:
            attempt_adjustment = resolve_attempt_adjustment(strategy, gate=normalized)

        # observed_score 优先级:trade primary 看 post_cost_sharpe,statistical 看 wf_ic_ir
        observed_score = (
            normalized.get("post_cost_sharpe")
            or normalized.get("wf_ic_ir")
            or normalized.get("sharpe_ratio")
            or 0.0
        )
        try:
            observed_score = float(observed_score or 0.0)
        except Exception:
            observed_score = 0.0

        # score_series:从 validation_report.walk_forward 抽 fold-level 收益序列
        # (用于 sample_size>=24 时的 bootstrap proxy)
        score_series = None
        try:
            wf = dict((validation_report or {}).get("walk_forward") or {})
            folds = list(wf.get("fold_results") or wf.get("folds") or [])
            if folds:
                fold_scores = [
                    float(f.get("oos_sharpe") or f.get("sharpe") or f.get("oos_score") or 0.0)
                    for f in folds
                    if isinstance(f, dict)
                ]
                if fold_scores:
                    import numpy as _np
                    score_series = _np.asarray(fold_scores, dtype=float)
        except Exception:
            score_series = None

        # family_returns:优先取 backtest_metrics 直接携带的矩阵;若没有,
        # 用调用方预先合成的 family_returns_fallback(真实 K 线 + 真实 signal 的参数扰动族),
        # 用于打通 PBO/RC/SPA。两条来源都是真实回测序列,缺失时保持 None(诚实 missing,不兜底造假)。
        family_returns = None
        try:
            mt_payload = dict((backtest_metrics or {}).get("multiple_testing") or {})
            fam_data = mt_payload.get("family_returns") or mt_payload.get("trial_returns")
            if fam_data is not None:
                import numpy as _np
                family_returns = _np.asarray(fam_data, dtype=float)
        except Exception:
            family_returns = None

        if family_returns is None and family_returns_fallback is not None:
            try:
                import numpy as _np
                candidate_matrix = _np.asarray(family_returns_fallback, dtype=float)
                if candidate_matrix.ndim == 2 and candidate_matrix.shape[0] >= 12 and candidate_matrix.shape[1] >= 2:
                    family_returns = candidate_matrix
            except Exception:
                family_returns = None

        # validation_runtime:从 mcp_services 拉真实运行时
        try:
            from strategy_factory.infrastructure.mcp_services import get_validation_runtime
            validation_runtime = get_validation_runtime()
        except Exception:
            validation_runtime = None

        if validation_runtime is None:
            return {"multiple_testing_inject_status": "validation_runtime_unavailable"}

        result = _estimate_run_correction_metrics(
            attempt_adjustment,
            observed_score=observed_score,
            score_series=score_series,
            family_returns=family_returns,
            validation_runtime=validation_runtime,
        )

        # 标记注入路径,便于监控
        result["multiple_testing_inject_status"] = "ok"
        return result
    except Exception as exc:
        # 完全软降级:任何异常都不打断主流程
        return {
            "multiple_testing_inject_status": "exception",
            "multiple_testing_inject_error": f"{type(exc).__name__}:{str(exc)[:120]}",
        }


def _trade_gate_thresholds(
    strategy: dict,
    profile: dict[str, Any],
    attempt_adjustment: dict[str, Any],
    *,
    admission_level: str = "incubation",
) -> dict[str, float]:
    penalty = float(attempt_adjustment.get("penalty") or 0.0)
    validation_focus = str(profile.get("validation_focus") or "target_plus_representative")
    is_event = str(profile.get("profile") or "") == "event_trade_validation" or validation_focus == "event_target_only"
    trade_profiles = dict(_admission_threshold_bundle(admission_level).get("trade_profiles") or TRADE_GATE_PROFILE_THRESHOLDS)
    base = dict(trade_profiles.get("event_trade_validation" if is_event else "default") or TRADE_GATE_PROFILE_THRESHOLDS["default"])
    if admission_level == "live":
        family = _target_only_live_trade_family(strategy, profile)
        if family == "quality_factor":
            base["trade_count_min"] = min(float(base.get("trade_count_min", 8.0)), 4.0)
        elif family in {"ma_cross", "momentum"}:
            base["trade_count_min"] = min(float(base.get("trade_count_min", 8.0)), 6.0)
    return {
        "post_cost_sharpe_min": float(base.get("post_cost_sharpe_min", 0.10)) + penalty,
        "trade_count_min": float(base.get("trade_count_min", 4.0)),
        "total_return_min": float(base.get("total_return_min", -0.02)),
        "target_layer_oos_return_min": float(base.get("target_layer_oos_return_min", -0.01)),
        "max_drawdown_max": float(base.get("max_drawdown_max", 0.45)),
        "event_window_hit_ratio_min": float(base.get("event_window_hit_ratio_min", 0.0)),
        "post_event_decay_min": float(base.get("post_event_decay_min", -1.0)),
        "trade_density_max": float(base.get("trade_density_max", 1.2)),
        "parameter_perturbation_trade_stability_min": float(
            base.get("parameter_perturbation_trade_stability_min", 0.25)
        ),
        # PR-S14: 命中率 / 盈亏比 / profit_factor / expectancy 门禁
        "profit_factor_min": float(base.get("profit_factor_min", 1.2)),
        "payoff_ratio_min": float(base.get("payoff_ratio_min", 0.8)),
        "win_rate_min": float(base.get("win_rate_min", 0.30)),
        "expectancy_min": float(base.get("expectancy_min", 0.0)),
    }


def _has_trade_validation_audit(backtest_metrics: Optional[dict]) -> bool:
    metrics = _materialize_backtest_metrics_contract(backtest_metrics)
    required_markers = {
        "post_cost_sharpe",
        "target_layer_oos_return",
        "target_layer_abnormal_return",
        "event_window_hit_ratio",
        "post_event_decay",
        "trade_density",
        "parameter_perturbation_trade_stability",
        "primary_validation_layer",
    }
    return any(key in metrics and metrics.get(key) is not None for key in required_markers)


def _trade_validation_audit_mode(
    *,
    incubation_budget_track: Optional[str] = None,
    submission_lane: Optional[str] = None,
) -> str:
    track = str(incubation_budget_track or "").strip().lower()
    lane = str(submission_lane or "").strip().lower()
    if lane == "live_ready_review" or track in {"formal_incubation", "live_ready_review"}:
        return "hard_fail"
    return "research_only_fallback"


def _can_soften_incubation_trade_metric(
    *,
    admission_level: str,
    post_cost_sharpe: float,
    trade_count: float,
    target_layer_abnormal_return: float,
    primary_validation_layer: str,
    is_event: bool,
) -> bool:
    if admission_level != "incubation":
        return False
    if trade_count < _INCUBATION_OBSERVE_TRADE_COUNT_FLOOR:
        return False
    if post_cost_sharpe < _INCUBATION_OBSERVE_POST_COST_SHARPE_FLOOR:
        return False
    if target_layer_abnormal_return > 0.0:
        return True
    if is_event:
        return False
    return primary_validation_layer in {"target", "combined"}


def _evaluate_trade_profile(
    strategy: dict,
    profile: dict[str, Any],
    backtest_metrics: Optional[dict],
    risk_report: Optional[dict],
    *,
    admission_level: str = "incubation",
    attempt_adjustment: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    metrics = dict(backtest_metrics or {})
    metric_source_audit = dict(metrics.get("metric_source_audit") or {})
    attempt_adjustment = resolve_attempt_adjustment(strategy, attempt_adjustment=attempt_adjustment)
    thresholds = _trade_gate_thresholds(
        strategy,
        profile,
        attempt_adjustment,
        admission_level=admission_level,
    )
    validation_focus = str(profile.get("validation_focus") or "target_plus_representative")
    is_event = str(profile.get("profile") or "") == "event_trade_validation" or validation_focus == "event_target_only"
    reasons: list[str] = []
    warnings: list[str] = []

    post_cost_sharpe = _optional_metric_value(metrics, "post_cost_sharpe", "sharpe_ratio")
    total_return = safe_metric_value(metrics, "total_return", "target_layer_oos_return")
    target_layer_oos_return = safe_metric_value(metrics, "target_layer_oos_return", "total_return")
    target_layer_abnormal_return = safe_metric_value(metrics, "target_layer_abnormal_return", "target_layer_oos_return", "total_return")
    trade_count = _optional_metric_value(metrics, "trade_count", "trades_count")
    max_drawdown_raw = _optional_metric_value(metrics, "max_drawdown")
    max_drawdown = abs(max_drawdown_raw) if max_drawdown_raw is not None else None
    avg_holding_days = safe_metric_value(metrics, "avg_holding_days")
    turnover_proxy = safe_metric_value(metrics, "turnover_proxy")
    if turnover_proxy <= 0 and (trade_count or 0) > 0:
        turnover_proxy = round(trade_count / max(avg_holding_days, 5.0), 4) if avg_holding_days > 0 else float(trade_count)
    event_window_hit_ratio = _optional_metric_value(metrics, "event_window_hit_ratio")
    post_event_decay = _optional_metric_value(metrics, "post_event_decay")
    trade_density = safe_metric_value(metrics, "trade_density")
    parameter_stability = _optional_metric_value(metrics, "parameter_perturbation_trade_stability")
    primary_validation_layer = str(metrics.get("primary_validation_layer") or "").strip().lower()
    event_study_mode = str(metrics.get("event_study_mode") or "").strip().lower()
    event_sample_count = int(safe_metric_value(metrics, "event_sample_count"))
    event_anchor_count = int(safe_metric_value(metrics, "event_anchor_count"))
    control_group_count = int(safe_metric_value(metrics, "control_group_count"))
    event_sample_source = metrics.get("event_sample_source")
    event_time_anchors = list(metrics.get("event_time_anchors") or [])
    traceable_to_event_samples = bool(metrics.get("traceable_to_event_samples"))
    event_audit_incomplete = bool(metrics.get("event_audit_incomplete"))
    observe_softening_allowed = _can_soften_incubation_trade_metric(
        admission_level=admission_level,
        post_cost_sharpe=post_cost_sharpe if post_cost_sharpe is not None else 0.0,
        trade_count=trade_count if trade_count is not None else 0.0,
        target_layer_abnormal_return=target_layer_abnormal_return,
        primary_validation_layer=primary_validation_layer,
        is_event=is_event,
    )

    if post_cost_sharpe is None:
        reasons.append("missing_post_cost_sharpe")
    elif post_cost_sharpe < thresholds["post_cost_sharpe_min"]:
        reasons.append(f"post_cost_sharpe {post_cost_sharpe:.3f} < {thresholds['post_cost_sharpe_min']:.3f}")
    if trade_count is None:
        reasons.append("missing_trade_count")
    elif trade_count < thresholds["trade_count_min"]:
        reasons.append(f"trade_count {trade_count:.0f} < {thresholds['trade_count_min']:.0f}")
    if total_return < thresholds["total_return_min"]:
        warnings.append(f"total_return {total_return:.3f} < {thresholds['total_return_min']:.3f}")
    if target_layer_oos_return < thresholds["target_layer_oos_return_min"]:
        target_layer_reason = (
            f"target_layer_oos_return {target_layer_oos_return:.3f} < {thresholds['target_layer_oos_return_min']:.3f}"
        )
        target_layer_shortfall = thresholds["target_layer_oos_return_min"] - target_layer_oos_return
        if (
            observe_softening_allowed
            and target_layer_shortfall <= _INCUBATION_OBSERVE_TARGET_LAYER_OOS_SOFT_BAND
        ):
            warnings.append(f"{target_layer_reason} [observe_band]")
        else:
            reasons.append(target_layer_reason)
    if max_drawdown is None:
        reasons.append("missing_max_drawdown")
    elif max_drawdown > thresholds["max_drawdown_max"]:
        drawdown_reason = f"max_drawdown {max_drawdown:.3f} > {thresholds['max_drawdown_max']:.3f}"
        drawdown_excess = max_drawdown - thresholds["max_drawdown_max"]
        if observe_softening_allowed and drawdown_excess <= _INCUBATION_OBSERVE_MDD_SOFT_BAND:
            warnings.append(f"{drawdown_reason} [observe_band]")
        else:
            reasons.append(drawdown_reason)
    if thresholds["event_window_hit_ratio_min"] > 0:
        if event_window_hit_ratio is None:
            if admission_level in {"incubation", "live"}:
                reasons.append("missing_event_window_hit_ratio")
            else:
                warnings.append("event_window_hit_ratio_missing")
        elif event_window_hit_ratio < thresholds["event_window_hit_ratio_min"]:
            reasons.append(
                f"event_window_hit_ratio {event_window_hit_ratio:.3f} < {thresholds['event_window_hit_ratio_min']:.3f}"
            )
    if post_event_decay is None:
        warnings.append("missing_post_event_decay")
    elif post_event_decay < thresholds["post_event_decay_min"]:
        warnings.append(
            f"post_event_decay {post_event_decay:.3f} < {thresholds['post_event_decay_min']:.3f}"
        )
    if trade_density > thresholds["trade_density_max"]:
        density_reason = f"trade_density {trade_density:.3f} > {thresholds['trade_density_max']:.3f}"
        if admission_level in {"incubation", "live"}:
            reasons.append(density_reason)
        else:
            warnings.append(density_reason)
    if parameter_stability is None:
        if admission_level in {"incubation", "live"}:
            reasons.append("missing_parameter_perturbation_trade_stability")
        else:
            warnings.append("missing_parameter_perturbation_trade_stability")
    elif parameter_stability < thresholds["parameter_perturbation_trade_stability_min"]:
        stability_reason = (
            "parameter_perturbation_trade_stability "
            f"{parameter_stability:.3f} < {thresholds['parameter_perturbation_trade_stability_min']:.3f}"
        )
        if admission_level in {"incubation", "live"}:
            reasons.append(stability_reason)
        else:
            warnings.append(stability_reason)
    if is_event:
        if event_sample_count <= 0:
            reasons.append("event_sample_count_missing")
        if event_audit_incomplete:
            reasons.append("event_audit_incomplete")
        if event_study_mode and event_study_mode != "sample_driven":
            reasons.append(f"event_study_mode_{event_study_mode}")
        if str(event_sample_source or "").strip().lower() == "auto_context_minimal":
            reasons.append("event_sample_source_auto_context_minimal")
        if event_sample_count > 0 and not traceable_to_event_samples:
            reasons.append("event_sample_traceability_missing")

    risk = dict(risk_report or {})
    stress_loss_percent = _optional_metric_value(risk, "stress_loss_percent")
    # Only require stress when a risk_report object was provided. Callers that
    # omit risk_report entirely keep the historical soft path (warning only).
    if risk_report is None:
        if stress_loss_percent is not None and stress_loss_percent <= -25.0:
            reasons.append(f"stress_loss_percent {stress_loss_percent:.2f} <= -25.00")
    elif stress_loss_percent is None:
        if admission_level in {"incubation", "live"}:
            reasons.append("missing_stress_loss_percent")
        else:
            warnings.append("missing_stress_loss_percent")
    elif stress_loss_percent <= -25.0:
        reasons.append(f"stress_loss_percent {stress_loss_percent:.2f} <= -25.00")

    # PR-S15: Walk-Forward 样本外指标门禁
    # Missing ≠ 0: if the key is present, always compare (including negative OOS).
    # If the key is absent, do not invent a pass.
    walk_forward_efficiency = _optional_metric_value(metrics, "walk_forward_efficiency")
    out_of_sample_sharpe = _optional_metric_value(metrics, "out_of_sample_sharpe")
    out_of_sample_profit_factor = _optional_metric_value(metrics, "out_of_sample_profit_factor")
    _oos_keys_present = any(
        key in metrics
        for key in (
            "walk_forward_efficiency",
            "out_of_sample_sharpe",
            "out_of_sample_profit_factor",
        )
    )
    if _oos_keys_present:
        if walk_forward_efficiency is None:
            reasons.append("missing_walk_forward_efficiency")
        elif walk_forward_efficiency < 0.5:
            reasons.append(
                f"walk_forward_efficiency {walk_forward_efficiency:.3f} < 0.500 (overfit risk)"
            )
        if out_of_sample_sharpe is None:
            reasons.append("missing_out_of_sample_sharpe")
        elif out_of_sample_sharpe < 0.30:
            reasons.append(
                f"out_of_sample_sharpe {out_of_sample_sharpe:.3f} < 0.300"
            )
        if out_of_sample_profit_factor is None:
            warnings.append("missing_out_of_sample_profit_factor")
        elif out_of_sample_profit_factor < 1.2:
            warnings.append(
                f"out_of_sample_profit_factor {out_of_sample_profit_factor:.3f} < 1.200"
            )

    # PR-A3: 命中率 / 利润因子 / 期望值门禁
    # missing → hard fail on incubation/live; explicit 0 participates in compare.
    win_rate = _optional_metric_value(metrics, "win_rate")
    profit_factor = _optional_metric_value(metrics, "profit_factor")
    expectancy = _optional_metric_value(metrics, "expectancy")
    breakeven_wr = _optional_metric_value(metrics, "breakeven_win_rate")
    strategy_type = str(strategy.get("strategy_type") or "").strip().lower()
    if strategy_type in {"momentum", "ma_cross", "volatility_breakout", "breakout"}:
        _min_wr, _min_pf = 0.40, 1.8
    elif strategy_type in {"rsi", "mean_reversion", "mean_reversion_short", "gap_fill"}:
        _min_wr, _min_pf = 0.55, 1.5
    elif strategy_type in {"multi_factor", "value_factor", "quality_factor", "growth_factor"}:
        _min_wr, _min_pf = 0.50, 1.5
    else:
        _min_wr, _min_pf = 0.45, 1.5
    if win_rate is None:
        if admission_level in {"incubation", "live"}:
            reasons.append("missing_win_rate")
        else:
            warnings.append("missing_win_rate")
    elif win_rate < _min_wr:
        reasons.append(f"win_rate {win_rate:.3f} < {_min_wr:.3f}")
    if profit_factor is None:
        if admission_level in {"incubation", "live"}:
            reasons.append("missing_profit_factor")
        else:
            warnings.append("missing_profit_factor")
    elif profit_factor < _min_pf:
        reasons.append(f"profit_factor {profit_factor:.3f} < {_min_pf:.3f}")
    if expectancy is not None and expectancy <= 0 and (trade_count or 0) >= 10:
        reasons.append(f"expectancy {expectancy:.4f} <= 0 (negative expected value)")
    if (
        breakeven_wr is not None
        and win_rate is not None
        and (win_rate - breakeven_wr) < 0.05
    ):
        warnings.append(
            f"win_rate_margin_over_breakeven {win_rate - breakeven_wr:.3f} < 0.05 (thin safety margin)"
        )

    if admission_level == "live":
        mt_thresholds = _effective_live_multiple_testing_thresholds(strategy, profile, metrics)
        reasons.extend(_live_multiple_testing_reasons(metrics, mt_thresholds))

    return normalize_quality_gate_result(
        {
            "passed": len(reasons) == 0,
            "passed_strict": len(reasons) == 0,
            "profile": profile.get("profile"),
            "validation_focus": profile.get("validation_focus"),
            "primary_validation_layer": profile.get("primary_validation_layer"),
            "attempt_adjustment": attempt_adjustment,
            "thresholds": thresholds,
            "admission_level": admission_level,
            "reasons": reasons,
            "warnings": warnings,
            "trade_count": None if trade_count is None else round(trade_count, 4),
            "avg_holding_days": round(avg_holding_days, 4),
            "turnover_proxy": round(turnover_proxy, 4),
            "post_cost_sharpe": None if post_cost_sharpe is None else round(post_cost_sharpe, 4),
            "target_layer_oos_return": round(target_layer_oos_return, 4),
            "target_layer_abnormal_return": round(target_layer_abnormal_return, 4),
            "event_window_hit_ratio": None if event_window_hit_ratio is None else round(event_window_hit_ratio, 4),
            "post_event_decay": None if post_event_decay is None else round(post_event_decay, 4),
            "trade_density": round(trade_density, 4),
            "parameter_perturbation_trade_stability": (
                None if parameter_stability is None else round(parameter_stability, 4)
            ),
            "event_study_mode": event_study_mode or None,
            "event_sample_count": int(event_sample_count),
            "event_anchor_count": int(event_anchor_count),
            "control_group_count": int(control_group_count),
            "event_sample_source": event_sample_source,
            "event_time_anchors": event_time_anchors[:8],
            "traceable_to_event_samples": bool(traceable_to_event_samples),
            "event_audit_incomplete": bool(event_audit_incomplete),
            "profit_factor": None if profit_factor is None else round(profit_factor, 4),
            "expectancy": round(expectancy, 4) if expectancy is not None else None,
            "expectancy_return": round(safe_metric_value(metrics, "expectancy_return"), 6),
            "win_rate": None if win_rate is None else round(win_rate, 4),
            "metric_source_audit": metric_source_audit,
            "max_drawdown": None if max_drawdown is None else round(max_drawdown, 4),
        }
    )


def _evaluate_trade_profile_for_admission(
    strategy: dict,
    profile: dict[str, Any],
    gate_payload: Optional[dict],
    risk_report: Optional[dict],
    *,
    admission_level: str,
    attempt_adjustment: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    try:
        return _evaluate_trade_profile(
            strategy,
            profile,
            gate_payload,
            risk_report,
            admission_level=admission_level,
            attempt_adjustment=attempt_adjustment,
        )
    except TypeError as exc:
        message = str(exc)
        if "unexpected keyword argument" not in message:
            raise
        return _evaluate_trade_profile(strategy, profile, gate_payload, risk_report)


def _optional_metric_value(payload: Optional[dict], *keys: str) -> float | None:
    data = dict(payload or {})
    for key in keys:
        if key not in data or data.get(key) in (None, "", [], {}):
            continue
        try:
            value = float(data.get(key))
        except Exception:
            return None
        if not np.isfinite(value):
            return None
        return value
    return None


def _classify_gate3_metric_value(value: float | None) -> str:
    """Classify a Gate-3 statistical metric reading.

    Returns one of:
        - ``missing``: value is None / NaN / inf, or upstream returned a
          structural 0.0 placeholder (the P1-prep audit observed that
          ``factor_validation_bootstrap`` writes 0.0 when it can't run a
          real fold/bootstrap, so 0.0 is *not* "weak signal" — it's
          "no signal at all").
        - ``present_real``: a finite non-zero value the gate can compare
          against its threshold.

    Treating placeholder 0.0 as ``missing`` rather than ``weak`` is the
    explicit fix from R5.1 informed by the P1-prep audit; otherwise Gate-3
    would penalize candidates for an upstream bootstrap pipeline failure.
    """
    if value is None:
        return "missing"
    try:
        f = float(value)
    except Exception:
        return "missing"
    if f != f or not np.isfinite(f):  # NaN / inf
        return "missing"
    if abs(f) < 1e-12:
        return "missing"
    return "present_real"


def _append_missing_statistical_metric(
    missing_metrics: list[str],
    payload: dict[str, Any],
    result_key: str,
    *keys: str,
) -> float | None:
    """Read a metric from payload and, when classified as ``missing``,
    record it. Returns the underlying numeric value if any, else None.

    Note: callers downstream still receive the raw value (could be 0.0)
    so they can compute deltas if needed. The ``missing`` decision lives
    purely in ``missing_metrics``.
    """
    value = _optional_metric_value(payload, *keys)
    status = _classify_gate3_metric_value(value)
    if status == "missing":
        missing_metrics.append(result_key)
    return value

# --- runner_parts/multiple_testing_admission.py ---
def _evaluate_statistical_admission(
    strategy: dict,
    profile: dict[str, Any],
    gate_payload: Optional[dict],
    *,
    admission_level: str = "incubation",
    attempt_adjustment: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(gate_payload or {})
    attempt_adjustment = resolve_attempt_adjustment(strategy, attempt_adjustment=attempt_adjustment)
    thresholds = _statistical_gate_thresholds(
        attempt_adjustment,
        admission_level=admission_level,
    )
    reasons: list[str] = []
    warnings: list[str] = []
    missing_metrics: list[str] = []

    wf_ic_ir = _append_missing_statistical_metric(missing_metrics, payload, "wf_ic_ir", "wf_ic_ir")
    pkf_ic = _append_missing_statistical_metric(missing_metrics, payload, "pkf_ic", "pkf_ic")
    bootstrap_ci_lower = _append_missing_statistical_metric(
        missing_metrics,
        payload,
        "bootstrap_ci_lower",
        "bootstrap_ci_lower",
    )
    param_sensitivity = _append_missing_statistical_metric(
        missing_metrics,
        payload,
        "param_sensitivity",
        "param_sensitivity",
    )

    if missing_metrics:
        reasons.append("insufficient_statistical_evidence")
        reasons.append(f"missing_statistical_metrics:{','.join(missing_metrics)}")
        # P1: also emit structured reason_codes per metric so the topN
        # aggregator and dashboard can group failures by metric. The
        # aggregate ``missing_statistical_metrics:..`` line stays for
        # backward compatibility.
        for metric_name in missing_metrics:
            reasons.append(f"missing_{metric_name}")

    # P1 (R5.1, R5.3): for metrics that are present-and-real but below
    # threshold, emit a structured ``weak_<metric>`` code in addition to
    # the human-readable threshold message. ``_classify_gate3_metric_value``
    # already filtered out the placeholder 0.0 case (see audit P1-prep).
    if wf_ic_ir is not None and _classify_gate3_metric_value(wf_ic_ir) == "present_real" \
            and wf_ic_ir < thresholds["walk_forward_ic_ir_min"]:
        reasons.append(f"walk_forward_ic_ir {wf_ic_ir:.3f} < {thresholds['walk_forward_ic_ir_min']:.3f}")
        reasons.append("weak_wf_ic_ir")
    if pkf_ic is not None and _classify_gate3_metric_value(pkf_ic) == "present_real" \
            and pkf_ic < thresholds["purged_kfold_ic_min"]:
        reasons.append(f"purged_kfold_ic {pkf_ic:.3f} < {thresholds['purged_kfold_ic_min']:.3f}")
        reasons.append("weak_pkf_ic")
    if bootstrap_ci_lower is not None and _classify_gate3_metric_value(bootstrap_ci_lower) == "present_real" \
            and bootstrap_ci_lower < thresholds["bootstrap_ci_lower_min"]:
        reasons.append(f"bootstrap_ci_lower {bootstrap_ci_lower:.3f} < {thresholds['bootstrap_ci_lower_min']:.3f}")
        reasons.append("weak_bootstrap_ci_lower")
    if param_sensitivity is not None and _classify_gate3_metric_value(param_sensitivity) == "present_real" \
            and param_sensitivity > thresholds["param_sensitivity_max"]:
        reasons.append(f"param_sensitivity {param_sensitivity:.3f} > {thresholds['param_sensitivity_max']:.3f}")
        reasons.append("weak_param_sensitivity")

    period_robustness = dict(payload.get("period_robustness") or {})
    first_ic = _first_float_value(period_robustness, "first_half_ic")
    second_ic = _first_float_value(period_robustness, "second_half_ic")
    if first_ic is not None and second_ic is not None:
        if first_ic < -0.02 or second_ic < -0.02:
            reasons.append(
                f"period_robustness {first_ic:.3f}/{second_ic:.3f} < -0.020"
            )
        elif (first_ic > 0.01 > second_ic) or (second_ic > 0.01 > first_ic):
            warnings.append(
                f"period_direction_reversal {first_ic:.3f}/{second_ic:.3f}"
            )

    if admission_level == "live":
        mt_thresholds = _multiple_testing_thresholds(admission_level)
        reasons.extend(_live_multiple_testing_reasons(payload, mt_thresholds))

    # P1 (R5): per-metric breakdown for gate_3_evaluation. Captures the
    # value, status (missing / weak / pass), reason_code, threshold, and
    # whether the metric was derived (P1 stop-gap) vs computed (P4 final).
    gate_3_evaluation = _build_gate3_evaluation(
        wf_ic_ir=wf_ic_ir,
        pkf_ic=pkf_ic,
        bootstrap_ci_lower=bootstrap_ci_lower,
        param_sensitivity=param_sensitivity,
        thresholds=thresholds,
        payload=payload,
    )

    return normalize_quality_gate_result(
        {
            "passed": len(reasons) == 0,
            "passed_strict": len(reasons) == 0,
            "profile": profile.get("profile"),
            "validation_focus": profile.get("validation_focus"),
            "primary_validation_layer": profile.get("primary_validation_layer"),
            "attempt_adjustment": attempt_adjustment,
            "thresholds": thresholds,
            "admission_level": admission_level,
            "reasons": reasons,
            "warnings": warnings,
            "wf_ic_ir": round(wf_ic_ir, 4) if wf_ic_ir is not None else None,
            "pkf_ic": round(pkf_ic, 4) if pkf_ic is not None else None,
            "bootstrap_ci_lower": round(bootstrap_ci_lower, 4) if bootstrap_ci_lower is not None else None,
            "param_sensitivity": round(param_sensitivity, 4) if param_sensitivity is not None else None,
            "missing_statistical_metrics": list(missing_metrics),
            "statistical_metric_missing_counts": {key: 1 for key in missing_metrics},
            "gate_3_evaluation": gate_3_evaluation,
        }
    )


def _build_gate3_evaluation(
    *,
    wf_ic_ir: float | None,
    pkf_ic: float | None,
    bootstrap_ci_lower: float | None,
    param_sensitivity: float | None,
    thresholds: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build per-metric Gate-3 evaluation rows (R5 / Property 4 / Property 9).

    Each row carries:
        - ``metric``: stable name (matches reason_code suffix)
        - ``value``: rounded numeric or None
        - ``status``: missing / weak / pass
        - ``reason_code``: ``missing_<m>``, ``weak_<m>``, or ``pass``
        - ``threshold``: numeric threshold used (None for raw signals)
        - ``derived``: True when the value is a stop-gap derivation
          rather than a real upstream computation (P1 → P4 transition)
        - ``comparator``: ">" or "<" — direction of the threshold check
    """
    audit = dict(payload.get("metric_source_audit") or {})

    def _entry(
        metric: str,
        value: float | None,
        threshold: float | None,
        comparator: str,
    ) -> dict[str, Any]:
        status = _classify_gate3_metric_value(value)
        if status == "missing":
            return {
                "metric": metric,
                "value": None,
                "status": "missing",
                "reason_code": f"missing_{metric}",
                "threshold": threshold,
                "comparator": comparator,
                "derived": _is_metric_derived(metric, audit),
            }
        # present_real
        v = float(value) if value is not None else None
        if threshold is not None and v is not None:
            if comparator == ">=":
                weak = v < threshold
            elif comparator == "<=":
                weak = v > threshold
            else:
                weak = False
        else:
            weak = False
        return {
            "metric": metric,
            "value": round(v, 4) if v is not None else None,
            "status": "weak" if weak else "pass",
            "reason_code": f"weak_{metric}" if weak else "pass",
            "threshold": threshold,
            "comparator": comparator,
            "derived": _is_metric_derived(metric, audit),
        }

    return [
        _entry("wf_ic_ir", wf_ic_ir,
               thresholds.get("walk_forward_ic_ir_min"), ">="),
        _entry("pkf_ic", pkf_ic,
               thresholds.get("purged_kfold_ic_min"), ">="),
        _entry("bootstrap_ci_lower", bootstrap_ci_lower,
               thresholds.get("bootstrap_ci_lower_min"), ">="),
        _entry("param_sensitivity", param_sensitivity,
               thresholds.get("param_sensitivity_max"), "<="),
    ]


def _is_metric_derived(metric: str, audit: dict[str, Any]) -> bool:
    """Return True when the metric value came from a stop-gap derivation
    (e.g., ``param_sensitivity`` derived from
    ``parameter_perturbation_trade_stability_inverse``).

    The audit dict is populated by ``_assign_statistical_metric`` in
    trade_profile.py with the source path string. Anything containing
    'inverse' or '_derived' marks a stop-gap path. Real upstream paths
    like ``validation_report.walk_forward`` return False.
    """
    src = str(audit.get(metric) or "").lower()
    if not src:
        return False
    if "inverse" in src or "derived" in src or "stop_gap" in src:
        return True
    return False


def _merge_text_items(*groups: Optional[list[str]]) -> list[str]:
    items: list[str] = []
    for group in groups:
        for item in group or []:
            text = str(item or "").strip()
            if text and text not in items:
                items.append(text)
    return items


def _with_gate_protocol(gate: dict[str, Any], protocol: str) -> dict[str, Any]:
    return normalize_quality_gate_result({**dict(gate or {}), "gate_protocol": protocol})


def _supplemental_statistical_binding_mode(
    *,
    incubation_budget_track: Optional[str] = None,
    submission_lane: Optional[str] = None,
) -> str:
    """Whether Gate-3 statistical evidence can block a trade-primary candidate.

    Same lane/track split as ``_trade_validation_audit_mode``: promotion tracks
    that lead to capital ("formal_incubation", "live_ready_review") require the
    statistical evidence to hold, while research lanes keep it advisory so
    exploratory candidates can still be recorded.

    This exists because the merged gate used to fold every supplemental reason
    into a single ``supplemental_statistical_gate_failed`` warning and take
    ``passed`` from the trade gate alone — walk-forward IC/IR, purged-k-fold IC,
    bootstrap CI lower bound and parameter sensitivity were computed carefully
    and then discarded on the main path.
    """
    track = str(incubation_budget_track or "").strip().lower()
    lane = str(submission_lane or "").strip().lower()
    if lane == "live_ready_review" or track in {"formal_incubation", "live_ready_review"}:
        return "binding"
    return "advisory"


def _merge_trade_primary_gate(
    trade_gate: dict[str, Any],
    supplemental_statistical_gate: Optional[dict[str, Any]],
    *,
    binding_mode: str = "advisory",
) -> dict[str, Any]:
    supplemental = normalize_quality_gate_result(supplemental_statistical_gate)
    trade_gate_payload = normalize_quality_gate_result(trade_gate)
    warnings = _merge_text_items(trade_gate.get("warnings"), supplemental.get("warnings"))
    supplemental_reasons = list(supplemental.get("reasons") or [])
    reasons = list(trade_gate_payload.get("reasons") or [])
    binding = str(binding_mode or "advisory").strip().lower() == "binding"

    if supplemental_reasons:
        if binding:
            # Promote the supplemental verdict into the merged result: on a
            # capital-bound track, missing or weak statistical evidence blocks.
            reasons = _merge_text_items(
                reasons,
                ["supplemental_statistical_gate_failed"],
                supplemental_reasons,
            )
        else:
            warnings = _merge_text_items(warnings, ["supplemental_statistical_gate_failed"])

    base_protocol = str(trade_gate_payload.get("gate_protocol") or "").strip().lower()
    profile_name = base_protocol.split(":", 1)[0] if ":" in base_protocol else base_protocol
    suffix = (
        "trade_primary_with_binding_supplemental_audit"
        if binding
        else "trade_primary_with_supplemental_audit"
    )
    merged_protocol = f"{profile_name}:{suffix}" if profile_name else suffix
    merged = {
        **dict(trade_gate_payload or {}),
        **{
            key: value
            for key, value in supplemental.items()
            if key in _SUPPLEMENTAL_STATISTICAL_FIELDS
        },
        "reasons": reasons,
        "warnings": warnings,
        "passed": bool(trade_gate_payload.get("passed")) and not reasons,
        "primary_gate_protocol": trade_gate_payload.get("gate_protocol"),
        "supplemental_gate_protocol": "supplemental_statistical_audit",
        "supplemental_statistical_binding_mode": "binding" if binding else "advisory",
        "gate_protocol": merged_protocol,
        "supplemental_statistical_gate": {
            "passed": bool(supplemental.get("passed")),
            "reasons": supplemental_reasons,
            "warnings": list(supplemental.get("warnings") or []),
        },
    }
    return normalize_quality_gate_result(merged)


def _committee_review_snapshot(strategy: dict) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    candidate_provenance = dict(_strategy_payload_value(payload, "candidate_provenance") or payload.get("candidate_provenance") or {})
    summary = dict(_strategy_payload_value(payload, "quality_summary") or payload.get("quality_summary") or {})
    review_report = dict(_strategy_payload_value(payload, "quality_report") or payload.get("quality_report") or payload.get("review_report") or {})
    return dict(
        payload.get("committee_review")
        or params.get("committee_review")
        or candidate_provenance.get("committee_review")
        or review_report.get("committee_review")
        or summary.get("committee_review")
        or {}
    )

# --- runner_parts/trade_profile.py ---
def _derive_trade_aware_validation_grade(
    strategy: dict,
    gate: Optional[dict[str, Any]],
    *,
    raw_validation_grade: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    grade = str(raw_validation_grade or "").strip().upper() or None
    if grade != "D":
        return grade, None
    payload = dict(strategy or {})
    strategy_profile = dict(_strategy_payload_value(payload, "strategy_profile") or payload.get("strategy_profile") or {})
    candidate_provenance = dict(
        _strategy_payload_value(payload, "candidate_provenance") or payload.get("candidate_provenance") or {}
    )
    task_preference = dict(gate or {}).get("task_preference") if isinstance(gate, dict) else {}
    preferred_strategy_types = list(dict(task_preference or {}).get("preferred_strategy_types") or [])
    strategy_type = str(
        payload.get("strategy_type")
        or _strategy_payload_value(payload, "candidate_family")
        or candidate_provenance.get("candidate_family")
        or strategy_profile.get("strategy_family")
        or (preferred_strategy_types[0] if preferred_strategy_types else "")
        or ""
    ).strip().lower()
    if strategy_type not in _TRADE_AWARE_VALIDATION_GRADE_FAMILIES:
        return grade, None
    normalized_gate = dict(gate or {})
    profile_name = str(normalized_gate.get("profile") or "").strip().lower()
    validation_focus = str(
        normalized_gate.get("validation_focus")
        or dict(payload.get("params") or {}).get("validation_focus")
        or dict(dict(payload.get("params") or {}).get("validation_profile") or {}).get("validation_focus")
        or dict(payload.get("research_task") or {}).get("validation_focus")
        or ""
    ).strip().lower()
    if profile_name not in _TRADE_PRIMARY_PROFILES:
        return grade, None
    if validation_focus not in _TARGET_ONLY_VALIDATION_FOCUSES:
        return grade, None

    trade_density = safe_metric_value(normalized_gate, "trade_density")
    post_cost_sharpe = safe_metric_value(normalized_gate, "post_cost_sharpe")
    target_layer_oos_return = safe_metric_value(normalized_gate, "target_layer_oos_return")
    trade_stability = safe_metric_value(normalized_gate, "parameter_perturbation_trade_stability")
    dsr = safe_metric_value(normalized_gate, "deflated_sharpe_ratio")
    pbo = safe_metric_value(normalized_gate, "pbo")
    rc_pvalue = safe_metric_value(normalized_gate, "white_reality_check_pvalue")
    spa_pvalue = safe_metric_value(normalized_gate, "hansen_spa_pvalue")

    if trade_density <= 0 or trade_density > 1.2:
        return grade, None

    evidence_score = 0.0
    if trade_density <= 1.0:
        evidence_score += 2.0
    elif trade_density <= 1.2:
        evidence_score += 1.0
    if post_cost_sharpe >= 1.0:
        evidence_score += 2.0
    elif post_cost_sharpe >= 0.8:
        evidence_score += 1.0
    if target_layer_oos_return >= 0.18:
        evidence_score += 1.5
    elif target_layer_oos_return >= 0.08:
        evidence_score += 1.0
    if trade_stability >= 0.5:
        evidence_score += 1.0
    elif trade_stability >= 0.25:
        evidence_score += 0.5
    if dsr >= 0.1:
        evidence_score += 1.0
    elif dsr >= 0.03:
        evidence_score += 0.5
    if pbo <= 0.7:
        evidence_score += 1.0
    elif pbo <= 0.85:
        evidence_score += 0.5
    if rc_pvalue <= 0.2 and spa_pvalue <= 0.2:
        evidence_score += 0.5

    if evidence_score < 5.0:
        return grade, None
    return "C", f"trade_aware_validation_grade_upgrade:{strategy_type}:score={evidence_score:.2f}"


def _minimum_grade_for_admission(admission_level: str) -> str:
    if str(admission_level or "").strip().lower() == "research":
        return ""
    try:
        from strategy_factory.application._runtime_toggles import strategy_factory_min_validation_grade

        return strategy_factory_min_validation_grade()
    except Exception:
        return "S"


def _validation_grade_meets_minimum(grade: str, minimum: str) -> bool:
    if not minimum:
        return True
    try:
        from strategy_factory.application._runtime_toggles import validation_grade_at_least

        return validation_grade_at_least(grade, minimum)
    except Exception:
        order = {"D": 0, "C": 1, "B": 2, "A": 3, "S": 4, "SS": 5, "SSS": 6}
        min_grade = str(minimum or "").strip().upper()
        if min_grade not in order:
            return True
        return order.get(str(grade or "").strip().upper(), -1) >= order[min_grade]


def _resolve_admission_review_context(
    strategy: dict,
    *,
    validation_report: Optional[dict] = None,
    gate: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    quality_summary = dict(_strategy_payload_value(payload, "quality_summary") or payload.get("quality_summary") or {})
    review_report = dict(_strategy_payload_value(payload, "quality_report") or payload.get("quality_report") or payload.get("review_report") or {})
    committee_review = _committee_review_snapshot(payload)
    validation = dict(validation_report or {})
    rating = dict(validation.get("rating") or {})
    trade_quality_adjustment = dict(validation.get("trade_quality_adjustment") or {})
    validation_profile = dict(validation.get("validation_profile") or {})
    reported_validation_grade = str(
        rating.get("grade")
        or _strategy_payload_value(payload, "validation_grade")
        or quality_summary.get("validation_grade")
        or dict(review_report.get("summary") or {}).get("validation_grade")
        or ""
    ).strip().upper() or None
    baseline_validation_grade = str(
        rating.get("base_grade")
        or reported_validation_grade
        or ""
    ).strip().upper() or None
    raw_validation_grade = reported_validation_grade
    validation_grade, validation_grade_adjustment_reason = _derive_trade_aware_validation_grade(
        payload,
        gate,
        raw_validation_grade=raw_validation_grade,
    )
    if not validation_grade_adjustment_reason and bool(trade_quality_adjustment.get("applied")):
        validation_grade_adjustment_reason = str(
            trade_quality_adjustment.get("adjustment_reason") or ""
        ).strip() or None
    committee_decision = _normalize_text(
        committee_review.get("decision")
        or _strategy_payload_value(payload, "promotion_review_recommendation")
        or payload.get("promotion_review_recommendation")
    ) or None
    committee_final_score = committee_review.get("final_score")
    try:
        committee_final_score = None if committee_final_score is None else round(float(committee_final_score), 4)
    except Exception:
        committee_final_score = None
    promotion_review_score = (
        _strategy_payload_value(payload, "promotion_review_score")
        or quality_summary.get("promotion_review_score")
        or payload.get("promotion_review_score")
    )
    try:
        promotion_review_score = None if promotion_review_score is None else round(float(promotion_review_score), 4)
    except Exception:
        promotion_review_score = None
    accept_blockers = [
        str(item or "").strip()
        for item in list(committee_review.get("accept_blockers") or [])
        if str(item or "").strip()
    ]
    validation_focus = str(
        dict(gate or {}).get("validation_focus")
        or dict(params.get("validation_profile") or {}).get("validation_focus")
        or dict(params.get("research_task") or {}).get("validation_focus")
        or validation_profile.get("validation_focus")
        or ""
    ).strip().lower() or None
    return {
        "validation_grade": validation_grade,
        "raw_validation_grade": raw_validation_grade,
        "validation_baseline_grade": baseline_validation_grade,
        "effective_validation_grade": validation_grade,
        "validation_grade_adjustment_reason": validation_grade_adjustment_reason,
        "validation_focus": validation_focus,
        "validation_focus_layer": _resolve_validation_focus_layer(validation_focus or ""),
        "committee_decision": committee_decision,
        "committee_final_score": committee_final_score,
        "promotion_review_score": promotion_review_score,
        "accept_blockers": accept_blockers,
    }


def _review_stage_blockers(
    strategy: dict,
    *,
    admission_level: str,
    validation_report: Optional[dict] = None,
    gate: Optional[dict[str, Any]] = None,
) -> tuple[list[str], dict[str, Any]]:
    semantic_runtime_context = _resolve_semantic_runtime_context(strategy, gate=gate)
    if admission_level == "research":
        context = _resolve_admission_review_context(strategy, validation_report=validation_report, gate=gate)
        context.update(semantic_runtime_context)
        return [], context
    context = _resolve_admission_review_context(strategy, validation_report=validation_report, gate=gate)
    context.update(semantic_runtime_context)
    thresholds = _review_gate_thresholds(admission_level)
    blockers: list[str] = []
    validation_grade = str(context.get("validation_grade") or "").strip().upper()
    committee_decision = _normalize_text(context.get("committee_decision"))
    committee_final_score = context.get("committee_final_score")
    promotion_review_score = context.get("promotion_review_score")
    accept_blockers = list(context.get("accept_blockers") or [])

    if validation_grade in _NON_PROMOTABLE_VALIDATION_GRADES:
        blockers.append(f"validation_grade_{validation_grade.lower()}_not_allowed_for_{admission_level}")
    minimum_validation_grade = _minimum_grade_for_admission(admission_level)
    context["minimum_validation_grade"] = minimum_validation_grade or None
    context["minimum_validation_grade_met"] = _validation_grade_meets_minimum(
        validation_grade,
        minimum_validation_grade,
    )
    if minimum_validation_grade and not context["minimum_validation_grade_met"]:
        blockers.append(
            f"validation_grade_{validation_grade.lower() or 'unknown'}_below_minimum_{minimum_validation_grade.lower()}_for_{admission_level}"
        )
    if committee_decision in _NON_PROMOTABLE_REVIEW_DECISIONS or committee_decision in _NON_PROMOTABLE_REVIEW_RECOMMENDATIONS:
        blockers.append(f"committee_review_{committee_decision}_not_allowed_for_{admission_level}")
    if committee_final_score is not None and committee_final_score < thresholds["committee_final_score_min"]:
        blockers.append(
            f"committee_final_score {committee_final_score:.3f} < {thresholds['committee_final_score_min']:.3f}"
        )
    if promotion_review_score is not None and promotion_review_score < thresholds["promotion_review_score_min"]:
        blockers.append(
            f"promotion_review_score {promotion_review_score:.3f} < {thresholds['promotion_review_score_min']:.3f}"
        )
    if accept_blockers:
        blockers.extend(f"committee_accept_blocker:{item}" for item in accept_blockers)
    semantic_contract_missing_fields = list(context.get("semantic_contract_missing_fields") or [])
    if semantic_contract_missing_fields:
        blockers.append("final_strategy_missing_semantic_contract")
    if not bool(context.get("semantic_runtime_match", True)):
        blockers.append("runtime_family_semantic_mismatch")
    if bool(context.get("proxy_runtime_used")):
        blockers.append("proxy_runtime_not_allowed_for_formal_incubation")
    if bool(context.get("default_profile_not_allowed")):
        blockers.append("default_profile_not_allowed_for_single_name_runtime")
    if bool(context.get("diagnostic_only")):
        blockers.append(f"diagnostic_only_not_allowed_for_{admission_level}")
    if str(context.get("execution_readiness_tier") or "").strip().lower() not in {"", "formal_runtime_ready"}:
        blockers.append(f"execution_readiness_tier:{context.get('execution_readiness_tier')}")
    return blockers, context


def _attach_admission_evaluations(
    strategy: dict,
    profile: dict[str, Any],
    gate: dict[str, Any],
    *,
    risk_report: Optional[dict] = None,
    validation_report: Optional[dict] = None,
    backtest_metrics: Optional[dict] = None,
) -> dict[str, Any]:
    normalized_gate = normalize_quality_gate_result(gate)
    materialized_backtest_metrics = _materialize_backtest_metrics_contract(backtest_metrics)
    admission_metric_gate = normalize_quality_gate_result(
        _merge_authoritative_backtest_metrics(normalized_gate, materialized_backtest_metrics)
    )
    attempt_adjustment = resolve_attempt_adjustment(strategy, gate=normalized_gate)
    evaluations: dict[str, dict[str, Any]] = {}
    profile_name = str(profile.get("profile") or "").strip().lower()
    research_only_due_to_trade_audit_gap = bool(normalized_gate.get("research_only_due_to_trade_audit_gap"))
    research_protocol_adapter = _resolve_research_protocol_submission_adapter(strategy)
    research_protocol_evaluation = evaluate_research_validation_contract_admission(
        research_protocol_adapter,
        observed=_resolve_research_protocol_observed_payload(
            strategy,
            backtest_metrics=materialized_backtest_metrics,
        ),
        spec_completeness_mode=STRATEGY_FACTORY_SPEC_COMPLETENESS_MODE,
    )
    research_protocol_available = bool(research_protocol_evaluation.get("available"))
    research_protocol_review_decision = str(
        research_protocol_evaluation.get("review_decision") or "pending"
    ).strip().lower()
    research_protocol_blockers = list(research_protocol_evaluation.get("blocking_reasons") or [])
    research_protocol_warnings = list(research_protocol_evaluation.get("warnings") or [])
    high_precision_evaluation = _evaluate_high_precision_admission(
        strategy,
        profile,
        normalized_gate,
        backtest_metrics=materialized_backtest_metrics,
    )
    high_precision_available = bool(high_precision_evaluation.get("available"))
    high_precision_review_decision = _normalize_text(
        high_precision_evaluation.get("decision") or "pass"
    ) or "pass"
    high_precision_blockers = list(high_precision_evaluation.get("blocking_reasons") or [])
    high_precision_warnings = list(high_precision_evaluation.get("warnings") or [])
    merged_gate_b_review_decision = _merge_review_decision(
        research_protocol_review_decision if research_protocol_available else "pass",
        high_precision_review_decision if high_precision_available else "pass",
    )

    if research_protocol_available:
        merged_gate_payload = {
            **normalized_gate,
            "warnings": _merge_text_items(normalized_gate.get("warnings"), research_protocol_warnings),
            "business_admission_decision": dict(
                research_protocol_evaluation.get("business_admission_decision") or {}
            ),
            "benchmark_comparison": dict(research_protocol_evaluation.get("benchmark_comparison") or {}),
            "cost_sensitivity_summary": dict(
                research_protocol_evaluation.get("cost_sensitivity_summary") or {}
            ),
            "cash_sleeve_audit": dict(research_protocol_evaluation.get("cash_sleeve_audit") or {}),
            "family_holding_bucket": dict(research_protocol_evaluation.get("family_holding_bucket") or {}),
            "gate_b_review_decision": merged_gate_b_review_decision,
            "artifact_ids": list(research_protocol_evaluation.get("artifact_ids") or []),
            "retrieval_context_ids": list(research_protocol_evaluation.get("retrieval_context_ids") or []),
            "prediction_trace_id": research_protocol_evaluation.get("prediction_trace_id"),
        }
        if research_protocol_review_decision in {"revise", "reject"}:
            merged_gate_payload.update(
                {
                    "passed": False,
                    "passed_strict": False,
                    "provisional_pass": False,
                    "reasons": _merge_text_items(normalized_gate.get("reasons"), research_protocol_blockers),
                }
            )
        normalized_gate = normalize_quality_gate_result(merged_gate_payload)
    if high_precision_available:
        merged_gate_payload = {
            **normalized_gate,
            "warnings": _merge_text_items(normalized_gate.get("warnings"), high_precision_warnings),
            "objective_profile": high_precision_evaluation.get("objective_profile"),
            "precision_readiness": high_precision_evaluation.get("precision_readiness"),
            "regime_validation_summary": dict(
                high_precision_evaluation.get("regime_validation_summary") or {}
            ),
            "cost_robustness_summary": dict(
                high_precision_evaluation.get("cost_robustness_summary") or {}
            ),
            "trade_density_summary": dict(
                high_precision_evaluation.get("trade_density_summary") or {}
            ),
            "event_prefilter_summary": dict(
                high_precision_evaluation.get("event_prefilter_summary") or {}
            ),
            "event_anchor_summary": dict(
                high_precision_evaluation.get("event_anchor_summary") or {}
            ),
            "backtest_metrics_contract_status": normalized_gate.get("backtest_metrics_contract_status"),
            "gate_b_review_decision": merged_gate_b_review_decision,
        }
        if high_precision_review_decision in {"revise", "reject"}:
            merged_gate_payload.update(
                {
                    "passed": False,
                    "passed_strict": False,
                    "provisional_pass": False,
                    "reasons": _merge_text_items(normalized_gate.get("reasons"), high_precision_blockers),
                }
            )
        normalized_gate = normalize_quality_gate_result(merged_gate_payload)

    for admission_level in _ADMISSION_LEVEL_ORDER:
        if profile_name in _TRADE_PRIMARY_PROFILES:
            stage_result = _evaluate_trade_profile_for_admission(
                strategy,
                profile,
                admission_metric_gate,
                risk_report,
                admission_level=admission_level,
                attempt_adjustment=attempt_adjustment,
            )
        else:
            stage_result = _evaluate_statistical_admission(
                strategy,
                profile,
                normalized_gate,
                admission_level=admission_level,
                attempt_adjustment=attempt_adjustment,
            )
        review_blockers, review_context = _review_stage_blockers(
            strategy,
            admission_level=admission_level,
            validation_report=validation_report,
            gate=normalized_gate,
        )
        stage_reasons = _merge_text_items(stage_result.get("reasons"), review_blockers)
        stage_warnings = list(stage_result.get("warnings") or [])
        if research_protocol_available:
            if research_protocol_review_decision in {"revise", "reject"}:
                stage_reasons = _merge_text_items(stage_reasons, research_protocol_blockers)
            stage_warnings = _merge_text_items(stage_warnings, research_protocol_warnings)
        if high_precision_available:
            if high_precision_review_decision in {"revise", "reject"}:
                stage_reasons = _merge_text_items(stage_reasons, high_precision_blockers)
            stage_warnings = _merge_text_items(stage_warnings, high_precision_warnings)
        evaluations[admission_level] = {
            "passed": len(stage_reasons) == 0 and bool(stage_result.get("passed")),
            "reasons": stage_reasons,
            "warnings": stage_warnings,
            "thresholds": dict(stage_result.get("thresholds") or {}),
            "review_context": {
                **dict(review_context or {}),
                "business_admission_decision": dict(
                    research_protocol_evaluation.get("business_admission_decision") or {}
                ),
                "benchmark_comparison": dict(
                    research_protocol_evaluation.get("benchmark_comparison") or {}
                ),
                "cost_sensitivity_summary": dict(
                    research_protocol_evaluation.get("cost_sensitivity_summary") or {}
                ),
                "cash_sleeve_audit": dict(research_protocol_evaluation.get("cash_sleeve_audit") or {}),
                "family_holding_bucket": dict(
                    research_protocol_evaluation.get("family_holding_bucket") or {}
                ),
                "objective_profile": high_precision_evaluation.get("objective_profile"),
                "precision_readiness": high_precision_evaluation.get("precision_readiness"),
                "regime_validation_summary": dict(
                    high_precision_evaluation.get("regime_validation_summary") or {}
                ),
                "cost_robustness_summary": dict(
                    high_precision_evaluation.get("cost_robustness_summary") or {}
                ),
                "trade_density_summary": dict(
                    high_precision_evaluation.get("trade_density_summary") or {}
                ),
                "event_prefilter_summary": dict(
                    high_precision_evaluation.get("event_prefilter_summary") or {}
                ),
                "event_anchor_summary": dict(
                    high_precision_evaluation.get("event_anchor_summary") or {}
                ),
                "backtest_metrics_contract_status": normalized_gate.get("backtest_metrics_contract_status"),
                "metric_source_audit": dict(admission_metric_gate.get("metric_source_audit") or {}),
            },
        }

    if research_only_due_to_trade_audit_gap:
        research_passed = bool(normalized_gate.get("passed"))
        base_reasons = list(normalized_gate.get("reasons") or [])
        base_warnings = list(normalized_gate.get("warnings") or [])
        evaluations["research"] = {
            "passed": research_passed,
            "reasons": [] if research_passed else list(base_reasons),
            "warnings": list(base_warnings),
            "thresholds": dict((evaluations.get("research") or {}).get("thresholds") or {}),
        }
        evaluations["incubation"] = {
            "passed": False,
            "reasons": _merge_text_items(base_reasons, ["trade_validation_audit_missing_for_incubation_admission"]),
            "warnings": list(base_warnings),
            "thresholds": dict((evaluations.get("incubation") or {}).get("thresholds") or {}),
        }
        evaluations["live"] = {
            "passed": False,
            "reasons": _merge_text_items(base_reasons, ["trade_validation_audit_missing_for_live_admission"]),
            "warnings": list(base_warnings),
            "thresholds": dict((evaluations.get("live") or {}).get("thresholds") or {}),
        }
        strict_incubation_ready = False
        incubation_candidate_ready = False
        live_candidate_ready = False
        research_candidate_ready = research_passed
        if research_candidate_ready:
            admission_stage = "research"
            block_reasons = list((evaluations.get("incubation") or {}).get("reasons") or [])
        else:
            admission_stage = "rejected"
            block_reasons = list(base_reasons or (evaluations.get("incubation") or {}).get("reasons") or [])
    else:
        strict_incubation_ready = bool((evaluations.get("incubation") or {}).get("passed"))
        strict_incubation_blocked = bool((evaluations.get("incubation") or {}).get("reasons") or []) and bool(normalized_gate.get("passed"))
        incubation_candidate_ready = bool(normalized_gate.get("passed")) and not strict_incubation_blocked
        live_candidate_ready = bool(
            incubation_candidate_ready
            and not normalized_gate.get("provisional_pass")
            and (evaluations.get("live") or {}).get("passed")
        )
        research_candidate_ready = bool((evaluations.get("research") or {}).get("passed"))

        if live_candidate_ready:
            admission_stage = "live"
            block_reasons = []
        elif incubation_candidate_ready:
            admission_stage = "incubation"
            block_reasons = list((evaluations.get("live") or {}).get("reasons") or [])
        elif research_candidate_ready:
            admission_stage = "research"
            block_reasons = list((evaluations.get("incubation") or {}).get("reasons") or [])
        else:
            admission_stage = "rejected"
            block_reasons = list(normalized_gate.get("reasons") or (evaluations.get("incubation") or {}).get("reasons") or [])

    incubation_pass_mode = (
        "provisional"
        if normalized_gate.get("provisional_pass")
        else ("strict" if strict_incubation_ready and incubation_candidate_ready else "failed")
    )
    return normalize_quality_gate_result(
        {
            **normalized_gate,
            "admission_stage": admission_stage,
            "incubation_pass_mode": incubation_pass_mode,
            "research_candidate_ready": research_candidate_ready,
            "incubation_candidate_ready": incubation_candidate_ready,
            "live_candidate_ready": live_candidate_ready,
            "admission_evaluations": evaluations,
            "admission_block_reasons": block_reasons,
            "research_only_due_to_trade_audit_gap": research_only_due_to_trade_audit_gap,
            "strict_incubation_ready": strict_incubation_ready,
            "strict_incubation_blocked": bool((evaluations.get("incubation") or {}).get("reasons") or [])
            and bool(normalized_gate.get("passed")),
            "admission_review_context": dict((evaluations.get("incubation") or {}).get("review_context") or {}),
            "validation_grade": dict((evaluations.get("incubation") or {}).get("review_context") or {}).get("validation_grade"),
            "raw_validation_grade": dict((evaluations.get("incubation") or {}).get("review_context") or {}).get("raw_validation_grade"),
            "effective_validation_grade": dict((evaluations.get("incubation") or {}).get("review_context") or {}).get("effective_validation_grade"),
            "validation_grade_adjustment_reason": dict((evaluations.get("incubation") or {}).get("review_context") or {}).get("validation_grade_adjustment_reason"),
            "gate_b_review_decision": merged_gate_b_review_decision if (
                research_protocol_available or high_precision_available
            ) else ("pass" if normalized_gate.get("passed") else "reject"),
            "business_admission_decision": dict(
                research_protocol_evaluation.get("business_admission_decision") or {}
            ),
            "benchmark_comparison": dict(research_protocol_evaluation.get("benchmark_comparison") or {}),
            "cost_sensitivity_summary": dict(
                research_protocol_evaluation.get("cost_sensitivity_summary") or {}
            ),
            "cash_sleeve_audit": dict(research_protocol_evaluation.get("cash_sleeve_audit") or {}),
            "family_holding_bucket": dict(research_protocol_evaluation.get("family_holding_bucket") or {}),
            "objective_profile": high_precision_evaluation.get("objective_profile"),
            "precision_readiness": high_precision_evaluation.get("precision_readiness"),
            "regime_validation_summary": dict(
                high_precision_evaluation.get("regime_validation_summary") or {}
            ),
            "cost_robustness_summary": dict(
                high_precision_evaluation.get("cost_robustness_summary") or {}
            ),
            "trade_density_summary": dict(
                high_precision_evaluation.get("trade_density_summary") or {}
            ),
            "event_prefilter_summary": dict(
                high_precision_evaluation.get("event_prefilter_summary") or {}
            ),
            "event_anchor_summary": dict(
                high_precision_evaluation.get("event_anchor_summary") or {}
            ),
            "backtest_metrics_contract_status": normalized_gate.get("backtest_metrics_contract_status"),
            "metric_source_audit": dict(admission_metric_gate.get("metric_source_audit") or {}),
        }
    )


def _resolve_multiple_testing_panel(
    strategy: dict,
    profile: dict[str, Any],
    contract_snapshot: Optional[dict[str, Any]] = None,
) -> tuple[list[str], str]:
    payload = dict(strategy or {})
    contract = dict(contract_snapshot or {})
    research_task = dict(profile.get("research_task") or {})
    target_codes = _extract_target_codes_from_payload(payload)
    targeting = dict(contract.get("targeting") or {})
    stock_pool = dict(targeting.get("stock_pool") or {})
    candidate_provenance = dict(_strategy_payload_value(payload, "candidate_provenance") or payload.get("candidate_provenance") or {})
    strategy_profile = dict(contract.get("strategy_profile") or {})
    if not strategy_profile:
        strategy_profile = infer_candidate_strategy_profile(payload, research_task=research_task)
    validation_focus = _normalize_text(profile.get("validation_focus"))
    if validation_focus in _TARGET_ONLY_VALIDATION_FOCUSES or len(target_codes) <= 1:
        return list(dict.fromkeys(target_codes)), "target_only"

    pool_symbols = _normalize_symbol_list(
        research_task.get("target_pool_symbols"),
        research_task.get("peer_symbols"),
        research_task.get("same_theme_symbols"),
        research_task.get("theme_members"),
        stock_pool.get("symbols"),
        stock_pool.get("codes"),
        candidate_provenance.get("peer_symbols"),
        limit=8,
    )
    if pool_symbols:
        return list(dict.fromkeys([*target_codes, *pool_symbols]))[:8], "task_pool"

    if _normalize_text(strategy_profile.get("generator_mode")) in {"bulk_stock_matrix", "snapshot"} and len(target_codes) <= 2:
        return list(dict.fromkeys(target_codes)), "target_only"

    return list(dict.fromkeys([*target_codes, "600519", "000858", "601318"]))[:6], "representative_fallback"


def _assign_statistical_metric(
    payload: dict[str, Any],
    audit: dict[str, Any],
    field_name: str,
    value: Any,
    source: str,
    *,
    override: bool = False,
) -> None:
    if value in (None, "", [], {}):
        return
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return
    if not np.isfinite(numeric):
        return
    if not override and _optional_metric_value(payload, field_name) is not None:
        return
    payload[field_name] = numeric
    audit[field_name] = source


def _copy_statistical_fields(
    payload: dict[str, Any],
    audit: dict[str, Any],
    source_payload: Optional[dict[str, Any]],
    source_name: str,
) -> None:
    source = dict(source_payload or {})
    for field_name in _SUPPLEMENTAL_STATISTICAL_FIELDS:
        if field_name == "period_robustness":
            period_payload = dict(source.get("period_robustness") or {})
            if period_payload and not payload.get("period_robustness"):
                payload["period_robustness"] = period_payload
                audit["period_robustness"] = source_name
            continue
        if field_name in source:
            _assign_statistical_metric(
                payload,
                audit,
                field_name,
                source.get(field_name),
                source_name,
            )


def _copy_statistical_metric_records(
    payload: dict[str, Any],
    audit: dict[str, Any],
    source_payload: Optional[dict[str, Any]],
    source_name: str,
) -> None:
    source = dict(source_payload or {})
    for field_name in _SUPPLEMENTAL_STATISTICAL_FIELDS:
        if field_name not in source:
            continue
        record = source.get(field_name)
        record_payload = dict(record or {}) if isinstance(record, dict) else {}
        value = record_payload.get("value") if record_payload else record
        derived = bool(record_payload.get("derived")) if record_payload else False
        record_source = str(record_payload.get("source") or "").strip()
        audit_source = f"{source_name}.{field_name}"
        if record_source:
            audit_source = f"{audit_source}:{record_source}"
        if derived and "derived" not in audit_source.lower():
            audit_source = f"{audit_source}_derived"
        if field_name == "period_robustness":
            period_payload = dict(value or {}) if isinstance(value, dict) else {}
            if period_payload and not payload.get("period_robustness"):
                payload["period_robustness"] = period_payload
                audit["period_robustness"] = audit_source
            continue
        _assign_statistical_metric(
            payload,
            audit,
            field_name,
            value,
            audit_source,
        )


def _build_statistical_gate_payload(
    strategy: dict,
    profile: dict[str, Any],
    *,
    gate_payload: Optional[dict[str, Any]] = None,
    backtest_metrics: Optional[dict[str, Any]] = None,
    validation_report: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = dict(gate_payload or {})
    audit = dict(payload.get("metric_source_audit") or {})
    metrics = _materialize_backtest_metrics_contract(backtest_metrics)

    for source_name, source_payload in (
        ("gate_payload", gate_payload),
        ("backtest_metrics", metrics),
        ("factor_metrics", dict(metrics.get("factor_metrics") or {})),
        ("statistical_validation", dict(metrics.get("statistical_validation") or {})),
        ("validation", dict(metrics.get("validation") or {})),
    ):
        _copy_statistical_fields(payload, audit, source_payload, source_name)

    validation = dict(validation_report or {})
    walk_forward = dict(validation.get("walk_forward") or {})
    purged_kfold = dict(validation.get("purged_kfold") or {})
    bootstrap_ci = dict(validation.get("bootstrap_ci") or {})
    multiple_testing = dict(validation.get("multiple_testing") or {})
    statistical_metrics = dict(validation.get("statistical_metrics") or {})

    _assign_statistical_metric(
        payload,
        audit,
        "wf_ic_ir",
        _optional_metric_value(walk_forward, "oos_rank_ic_ir", "oos_ic_ir"),
        "validation_report.walk_forward",
    )
    _assign_statistical_metric(
        payload,
        audit,
        "pkf_ic",
        _optional_metric_value(purged_kfold, "oos_rank_ic_mean", "oos_ic_mean"),
        "validation_report.purged_kfold",
    )
    _assign_statistical_metric(
        payload,
        audit,
        "bootstrap_ci_lower",
        _optional_metric_value(bootstrap_ci, "ci_lower"),
        "validation_report.bootstrap_ci",
    )
    _copy_statistical_metric_records(
        payload,
        audit,
        statistical_metrics,
        "validation_report.statistical_metrics",
    )

    if _optional_metric_value(payload, "param_sensitivity") is None:
        stability = _optional_metric_value(metrics, "parameter_perturbation_trade_stability")
        if stability is not None:
            sensitivity = max(0.0, min(1.0, 1.0 - float(stability)))
            _assign_statistical_metric(
                payload,
                audit,
                "param_sensitivity",
                sensitivity,
                "backtest_metrics.parameter_perturbation_trade_stability_inverse",
            )

    deflated = dict(multiple_testing.get("deflated_sharpe") or {})
    pbo = dict(multiple_testing.get("pbo") or {})
    reality_check = dict(multiple_testing.get("white_reality_check") or {})
    spa = dict(multiple_testing.get("hansen_spa") or {})
    _assign_statistical_metric(
        payload,
        audit,
        "deflated_sharpe_ratio",
        _optional_metric_value(deflated, "dsr", "deflated_sharpe_ratio"),
        "validation_report.multiple_testing.deflated_sharpe",
    )
    _assign_statistical_metric(
        payload,
        audit,
        "pbo",
        _optional_metric_value(pbo, "pbo"),
        "validation_report.multiple_testing.pbo",
    )
    _assign_statistical_metric(
        payload,
        audit,
        "white_reality_check_pvalue",
        _optional_metric_value(reality_check, "p_value"),
        "validation_report.multiple_testing.white_reality_check",
    )
    _assign_statistical_metric(
        payload,
        audit,
        "hansen_spa_pvalue",
        _optional_metric_value(spa, "p_value"),
        "validation_report.multiple_testing.hansen_spa",
    )

    if audit:
        payload["metric_source_audit"] = audit
    return payload


# PR-C1: 补充缺失的 _run_statistical_gate 函数（修复 NameError）
async def _run_statistical_gate(
    db,
    strategy: dict,
    *,
    profile: dict = None,
    klass=None,
    gate_payload: Optional[dict[str, Any]] = None,
    backtest_metrics: Optional[dict[str, Any]] = None,
    validation_report: Optional[dict[str, Any]] = None,
) -> dict:
    """执行统计门禁评估（factor_rank_validation 路径）。

    调用 _evaluate_statistical_admission 并包装为标准 gate result。
    PR-R0: 修复 TypeError — 不再传 db=db/klass=klass（签名不接受），
    改为传入空 gate_payload（supplemental metrics 当前不可用时走默认 0 值）。
    """
    import inspect as _inspect
    profile = profile or {}
    gate_payload = _build_statistical_gate_payload(
        strategy,
        profile,
        gate_payload=gate_payload,
        backtest_metrics=backtest_metrics,
        validation_report=validation_report,
    )
    try:
        result = _evaluate_statistical_admission(
            strategy,
            profile,
            gate_payload,
        )
        if _inspect.isawaitable(result):
            result = await result
        normalized = normalize_quality_gate_result(result or {"passed": False, "reason": "empty_result"})
        if gate_payload.get("metric_source_audit"):
            normalized["metric_source_audit"] = dict(gate_payload.get("metric_source_audit") or {})
        return normalized
    except Exception as exc:
        return normalize_quality_gate_result(
            {
                "passed": False,
                "reason": f"statistical_gate_error: {type(exc).__name__}: {exc}",
                "warnings": [f"statistical_gate_exception:{type(exc).__name__}"],
            }
        )

# --- runner_parts/entry.py ---
async def run_submission_quality_gate(
    db,
    strategy: dict,
    *,
    validation_report: dict | None = None,
    risk_report: dict | None = None,
    backtest_metrics: dict | None = None,
    incubation_budget_track: str | None = None,
    submission_lane: str | None = None,
) -> Dict[str, Any]:
    """Run the submission-stage quality gate and return the final authority result."""
    try:
        strategy = apply_resolved_candidate_envelope(strategy)
        materialized_backtest_metrics = _materialize_backtest_metrics_contract(backtest_metrics)
        backtest_metrics_contract_status = _normalize_text(
            materialized_backtest_metrics.get("backtest_metrics_contract_status")
        ) or "missing"
        try:
            from strategy_factory.application.research.statistical_robustness import (
                enrich_validation_report_with_robustness_derivations,
            )

            validation_report = enrich_validation_report_with_robustness_derivations(
                validation_report,
                backtest_metrics=materialized_backtest_metrics,
            )
        except Exception:
            # The submission gate then evaluates a validation report without the
            # robustness derivations it would otherwise carry. That is a weaker
            # report, and until now nothing in the output said so.
            logger.warning(
                "Submission gate could not enrich the validation report with robustness "
                "derivations for strategy %s; the report is evaluated without them",
                (strategy or {}).get("strategy_id") or (strategy or {}).get("id") or "unknown",
                exc_info=True,
            )
        profile = _resolve_validation_profile(strategy)
        profile_name = str(profile.get("profile") or "").strip().lower()
        strategy_type = str(strategy.get("strategy_type", "") or "").strip().lower()
        strategy_registry = get_strategy_registry()
        klass = strategy_registry.get(strategy_type) if strategy_type else None
        if klass is None:
            return normalize_quality_gate_result(
                {
                    "passed": False,
                    "reason": f"Strategy type not in registry: {strategy_type}",
                    "attempt_adjustment": resolve_attempt_adjustment(strategy),
                }
            )

        normalized: dict[str, Any]
        if profile_name == "factor_rank_validation":
            statistical_gate = await _run_statistical_gate(
                db,
                strategy,
                profile=profile,
                klass=klass,
                backtest_metrics=materialized_backtest_metrics,
                validation_report=validation_report,
            )
            normalized = _with_gate_protocol(
                statistical_gate,
                "factor_rank_validation:statistical_primary",
            )
        elif profile_name in _TRADE_PRIMARY_PROFILES:
            if _has_trade_validation_audit(materialized_backtest_metrics):
                trade_gate = _with_gate_protocol(
                    _evaluate_trade_profile(strategy, profile, materialized_backtest_metrics, risk_report),
                    f"{profile_name}:trade_primary",
                )
                supplemental_gate: dict[str, Any]
                try:
                    supplemental_gate = await _run_statistical_gate(
                        db,
                        strategy,
                        profile=profile,
                        klass=klass,
                        backtest_metrics=materialized_backtest_metrics,
                        validation_report=validation_report,
                    )
                except Exception as exc:
                    supplemental_gate = normalize_quality_gate_result(
                        {
                            "passed": False,
                            "reason": f"Supplemental statistical gate error: {exc}",
                            "warnings": [f"supplemental_statistical_gate_error:{type(exc).__name__}"],
                        }
                    )
                normalized = _merge_trade_primary_gate(
                    trade_gate,
                    supplemental_gate,
                    binding_mode=_supplemental_statistical_binding_mode(
                        incubation_budget_track=incubation_budget_track,
                        submission_lane=submission_lane,
                    ),
                )
            else:
                audit_mode = _trade_validation_audit_mode(
                    incubation_budget_track=incubation_budget_track,
                    submission_lane=submission_lane,
                )
                if audit_mode == "hard_fail":
                    hard_fail_reasons = [f"{profile_name}:trade_validation_audit_missing"]
                    if backtest_metrics_contract_status == "missing":
                        hard_fail_reasons.append("missing_backtest_metrics_contract")
                    normalized = normalize_quality_gate_result(
                        {
                            "passed": False,
                            "gate_protocol": f"{profile_name}:hard_fail_missing_trade_audit",
                            "reasons": hard_fail_reasons,
                            "trade_validation_audit_missing": True,
                            "trade_validation_audit_mode": audit_mode,
                            "backtest_metrics_contract_status": backtest_metrics_contract_status,
                        }
                    )
                else:
                    statistical_gate = await _run_statistical_gate(
                        db,
                        strategy,
                        profile=profile,
                        klass=klass,
                        backtest_metrics=materialized_backtest_metrics,
                        validation_report=validation_report,
                    )
                    warnings = _merge_text_items(
                        statistical_gate.get("warnings"),
                        _merge_text_items(
                            [f"{profile_name}:trade_validation_audit_missing"],
                            ["missing_backtest_metrics_contract"] if backtest_metrics_contract_status == "missing" else [],
                        ),
                    )
                    normalized = normalize_quality_gate_result(
                        {
                            **statistical_gate,
                            "gate_protocol": f"{profile_name}:statistical_fallback_research_only",
                            "warnings": warnings,
                            "trade_validation_audit_missing": True,
                            "trade_validation_audit_mode": audit_mode,
                            "research_only_due_to_trade_audit_gap": True,
                            "backtest_metrics_contract_status": backtest_metrics_contract_status,
                        }
                    )
        else:
            statistical_gate = await _run_statistical_gate(
                db,
                strategy,
                profile=profile,
                klass=klass,
                backtest_metrics=materialized_backtest_metrics,
                validation_report=validation_report,
            )
            warnings = list(statistical_gate.get("warnings") or [])
            protocol = f"{profile_name or 'unknown'}:statistical_fallback"
            if profile_name in _TRADE_PRIMARY_PROFILES:
                warnings = _merge_text_items(
                    warnings,
                    [f"{profile_name}:trade_validation_audit_missing"],
                )
            normalized = normalize_quality_gate_result(
                {
                    **statistical_gate,
                    "gate_protocol": protocol,
                    "warnings": warnings,
                }
            )
        normalized = maybe_grant_provisional_incubation(
            strategy,
            normalized,
            validation_report=validation_report,
            risk_report=risk_report,
            backtest_metrics=materialized_backtest_metrics,
        )
        normalized = normalize_quality_gate_result(
            {
                **normalized,
                "backtest_metrics_contract_status": backtest_metrics_contract_status,
                "attempt_adjustment": resolve_attempt_adjustment(strategy, gate=normalized),
                "cohort_effective_trials": float(
                    normalized.get("deflated_sharpe_effective_trials")
                    or normalized.get("cohort_effective_trials")
                    or dict(normalized.get("attempt_adjustment") or {}).get("cohort_effective_trials")
                    or 0.0
                ),
                "multiple_testing_registry": _build_multiple_testing_registry(
                    strategy,
                    profile,
                    normalized,
                ),
            }
        )
        # === DEV-V1 V5-PR-1: 接入 _estimate_run_correction_metrics ===
        # 计算 deflated_sharpe_ratio / pbo / RC / SPA pvalue 并合并到 normalized,
        # 让下游 _derive_trade_aware_validation_grade 的 evidence_score 公式能用上多重检验调整指标。
        # 任何提取/计算失败软降级,不打断主流程。
        #
        # family_returns 合成 (打通 PBO/RC/SPA):用真实 K 线 + 真实 signal 生成器对策略参数
        # 做 ±20% 扰动,生成同族变体的逐期收益矩阵 (CSCV/RC/SPA 检测参数过拟合的合法比较族)。
        # 取不到真实 K 线时保持 None,PBO/RC/SPA 诚实地维持 missing,绝不用噪声兜底造假。
        family_returns_fallback = await _build_family_returns_from_klines(
            db, strategy, klass,
        )
        run_correction_fields = _inject_run_correction_metrics(
            strategy,
            profile,
            normalized,
            validation_report=validation_report,
            backtest_metrics=materialized_backtest_metrics,
            family_returns_fallback=family_returns_fallback,
        )
        if run_correction_fields:
            normalized = normalize_quality_gate_result(
                {
                    **normalized,
                    **run_correction_fields,
                }
            )
        semantic_runtime_context = _resolve_semantic_runtime_context(strategy, gate=normalized)
        normalized = normalize_quality_gate_result(
            {
                **normalized,
                **semantic_runtime_context,
            }
        )
        if str(semantic_runtime_context.get("trade_prediction_contract_status") or "").strip().lower() != "ready":
            contract_reasons = _merge_text_items(
                ["trade_prediction_contract_not_ready"],
                list(semantic_runtime_context.get("trade_prediction_contract_reject_reasons") or []),
            )
            if bool(semantic_runtime_context.get("trade_prediction_contract_observation_gap")):
                normalized = normalize_quality_gate_result(
                    {
                        **normalized,
                        "diagnostic_only": True,
                        "review_mode": "incubation_only",
                        "warnings": _merge_text_items(normalized.get("warnings"), contract_reasons),
                        "trade_prediction_contract_observation_gap": True,
                    }
                )
            else:
                normalized = normalize_quality_gate_result(
                    {
                        **normalized,
                        "passed": False,
                        "passed_strict": False,
                        "provisional_pass": False,
                        "reasons": _merge_text_items(normalized.get("reasons"), contract_reasons),
                        "hard_fail_reasons": _merge_text_items(normalized.get("hard_fail_reasons"), contract_reasons),
                    }
                )
        return _attach_admission_evaluations(
            strategy,
            profile,
            normalized,
            risk_report=risk_report,
            validation_report=validation_report,
            backtest_metrics=materialized_backtest_metrics,
        )
    except Exception as e:
        return normalize_quality_gate_result(
            {
                "passed": False,
                "reason": str(e),
                "attempt_adjustment": resolve_attempt_adjustment(strategy),
            }
        )

