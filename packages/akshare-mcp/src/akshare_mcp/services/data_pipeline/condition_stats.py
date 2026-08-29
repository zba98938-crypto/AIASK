"""条件统计与信号命中率助手。"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from ..factor_calculator import factor_calculator


def normalize_klines(klines: Iterable[dict]) -> list[dict]:
    rows = list(klines or [])
    if len(rows) >= 2:
        first = str(rows[0].get("date") or rows[0].get("time") or "")
        last = str(rows[-1].get("date") or rows[-1].get("time") or "")
        if first > last:
            rows.reverse()
    return rows


def _sma(arr: np.ndarray, period: int) -> np.ndarray:
    """简单移动平均，返回与 arr 等长的序列（前 period-1 值为 NaN）。"""
    result = np.full(len(arr), np.nan)
    if len(arr) < period:
        return result
    cumsum = np.cumsum(arr)
    result[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
    return result


def _bollinger_bands(closes: np.ndarray, period: int = 20, std_mult: float = 2.0):
    """返回 (upper, lower)，等长序列。"""
    ma = _sma(closes, period)
    std = np.full(len(closes), np.nan)
    for i in range(period - 1, len(closes)):
        std[i] = np.std(closes[i - period + 1 : i + 1])
    upper = ma + std_mult * std
    lower = ma - std_mult * std
    return upper, lower


def _signal_indices(
    closes: np.ndarray,
    signal: str,
    signal_params: dict[str, Any] | None,
    *,
    klines: list[dict] | None = None,
) -> list[int]:
    params = signal_params or {}

    # ── 原有信号 ──
    if signal == "rsi_oversold":
        period = int(params.get("period", 14))
        threshold = float(params.get("threshold", 30))
        series = factor_calculator.calculate_rsi(closes.tolist(), period=period, as_series=True)
        offset = max(0, len(closes) - len(series))
        return [offset + i for i, value in enumerate(series) if np.isfinite(value) and float(value) <= threshold]

    if signal == "macd_golden_cross":
        series = factor_calculator.calculate_macd(closes.tolist(), as_series=True)
        return [i for i in range(1, len(series)) if np.isfinite(series[i - 1]) and np.isfinite(series[i]) and series[i - 1] <= 0 < series[i]]

    if signal == "ma_bullish_alignment":
        return [i for i in range(59, len(closes)) if closes[i] > np.mean(closes[i - 19:i + 1]) > np.mean(closes[i - 59:i + 1])]

    # ── 新增信号 ──
    if signal == "rsi_overbought":
        period = int(params.get("period", 14))
        threshold = float(params.get("threshold", 70))
        series = factor_calculator.calculate_rsi(closes.tolist(), period=period, as_series=True)
        offset = max(0, len(closes) - len(series))
        return [offset + i for i, value in enumerate(series) if np.isfinite(value) and float(value) >= threshold]

    if signal == "macd_death_cross":
        series = factor_calculator.calculate_macd(closes.tolist(), as_series=True)
        return [i for i in range(1, len(series)) if np.isfinite(series[i - 1]) and np.isfinite(series[i]) and series[i - 1] >= 0 > series[i]]

    if signal == "ma_death_cross":
        ma5 = _sma(closes, 5)
        ma20 = _sma(closes, 20)
        return [
            i for i in range(20, len(closes))
            if np.isfinite(ma5[i]) and np.isfinite(ma5[i - 1])
            and np.isfinite(ma20[i]) and np.isfinite(ma20[i - 1])
            and ma5[i - 1] >= ma20[i - 1] and ma5[i] < ma20[i]
        ]

    if signal == "volume_breakout":
        volumes = np.array(
            [float(k.get("volume", 0) or 0) for k in (klines or [])],
            dtype=np.float64,
        )
        if len(volumes) != len(closes):
            return []
        avg_vol_20 = _sma(volumes, 20)
        mult = float(params.get("multiplier", 2.0))
        return [
            i for i in range(20, len(volumes))
            if np.isfinite(avg_vol_20[i]) and avg_vol_20[i] > 0
            and volumes[i] >= avg_vol_20[i] * mult
        ]

    if signal == "bollinger_break_upper":
        upper, _ = _bollinger_bands(closes)
        return [i for i in range(20, len(closes)) if np.isfinite(upper[i]) and closes[i] > upper[i]]

    if signal == "bollinger_break_lower":
        _, lower = _bollinger_bands(closes)
        return [i for i in range(20, len(closes)) if np.isfinite(lower[i]) and closes[i] < lower[i]]

    if signal == "ma_bearish_alignment":
        return [i for i in range(59, len(closes)) if closes[i] < np.mean(closes[i - 19:i + 1]) < np.mean(closes[i - 59:i + 1])]

    # ── 组合信号 ──
    if signal == "rsi_oversold_and_macd_golden":
        rsi_idx = set(_signal_indices(closes, "rsi_oversold", params, klines=klines))
        macd_idx = set(_signal_indices(closes, "macd_golden_cross", params, klines=klines))
        window = int(params.get("combine_window", 3))
        combined = []
        for m in sorted(macd_idx):
            if any(r in rsi_idx for r in range(m - window, m + window + 1)):
                combined.append(m)
        return combined

    if signal == "volume_and_ma_bullish":
        vol_idx = set(_signal_indices(closes, "volume_breakout", params, klines=klines))
        ma_idx = set(_signal_indices(closes, "ma_bullish_alignment", params, klines=klines))
        return sorted(vol_idx & ma_idx)

    return []


def compute_signal_hit_rate(klines: Iterable[dict], signal: str, forward_days: list[int], signal_params: dict[str, Any] | None = None) -> dict:
    ordered = normalize_klines(klines)
    closes = np.array([float(row.get("close", 0) or 0) for row in ordered], dtype=np.float64)
    if len(closes) < 30:
        return {"signal": signal, "sample_count": 0, "forward_returns": {}, "by_regime": {}, "recent_signals": []}
    indices = _signal_indices(closes, signal, signal_params, klines=ordered)
    overall = {int(fd): [] for fd in forward_days}
    by_regime = {name: {int(fd): [] for fd in forward_days} for name in ("bullish", "neutral", "bearish")}
    recent_signals = []
    for idx in indices:
        if idx < 20:
            continue
        regime_ret = (closes[idx] - closes[idx - 20]) / closes[idx - 20] if closes[idx - 20] > 0 else 0.0
        regime = "bullish" if regime_ret >= 0.05 else ("bearish" if regime_ret <= -0.05 else "neutral")
        signal_row = {
            "date": ordered[idx].get("date") or ordered[idx].get("time"),
            "regime": regime,
            "forward_returns": {},
        }
        any_forward = False
        for fd in forward_days:
            future_idx = idx + int(fd)
            if future_idx >= len(closes) or closes[idx] <= 0:
                continue
            fwd_ret = float((closes[future_idx] - closes[idx]) / closes[idx])
            overall[int(fd)].append(fwd_ret)
            by_regime[regime][int(fd)].append(fwd_ret)
            signal_row["forward_returns"][f"{int(fd)}d"] = round(fwd_ret, 6)
            any_forward = True
        if any_forward:
            recent_signals.append(signal_row)
    _MIN_RELIABLE_SAMPLES = 10

    def summarize(values):
        n = len(values)
        if not values:
            return {"samples": 0, "hit_rate": None, "avg_return": None, "reliable": False}
        hr = round(float(np.mean(np.array(values) > 0)), 4)
        ar = round(float(np.mean(values)), 4)
        return {"samples": n, "hit_rate": hr, "avg_return": ar, "reliable": n >= _MIN_RELIABLE_SAMPLES}

    total_samples = len(indices)
    reliability_warning = None
    if total_samples < _MIN_RELIABLE_SAMPLES:
        reliability_warning = f"样本量不足（{total_samples} < {_MIN_RELIABLE_SAMPLES}），统计结果参考价值有限"

    return {
        "signal": signal,
        "sample_count": total_samples,
        "reliability_warning": reliability_warning,
        "forward_returns": {f"{fd}d": summarize(values) for fd, values in overall.items()},
        "by_regime": {regime: {f"{fd}d": summarize(values) for fd, values in buckets.items()} for regime, buckets in by_regime.items()},
        "recent_signals": recent_signals[-20:],
    }
