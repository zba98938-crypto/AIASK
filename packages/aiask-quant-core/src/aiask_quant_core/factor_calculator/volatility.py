"""波动率因子 - 波动率/ATR/布林带宽度"""
import numpy as np
from typing import List


class VolatilityFactorsMixin:
    """波动率因子混入类"""

    @staticmethod
    def calculate_volatility(closes: List[float], period: int = 20) -> float:
        """波动率因子（通用）"""
        if len(closes) < period:
            return 0.0
        returns = np.diff(closes[-period:]) / closes[-period-1:-1]
        return float(np.std(returns) * np.sqrt(252))

    @staticmethod
    def calculate_vol_5d(closes: List[float]) -> float:
        """5日波动率 VOL_5D"""
        if len(closes) < 6:
            return 0.0
        returns = np.diff(closes[-6:]) / closes[-6:-1]
        return float(np.std(returns) * np.sqrt(252))

    @staticmethod
    def calculate_vol_10d(closes: List[float]) -> float:
        """10日波动率 VOL_10D"""
        if len(closes) < 11:
            return 0.0
        returns = np.diff(closes[-11:]) / closes[-11:-1]
        return float(np.std(returns) * np.sqrt(252))

    @staticmethod
    def calculate_vol_20d(closes: List[float]) -> float:
        """20日波动率 VOL_20D"""
        if len(closes) < 21:
            return 0.0
        returns = np.diff(closes[-21:]) / closes[-21:-1]
        return float(np.std(returns) * np.sqrt(252))

    @staticmethod
    def calculate_vol_60d(closes: List[float]) -> float:
        """60日波动率 VOL_60D"""
        if len(closes) < 61:
            return 0.0
        returns = np.diff(closes[-61:]) / closes[-61:-1]
        return float(np.std(returns) * np.sqrt(252))

    @staticmethod
    def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """平均真实波幅 ATR（通用）"""
        if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
            return 0.0
        true_ranges = []
        for i in range(1, period + 1):
            idx = -(period + 1 - i + 1)
            high_low = highs[idx] - lows[idx]
            high_close = abs(highs[idx] - closes[idx - 1])
            low_close = abs(lows[idx] - closes[idx - 1])
            true_range = max(high_low, high_close, low_close)
            true_ranges.append(true_range)
        atr = np.mean(true_ranges)
        return float(atr)

    @staticmethod
    def calculate_atr_14(highs: List[float], lows: List[float], closes: List[float]) -> float:
        """14日ATR"""
        return VolatilityFactorsMixin.calculate_atr(highs, lows, closes, period=14)

    @staticmethod
    def calculate_atr_20(highs: List[float], lows: List[float], closes: List[float]) -> float:
        """20日ATR"""
        return VolatilityFactorsMixin.calculate_atr(highs, lows, closes, period=20)

    @staticmethod
    def calculate_bbwidth(closes: List[float], period: int = 20, num_std: float = 2.0) -> float:
        """布林带宽度 BBWIDTH"""
        if len(closes) < period:
            return 0.0
        middle = np.mean(closes[-period:])
        std = np.std(closes[-period:])
        upper = middle + num_std * std
        lower = middle - num_std * std
        if middle == 0:
            return 0.0
        bbwidth = (upper - lower) / middle
        return float(bbwidth)

    # ========== 新增波动率因子 ==========

    @staticmethod
    def calculate_atr_wilder(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """平均真实波幅 ATR（Wilder 指数平滑版）"""
        n = min(len(highs), len(lows), len(closes))
        if n < period + 1:
            return 0.0
        # 计算全部 True Range
        true_ranges = []
        for i in range(1, n):
            h, l, c_prev = float(highs[i]), float(lows[i]), float(closes[i - 1])
            tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
            true_ranges.append(tr)
        if len(true_ranges) < period:
            return 0.0
        # 初始种子用 SMA
        atr = float(np.mean(true_ranges[:period]))
        # Wilder 递推
        for i in range(period, len(true_ranges)):
            atr = (atr * (period - 1) + true_ranges[i]) / period
        return float(atr)

    @staticmethod
    def calculate_parkinson_vol(highs: List[float], lows: List[float], period: int = 20) -> float:
        """Parkinson 波动率（基于 High/Low，比 close-close 更精准）"""
        n = min(len(highs), len(lows))
        if n < period:
            return 0.0
        log_hl_sq = []
        for i in range(-period, 0):
            h, l = float(highs[i]), float(lows[i])
            if h > 0 and l > 0 and h >= l:
                log_hl_sq.append(np.log(h / l) ** 2)
        if not log_hl_sq:
            return 0.0
        variance = np.mean(log_hl_sq) / (4.0 * np.log(2))
        return float(np.sqrt(variance * 252))

    @staticmethod
    def calculate_garman_klass_vol(
        opens: List[float], highs: List[float], lows: List[float], closes: List[float], period: int = 20
    ) -> float:
        """Garman-Klass 波动率（综合 OHLC）"""
        n = min(len(opens), len(highs), len(lows), len(closes))
        if n < period:
            return 0.0
        vals = []
        for i in range(-period, 0):
            o, h, l, c = float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i])
            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                continue
            term1 = 0.5 * (np.log(h / l)) ** 2
            term2 = (2.0 * np.log(2) - 1.0) * (np.log(c / o)) ** 2
            vals.append(term1 - term2)
        if not vals:
            return 0.0
        return float(np.sqrt(np.mean(vals) * 252))

    @staticmethod
    def calculate_vol_ratio(closes: List[float], short: int = 5, long: int = 60) -> float:
        """波动率比率 VOL_SHORT / VOL_LONG，捕捉波动率变化"""
        n = len(closes)
        if n < long + 1:
            return 1.0
        short_returns = np.diff(closes[-(short + 1):]) / np.array(closes[-(short + 1):-1], dtype=np.float64)
        long_returns = np.diff(closes[-(long + 1):]) / np.array(closes[-(long + 1):-1], dtype=np.float64)
        vol_short = float(np.std(short_returns))
        vol_long = float(np.std(long_returns))
        if vol_long <= 1e-12:
            return 1.0
        return float(vol_short / vol_long)
