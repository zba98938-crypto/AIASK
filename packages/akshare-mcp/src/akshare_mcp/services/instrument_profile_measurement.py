"""Measure realized instrument-profile metrics from daily K-line history.

P0-b: 单标的趋势策略的 formal readiness 需要 measured instrument_profile,否则
``measurement_source`` 恒为 ``default_board_profile``,触发
``default_profile_not_allowed_for_single_name_runtime`` 阻塞。生成端从不产出 realized
指标,本模块用真实日线计算它们,输出一个 ``source_symbol_summary`` 字典,键名严格匹配
``strategy_spec.normalizers._normalize_instrument_profile`` 的 ``measured_keys``,使
``_resolve_profile_metric`` 把它们识别为 measured 而非 default。

纯函数实现,不访问网络,输入为已取好的日线列表,便于单测。
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any, Mapping, Optional, Sequence

# 至少需要这么多根日线才认为测量可信(覆盖 ATR14 + 60d 趋势效率窗口)。
_MIN_BARS = 70
# 与 normalizers._normalize_instrument_profile 的 clip 边界保持一致,
# 超界值会被 clip,这里不强行裁剪,交给下游统一处理;但低于地板的样本量直接放弃测量。

_REQUIRED_MEASURED_KEYS = (
    "annual_volatility_realized_252d",
    "atr14_pct_realized",
    "gap_p95_realized",
    "intraday_range_p90",
    "trend_efficiency_60d_realized",
    "volume_ratio_p80",
    "volume_ratio_p90",
    "turnover_rate_p80",
    "turnover_rate_p90",
)


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _percentile(sorted_values: Sequence[float], pct: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct * (len(sorted_values) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return sorted_values[low]
    frac = rank - low
    return sorted_values[low] * (1.0 - frac) + sorted_values[high] * frac


def _rolling_mean(values: Sequence[float], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    running = 0.0
    for idx, value in enumerate(values):
        running += value
        if idx >= window:
            running -= values[idx - window]
        if idx >= window - 1:
            out.append(running / window)
        else:
            out.append(None)
    return out


def measure_instrument_profile(
    klines: Sequence[Mapping[str, Any]],
    *,
    min_bars: int = _MIN_BARS,
) -> dict[str, Any]:
    """从日线序列计算 realized instrument-profile 指标。

    Args:
        klines: 升序日线列表,每根含 open/high/low/close/volume(可选 turnover)。
        min_bars: 触发可信测量所需的最小根数。

    Returns:
        source_symbol_summary dict。当样本不足或数据异常时返回
        ``{"measured": False, "reason": ...}``,不含 realized 指标,下游会维持 default。
    """
    rows = [dict(item) for item in (klines or [])]
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    opens: list[float] = []
    volumes: list[float] = []
    turnovers: list[float] = []
    for row in rows:
        close = _to_float(row.get("close"))
        high = _to_float(row.get("high"))
        low = _to_float(row.get("low"))
        open_ = _to_float(row.get("open"))
        if close is None or close <= 0 or high is None or low is None or open_ is None:
            continue
        closes.append(close)
        highs.append(high)
        lows.append(low)
        opens.append(open_)
        volumes.append(_to_float(row.get("volume")) or 0.0)
        turnover_val = _to_float(row.get("turnover"))
        turnovers.append(turnover_val if turnover_val is not None else 0.0)

    n = len(closes)
    if n < max(min_bars, 21):
        return {"measured": False, "reason": f"insufficient_bars:{n}<{max(min_bars, 21)}"}

    # 日收益率
    daily_returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, n)]
    if not daily_returns:
        return {"measured": False, "reason": "no_returns"}

    # annual volatility (252d)
    mean_ret = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean_ret) ** 2 for r in daily_returns) / max(1, len(daily_returns) - 1)
    annual_vol = math.sqrt(var) * math.sqrt(252.0)

    # ATR14 (% of close): true range / close, 取最近窗口均值
    true_ranges: list[float] = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr / closes[i] if closes[i] > 0 else 0.0)
    atr_window = true_ranges[-14:] if len(true_ranges) >= 14 else true_ranges
    atr14_pct = sum(atr_window) / len(atr_window) if atr_window else 0.0

    # gap p95: |open_t / close_{t-1} - 1|
    gaps = [abs(opens[i] / closes[i - 1] - 1.0) for i in range(1, n) if closes[i - 1] > 0]
    gap_p95 = _percentile(sorted(gaps), 0.95) or 0.0

    # intraday range p90: (high - low) / close
    intraday = [(highs[i] - lows[i]) / closes[i] for i in range(n) if closes[i] > 0]
    intraday_range_p90 = _percentile(sorted(intraday), 0.90) or 0.0

    # trend efficiency 60d: |net move| / sum(|daily moves|) 取最近 60 根
    window = closes[-60:] if n >= 60 else closes
    if len(window) >= 2:
        net = abs(window[-1] - window[0])
        path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
        trend_efficiency = (net / path) if path > 0 else 0.0
    else:
        trend_efficiency = 0.0

    # volume ratio: volume / MA20(volume)
    vol_ma20 = _rolling_mean(volumes, 20)
    vol_ratios = [
        volumes[i] / vol_ma20[i]
        for i in range(n)
        if vol_ma20[i] not in (None, 0.0) and vol_ma20[i]
    ]
    vol_ratios_sorted = sorted(vol_ratios)
    volume_ratio_p80 = _percentile(vol_ratios_sorted, 0.80) or 1.0
    volume_ratio_p90 = _percentile(vol_ratios_sorted, 0.90) or 1.0

    # turnover rate: 若有 turnover 列用它的 MA20 比率,否则用 volume ratio 代理
    has_turnover = any(t > 0 for t in turnovers)
    if has_turnover:
        tn_ma20 = _rolling_mean(turnovers, 20)
        tn_ratios = [
            turnovers[i] / tn_ma20[i]
            for i in range(n)
            if tn_ma20[i] not in (None, 0.0) and tn_ma20[i]
        ]
        tn_sorted = sorted(tn_ratios)
        turnover_rate_p80 = _percentile(tn_sorted, 0.80) or 1.0
        turnover_rate_p90 = _percentile(tn_sorted, 0.90) or 1.0
        turnover_median = median(tn_ratios) if tn_ratios else 1.0
    else:
        turnover_rate_p80 = volume_ratio_p80
        turnover_rate_p90 = volume_ratio_p90
        turnover_median = median(vol_ratios) if vol_ratios else 1.0

    summary = {
        "measured": True,
        "bars": n,
        "annual_volatility_realized_252d": round(annual_vol, 6),
        "atr14_pct_realized": round(atr14_pct, 6),
        "gap_p95_realized": round(gap_p95, 6),
        "intraday_range_p90": round(intraday_range_p90, 6),
        "trend_efficiency_60d_realized": round(trend_efficiency, 6),
        "volume_ratio_p80": round(volume_ratio_p80, 6),
        "volume_ratio_p90": round(volume_ratio_p90, 6),
        "turnover_rate_p80": round(turnover_rate_p80, 6),
        "turnover_rate_p90": round(turnover_rate_p90, 6),
        "turnover_median_realized": round(turnover_median, 6),
    }
    # 任何 realized 指标算出非正值(异常)时,标记不完整,避免污染 measured 判定。
    incomplete = [
        key for key in _REQUIRED_MEASURED_KEYS
        if (_to_float(summary.get(key)) or 0.0) <= 0.0
    ]
    if incomplete:
        summary["measured"] = False
        summary["reason"] = f"non_positive_metrics:{','.join(incomplete)}"
    return summary


async def measure_instrument_profile_from_db(
    db: Any,
    code: str,
    *,
    lookback_bars: int = 260,
    min_bars: int = _MIN_BARS,
) -> dict[str, Any]:
    """从 db.get_klines 拉取最近日线并测量。失败/不足时返回 measured=False。"""
    getter = getattr(db, "get_klines", None)
    if getter is None:
        return {"measured": False, "reason": "db_missing_get_klines"}
    try:
        rows = await getter(code, limit=int(lookback_bars))
    except Exception as exc:  # noqa: BLE001 - 数据不可用不得阻断
        return {"measured": False, "reason": f"kline_fetch_failed:{type(exc).__name__}"}
    return measure_instrument_profile(rows or [], min_bars=min_bars)
