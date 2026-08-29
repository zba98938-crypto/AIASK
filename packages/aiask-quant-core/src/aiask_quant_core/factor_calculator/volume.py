"""成交量因子 - OBV/AD/ADOSC/CMF/力度/简易波动/VPT/NVI/PVI/VWAP/换手率/非流动性/价格位置/趋势"""
import numpy as np
from typing import List


class VolumeFactorsMixin:
    """成交量因子混入类"""

    @staticmethod
    def calculate_volume_factor(volumes: List[float], period: int = 20) -> float:
        """成交量因子"""
        if len(volumes) < period:
            return 0.0
        recent_avg = np.mean(volumes[-period:])
        long_avg = np.mean(volumes[-period*2:-period]) if len(volumes) >= period*2 else recent_avg
        return (recent_avg - long_avg) / long_avg if long_avg > 0 else 0.0

    @staticmethod
    def calculate_vol_ratio_5_20(volumes: List[float]) -> float:
        """5日/20日成交量比率 VOL_RATIO_5_20"""
        if len(volumes) < 20:
            return 1.0
        vol_5 = np.mean(volumes[-5:])
        vol_20 = np.mean(volumes[-20:])
        return vol_5 / vol_20 if vol_20 > 0 else 1.0

    @staticmethod
    def calculate_vol_ratio_10_60(volumes: List[float]) -> float:
        """10日/60日成交量比率 VOL_RATIO_10_60"""
        if len(volumes) < 60:
            return 1.0
        vol_10 = np.mean(volumes[-10:])
        vol_60 = np.mean(volumes[-60:])
        return vol_10 / vol_60 if vol_60 > 0 else 1.0

    @staticmethod
    def calculate_obv(closes: List[float], volumes: List[float]) -> float:
        """能量潮指标 OBV (On Balance Volume)"""
        if len(closes) < 2 or len(volumes) < 2:
            return 0.0
        obv = 0.0
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv += volumes[i]
            elif closes[i] < closes[i-1]:
                obv -= volumes[i]
        return float(obv)

    @staticmethod
    def calculate_obv_slope(closes: List[float], volumes: List[float], period: int = 20) -> float:
        """OBV斜率 OBV_SLOPE"""
        if len(closes) < period or len(volumes) < period:
            return 0.0
        obv_values = []
        obv = 0.0
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv += volumes[i]
            elif closes[i] < closes[i-1]:
                obv -= volumes[i]
            obv_values.append(obv)
        if len(obv_values) < period:
            return 0.0
        y = np.array(obv_values[-period:])
        x = np.arange(len(y))
        slope = np.polyfit(x, y, 1)[0]
        return float(slope)

    @staticmethod
    def calculate_ad(highs: List[float], lows: List[float], closes: List[float], volumes: List[float]) -> float:
        """累积/派发线 AD (Accumulation/Distribution)"""
        if len(highs) < 1 or len(lows) < 1 or len(closes) < 1 or len(volumes) < 1:
            return 0.0
        ad = 0.0
        for i in range(len(closes)):
            high_low_diff = highs[i] - lows[i]
            if high_low_diff == 0:
                continue
            mfm = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / high_low_diff
            mfv = mfm * volumes[i]
            ad += mfv
        return float(ad)

    @staticmethod
    def calculate_adosc(highs: List[float], lows: List[float], closes: List[float],
                       volumes: List[float], fast: int = 3, slow: int = 10) -> float:
        """AD震荡指标 ADOSC (AD Oscillator)"""
        if len(highs) < slow or len(lows) < slow or len(closes) < slow or len(volumes) < slow:
            return 0.0
        ad_values = []
        ad = 0.0
        for i in range(len(closes)):
            high_low_diff = highs[i] - lows[i]
            if high_low_diff == 0:
                ad_values.append(ad)
                continue
            mfm = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / high_low_diff
            mfv = mfm * volumes[i]
            ad += mfv
            ad_values.append(ad)
        if len(ad_values) < slow:
            return 0.0
        ad_arr = np.array(ad_values, dtype=np.float64)
        # 使用真正的 EMA 而非 SMA
        alpha_fast = 2.0 / (fast + 1)
        alpha_slow = 2.0 / (slow + 1)
        ema_fast = float(ad_arr[0])
        ema_slow = float(ad_arr[0])
        for val in ad_arr[1:]:
            ema_fast = alpha_fast * float(val) + (1.0 - alpha_fast) * ema_fast
            ema_slow = alpha_slow * float(val) + (1.0 - alpha_slow) * ema_slow
        return float(ema_fast - ema_slow)

    @staticmethod
    def calculate_cmf(highs: List[float], lows: List[float], closes: List[float],
                     volumes: List[float], period: int = 20) -> float:
        """蔡金资金流量 CMF (Chaikin Money Flow)"""
        if len(highs) < period or len(lows) < period or len(closes) < period or len(volumes) < period:
            return 0.0
        mfv_sum = 0.0
        volume_sum = 0.0
        for i in range(-period, 0):
            high_low_diff = highs[i] - lows[i]
            if high_low_diff == 0:
                continue
            mfm = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / high_low_diff
            mfv = mfm * volumes[i]
            mfv_sum += mfv
            volume_sum += volumes[i]
        return float(mfv_sum / volume_sum) if volume_sum > 0 else 0.0

    @staticmethod
    def calculate_force_index(closes: List[float], volumes: List[float], period: int = 13) -> float:
        """力度指标 FORCE_INDEX"""
        if len(closes) < period + 1 or len(volumes) < period + 1:
            return 0.0
        force_values = []
        for i in range(1, len(closes)):
            force = (closes[i] - closes[i-1]) * volumes[i]
            force_values.append(force)
        if len(force_values) < period:
            return 0.0
        return float(np.mean(force_values[-period:]))

    @staticmethod
    def calculate_ease_of_move(highs: List[float], lows: List[float], volumes: List[float], period: int = 14) -> float:
        """简易波动指标 EASE_OF_MOVE"""
        if len(highs) < period + 1 or len(lows) < period + 1 or len(volumes) < period + 1:
            return 0.0
        emv_values = []
        for i in range(1, len(highs)):
            distance = ((highs[i] + lows[i]) / 2) - ((highs[i-1] + lows[i-1]) / 2)
            high_low_diff = highs[i] - lows[i]
            if high_low_diff == 0 or volumes[i] == 0:
                emv_values.append(0.0)
                continue
            box_ratio = volumes[i] / high_low_diff
            emv = distance / box_ratio if box_ratio > 0 else 0.0
            emv_values.append(emv)
        if len(emv_values) < period:
            return 0.0
        return float(np.mean(emv_values[-period:]))

    @staticmethod
    def calculate_vol_price_trend(closes: List[float], volumes: List[float]) -> float:
        """量价趋势指标 VOL_PRICE_TREND"""
        if len(closes) < 2 or len(volumes) < 2:
            return 0.0
        vpt = 0.0
        for i in range(1, len(closes)):
            price_change_pct = (closes[i] - closes[i-1]) / closes[i-1] if closes[i-1] > 0 else 0
            vpt += volumes[i] * price_change_pct
        return float(vpt)

    @staticmethod
    def calculate_nvi(closes: List[float], volumes: List[float]) -> float:
        """负量指标 NVI (Negative Volume Index)"""
        if len(closes) < 2 or len(volumes) < 2:
            return 1000.0
        nvi = 1000.0
        for i in range(1, len(closes)):
            if volumes[i] < volumes[i-1]:
                price_change_pct = (closes[i] - closes[i-1]) / closes[i-1] if closes[i-1] > 0 else 0
                nvi += nvi * price_change_pct
        return float(nvi)

    @staticmethod
    def calculate_pvi(closes: List[float], volumes: List[float]) -> float:
        """正量指标 PVI (Positive Volume Index)"""
        if len(closes) < 2 or len(volumes) < 2:
            return 1000.0
        pvi = 1000.0
        for i in range(1, len(closes)):
            if volumes[i] > volumes[i-1]:
                price_change_pct = (closes[i] - closes[i-1]) / closes[i-1] if closes[i-1] > 0 else 0
                pvi += pvi * price_change_pct
        return float(pvi)

    @staticmethod
    def calculate_vwap_deviation(closes: List[float], volumes: List[float], period: int = 20) -> float:
        """VWAP偏离度 VWAP_DEVIATION"""
        if len(closes) < period or len(volumes) < period:
            return 0.0
        price_volume = sum(closes[i] * volumes[i] for i in range(-period, 0))
        total_volume = sum(volumes[-period:])
        if total_volume == 0:
            return 0.0
        vwap = price_volume / total_volume
        current_price = closes[-1]
        deviation = (current_price - vwap) / vwap if vwap > 0 else 0.0
        return float(deviation)

    @staticmethod
    def calculate_turnover_rate(volume: float, shares_outstanding: float) -> float:
        """换手率 TURNOVER_RATE"""
        if not shares_outstanding or shares_outstanding <= 0:
            return 0.0
        if not volume:
            return 0.0
        return volume / shares_outstanding

    @staticmethod
    def calculate_illiquidity(returns: List[float], volumes: List[float], period: int = 20) -> float:
        """非流动性指标 ILLIQUIDITY (Amihud)"""
        if len(returns) < period or len(volumes) < period:
            return 0.0
        illiquidity_values = []
        for i in range(-period, 0):
            if volumes[i] > 0:
                illiq = abs(returns[i]) / volumes[i]
                illiquidity_values.append(illiq)
        return float(np.mean(illiquidity_values)) if illiquidity_values else 0.0

    @staticmethod
    def calculate_price_factor(closes: List[float], highs: List[float], lows: List[float]) -> float:
        """价格位置因子（当前价格在区间中的位置）"""
        if not closes or not highs or not lows:
            return 0.5
        high_max = max(highs[-20:]) if len(highs) >= 20 else max(highs)
        low_min = min(lows[-20:]) if len(lows) >= 20 else min(lows)
        if high_max == low_min:
            return 0.5
        return (closes[-1] - low_min) / (high_max - low_min)

    @staticmethod
    def calculate_trend_factor(closes: List[float], period: int = 60) -> float:
        """趋势因子（线性回归斜率）"""
        if len(closes) < period:
            return 0.0
        y = np.array(closes[-period:])
        x = np.arange(len(y))
        slope = np.polyfit(x, y, 1)[0]
        return slope / np.mean(y) if np.mean(y) > 0 else 0.0
