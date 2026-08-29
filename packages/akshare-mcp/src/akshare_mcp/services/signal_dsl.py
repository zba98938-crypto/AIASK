"""可解释信号 DSL（P1）：统一可序列化信号表达层 + 求值引擎。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def build_signal_definition(strategy: str, params: dict | None = None) -> dict:
    """构建统一 DSL schema，用于 backtest/execution/performance 透传与审计。"""
    params = params or {}
    s = str(strategy or "").strip().lower() or "unknown"

    if s == "ma_cross":
        expr = {
            "operator": "cross_over",
            "left": {"indicator": "MA", "period": _to_int(params.get("short_period", 5), 5)},
            "right": {"indicator": "MA", "period": _to_int(params.get("long_period", 20), 20)},
            "exit": {
                "operator": "cross_under",
                "left": {"indicator": "MA", "period": _to_int(params.get("short_period", 5), 5)},
                "right": {"indicator": "MA", "period": _to_int(params.get("long_period", 20), 20)},
            },
        }
    elif s == "momentum":
        expr = {
            "operator": "gt",
            "left": {"indicator": "ROC", "period": _to_int(params.get("lookback", 20), 20)},
            "right": {"const": float(params.get("threshold", 0.02) or 0.02)},
            "exit": {
                "operator": "lt",
                "left": {"indicator": "ROC", "period": _to_int(params.get("lookback", 20), 20)},
                "right": {"const": -float(params.get("threshold", 0.02) or 0.02)},
            },
        }
    elif s == "rsi":
        expr = {
            "operator": "lt",
            "left": {"indicator": "RSI", "period": _to_int(params.get("rsi_period", 14), 14)},
            "right": {"const": float(params.get("oversold", 30) or 30)},
            "exit": {
                "operator": "gt",
                "left": {"indicator": "RSI", "period": _to_int(params.get("rsi_period", 14), 14)},
                "right": {"const": float(params.get("overbought", 70) or 70)},
            },
        }
    elif s == "buy_and_hold":
        expr = {
            "operator": "always_true",
            "left": {"const": True},
            "right": {"const": True},
            "exit": {"operator": "end_of_window"},
        }
    else:
        expr = {
            "operator": "custom",
            "left": {"const": True},
            "right": {"const": True},
            "exit": {"operator": "custom"},
        }

    return {
        "schema_version": "signal_dsl_v1",
        "strategy": s,
        "engine": "rule_based",
        "expression": expr,
        "params_snapshot": dict(params),
        "created_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# 信号求值引擎
# ---------------------------------------------------------------------------

def _compute_indicator(node: dict, close: pd.Series) -> pd.Series:
    """根据 DSL 节点计算技术指标，返回与 close 等长的 Series。"""
    if "const" in node:
        return pd.Series(node["const"], index=close.index, dtype=float)

    ind = str(node.get("indicator", "")).upper()
    period = _to_int(node.get("period", 14), 14)

    if ind == "MA":
        return close.rolling(window=period, min_periods=period).mean()
    elif ind == "ROC":
        shifted = close.shift(period)
        return (close - shifted) / shifted.replace(0, np.nan)
    elif ind == "RSI":
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100.0 - (100.0 / (1.0 + rs))
    else:
        return pd.Series(np.nan, index=close.index, dtype=float)


def _eval_operator(op: str, left: pd.Series, right: pd.Series) -> pd.Series:
    """对两条指标序列执行比较/交叉运算，返回布尔 Series。"""
    if op == "gt":
        return left > right
    elif op == "lt":
        return left < right
    elif op == "gte":
        return left >= right
    elif op == "lte":
        return left <= right
    elif op == "cross_over":
        prev_left, prev_right = left.shift(1), right.shift(1)
        return (prev_left <= prev_right) & (left > right)
    elif op == "cross_under":
        prev_left, prev_right = left.shift(1), right.shift(1)
        return (prev_left >= prev_right) & (left < right)
    elif op == "always_true":
        return pd.Series(True, index=left.index, dtype=bool)
    elif op == "end_of_window":
        s = pd.Series(False, index=left.index, dtype=bool)
        if len(s) > 0:
            s.iloc[-1] = True
        return s
    else:
        # custom / unknown → 全 False
        return pd.Series(False, index=left.index, dtype=bool)


def _eval_expression(expr: dict, close: pd.Series) -> pd.Series:
    """递归求值单个 expression 节点。"""
    op = str(expr.get("operator", "custom"))
    left = _compute_indicator(expr.get("left", {"const": True}), close)
    right = _compute_indicator(expr.get("right", {"const": True}), close)
    return _eval_operator(op, left, right)


def evaluate_signal(
    signal_def: dict,
    df: pd.DataFrame,
    *,
    close_col: str = "close",
) -> dict[str, pd.Series]:
    """对 DataFrame 执行信号求值，返回 entry / exit 布尔 Series。

    Parameters
    ----------
    signal_def : dict
        ``build_signal_definition()`` 的返回值（或其 ``expression`` 子字典）。
    df : pd.DataFrame
        至少包含 ``close_col`` 列的 OHLCV 数据。
    close_col : str
        收盘价列名，默认 ``"close"``。

    Returns
    -------
    dict with keys ``"entry"`` and ``"exit"``，值为与 df 等长的布尔 Series。
    """
    expr = signal_def.get("expression", signal_def)
    close = df[close_col].astype(float)

    entry = _eval_expression(expr, close).fillna(False).astype(bool)

    exit_expr = expr.get("exit")
    if exit_expr and isinstance(exit_expr, dict):
        exit_sig = _eval_expression(exit_expr, close).fillna(False).astype(bool)
    else:
        exit_sig = pd.Series(False, index=df.index, dtype=bool)

    return {"entry": entry, "exit": exit_sig}

