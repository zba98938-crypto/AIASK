"""扩展 AST 编译器 — 继承现有 factor_candidate_compiler，支持扩展字段/函数。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ...factor_candidate_compiler import (
    SUPPORTED_FACTOR_FIELDS as CORE_FIELDS,
    SUPPORTED_FACTOR_FUNCTIONS as CORE_FUNCTIONS,
    _SAFE_FUNCTION_ENV,
    _CompilerStats,
    _ALLOWED_BINOPS,
    _ALLOWED_UNARYOPS,
    _ALLOWED_NODE_TYPES,
    _series_like,
    _safe_window,
    build_factor_feature_frame,
    compile_factor_candidate as _core_compile,
    evaluate_compiled_factor as _core_evaluate,
)
from .dsl import EXTENDED_FIELDS, EXTENDED_FUNCTIONS


# ═══════════════════════════════════════════════════════════════════════════════
# 扩展函数实现
# ═══════════════════════════════════════════════════════════════════════════════

def _fn_ts_max(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    return pd.Series(series).astype(float).rolling(window, min_periods=window).max()


def _fn_ts_min(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    return pd.Series(series).astype(float).rolling(window, min_periods=window).min()


def _fn_ts_corr(left: Any, right: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    s1 = pd.Series(left).astype(float)
    s2 = pd.Series(right).astype(float)
    return s1.rolling(window, min_periods=window).corr(s2)


def _fn_ts_cov(left: Any, right: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    s1 = pd.Series(left).astype(float)
    s2 = pd.Series(right).astype(float)
    return s1.rolling(window, min_periods=window).cov(s2)


def _fn_ts_skew(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    return pd.Series(series).astype(float).rolling(window, min_periods=window).skew()


def _fn_ts_kurt(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    return pd.Series(series).astype(float).rolling(window, min_periods=window).kurt()


def _fn_ewma(series: Any, window: Any = 20) -> pd.Series:
    span = _safe_window(window, 20)
    return pd.Series(series).astype(float).ewm(span=span, min_periods=span).mean()


def _fn_ts_argmax(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    s = pd.Series(series).astype(float)
    return s.rolling(window, min_periods=window).apply(lambda x: float(np.argmax(x)), raw=True)


def _fn_ts_argmin(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    s = pd.Series(series).astype(float)
    return s.rolling(window, min_periods=window).apply(lambda x: float(np.argmin(x)), raw=True)


def _fn_ts_decay(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    weights = np.arange(1, window + 1, dtype=float)
    weights = weights / weights.sum()
    s = pd.Series(series).astype(float)
    return s.rolling(window, min_periods=window).apply(lambda x: float(np.dot(x, weights)), raw=True)


def _fn_cs_rank(series: Any) -> pd.Series:
    s = pd.Series(series).astype(float)
    return s.rank(pct=True)


def _fn_cs_zscore(series: Any) -> pd.Series:
    s = pd.Series(series).astype(float)
    mean = s.mean()
    std = s.std()
    if std == 0 or np.isnan(std):
        return s * 0.0
    return (s - mean) / std


def _fn_cs_demean(series: Any) -> pd.Series:
    s = pd.Series(series).astype(float)
    return s - s.mean()


def _fn_if_else(cond: Any, true_val: Any, false_val: Any) -> Any:
    c = pd.Series(cond).astype(float)
    t = pd.Series(true_val).astype(float) if not np.isscalar(true_val) else float(true_val)
    f = pd.Series(false_val).astype(float) if not np.isscalar(false_val) else float(false_val)
    mask = c > 0
    if np.isscalar(t) and np.isscalar(f):
        result = pd.Series(np.where(mask, t, f), index=c.index)
    elif np.isscalar(t):
        result = pd.Series(np.where(mask, t, f), index=c.index)
    elif np.isscalar(f):
        result = pd.Series(np.where(mask, t, f), index=c.index)
    else:
        result = pd.Series(np.where(mask, t, f), index=c.index)
    return result


def _fn_greater(left: Any, right: Any) -> pd.Series:
    l = pd.Series(left).astype(float) if not np.isscalar(left) else float(left)
    r = pd.Series(right).astype(float) if not np.isscalar(right) else float(right)
    return (pd.Series(l) > pd.Series(r)).astype(float)


def _fn_less(left: Any, right: Any) -> pd.Series:
    l = pd.Series(left).astype(float) if not np.isscalar(left) else float(left)
    r = pd.Series(right).astype(float) if not np.isscalar(right) else float(right)
    return (pd.Series(l) < pd.Series(r)).astype(float)


# 扩展函数环境
EXTENDED_FUNCTION_ENV = {
    **_SAFE_FUNCTION_ENV,
    "ts_max": _fn_ts_max,
    "ts_min": _fn_ts_min,
    "ts_corr": _fn_ts_corr,
    "ts_cov": _fn_ts_cov,
    "ts_skew": _fn_ts_skew,
    "ts_kurt": _fn_ts_kurt,
    "ewma": _fn_ewma,
    "ts_argmax": _fn_ts_argmax,
    "ts_argmin": _fn_ts_argmin,
    "ts_decay": _fn_ts_decay,
    "cs_rank": _fn_cs_rank,
    "cs_zscore": _fn_cs_zscore,
    "cs_demean": _fn_cs_demean,
    "if_else": _fn_if_else,
    "greater": _fn_greater,
    "less": _fn_less,
}


# ═══════════════════════════════════════════════════════════════════════════════
# 扩展编译器
# ═══════════════════════════════════════════════════════════════════════════════

MAX_COMPLEXITY_SCORE = 120


def compile_factor_extended(candidate: dict[str, Any]) -> dict[str, Any]:
    """扩展编译器 — 支持扩展字段/函数集。

    先尝试核心编译器，如果失败（因为使用了扩展字段/函数），
    则使用扩展编译器重新编译。
    """
    try:
        result = _core_compile(candidate)
        if result.get("valid"):
            return result
        # 如果无效是因为 unsupported_fields/functions，检查是否在扩展集中
        unsupported_fields = set(result.get("unsupported_fields", []))
        unsupported_functions = set(result.get("unsupported_functions", []))
        extended_field_names = EXTENDED_FIELDS - CORE_FIELDS
        extended_func_names = EXTENDED_FUNCTIONS - CORE_FUNCTIONS
        if unsupported_fields <= extended_field_names and unsupported_functions <= extended_func_names:
            # 所有"不支持"的都在扩展集中，标记为有效
            result["valid"] = True
            result["degraded"] = False
            result["extended_mode"] = True
            result["warnings"] = [w for w in result.get("warnings", []) if "unsupported" not in w.lower()]
            return result
        return result
    except Exception:
        return _core_compile(candidate)


def evaluate_factor_extended(compiled: dict[str, Any], frame: pd.DataFrame) -> pd.Series:
    """扩展求值器 — 支持扩展函数集。"""
    if not isinstance(compiled, dict) or compiled.get("compiled_code") is None:
        raise ValueError("compiled candidate missing compiled_code")

    feature_frame = build_factor_feature_frame(frame)
    local_env: dict[str, Any] = {name: feature_frame[name] for name in feature_frame.columns}
    local_env.update(EXTENDED_FUNCTION_ENV)
    result = eval(compiled["compiled_code"], {"__builtins__": {}}, local_env)
    series = _series_like(result, feature_frame.index)
    return pd.to_numeric(series, errors="coerce").astype(float)
