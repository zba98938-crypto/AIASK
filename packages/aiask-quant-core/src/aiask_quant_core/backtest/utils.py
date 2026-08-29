"""回测工具函数"""

from typing import List, Dict, Any, Optional, Union
import numpy as np
from aiask_quant_core.slippage import SlippageCalculator, SlippageModelType


def _ensure_dict_list(klines: List[Union[Dict, Any]]) -> List[Dict[str, Any]]:
    """确保 K线数据是字典列表"""
    result = []
    for k in klines:
        if isinstance(k, dict):
            result.append(k)
        elif hasattr(k, 'model_dump'):
            result.append(k.model_dump())
        elif hasattr(k, '__dict__'):
            result.append({
                attr: getattr(k, attr)
                for attr in ('date', 'open', 'high', 'low', 'close', 'volume', 'amount')
                if hasattr(k, attr)
            })
        else:
            result.append(dict(k))
    return result


def normalize_trade_date(value: Any) -> str:
    """Normalize heterogeneous date labels to ``YYYYMMDD`` for calendar joins.

    Accepts ``YYYYMMDD``, ``YYYY-MM-DD``, and ISO datetime prefixes. Returns
    empty string when the value cannot be interpreted as a calendar day.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    # ISO datetime / date with separators
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return ""


def align_klines_by_trade_date(
    series_map: Dict[str, List[Union[Dict, Any]]],
) -> tuple[list[str], Dict[str, List[Dict[str, Any]]]]:
    """Align multiple kline series on the intersection of trade dates.

    Returns ``(common_dates_sorted, {code: rows_on_common_calendar})``.
    Empty common calendar yields ``([], {})``.
    """
    rows_by_code_date: Dict[str, Dict[str, Dict[str, Any]]] = {}
    date_sets: list[set[str]] = []
    for raw_code, raw_rows in dict(series_map or {}).items():
        code = str(raw_code or "").strip()
        if not code or not raw_rows:
            continue
        by_date: Dict[str, Dict[str, Any]] = {}
        for row in _ensure_dict_list(list(raw_rows)):
            if not isinstance(row, dict):
                continue
            day = normalize_trade_date(
                row.get("date")
                or row.get("trade_date")
                or row.get("time")
                or row.get("datetime")
            )
            if not day:
                continue
            by_date[day] = row
        if by_date:
            rows_by_code_date[code] = by_date
            date_sets.append(set(by_date.keys()))
    if len(rows_by_code_date) < 1 or not date_sets:
        return [], {}
    common = sorted(set.intersection(*date_sets)) if len(date_sets) > 1 else sorted(date_sets[0])
    if not common:
        return [], {}
    aligned: Dict[str, List[Dict[str, Any]]] = {
        code: [by_date[day] for day in common]
        for code, by_date in rows_by_code_date.items()
    }
    return common, aligned


def _resolve_slippage_model(model_value: Any) -> SlippageModelType:
    """解析滑点模型类型"""
    if isinstance(model_value, SlippageModelType):
        return model_value
    if isinstance(model_value, str):
        normalized = model_value.strip().lower()
        for model in SlippageModelType:
            if model.value == normalized:
                return model
    return SlippageModelType.FIXED


def _compute_slippage_rate(
    prices: np.ndarray,
    volumes: Optional[np.ndarray],
    params: Dict[str, Any],
    default_rate: float = 0.0
) -> float:
    """根据参数计算滑点率"""
    if params is None:
        return default_rate

    if 'slippage_rate' in params and params['slippage_rate'] is not None:
        return float(params['slippage_rate'])

    model_value = params.get('slippage_model')
    if not model_value:
        return default_rate

    model_type = _resolve_slippage_model(model_value)
    calculator = SlippageCalculator(model_type=model_type)

    price = float(np.nanmean(prices)) if prices is not None and len(prices) > 0 else 0.0
    volume = 0.0
    if volumes is not None and len(volumes) > 0:
        volume = float(np.nanmean(volumes))

    if price <= 0:
        return default_rate

    order_size = params.get('order_size')
    if order_size is None:
        order_size = params.get('initial_capital', 100000) / price

    result = calculator.calculate(price, volume, float(order_size), True)
    return abs(float(result.get('slippage_rate', default_rate)))
