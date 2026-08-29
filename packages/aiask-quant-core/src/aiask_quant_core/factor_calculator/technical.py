"""技术因子 - 动量/RSI/MACD/ROC/TRIX/反转/WillR/CCI/MFI/StochK"""
import numpy as np
from typing import List, Union


class TechnicalFactorsMixin:
    """技术因子混入类 - 动量、反转、振荡器指标"""

    # ========== 技术因子 - 动量因子 ==========

    @staticmethod
    def calculate_momentum(closes: List[float], period: int = 20, as_series: bool = False) -> Union[float, np.ndarray]:
        """动量因子（通用）

        Args:
            closes: 收盘价序列
            period: 动量周期
            as_series: True 时返回完整动量序列 (np.ndarray)，前 period 个值为 NaN
        """
        if as_series:
            arr = np.array(closes, dtype=np.float64)
            n = len(arr)
            result = np.full(n, np.nan)
            for i in range(period, n):
                if arr[i - period] != 0:
                    result[i] = (arr[i] - arr[i - period]) / arr[i - period]
                else:
                    result[i] = 0.0
            return result
        if len(closes) < period:
            return 0.0
        return (closes[-1] - closes[-period]) / closes[-period]

    @staticmethod
    def calculate_mom_1d(closes: List[float]) -> float:
        """1日动量因子 MOM_1D"""
        if len(closes) < 2:
            return 0.0
        return (closes[-1] - closes[-2]) / closes[-2]

    @staticmethod
    def calculate_mom_5d(closes: List[float]) -> float:
        """5日动量因子 MOM_5D"""
        if len(closes) < 6:
            return 0.0
        return (closes[-1] - closes[-6]) / closes[-6]

    @staticmethod
    def calculate_mom_10d(closes: List[float]) -> float:
        """10日动量因子 MOM_10D"""
        if len(closes) < 11:
            return 0.0
        return (closes[-1] - closes[-11]) / closes[-11]

    @staticmethod
    def calculate_mom_20d(closes: List[float]) -> float:
        """20日动量因子 MOM_20D"""
        if len(closes) < 21:
            return 0.0
        return (closes[-1] - closes[-21]) / closes[-21]

    @staticmethod
    def calculate_mom_60d(closes: List[float]) -> float:
        """60日动量因子 MOM_60D"""
        if len(closes) < 61:
            return 0.0
        return (closes[-1] - closes[-61]) / closes[-61]

    @staticmethod
    def calculate_rsi(closes: List[float], period: int = 14, as_series: bool = False) -> Union[float, np.ndarray]:
        """RSI相对强弱指数（Wilder 指数平滑法）

        Args:
            closes: 收盘价序列
            period: RSI 计算周期
            as_series: True 时返回完整 RSI 序列 (np.ndarray)，长度 = len(closes)-1
        """
        if len(closes) < period + 1:
            if as_series:
                return np.full(max(len(closes) - 1, 0), 50.0)
            return 50.0
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        # 初始周期使用 SMA 作为种子
        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))

        if as_series:
            n = len(gains)
            rsi_arr = np.full(n, np.nan)
            # 第 period-1 个位置对应初始 SMA 种子后的第一个 RSI
            if avg_loss <= 1e-12:
                rsi_arr[period - 1] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi_arr[period - 1] = 100.0 - (100.0 / (1.0 + rs))
            for i in range(period, n):
                avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
                avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
                if avg_loss <= 1e-12:
                    rsi_arr[i] = 100.0
                else:
                    rs = avg_gain / avg_loss
                    rsi_arr[i] = 100.0 - (100.0 / (1.0 + rs))
            return rsi_arr

        # 标量模式（原逻辑）
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
            avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
        if avg_loss <= 1e-12:
            return 100.0
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return float(rsi)

    @staticmethod
    def calculate_rsi_6(closes: List[float]) -> float:
        """6日RSI"""
        return TechnicalFactorsMixin.calculate_rsi(closes, period=6)

    @staticmethod
    def calculate_rsi_14(closes: List[float]) -> float:
        """14日RSI"""
        return TechnicalFactorsMixin.calculate_rsi(closes, period=14)

    @staticmethod
    def calculate_rsi_24(closes: List[float]) -> float:
        """24日RSI"""
        return TechnicalFactorsMixin.calculate_rsi(closes, period=24)

    @staticmethod
    def calculate_macd(closes: List[float], fast: int = 12, slow: int = 26, as_series: bool = False) -> Union[float, np.ndarray]:
        """MACD指标

        Args:
            closes: 收盘价序列
            fast: 快线周期
            slow: 慢线周期
            as_series: True 时返回完整 MACD 序列 (np.ndarray)
        """
        if as_series:
            closes_arr = np.array(closes, dtype=np.float64)
            if len(closes_arr) < slow:
                return np.full(len(closes_arr), 0.0)
            ema_fast_series = TechnicalFactorsMixin._calculate_ema_series(closes_arr, fast)
            ema_slow_series = TechnicalFactorsMixin._calculate_ema_series(closes_arr, slow)
            return ema_fast_series - ema_slow_series
        if len(closes) < slow:
            return 0.0
        closes_arr = np.array(closes)
        ema_fast = TechnicalFactorsMixin._calculate_ema(closes_arr, fast)
        ema_slow = TechnicalFactorsMixin._calculate_ema(closes_arr, slow)
        macd = ema_fast - ema_slow
        return float(macd)

    @staticmethod
    def calculate_macd_signal(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> float:
        """MACD信号线"""
        if len(closes) < slow + signal:
            return 0.0
        macd_values = []
        for i in range(slow, len(closes) + 1):
            macd = TechnicalFactorsMixin.calculate_macd(closes[:i], fast, slow)
            macd_values.append(macd)
        if len(macd_values) < signal:
            return 0.0
        signal_line = TechnicalFactorsMixin._calculate_ema(np.array(macd_values), signal)
        return float(signal_line)

    @staticmethod
    def calculate_macd_hist(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> float:
        """MACD柱状图（MACD - Signal）"""
        macd = TechnicalFactorsMixin.calculate_macd(closes, fast, slow)
        macd_signal = TechnicalFactorsMixin.calculate_macd_signal(closes, fast, slow, signal)
        return macd - macd_signal

    @staticmethod
    def calculate_roc(closes: List[float], period: int = 10) -> float:
        """变动率ROC（通用）"""
        if len(closes) < period + 1:
            return 0.0
        return ((closes[-1] - closes[-(period+1)]) / closes[-(period+1)]) * 100

    @staticmethod
    def calculate_roc_5(closes: List[float]) -> float:
        """5日变动率 ROC_5"""
        return TechnicalFactorsMixin.calculate_roc(closes, period=5)

    @staticmethod
    def calculate_roc_10(closes: List[float]) -> float:
        """10日变动率 ROC_10"""
        return TechnicalFactorsMixin.calculate_roc(closes, period=10)

    @staticmethod
    def calculate_roc_20(closes: List[float]) -> float:
        """20日变动率 ROC_20"""
        return TechnicalFactorsMixin.calculate_roc(closes, period=20)

    @staticmethod
    def calculate_trix(closes: List[float], period: int = 12) -> float:
        """三重指数平滑TRIX（O(n) 增量计算）"""
        if len(closes) < period * 3:
            return 0.0
        closes_arr = np.array(closes, dtype=np.float64)
        # 三层 EMA，每层一次遍历 O(n)
        ema1_series = TechnicalFactorsMixin._calculate_ema_series(closes_arr, period)
        if len(ema1_series) < period:
            return 0.0
        ema2_series = TechnicalFactorsMixin._calculate_ema_series(ema1_series, period)
        if len(ema2_series) < 2:
            return 0.0
        ema3_series = TechnicalFactorsMixin._calculate_ema_series(ema2_series, period)
        if len(ema3_series) < 2:
            return 0.0
        ema3_prev = float(ema3_series[-2])
        if ema3_prev == 0:
            return 0.0
        trix = ((float(ema3_series[-1]) - ema3_prev) / ema3_prev) * 100
        return float(trix)

    @staticmethod
    def _calculate_ema_series(data: np.ndarray, period: int) -> np.ndarray:
        """计算完整 EMA 序列（单次遍历 O(n)），返回 ndarray"""
        n = len(data)
        if n == 0:
            return np.array([], dtype=np.float64)
        alpha = 2.0 / (period + 1)
        out = np.empty(n, dtype=np.float64)
        out[0] = float(data[0])
        for i in range(1, n):
            out[i] = alpha * float(data[i]) + (1.0 - alpha) * out[i - 1]
        return out

    @staticmethod
    def _calculate_ema(data: np.ndarray, period: int) -> float:
        """计算指数移动平均EMA（返回最终值）"""
        if len(data) < period:
            return float(np.mean(data))
        alpha = 2.0 / (period + 1)
        ema = float(data[0])
        for price in data[1:]:
            ema = alpha * float(price) + (1.0 - alpha) * ema
        return float(ema)

    # ========== 技术因子 - 反转因子 ==========

    @staticmethod
    def calculate_reversal(closes: List[float], period: int = 5) -> float:
        """反转因子（通用）"""
        if len(closes) < period:
            return 0.0
        return -(closes[-1] - closes[-period]) / closes[-period]

    @staticmethod
    def calculate_rev_1d(closes: List[float]) -> float:
        """1日反转因子 REV_1D"""
        if len(closes) < 2:
            return 0.0
        return -(closes[-1] - closes[-2]) / closes[-2]

    @staticmethod
    def calculate_rev_5d(closes: List[float]) -> float:
        """5日反转因子 REV_5D"""
        if len(closes) < 6:
            return 0.0
        return -(closes[-1] - closes[-6]) / closes[-6]

    @staticmethod
    def calculate_rev_10d(closes: List[float]) -> float:
        """10日反转因子 REV_10D"""
        if len(closes) < 11:
            return 0.0
        return -(closes[-1] - closes[-11]) / closes[-11]

    @staticmethod
    def calculate_rev_20d(closes: List[float]) -> float:
        """20日反转因子 REV_20D"""
        if len(closes) < 21:
            return 0.0
        return -(closes[-1] - closes[-21]) / closes[-21]

    @staticmethod
    def calculate_willr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """威廉指标 Williams %R"""
        if len(highs) < period or len(lows) < period or len(closes) < period:
            return -50.0
        highest_high = max(highs[-period:])
        lowest_low = min(lows[-period:])
        if highest_high == lowest_low:
            return -50.0
        willr = ((highest_high - closes[-1]) / (highest_high - lowest_low)) * -100
        return float(willr)

    @staticmethod
    def calculate_cci(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """商品通道指数 CCI"""
        if len(highs) < period or len(lows) < period or len(closes) < period:
            return 0.0
        typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs[-period:], lows[-period:], closes[-period:])]
        sma_tp = np.mean(typical_prices)
        mean_deviation = np.mean([abs(tp - sma_tp) for tp in typical_prices])
        if mean_deviation == 0:
            return 0.0
        cci = (typical_prices[-1] - sma_tp) / (0.015 * mean_deviation)
        return float(cci)

    @staticmethod
    def calculate_mfi(highs: List[float], lows: List[float], closes: List[float],
                     volumes: List[float], period: int = 14) -> float:
        """资金流量指数 MFI"""
        if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1 or len(volumes) < period + 1:
            return 50.0
        typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs[-(period+1):], lows[-(period+1):], closes[-(period+1):])]
        money_flows = [tp * v for tp, v in zip(typical_prices, volumes[-(period+1):])]
        positive_flow = 0.0
        negative_flow = 0.0
        for i in range(1, len(typical_prices)):
            if typical_prices[i] > typical_prices[i-1]:
                positive_flow += money_flows[i]
            elif typical_prices[i] < typical_prices[i-1]:
                negative_flow += money_flows[i]
        if negative_flow == 0:
            return 100.0
        money_flow_ratio = positive_flow / negative_flow
        mfi = 100 - (100 / (1 + money_flow_ratio))
        return float(mfi)

    @staticmethod
    def calculate_stoch_k(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """随机指标K值 STOCH_K"""
        if len(highs) < period or len(lows) < period or len(closes) < period:
            return 50.0
        highest_high = max(highs[-period:])
        lowest_low = min(lows[-period:])
        if highest_high == lowest_low:
            return 50.0
        stoch_k = ((closes[-1] - lowest_low) / (highest_high - lowest_low)) * 100
        return float(stoch_k)
