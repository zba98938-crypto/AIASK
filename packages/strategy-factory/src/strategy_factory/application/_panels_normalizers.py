
"""Normalization and scoring primitives for strategy validation panels."""

from __future__ import annotations

from typing import List

import numpy as np

from ..domain.targets import _resolve_strategy_sample_selection, _resolve_validation_focus_layer
from ..domain.constants import FACTORY_VALIDATION_PANEL_SAMPLE_SIZE
from ..infrastructure.mcp_services import (
    get_normalize_klines,
    get_risk_model_class,
    get_strategy_registry,
    get_validation_runtime,
)

_TREND_EXECUTABLE_DSL_TYPES = {"ma_cross", "momentum", "volatility_breakout", "event_structure_breakout"}


def _signal_series_from_events(length: int, events: list[dict] | None) -> np.ndarray:
    signals = np.zeros(max(0, int(length)), dtype=np.float64)
    for event in list(events or []):
        idx = int(event.get("index") or 0)
        signal = int(event.get("signal") or 0)
        if 0 <= idx < len(signals) and signal != 0:
            signals[idx] = 1.0 if signal > 0 else -1.0
    return signals


def _signal_series_from_masks(entry_mask: np.ndarray, exit_mask: np.ndarray) -> np.ndarray:
    entry = np.asarray(entry_mask, dtype=bool)
    exit_ = np.asarray(exit_mask, dtype=bool)
    size = max(len(entry), len(exit_))
    signals = np.zeros(size, dtype=np.float64)
    if len(entry):
        signals[: len(entry)][entry] = 1.0
    if len(exit_):
        exit_slice = signals[: len(exit_)]
        exit_slice[np.asarray(exit_, dtype=bool) & (exit_slice == 0.0)] = -1.0
    return signals


def _resolve_execution_semantic_status(strategy_type: str, params: dict) -> dict:
    payload = dict(params or {})
    normalized_strategy_type = str(strategy_type or "").strip().lower()
    trade_plan_to_dsl_map = dict(payload.get("trade_plan_to_dsl_map") or {})
    instrument_profile = dict(payload.get("instrument_profile") or {})
    dsl_required = bool(payload.get("dsl_required"))
    if not dsl_required:
        target_symbols = list(payload.get("target_symbols") or [])
        dsl_required = normalized_strategy_type in _TREND_EXECUTABLE_DSL_TYPES and len(target_symbols) == 1
    dsl_compiled = bool(payload.get("dsl_compiled"))
    if not dsl_compiled:
        dsl_compiled = bool(dict(payload.get("dsl") or {}))
    execution_semantic_mode = str(payload.get("execution_semantic_mode") or "").strip().lower()
    if not execution_semantic_mode:
        execution_semantic_mode = (
            "compiled_dsl"
            if dsl_compiled
            else "missing_executable_contract"
            if dsl_required
            else "builtin_legacy"
        )
    mapped_trade_step_count = int(trade_plan_to_dsl_map.get("mapped_trade_step_count") or 0)
    runtime_family_data_source = str(payload.get("runtime_family_data_source") or "").strip().lower() or None
    proxy_runtime_used = bool(payload.get("proxy_runtime_used"))
    semantic_runtime_match = (
        bool(payload.get("semantic_runtime_match"))
        if payload.get("semantic_runtime_match") is not None
        else not proxy_runtime_used
    )
    diagnostic_only = bool(payload.get("diagnostic_only"))
    execution_readiness_tier = str(payload.get("execution_readiness_tier") or "").strip().lower() or None
    measurement_source = str(
        instrument_profile.get("measurement_source") or "default_board_profile"
    ).strip().lower() or "default_board_profile"
    measured_profile_complete = bool(instrument_profile.get("measured_profile_complete"))
    default_profile_blocked = dsl_required and (
        measurement_source == "default_board_profile" or not measured_profile_complete
    )
    return {
        "execution_semantic_mode": execution_semantic_mode,
        "dsl_required": dsl_required,
        "dsl_compiled": dsl_compiled,
        "mapped_trade_step_count": mapped_trade_step_count,
        "semantic_runtime_match": semantic_runtime_match,
        "runtime_family_data_source": runtime_family_data_source,
        "proxy_runtime_used": proxy_runtime_used,
        "diagnostic_only": diagnostic_only,
        "execution_readiness_tier": execution_readiness_tier,
        "measurement_source": measurement_source,
        "measured_profile_complete": measured_profile_complete,
        "execution_semantic_ready": (
            execution_semantic_mode == "compiled_dsl"
            and dsl_compiled
            and mapped_trade_step_count > 0
            and semantic_runtime_match
            and not proxy_runtime_used
            and not diagnostic_only
            and not default_profile_blocked
            and execution_readiness_tier in {"", "formal_runtime_ready"}
        ),
    }


def _generate_strategy_signal_series(
    strategy_registry,
    strategy_type: str,
    params: dict,
    closes: np.ndarray,
    volumes: np.ndarray,
    *,
    klines: list[dict] | None = None,
) -> np.ndarray:
    if hasattr(strategy_registry, "create_runtime_strategy"):
        instance, _execution_semantic_mode = strategy_registry.create_runtime_strategy(strategy_type, params or {})
    else:
        klass = strategy_registry.get(strategy_type)
        instance = klass() if klass is not None else None
        if instance is not None:
            instance.set_parameters(params or {})
    if instance is None:
        return np.zeros(len(closes), dtype=np.float64)
    if klines and hasattr(instance, "generate_signal_events_from_klines"):
        events = instance.generate_signal_events_from_klines(klines)
        if events is not None:
            return _signal_series_from_events(len(klines), events)
    if klines and hasattr(instance, "generate_entry_exit_masks_from_klines"):
        entry_mask, exit_mask = instance.generate_entry_exit_masks_from_klines(klines)
        return _signal_series_from_masks(entry_mask, exit_mask)
    try:
        return np.asarray(instance.generate_signals(closes, volumes), dtype=np.float64)
    except TypeError:
        return np.asarray(instance.generate_signals(closes), dtype=np.float64)


def _resolve_validation_focus(params: dict) -> str:
    payload = dict(params or {})
    validation_profile = dict(payload.get("validation_profile") or {})
    research_task = dict(payload.get("research_task") or {})
    return str(
        validation_profile.get("validation_focus")
        or research_task.get("validation_focus")
        or ""
    ).strip().lower()


def _annualized_sharpe_ratio(returns: np.ndarray, periods_per_year: float = 252.0) -> float:
    series = np.asarray(returns, dtype=np.float64)
    if series.size == 0:
        return 0.0
    series = series[np.isfinite(series)]
    if series.size < 8:
        return 0.0
    std = float(np.std(series))
    if std <= 1e-12:
        return 0.0
    return float(np.mean(series) / std * np.sqrt(periods_per_year))


def _grade_for_total_score(total_score: float) -> str:
    total = float(total_score or 0.0)
    if total >= 90.0:
        return "SSS"
    if total >= 85.0:
        return "SS"
    if total >= 80.0:
        return "S"
    if total >= 70.0:
        return "A"
    if total >= 55.0:
        return "B"
    if total >= 40.0:
        return "C"
    return "D"


def _threshold_score(value: float, thresholds: list[tuple[float, float]]) -> float:
    for threshold, score in thresholds:
        if value >= threshold:
            return float(score)
    return 0.0


def _reverse_threshold_score(value: float, thresholds: list[tuple[float, float]]) -> float:
    for threshold, score in thresholds:
        if value <= threshold:
            return float(score)
    return 0.0


def _build_validation_focus_annotation(
    validation_focus: str,
    validation_focus_layer: str,
) -> dict:
    layer = str(validation_focus_layer or "broad_market").strip().lower() or "broad_market"
    focus = str(validation_focus or "").strip().lower() or None
    defaults = {
        "target_only": {
            "interpretation": "单目标或强目标约束 cohort，优先衡量策略在目标样本上的交易稳健性。",
            "threshold_note": "更强调 target-layer 一致性与 family 对齐，不应与宽市场因子候选直接横向比较。",
        },
        "family_peer": {
            "interpretation": "目标样本加 family peer cohort，优先衡量相近 family 样本上的可迁移性。",
            "threshold_note": "允许适度跨样本验证，但仍以 family 对齐为主，不按 broad market 口径解读。",
        },
        "sector_peer": {
            "interpretation": "目标样本加 sector peer proxy cohort，优先衡量近行业/近结构样本上的泛化。",
            "threshold_note": "比 family_peer 更宽，但仍应避免与全市场宽面板因子直接混评。",
        },
        "broad_market": {
            "interpretation": "宽市场代表样本 cohort，优先衡量一般化的样本外稳定性。",
            "threshold_note": "适合宽面板因子或无明确 target 的策略，不应用来否定 target-only 交易候选的局部有效性。",
        },
    }
    annotation = dict(defaults.get(layer) or defaults["broad_market"])
    annotation["validation_focus"] = focus
    annotation["validation_focus_layer"] = layer
    return annotation
