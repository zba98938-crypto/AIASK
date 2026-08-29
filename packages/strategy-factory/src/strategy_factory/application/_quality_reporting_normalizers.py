
"""Normalization primitives shared by quality reporting policies."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .market_evidence import summarize_market_fact_gate
from ..domain.constants import PROVISIONAL_PASS_THRESHOLDS, QUALITY_GATE_THRESHOLDS, RISK_REPORT_THRESHOLDS

logger = logging.getLogger("strategy_factory.application.quality_reporting")

PROVISIONAL_TECHNICAL_STRATEGY_TYPES = frozenset({
    "momentum",
    "ma_cross",
    "rsi",
    "macro_timing",
    "volatility_breakout",
    "event_structure_breakout",
    "gap_fill",
    "mean_reversion_short",
    "sector_rotation",
    "north_capital_track",
    "margin_divergence",
})

_DEGENERATE_STAT_EPSILON = 1e-9
_TREND_CLUSTER_TYPES = frozenset({"momentum", "ma_cross", "volatility_breakout", "event_structure_breakout", "sector_rotation"})
_POOL_ALLOWED_STRATEGY_TYPES = {
    "high_vol_growth": frozenset({"volatility_breakout", "event_structure_breakout", "gap_fill", "mean_reversion_short", "quality_factor"}),
    "low_vol_defensive": frozenset({"quality_factor", "north_capital_track", "sector_rotation", "ma_cross"}),
    "cycle_resource": frozenset({"sector_rotation", "north_capital_track", "ma_cross", "quality_factor"}),
}
_POOL_RISK_EXPECTATIONS = {
    "high_vol_growth_breakout": {
        "time_stop_min": 5,
        "time_stop_max": 8,
        "take_profit_min": 0.08,
        "take_profit_max": 0.10,
        "position_cap_max": 0.10,
    },
    "high_vol_growth_reversion": {
        "time_stop_min": 1,
        "time_stop_max": 5,
        "take_profit_min": 0.06,
        "take_profit_max": 0.08,
        "position_cap_max": 0.10,
    },
    "cycle_resource": {
        "time_stop_min": 8,
        "time_stop_max": 15,
        "take_profit_min": 0.12,
        "take_profit_max": 0.16,
        "position_cap_max": 0.14,
    },
    "low_vol_defensive": {
        "time_stop_min": 15,
        "time_stop_max": 25,
        "take_profit_min": 0.10,
        "take_profit_max": 0.14,
        "position_cap_max": 0.18,
    },
}
_REVIEW_ISSUE_BUCKET_PRIORITY = (
    "signal_lag",
    "pool_mismatch",
    "risk_parameter_mismatch",
    "homogeneous_exposure",
    "data_threshold_idle",
)


def quality_gate_reason_code(reason: str) -> str:
    text = str(reason or "").strip()
    if not text:
        return "unknown"
    lowered = text.lower()
    overrides = {
        "insufficient kline data": "insufficient_kline_data",
        "validation_grade_d": "validation_grade_d",
    }
    for needle, code in overrides.items():
        if needle in lowered:
            return code
    normalized = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return normalized or "unknown"


def _normalize_attempt_adjustment(value: Optional[dict]) -> dict:
    raw = dict(value or {})

    def _safe_int(value: object, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)

    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    if not raw:
        return {}
    attempt_count = max(1, _safe_int(raw.get("attempt_count"), 1))
    selected_count = max(0, _safe_int(raw.get("selected_count"), 0))
    selection_ratio = raw.get("selection_ratio")
    if selection_ratio is None:
        selection_ratio = selected_count / max(attempt_count, 1)
    penalty = _safe_float(raw.get("penalty"), 0.0)
    applied = raw.get("applied")
    if applied is None:
        applied = penalty > 0.0
    return {
        **raw,
        "attempt_count": attempt_count,
        "selected_count": selected_count,
        "selection_ratio": round(_safe_float(selection_ratio, 0.0), 4),
        "penalty": round(penalty, 4),
        "applied": bool(applied),
    }


def _normalize_committee_review(value: Optional[dict]) -> dict:
    raw = dict(value or {})
    if not raw:
        return {}

    def _unique_strings(values: object) -> list[str]:
        items: list[str] = []
        for item in list(values or []):
            text = str(item or "").strip()
            if text and text not in items:
                items.append(text)
        return items

    normalized: dict[str, object] = {}
    for key in (
        "decision",
        "review_mode",
    ):
        text = str(raw.get(key) or "").strip()
        if text:
            normalized[key] = text
    for key in (
        "final_score",
        "planner_score",
        "risk_score",
        "feasibility_score",
        "execution_score",
        "capacity_score",
        "task_alignment_score",
        "novelty_score",
    ):
        if raw.get(key) is None:
            continue
        try:
            normalized[key] = round(float(raw.get(key) or 0.0), 4)
        except Exception:
            continue
    for key in ("rank",):
        if raw.get(key) is None:
            continue
        try:
            normalized[key] = int(raw.get(key) or 0)
        except Exception:
            continue
    for key in ("is_champion",):
        if raw.get(key) is None:
            continue
        normalized[key] = bool(raw.get(key))
    for key in (
        "alignment_issues",
        "execution_issues",
        "capacity_issues",
        "suggestions",
        "accept_blockers",
    ):
        values = _unique_strings(raw.get(key))
        if values:
            normalized[key] = values
    for key in ("planner_context", "task_alignment_context"):
        mapping = dict(raw.get(key) or {})
        if mapping:
            normalized[key] = mapping
    return normalized


def normalize_quality_gate_result(result: Optional[dict]) -> dict:
    raw = dict(result or {})
    reasons: list[str] = []
    for item in raw.get("reasons") or []:
        text = str(item).strip()
        if text and text not in reasons:
            reasons.append(text)
    reason = str(raw.get("reason") or "").strip()
    if reason and reason not in reasons:
        reasons.append(reason)
    warnings: list[str] = []
    for item in raw.get("warnings") or []:
        text = str(item).strip()
        if text and text not in warnings:
            warnings.append(text)
    return {
        **raw,
        "passed": bool(raw.get("passed")),
        "reasons": reasons,
        "reason_codes": [quality_gate_reason_code(item) for item in reasons],
        "warnings": warnings,
        "warning_codes": [quality_gate_reason_code(item) for item in warnings],
        "attempt_adjustment": _normalize_attempt_adjustment(raw.get("attempt_adjustment")),
    }


def is_factory_ai_prototype_strategy(strategy: Optional[dict]) -> bool:
    payload = dict(strategy or {})
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    tags = {str(tag).strip().lower() for tag in list(payload.get("tags") or [])}
    if "factory" not in tags and "auto_generated" not in tags:
        return False
    if "external_llm" in tags or "ai_generated" in tags:
        return True
    return strategy_type == "dsl_rule"


def is_factory_provisional_candidate(strategy: Optional[dict]) -> bool:
    payload = dict(strategy or {})
    tags = {str(tag).strip().lower() for tag in list(payload.get("tags") or [])}
    if "factory" not in tags and "auto_generated" not in tags:
        return False
    if is_provisional_technical_strategy(payload):
        return True
    return is_factory_ai_prototype_strategy(payload)


def is_provisional_technical_strategy(strategy: Optional[dict]) -> bool:
    payload = dict(strategy or {})
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    return strategy_type in PROVISIONAL_TECHNICAL_STRATEGY_TYPES


def has_only_statistical_gate_failures(gate_result: Optional[dict]) -> bool:
    gate = normalize_quality_gate_result(gate_result)
    codes = list(gate.get("reason_codes") or [])
    if not codes:
        return False
    allowed_prefixes = (
        "walk_forward_ic_ir",
        "purged_k_fold_ic",
        "bootstrap_ci_lower",
        "parameter_sensitivity",
        "multi_period_ic",
    )
    return all(any(str(code).startswith(prefix) for prefix in allowed_prefixes) for code in codes)


def safe_metric_value(payload: Optional[dict], *keys: str) -> float:
    """Return a float metric, treating missing/unparseable values as 0.0.

    Prefer :func:`optional_metric_value` when a gate must distinguish
    "missing" from an explicit zero.
    """
    data = dict(payload or {})
    for key in keys:
        if key in data and data.get(key) is not None:
            try:
                return float(data.get(key) or 0.0)
            except Exception:
                return 0.0
    return 0.0


def optional_metric_value(payload: Optional[dict], *keys: str) -> float | None:
    """Return a metric float, or ``None`` when absent/unparseable.

    Explicit numeric zeros are preserved. Callers that treat missing as a
    hard failure should deny on ``None`` rather than comparing against a
    default of ``0.0``.
    """
    data = dict(payload or {})
    for key in keys:
        if key not in data or data.get(key) is None:
            continue
        try:
            return float(data[key])
        except (TypeError, ValueError):
            return None
    return None


def _safe_ratio(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except Exception:
        return 0.0


def _safe_optional_float(value: Any) -> float | None:
    try:
        if value in (None, "", [], {}):
            return None
        return float(value)
    except Exception:
        return None


def _normalized_string_list(values: Any) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _risk_expectation_key(pool_profile: str, strategy_type: str) -> str | None:
    profile = str(pool_profile or "").strip().lower()
    strategy_key = str(strategy_type or "").strip().lower()
    if profile == "high_vol_growth":
        if strategy_key in {"volatility_breakout", "event_structure_breakout", "momentum"}:
            return "high_vol_growth_breakout"
        return "high_vol_growth_reversion"
    if profile == "cycle_resource":
        return "cycle_resource"
    if profile == "low_vol_defensive":
        return "low_vol_defensive"
    return None


def _derive_review_issue_buckets(
    strategy_type: Optional[str],
    *,
    candidate_provenance: dict[str, Any],
    strategy_profile: dict[str, Any],
    market_fact_gate: dict[str, Any],
    backtest_assumptions: dict[str, Any],
    audit: dict[str, Any],
) -> tuple[list[str], str | None]:
    strategy_key = str(strategy_type or "").strip().lower()
    pool_profile = str(
        candidate_provenance.get("pool_profile")
        or strategy_profile.get("pool_profile")
        or ""
    ).strip().lower()
    holding_period_bucket = str(
        candidate_provenance.get("holding_period_bucket")
        or strategy_profile.get("holding_period_bucket")
        or ""
    ).strip().lower()
    buckets: list[str] = []

    def add(bucket: str) -> None:
        if bucket not in buckets:
            buckets.append(bucket)

    allowed_strategy_types = _POOL_ALLOWED_STRATEGY_TYPES.get(pool_profile)
    if allowed_strategy_types and strategy_key and strategy_key not in allowed_strategy_types:
        add("pool_mismatch")

    if pool_profile == "high_vol_growth":
        if strategy_key in {"ma_cross", "momentum"}:
            add("signal_lag")
        elif strategy_key in _TREND_CLUSTER_TYPES and holding_period_bucket in {"medium", "long"}:
            add("signal_lag")
        elif strategy_key in {"gap_fill", "mean_reversion_short", "volatility_breakout", "event_structure_breakout"} and holding_period_bucket == "long":
            add("signal_lag")

    expectation_key = _risk_expectation_key(pool_profile, strategy_key)
    expectation = _POOL_RISK_EXPECTATIONS.get(expectation_key or "")
    if expectation:
        stop_loss_mode = str(backtest_assumptions.get("stop_loss_mode") or "").strip().lower()
        time_stop_days = _safe_optional_float(
            backtest_assumptions.get("time_stop_days")
            if backtest_assumptions.get("time_stop_days") is not None
            else backtest_assumptions.get("max_holding_days")
        )
        take_profit_pct = _safe_optional_float(backtest_assumptions.get("take_profit_pct"))
        position_cap_pct = _safe_optional_float(
            backtest_assumptions.get("position_cap_pct")
            if backtest_assumptions.get("position_cap_pct") is not None
            else backtest_assumptions.get("max_position_pct")
        )
        if stop_loss_mode != "atr_bucketed":
            add("risk_parameter_mismatch")
        elif (
            time_stop_days is None
            or time_stop_days < float(expectation["time_stop_min"])
            or time_stop_days > float(expectation["time_stop_max"])
            or take_profit_pct is None
            or take_profit_pct < float(expectation["take_profit_min"])
            or take_profit_pct > float(expectation["take_profit_max"])
            or position_cap_pct is None
            or position_cap_pct > float(expectation["position_cap_max"])
        ):
            add("risk_parameter_mismatch")

    trend_cluster_ratio = _safe_ratio(
        audit.get("trend_cluster_ratio")
        if audit.get("trend_cluster_ratio") is not None
        else candidate_provenance.get("trend_cluster_ratio")
    )
    diversification_debt = _normalized_string_list(
        audit.get("diversification_debt")
        or candidate_provenance.get("diversification_debt")
    )
    if trend_cluster_ratio > 0.5 or diversification_debt:
        add("homogeneous_exposure")

    market_fact_gate_status = str(market_fact_gate.get("market_fact_gate_status") or "").strip().lower()
    has_market_fact_inputs = bool(market_fact_gate.get("market_facts"))
    if has_market_fact_inputs and market_fact_gate_status in {"missing", "degraded_only", "mixed_with_degraded"}:
        add("data_threshold_idle")

    ordered = [bucket for bucket in _REVIEW_ISSUE_BUCKET_PRIORITY if bucket in buckets]
    primary = ordered[0] if ordered else None
    return ordered, primary


def _is_near_zero(value: object, *, eps: float = _DEGENERATE_STAT_EPSILON) -> bool:
    try:
        return abs(float(value)) <= eps
    except Exception:
        return False


def has_degenerate_validation_statistics(validation_report: Optional[dict]) -> bool:
    validation = dict(validation_report or {})
    rating = dict(validation.get("rating") or {})
    walk_forward = dict(validation.get("walk_forward") or {})
    purged_kfold = dict(validation.get("purged_kfold") or {})
    bootstrap_ci = dict(validation.get("bootstrap_ci") or {})

    wf_n_folds = int(safe_metric_value(walk_forward, "n_folds"))
    pkf_n_folds = int(safe_metric_value(purged_kfold, "n_folds"))
    total_score = safe_metric_value(rating, "total_score")

    score_values: list[float] = []
    for value in dict(rating.get("scores") or {}).values():
        try:
            score_values.append(float(value or 0.0))
        except Exception:
            continue
    zero_score_map = bool(score_values) and all(_is_near_zero(value) for value in score_values)

    stat_surface = (
        safe_metric_value(walk_forward, "oos_rank_ic_mean", "oos_ic_mean"),
        safe_metric_value(walk_forward, "oos_rank_ic_ir", "oos_ic_ir"),
        safe_metric_value(purged_kfold, "oos_rank_ic_mean", "oos_ic_mean"),
        safe_metric_value(purged_kfold, "oos_rank_ic_ir", "oos_ic_ir"),
        safe_metric_value(bootstrap_ci, "ci_lower"),
        safe_metric_value(bootstrap_ci, "ci_upper"),
        safe_metric_value(bootstrap_ci, "sample_size"),
    )
    zero_stat_surface = all(_is_near_zero(value) for value in stat_surface)
    no_fold_evidence = wf_n_folds <= 0 and pkf_n_folds <= 0

    return no_fold_evidence or (
        total_score <= 0
        and zero_stat_surface
        and (not score_values or zero_score_map)
    )


def _count_statistical_checks_passed(gate: dict) -> tuple[int, list[str], list[str]]:
    check_map = {
        "walk_forward_ic_ir": ("wf_ic_ir", QUALITY_GATE_THRESHOLDS["walk_forward_ic_ir_min"], ">="),
        "purged_kfold_ic": ("pkf_ic", QUALITY_GATE_THRESHOLDS["purged_kfold_ic_min"], ">="),
        "bootstrap_ci_lower": ("bootstrap_ci_lower", QUALITY_GATE_THRESHOLDS["bootstrap_ci_lower_min"], ">="),
        "param_sensitivity": ("param_sensitivity", QUALITY_GATE_THRESHOLDS["param_sensitivity_max"], "<="),
    }
    passed_checks: list[str] = []
    failed_checks: list[str] = []
    for check_name, (key, threshold, op) in check_map.items():
        value = gate.get(key)
        if value is None:
            failed_checks.append(check_name)
            continue
        try:
            val = float(value)
        except (TypeError, ValueError):
            failed_checks.append(check_name)
            continue
        if op == ">=" and val >= threshold:
            passed_checks.append(check_name)
        elif op == "<=" and val <= threshold:
            passed_checks.append(check_name)
        else:
            failed_checks.append(check_name)

    period_robustness = gate.get("period_robustness") or {}
    first_ic = period_robustness.get("first_half_ic")
    second_ic = period_robustness.get("second_half_ic")
    if first_ic is not None and second_ic is not None:
        try:
            f_ic, s_ic = float(first_ic), float(second_ic)
            direction_consistent = not (f_ic > 0.01 and s_ic < -0.01) and not (f_ic < -0.01 and s_ic > 0.01)
            both_non_negative = f_ic >= -0.02 and s_ic >= -0.02
            if both_non_negative and direction_consistent:
                passed_checks.append("multi_period_robustness")
            else:
                failed_checks.append("multi_period_robustness")
        except (TypeError, ValueError):
            failed_checks.append("multi_period_robustness")

    return len(passed_checks), passed_checks, failed_checks
