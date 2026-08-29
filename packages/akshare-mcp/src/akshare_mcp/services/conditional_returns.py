"""条件收益统计服务。"""

from __future__ import annotations

from typing import Any

import numpy as np

from .factor_calculator import factor_calculator
from .screen_engine import engine as screen_engine
from .technical_analysis import TechnicalAnalysis
from . import screen_conditions as _screen_conditions  # noqa: F401


# FIX-6: 显式声明 _declared_condition_value 能识别的直读字段集合与前缀，
# 用于在工具层对 dict 形式条件做 field/op 校验，避免「未识别字段静默返回 0 匹配」。
_DECLARED_SCALAR_FIELDS = {
    "open", "high", "low", "close", "volume", "amount", "turnover",
    "pct_change", "change_pct", "return_1d", "volume_ratio",
}
_DECLARED_FIELD_PREFIXES = ("ma_", "ema_", "rsi_", "roc_")
_SUPPORTED_OPS = {
    ">", "gt", ">=", "gte", "<", "lt", "<=", "lte",
    "!=", "<>", "ne", "==", "=", "eq",
}


def _is_supported_declared_field(field: str) -> bool:
    """判断字段是否能被 _declared_condition_value 识别（含 ma_/ema_/rsi_/roc_ 前缀）。"""
    key = str(field or "").strip().lower()
    if not key:
        return False
    if key in _DECLARED_SCALAR_FIELDS:
        return True
    for prefix in _DECLARED_FIELD_PREFIXES:
        if key.startswith(prefix):
            suffix = key[len(prefix):]
            return suffix.isdigit() and int(suffix) > 0
    return False


def validate_conditions(conditions: Any, logic: str = "AND") -> tuple[bool, str | None]:
    """校验条件中的 field / op 合法性（FIX-6）。

    仅校验 dict 形式且带 `field` 的「直读条件」；带 `id` 的 screen_engine 条件
    与纯字符串条件交由引擎自行处理，不在此拦截。

    Returns:
        (ok, error_msg)。ok=False 时 error_msg 含支持列表，供 AI 区分
        「真无匹配」与「字段/运算符拼错」。
    """
    items = _normalize_conditions(conditions)
    for cond in items:
        # 引擎条件（id / 字符串）跳过，由 screen_engine 处理
        if isinstance(cond, str):
            continue
        if isinstance(cond, dict) and cond.get("id") and cond.get("field") is None:
            continue
        if not isinstance(cond, dict):
            return False, f"条件必须为对象（含 field/op/value），收到: {type(cond).__name__}"
        field = cond.get("field")
        if field is None:
            continue
        if not _is_supported_declared_field(str(field)):
            supported = sorted(_DECLARED_SCALAR_FIELDS) + [f"{p}<N>" for p in _DECLARED_FIELD_PREFIXES]
            return False, (
                f"未识别的 field='{field}'。支持的直读字段: {', '.join(supported)}"
                f"（如需技术形态/选股条件请用带 id 的条件）"
            )
        op = cond.get("op")
        if op is not None and str(op).strip().lower() not in _SUPPORTED_OPS:
            return False, (
                f"非法的 op='{op}'（field='{field}'）。支持的运算符: "
                f"{', '.join(sorted({'>', '>=', '<', '<=', '==', '!='}))}"
            )
    return True, None


def _normalize_klines(klines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [dict(k) for k in (klines or []) if isinstance(k, dict) and k.get("close") is not None]
    if len(items) < 2:
        return items
    first = str(items[0].get("date") or items[0].get("time") or "")
    last = str(items[-1].get("date") or items[-1].get("time") or "")
    if first and last and first > last:
        items.reverse()
    return items


def _normalize_conditions(conditions: Any) -> list[Any]:
    if conditions is None:
        return []
    if isinstance(conditions, str):
        return [conditions]
    if isinstance(conditions, dict):
        if isinstance(conditions.get("conditions"), list):
            return list(conditions.get("conditions") or [])
        if isinstance(conditions.get("items"), list):
            return list(conditions.get("items") or [])
        if conditions.get("id"):
            return [conditions]
        return []
    if isinstance(conditions, (list, tuple)):
        return list(conditions)
    return []


def _safe_series_value(series: list[float], idx: int) -> float | None:
    if idx < 0 or idx >= len(series):
        return None
    value = series[idx]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None
    return float(value)


def _compare(left: float | None, op: str, right: Any) -> bool:
    if left is None:
        return False
    try:
        right_value = float(right)
    except (TypeError, ValueError):
        return False

    operator = str(op or "==").strip().lower()
    if operator in {">", "gt"}:
        return left > right_value
    if operator in {">=", "gte"}:
        return left >= right_value
    if operator in {"<", "lt"}:
        return left < right_value
    if operator in {"<=", "lte"}:
        return left <= right_value
    if operator in {"!=", "<>", "ne"}:
        return left != right_value
    return left == right_value


def _declared_condition_value(window: list[dict[str, Any]], field: str) -> float | None:
    if not window:
        return None

    latest = window[-1]
    field_key = str(field or "").strip().lower()
    if not field_key:
        return None

    if field_key in {"open", "high", "low", "close", "volume", "amount", "turnover"}:
        raw = latest.get(field_key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return float(value) if np.isfinite(value) else None

    closes = [float(row.get("close", 0) or 0) for row in window]
    volumes = [float(row.get("volume", 0) or 0) for row in window]
    idx = len(window) - 1

    if field_key in {"pct_change", "change_pct", "return_1d"}:
        current = latest.get("change_pct")
        if current is not None:
            try:
                value = float(current)
            except (TypeError, ValueError):
                value = None
            if value is not None and np.isfinite(value):
                return float(value)
        if len(closes) < 2 or closes[-2] <= 0:
            return None
        return float((closes[-1] - closes[-2]) / closes[-2] * 100.0)

    if field_key == "volume_ratio":
        if len(volumes) < 6:
            return None
        avg_prev_5 = float(np.mean(volumes[-6:-1]))
        if avg_prev_5 <= 0:
            return None
        return float(volumes[-1] / avg_prev_5)

    if field_key.startswith("ma_"):
        try:
            period = int(field_key.split("_", 1)[1])
        except (IndexError, ValueError):
            return None
        return _safe_series_value(TechnicalAnalysis.calculate_sma(closes, period), idx)

    if field_key.startswith("ema_"):
        try:
            period = int(field_key.split("_", 1)[1])
        except (IndexError, ValueError):
            return None
        return _safe_series_value(TechnicalAnalysis.calculate_ema(closes, period), idx)

    if field_key.startswith("rsi_"):
        try:
            period = int(field_key.split("_", 1)[1])
        except (IndexError, ValueError):
            return None
        series = factor_calculator.calculate_rsi(closes, period=period, as_series=True)
        arr = np.asarray(series, dtype=np.float64)
        if arr.size == 0:
            return None
        padded = np.full(len(closes), np.nan, dtype=np.float64)
        usable = min(arr.size, max(0, len(closes) - 1))
        if usable > 0:
            padded[-usable:] = arr[-usable:]
        value = padded[idx]
        return float(value) if np.isfinite(value) else None

    if field_key.startswith("roc_"):
        try:
            period = int(field_key.split("_", 1)[1])
        except (IndexError, ValueError):
            return None
        value = factor_calculator.calculate_roc(closes, period=period)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return float(value) if np.isfinite(value) else None

    return None


def _evaluate_conditions(window: list[dict[str, Any]], conditions: list[Any], logic: str) -> dict[str, Any]:
    details: dict[str, bool] = {}
    results: list[bool] = []
    deferred_engine_conditions: list[Any] = []

    for index, condition in enumerate(conditions):
        if isinstance(condition, dict) and condition.get("id"):
            deferred_engine_conditions.append(condition)
            continue
        if isinstance(condition, str):
            deferred_engine_conditions.append(condition)
            continue

        cond_key = f"condition_{index + 1}"
        field = condition.get("field") if isinstance(condition, dict) else None
        op = condition.get("op") if isinstance(condition, dict) else None
        value = condition.get("value") if isinstance(condition, dict) else None
        if field:
            cond_key = str(field)
        match = _compare(_declared_condition_value(window, str(field or "")), str(op or "=="), value)
        details[cond_key] = bool(match)
        results.append(bool(match))

    if deferred_engine_conditions:
        engine_result = screen_engine.evaluate_multi(deferred_engine_conditions, window, logic="AND")
        engine_details = engine_result.get("details", {}) if isinstance(engine_result, dict) else {}
        if isinstance(engine_details, dict):
            details.update(engine_details)
            results.extend(bool(v) for v in engine_details.values())

    if not results:
        return {"match": False, "details": details}

    logic_value = str(logic or "AND").upper()
    matched = all(results) if logic_value == "AND" else any(results)
    return {"match": matched, "details": details}


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "win_rate": None,
            "std": None,
            "worst": None,
            "best": None,
        }
    arr = np.asarray(values, dtype=float)
    return {
        "count": int(arr.size),
        "mean": round(float(np.mean(arr)), 6),
        "median": round(float(np.median(arr)), 6),
        "win_rate": round(float(np.mean(arr > 0)), 4),
        "std": round(float(np.std(arr)), 6),
        "worst": round(float(np.min(arr)), 6),
        "best": round(float(np.max(arr)), 6),
    }


def calculate_conditional_returns(
    klines: list[dict[str, Any]],
    conditions: Any,
    forward_days: list[int] | None = None,
    logic: str = "AND",
) -> dict[str, Any]:
    ordered = _normalize_klines(klines)
    normalized_conditions = _normalize_conditions(conditions)
    days = sorted({max(1, int(day)) for day in (forward_days or [5, 10, 20])})
    if not normalized_conditions:
        return {
            "condition_matches": 0,
            "evaluation_count": 0,
            "logic": str(logic or "AND").upper(),
            "conditions": [],
            "forward_returns": {f"{day}d": _stats([]) for day in days},
            "recent_matches": [],
        }

    per_day: dict[int, list[float]] = {day: [] for day in days}
    matches: list[dict[str, Any]] = []
    logic_value = str(logic or "AND").upper()

    for idx in range(len(ordered) - 1):
        window = ordered[: idx + 1]
        evaluated = _evaluate_conditions(window, normalized_conditions, logic=logic_value)
        if not evaluated.get("match"):
            continue
        current_close = float(window[-1].get("close") or 0)
        if current_close <= 0:
            continue

        one_match = {
            "date": str(window[-1].get("date") or ""),
            "details": evaluated.get("details", {}),
            "forward_returns": {},
        }
        any_forward = False
        for day in days:
            future_idx = idx + day
            if future_idx >= len(ordered):
                continue
            future_close = float(ordered[future_idx].get("close") or 0)
            if future_close <= 0:
                continue
            ret = (future_close - current_close) / current_close
            per_day[day].append(float(ret))
            one_match["forward_returns"][f"{day}d"] = round(float(ret), 6)
            any_forward = True

        if any_forward:
            matches.append(one_match)

    return {
        "condition_matches": len(matches),
        "evaluation_count": max(0, len(ordered) - max(days, default=1)),
        "logic": logic_value,
        "conditions": normalized_conditions,
        "forward_returns": {f"{day}d": _stats(per_day[day]) for day in days},
        "recent_matches": matches[-5:],
    }
