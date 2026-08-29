"""因子候选编译器：DSL 白名单解析 + 安全求值。"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .factor_llm_provider import validate_factor_generation_payload

SUPPORTED_FACTOR_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "returns_1d",
    "return_5d",
    "return_20d",
    "momentum_20d",
    "momentum_60d",
    "volatility_20d",
    "volume_ratio_5_20",
}

SUPPORTED_FACTOR_FUNCTIONS = {
    "abs",
    "clip",
    "delta",
    "delay",
    "log1p",
    "max",
    "min",
    "rank",
    "sign",
    "ts_mean",
    "ts_rank",
    "ts_std",
    "zscore",
}

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)
_ALLOWED_NODE_TYPES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
)


@dataclass
class _CompilerStats:
    node_count: int = 0
    max_depth: int = 0
    call_count: int = 0
    binary_op_count: int = 0
    unary_op_count: int = 0


def _series_like(value: Any, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        return value.astype(float)
    if np.isscalar(value):
        return pd.Series(float(value), index=index, dtype=float)
    arr = np.asarray(value, dtype=float)
    if arr.ndim == 0:
        return pd.Series(float(arr), index=index, dtype=float)
    if len(arr) != len(index):
        raise ValueError("series length mismatch")
    return pd.Series(arr, index=index, dtype=float)


def _safe_window(value: Any, default: int = 20) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    return max(1, min(out, 252))


def build_factor_feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """构建编译器可用的标准字段帧。"""

    base = pd.DataFrame(index=frame.index)
    for field in ("open", "high", "low", "close", "volume", "amount"):
        series = pd.to_numeric(frame.get(field), errors="coerce") if field in frame else None
        if series is None:
            series = pd.Series(np.nan, index=frame.index, dtype=float)
        base[field] = series.astype(float)

    close = base["close"].replace(0.0, np.nan)
    returns_1d = close.pct_change()
    base["returns_1d"] = returns_1d
    base["return_5d"] = close.pct_change(5)
    base["return_20d"] = close.pct_change(20)
    base["momentum_20d"] = base["return_20d"]
    base["momentum_60d"] = close.pct_change(60)
    base["volatility_20d"] = returns_1d.rolling(20, min_periods=20).std() * np.sqrt(252)

    vol = base["volume"].replace(0.0, np.nan)
    vol5 = vol.rolling(5, min_periods=5).mean()
    vol20 = vol.rolling(20, min_periods=20).mean()
    base["volume_ratio_5_20"] = vol5 / vol20
    return base


def _fn_abs(value: Any) -> Any:
    return np.abs(value)


def _fn_sign(value: Any) -> Any:
    return np.sign(value)


def _fn_log1p(value: Any) -> Any:
    series = value if isinstance(value, pd.Series) else np.asarray(value, dtype=float)
    return np.log1p(np.clip(series, a_min=-0.999999, a_max=None))


def _fn_delay(series: Any, periods: Any = 1) -> pd.Series:
    periods = _safe_window(periods, 1)
    return pd.Series(series).astype(float).shift(periods)


def _fn_delta(series: Any, periods: Any = 1) -> pd.Series:
    periods = _safe_window(periods, 1)
    s = pd.Series(series).astype(float)
    return s - s.shift(periods)


def _fn_ts_mean(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    return pd.Series(series).astype(float).rolling(window, min_periods=window).mean()


def _fn_ts_std(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    return pd.Series(series).astype(float).rolling(window, min_periods=window).std()


def _fn_rank(series: Any) -> pd.Series:
    s = pd.Series(series).astype(float)
    return s.rank(pct=True)


def _rolling_rank(values: pd.Series) -> float:
    ranked = values.rank(pct=True)
    if ranked.empty:
        return np.nan
    return float(ranked.iloc[-1])


def _fn_ts_rank(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    s = pd.Series(series).astype(float)
    return s.rolling(window, min_periods=window).apply(_rolling_rank, raw=False)


def _fn_zscore(series: Any, window: Any = 20) -> pd.Series:
    window = _safe_window(window, 20)
    s = pd.Series(series).astype(float)
    mean = s.rolling(window, min_periods=window).mean()
    std = s.rolling(window, min_periods=window).std().replace(0.0, np.nan)
    return (s - mean) / std


def _fn_clip(value: Any, lo: Any, hi: Any) -> Any:
    return np.clip(value, float(lo), float(hi))


def _fn_min(left: Any, right: Any) -> Any:
    return np.minimum(left, right)


def _fn_max(left: Any, right: Any) -> Any:
    return np.maximum(left, right)


_SAFE_FUNCTION_ENV = {
    "abs": _fn_abs,
    "clip": _fn_clip,
    "delta": _fn_delta,
    "delay": _fn_delay,
    "log1p": _fn_log1p,
    "max": _fn_max,
    "min": _fn_min,
    "rank": _fn_rank,
    "sign": _fn_sign,
    "ts_mean": _fn_ts_mean,
    "ts_rank": _fn_ts_rank,
    "ts_std": _fn_ts_std,
    "zscore": _fn_zscore,
}


def _validate_node(
    node: ast.AST,
    *,
    stats: _CompilerStats,
    fields: set[str],
    calls: set[str],
    unsupported_fields: set[str],
    unsupported_functions: set[str],
    depth: int = 0,
) -> None:
    stats.node_count += 1
    stats.max_depth = max(stats.max_depth, depth)

    if not isinstance(node, _ALLOWED_NODE_TYPES):
        raise ValueError(f"unsupported AST node: {type(node).__name__}")

    if isinstance(node, ast.Expression):
        _validate_node(
            node.body,
            stats=stats,
            fields=fields,
            calls=calls,
            unsupported_fields=unsupported_fields,
            unsupported_functions=unsupported_functions,
            depth=depth + 1,
        )
        return

    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise ValueError(f"unsupported binary operator: {type(node.op).__name__}")
        stats.binary_op_count += 1
        _validate_node(node.left, stats=stats, fields=fields, calls=calls, unsupported_fields=unsupported_fields, unsupported_functions=unsupported_functions, depth=depth + 1)
        _validate_node(node.right, stats=stats, fields=fields, calls=calls, unsupported_fields=unsupported_fields, unsupported_functions=unsupported_functions, depth=depth + 1)
        return

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARYOPS):
            raise ValueError(f"unsupported unary operator: {type(node.op).__name__}")
        stats.unary_op_count += 1
        _validate_node(node.operand, stats=stats, fields=fields, calls=calls, unsupported_fields=unsupported_fields, unsupported_functions=unsupported_functions, depth=depth + 1)
        return

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("only simple function names are allowed")
        fn_name = str(node.func.id or "").strip()
        stats.call_count += 1
        calls.add(fn_name)
        if fn_name not in SUPPORTED_FACTOR_FUNCTIONS:
            unsupported_functions.add(fn_name)
        for arg in node.args:
            _validate_node(arg, stats=stats, fields=fields, calls=calls, unsupported_fields=unsupported_fields, unsupported_functions=unsupported_functions, depth=depth + 1)
        for kw in node.keywords:
            _validate_node(kw.value, stats=stats, fields=fields, calls=calls, unsupported_fields=unsupported_fields, unsupported_functions=unsupported_functions, depth=depth + 1)
        return

    if isinstance(node, ast.Name):
        name = str(node.id or "").strip()
        if name not in SUPPORTED_FACTOR_FIELDS and name not in SUPPORTED_FACTOR_FUNCTIONS:
            unsupported_fields.add(name)
        if name in SUPPORTED_FACTOR_FIELDS:
            fields.add(name)
        return

    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise ValueError("only numeric constants are allowed")


def compile_factor_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """编译并校验候选因子。"""

    normalized = validate_factor_generation_payload({"candidates": [candidate]})
    candidate_payload = dict((normalized.get("candidates") or [])[0] or {})
    expression = str(candidate_payload.get("expression_dsl") or "").strip()
    if not expression:
        raise ValueError("expression_dsl is required")

    tree = ast.parse(expression, mode="eval")
    stats = _CompilerStats()
    fields: set[str] = set()
    calls: set[str] = set()
    unsupported_fields: set[str] = set()
    unsupported_functions: set[str] = set()

    _validate_node(
        tree,
        stats=stats,
        fields=fields,
        calls=calls,
        unsupported_fields=unsupported_fields,
        unsupported_functions=unsupported_functions,
        depth=0,
    )

    complexity_score = int(
        stats.node_count
        + stats.call_count * 2
        + stats.binary_op_count
        + stats.unary_op_count
        + stats.max_depth * 2
    )
    declared_inputs = [str(item).strip() for item in list(candidate_payload.get("inputs") or []) if str(item).strip()]
    missing_declared_inputs = [item for item in declared_inputs if item not in fields]
    undeclared_fields = [item for item in sorted(fields) if item not in declared_inputs]
    warnings: list[str] = []
    if missing_declared_inputs:
        warnings.append(f"declared_inputs_unused={missing_declared_inputs}")
    if undeclared_fields:
        warnings.append(f"undeclared_fields_detected={undeclared_fields}")

    code = compile(tree, filename="<factor_candidate>", mode="eval")
    return {
        "candidate": candidate_payload,
        "compiled_code": code,
        "expression_ast": ast.dump(tree, include_attributes=False),
        "referenced_fields": sorted(fields),
        "function_calls": sorted(calls),
        "unsupported_fields": sorted(unsupported_fields),
        "unsupported_functions": sorted(unsupported_functions),
        "declared_inputs": declared_inputs,
        "complexity": {
            "score": complexity_score,
            "node_count": stats.node_count,
            "max_depth": stats.max_depth,
            "call_count": stats.call_count,
            "binary_op_count": stats.binary_op_count,
            "unary_op_count": stats.unary_op_count,
        },
        "warnings": warnings,
        "valid": not unsupported_fields and not unsupported_functions and complexity_score <= 80,
        "degraded": bool(unsupported_fields or unsupported_functions or complexity_score > 80),
    }


def evaluate_compiled_factor(compiled: dict[str, Any], frame: pd.DataFrame) -> pd.Series:
    """对标准行情帧执行已编译候选因子。"""

    if not isinstance(compiled, dict) or compiled.get("compiled_code") is None:
        raise ValueError("compiled candidate missing compiled_code")

    feature_frame = build_factor_feature_frame(frame)
    local_env: dict[str, Any] = {name: feature_frame[name] for name in feature_frame.columns}
    local_env.update(_SAFE_FUNCTION_ENV)
    result = eval(compiled["compiled_code"], {"__builtins__": {}}, local_env)
    series = _series_like(result, feature_frame.index)
    return pd.to_numeric(series, errors="coerce").astype(float)
