"""统一 RSI 算法(诊断报告 §4.2.5 P2-4.2.5 修复)。

历史问题:同股 600519 同时间窗口
- get_factor_profile.rsi.current = 22.5864 (RSI(14) Wilder's)
- alerts.check.rsi = 2.7586 (其他算法)
- 偏差 8 倍 / 87.7%

AI 看到两个 RSI 不知该信哪个,直接误用风险高。

修复:全 AIASK 工具统一走 Wilder's smoothing RSI(14)。
"""
from __future__ import annotations

from typing import List


def calculate_rsi_wilder(closes: List[float], period: int = 14) -> List[float | None]:
    """Wilder's smoothing RSI(canonical AIASK 实现)。

    与 talib.RSI / pandas_ta.rsi 默认结果一致。

    Args:
        closes: 收盘价序列
        period: RSI 周期,默认 14

    Returns:
        list[float | None]: 长度等于 closes,前 period 个为 None(warmup),其余为 0~100 之间的 RSI
    """
    n = len(closes)
    if n <= period:
        return [None] * n

    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for i in range(1, n):
        diff = float(closes[i]) - float(closes[i - 1])
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    rsi: list[float | None] = [None] * period
    if avg_loss == 0:
        rsi.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi.append(round(100.0 - 100.0 / (1.0 + rs), 4))

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(round(100.0 - 100.0 / (1.0 + rs), 4))
    return rsi


def calculate_rsi_current(closes: List[float], period: int = 14) -> float | None:
    """返回 RSI 序列最后一个值(简化场景)。"""
    series = calculate_rsi_wilder(closes, period=period)
    if not series:
        return None
    last = series[-1]
    return float(last) if last is not None else None


def rsi_method_metadata(period: int = 14) -> dict:
    """返回 RSI 计算方法元数据(供工具响应顶层标注使用)。

    工具使用方式:
        response['rsi_method'] = rsi_method_metadata(period=14)
        # → {'algorithm': 'wilder_smoothing', 'period': 14, 'source': 'services.rsi_unified'}
    """
    return {
        "algorithm": "wilder_smoothing",
        "period": int(period),
        "source": "services.rsi_unified.calculate_rsi_wilder",
        "version": "1.0",
    }
