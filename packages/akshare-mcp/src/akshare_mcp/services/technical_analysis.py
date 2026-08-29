"""
技术分析服务
使用 pandas-ta 和 ta-lib 实现高性能技术指标计算
"""

from typing import List, Dict, Any, Optional
import numpy as np

try:
    import pandas as pd
    import pandas_ta as ta
    PANDAS_TA_AVAILABLE = True
except ImportError:
    PANDAS_TA_AVAILABLE = False
    pd = None
    ta = None

try:
    import talib
    TALIB_AVAILABLE = True
except ImportError:
    TALIB_AVAILABLE = False
    talib = None


def _float_array(values: List[float]) -> np.ndarray:
    """TA-Lib expects contiguous double arrays."""
    return np.asarray(values, dtype=np.float64)


def _formula_sma(data: List[float], n: int, m: int = 1) -> List[float]:
    """公式引擎兼容的 SMA 实现。

    公式：Y = (M * X + (N - M) * Y') / N
    其中首个值取原始序列首值。
    """
    if not data:
        return []
    if n <= 0:
        return [float(x) for x in data]

    m = max(0, min(m, n))
    result = [float(data[0])]
    for value in data[1:]:
        prev = result[-1]
        current = (m * float(value) + (n - m) * prev) / n
        result.append(current)
    return result


class TechnicalAnalysis:
    """技术分析计算器"""
    
    @staticmethod
    def calculate_sma(closes: List[float], period: int = 20) -> List[float]:
        """计算简单移动平均

        P2-4.5.6 fix(诊断报告 §4.5.6):
        warmup 期(前 period-1 个)用 None 填充而非 0,与 MACD 的 null 行为一致。
        AI 看到 ma[0]=0 容易误读为'价格曾跌到 0'。
        """
        if not PANDAS_TA_AVAILABLE:
            return TechnicalAnalysis._calculate_sma_numpy(closes, period)

        df = pd.DataFrame({'close': closes})
        result = df.ta.sma(length=period)
        # warmup 区(NaN)统一返回 None,与 MACD 一致
        return [None if pd.isna(v) else float(v) for v in result.tolist()]

    @staticmethod
    def _calculate_sma_numpy(closes: List[float], period: int) -> List[float]:
        """NumPy实现的SMA（fallback）"""
        closes_arr = _float_array(closes)
        if len(closes_arr) < period:
            # P2-4.5.6 fix: 长度不足时返回 None list 而非 0
            return [None] * len(closes)

        weights = np.ones(period) / period
        sma = np.convolve(closes_arr, weights, mode='valid')

        # P2-4.5.6 fix: 前 period-1 用 None,统一 warmup 行为
        result: list[float | None] = [None] * (period - 1)
        result.extend(float(v) for v in sma.tolist())
        return result
    
    @staticmethod
    def calculate_ema(closes: List[float], period: int = 20) -> List[float]:
        """计算指数移动平均"""
        if TALIB_AVAILABLE:
            result = talib.EMA(_float_array(closes), timeperiod=period)
            return np.nan_to_num(result, 0).tolist()
        
        if PANDAS_TA_AVAILABLE:
            df = pd.DataFrame({'close': closes})
            result = df.ta.ema(length=period)
            return result.fillna(0).tolist()
        
        return TechnicalAnalysis._calculate_ema_numpy(closes, period)
    
    @staticmethod
    def _calculate_ema_numpy(closes: List[float], period: int) -> List[float]:
        """NumPy实现的EMA（fallback）"""
        closes_arr = _float_array(closes)
        alpha = 2 / (period + 1)
        ema = np.zeros(len(closes))
        ema[0] = closes_arr[0]
        
        for i in range(1, len(closes)):
            ema[i] = alpha * closes_arr[i] + (1 - alpha) * ema[i-1]
        
        return ema.tolist()
    
    @staticmethod
    def calculate_rsi(closes: List[float], period: int = 14) -> Dict[str, Any]:
        """计算RSI指标"""
        # F-N04-1 修复：warmup 不足时不得伪造 RSI=0/100 并给出超卖买入/超买卖出信号。
        # RSI(period) 至少需要 period+1 个收盘价才能形成一个有效值。
        n_closes = len([c for c in (closes or []) if c is not None])
        if n_closes < period + 1:
            return {
                'value': None,
                'signal': 'unknown',
                'overbought': None,
                'oversold': None,
                'reliable': False,
                'warning': (
                    f'数据不足：RSI({period}) 至少需要 {period + 1} 根K线，实际 {n_closes} 根；'
                    f'不输出超买/超卖信号（避免虚假 oversold buy）'
                ),
                'data_points': n_closes,
                'required_points': period + 1,
            }

        rsi_value = None
        if TALIB_AVAILABLE:
            rsi = talib.RSI(_float_array(closes), timeperiod=period)
            rsi_value = float(rsi[-1]) if not np.isnan(rsi[-1]) else None
        elif PANDAS_TA_AVAILABLE:
            df = pd.DataFrame({'close': closes})
            rsi_series = df.ta.rsi(length=period)
            last = rsi_series.iloc[-1] if rsi_series is not None and len(rsi_series) else None
            rsi_value = float(last) if last is not None and not pd.isna(last) else None
        else:
            rsi_value = TechnicalAnalysis._calculate_rsi_numpy(closes, period)

        # 计算结果仍可能为 NaN/None（边界），同样按数据不足处理，不伪造信号
        if rsi_value is None or (isinstance(rsi_value, float) and np.isnan(rsi_value)):
            return {
                'value': None,
                'signal': 'unknown',
                'overbought': None,
                'oversold': None,
                'reliable': False,
                'warning': f'RSI({period}) 计算结果无效（warmup 不足或数据异常）',
                'data_points': n_closes,
                'required_points': period + 1,
            }

        # 生成信号
        signal = 'hold'
        if rsi_value < 30:
            signal = 'buy'
        elif rsi_value > 70:
            signal = 'sell'
        
        return {
            'value': round(rsi_value, 2),
            'signal': signal,
            'overbought': rsi_value > 70,
            'oversold': rsi_value < 30,
            'reliable': True,
        }
    
    @staticmethod
    def _calculate_rsi_numpy(closes: List[float], period: int) -> float | None:
        """NumPy实现的RSI（fallback）。数据不足返回 None，不伪造 0/100。"""
        closes_arr = _float_array(closes)
        if len(closes_arr) < period + 1:
            return None
        deltas = np.diff(closes_arr)
        
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        if len(gains) < period:
            return None
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    @staticmethod
    def calculate_rsi_series(closes: List[float], period: int = 14) -> List[float]:
        """计算 RSI 序列（用于公式筛选的 Python 回退）"""
        if len(closes) < 2:
            return [0.0] * len(closes)

        if TALIB_AVAILABLE:
            rsi = talib.RSI(_float_array(closes), timeperiod=period)
            return np.nan_to_num(rsi, 0).tolist()

        closes_arr = _float_array(closes)
        deltas = np.diff(closes_arr)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        rsi = np.zeros(len(closes_arr), dtype=float)
        if len(deltas) < period:
            return rsi.tolist()

        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        if avg_loss == 0:
            rsi[period] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[period] = 100 - (100 / (1 + rs))

        for i in range(period + 1, len(closes_arr)):
            gain = gains[i - 1]
            loss = losses[i - 1]
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            if avg_loss == 0:
                rsi[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i] = 100 - (100 / (1 + rs))

        return rsi.tolist()
    
    @staticmethod
    def calculate_macd(
        closes: List[float],
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> Dict[str, Any]:
        """计算MACD指标"""
        warmup = slow_period + signal_period - 2

        def _nan_to_none(arr):
            return [None if (v != v or v is None) else float(v) for v in arr]

        if TALIB_AVAILABLE:
            macd, signal, hist = talib.MACD(
                _float_array(closes),
                fastperiod=fast_period,
                slowperiod=slow_period,
                signalperiod=signal_period
            )
            return {
                'macd': _nan_to_none(macd),
                'signal': _nan_to_none(signal),
                'histogram': _nan_to_none(hist),
                'warmup_periods': warmup,
            }
        
        if PANDAS_TA_AVAILABLE:
            df = pd.DataFrame({'close': closes})
            macd_df = df.ta.macd(fast=fast_period, slow=slow_period, signal=signal_period)
            return {
                'macd': _nan_to_none(macd_df[f'MACD_{fast_period}_{slow_period}_{signal_period}']),
                'signal': _nan_to_none(macd_df[f'MACDs_{fast_period}_{slow_period}_{signal_period}']),
                'histogram': _nan_to_none(macd_df[f'MACDh_{fast_period}_{slow_period}_{signal_period}']),
                'warmup_periods': warmup,
            }
        
        # NumPy fallback
        ema_fast = TechnicalAnalysis._calculate_ema_numpy(closes, fast_period)
        ema_slow = TechnicalAnalysis._calculate_ema_numpy(closes, slow_period)
        macd = np.array(ema_fast) - np.array(ema_slow)
        signal = TechnicalAnalysis._calculate_ema_numpy(macd.tolist(), signal_period)
        histogram = macd - np.array(signal)
        
        return {
            'macd': macd.tolist(),
            'signal': signal,
            'histogram': histogram.tolist(),
            'warmup_periods': warmup,
        }
    
    @staticmethod
    def calculate_kdj(
        highs: List[float],
        lows: List[float],
        closes: List[float],
        period: int = 9,
        k_period: int = 3,
        d_period: int = 3
    ) -> Dict[str, Any]:
        """计算KDJ指标"""
        if TALIB_AVAILABLE:
            k, d = talib.STOCH(
                _float_array(highs),
                _float_array(lows),
                _float_array(closes),
                fastk_period=period,
                slowk_period=k_period,
                slowd_period=d_period
            )
            k = np.nan_to_num(k, 0)
            d = np.nan_to_num(d, 0)
            j = 3 * k - 2 * d
            
            return {
                'k': k.tolist(),
                'd': d.tolist(),
                'j': j.tolist(),
            }
        
        # NumPy实现
        n = len(closes)
        rsv = np.zeros(n)
        
        for i in range(period - 1, n):
            period_high = max(highs[i-period+1:i+1])
            period_low = min(lows[i-period+1:i+1])
            
            if period_high != period_low:
                rsv[i] = (closes[i] - period_low) / (period_high - period_low) * 100
            else:
                rsv[i] = 50
        
        # 计算K值
        k = np.zeros(n)
        k[0] = 50
        for i in range(1, n):
            k[i] = (2/3) * k[i-1] + (1/3) * rsv[i]
        
        # 计算D值
        d = np.zeros(n)
        d[0] = 50
        for i in range(1, n):
            d[i] = (2/3) * d[i-1] + (1/3) * k[i]
        
        # 计算J值
        j = 3 * k - 2 * d
        
        return {
            'k': k.tolist(),
            'd': d.tolist(),
            'j': j.tolist(),
        }
    
    @staticmethod
    def calculate_bollinger_bands(
        closes: List[float],
        period: int = 20,
        std_dev: float = 2.0
    ) -> Dict[str, Any]:
        """计算布林带"""
        if TALIB_AVAILABLE:
            upper, middle, lower = talib.BBANDS(
                _float_array(closes),
                timeperiod=period,
                nbdevup=std_dev,
                nbdevdn=std_dev
            )
            return {
                'upper': np.nan_to_num(upper, 0).tolist(),
                'middle': np.nan_to_num(middle, 0).tolist(),
                'lower': np.nan_to_num(lower, 0).tolist(),
            }
        
        if PANDAS_TA_AVAILABLE:
            df = pd.DataFrame({'close': closes})
            bbands = df.ta.bbands(length=period, std=std_dev)
            return {
                'upper': bbands[f'BBU_{period}_{std_dev}'].fillna(0).tolist(),
                'middle': bbands[f'BBM_{period}_{std_dev}'].fillna(0).tolist(),
                'lower': bbands[f'BBL_{period}_{std_dev}'].fillna(0).tolist(),
            }
        
        # NumPy实现
        sma = TechnicalAnalysis._calculate_sma_numpy(closes, period)
        closes_arr = _float_array(closes)
        
        std = np.zeros(len(closes))
        for i in range(period - 1, len(closes)):
            std[i] = np.std(closes_arr[i-period+1:i+1])
        
        upper = np.array(sma) + std_dev * std
        lower = np.array(sma) - std_dev * std
        
        return {
            'upper': upper.tolist(),
            'middle': sma,
            'lower': lower.tolist(),
        }
    
    @staticmethod
    def calculate_atr(
        highs: List[float],
        lows: List[float],
        closes: List[float],
        period: int = 14
    ) -> List[float]:
        """计算ATR（平均真实波幅）"""
        if TALIB_AVAILABLE:
            atr = talib.ATR(
                _float_array(highs),
                _float_array(lows),
                _float_array(closes),
                timeperiod=period
            )
            return np.nan_to_num(atr, 0).tolist()
        
        # NumPy实现
        n = len(closes)
        tr = np.zeros(n)
        
        for i in range(1, n):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            tr[i] = max(hl, hc, lc)
        
        # 计算ATR
        atr = np.zeros(n)
        atr[period-1] = np.mean(tr[1:period])
        
        for i in range(period, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
        
        return atr.tolist()
    
    @staticmethod
    def calculate_all_indicators(
        klines: List[Dict[str, Any]],
        indicators: List[str]
    ) -> Dict[str, Any]:
        """批量计算多个技术指标"""
        if not klines:
            return {}
        
        # 提取OHLCV数据
        closes = [k['close'] for k in klines]
        opens = [k['open'] for k in klines]
        highs = [k['high'] for k in klines]
        lows = [k['low'] for k in klines]
        volumes = [k['volume'] for k in klines]
        
        results = {}
        
        for indicator in indicators:
            indicator_lower = indicator.lower()
            
            if indicator_lower == 'ma' or indicator_lower == 'sma':
                results['ma'] = TechnicalAnalysis.calculate_sma(closes, 20)
            elif indicator_lower == 'ema':
                results['ema'] = TechnicalAnalysis.calculate_ema(closes, 20)
            elif indicator_lower == 'rsi':
                results['rsi'] = TechnicalAnalysis.calculate_rsi(closes, 14)
            elif indicator_lower == 'macd':
                results['macd'] = TechnicalAnalysis.calculate_macd(closes)
            elif indicator_lower == 'kdj':
                results['kdj'] = TechnicalAnalysis.calculate_kdj(highs, lows, closes)
            elif indicator_lower == 'boll' or indicator_lower == 'bbands':
                results['boll'] = TechnicalAnalysis.calculate_bollinger_bands(closes)
            elif indicator_lower == 'atr':
                results['atr'] = TechnicalAnalysis.calculate_atr(highs, lows, closes)
            elif indicator_lower == 'obv':
                results['obv'] = TechnicalAnalysis.calculate_obv(closes, volumes)
            elif indicator_lower == 'cci':
                results['cci'] = TechnicalAnalysis.calculate_cci(highs, lows, closes)
            elif indicator_lower == 'wr':
                results['wr'] = TechnicalAnalysis.calculate_wr(highs, lows, closes)
            elif indicator_lower == 'roc':
                results['roc'] = TechnicalAnalysis.calculate_roc(closes)
        
        return results
    
    @staticmethod
    def calculate_obv(closes: List[float], volumes: List[float]) -> List[float]:
        """计算能量潮指标 (On Balance Volume)"""
        if TALIB_AVAILABLE:
            obv = talib.OBV(_float_array(closes), _float_array(volumes))
            return np.nan_to_num(obv, 0).tolist()
        
        # NumPy实现
        obv = np.zeros(len(closes))
        obv[0] = volumes[0]
        
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv[i] = obv[i-1] + volumes[i]
            elif closes[i] < closes[i-1]:
                obv[i] = obv[i-1] - volumes[i]
            else:
                obv[i] = obv[i-1]
        
        return obv.tolist()
    
    @staticmethod
    def calculate_cci(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
        """计算顺势指标 (Commodity Channel Index)"""
        if TALIB_AVAILABLE:
            cci = talib.CCI(_float_array(highs), _float_array(lows), _float_array(closes), timeperiod=period)
            return np.nan_to_num(cci, 0).tolist()
        
        # NumPy实现
        tp = (_float_array(highs) + _float_array(lows) + _float_array(closes)) / 3
        cci = np.zeros(len(closes))
        
        for i in range(period - 1, len(closes)):
            sma = np.mean(tp[i-period+1:i+1])
            md = np.mean(np.abs(tp[i-period+1:i+1] - sma))
            if md != 0:
                cci[i] = (tp[i] - sma) / (0.015 * md)
        
        return cci.tolist()
    
    @staticmethod
    def calculate_wr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
        """计算威廉指标 (Williams %R)"""
        if TALIB_AVAILABLE:
            wr = talib.WILLR(_float_array(highs), _float_array(lows), _float_array(closes), timeperiod=period)
            return np.nan_to_num(wr, 0).tolist()
        
        # NumPy实现
        wr = np.zeros(len(closes))
        
        for i in range(period - 1, len(closes)):
            highest = max(highs[i-period+1:i+1])
            lowest = min(lows[i-period+1:i+1])
            
            if highest != lowest:
                wr[i] = -100 * (highest - closes[i]) / (highest - lowest)
            else:
                wr[i] = -50
        
        return wr.tolist()
    
    @staticmethod
    def calculate_roc(closes: List[float], period: int = 12) -> List[float]:
        """计算变动率指标 (Rate of Change)"""
        if TALIB_AVAILABLE:
            roc = talib.ROC(_float_array(closes), timeperiod=period)
            return np.nan_to_num(roc, 0).tolist()
        
        # NumPy实现
        roc = np.zeros(len(closes))
        
        for i in range(period, len(closes)):
            if closes[i-period] != 0:
                roc[i] = ((closes[i] - closes[i-period]) / closes[i-period]) * 100
        
        return roc.tolist()

    @staticmethod
    def calculate_trix(
        closes: List[float], period: int = 12, n: Optional[int] = None, m: Optional[int] = None
    ) -> Dict[str, List[float]]:
        """计算 TRIX 指标，兼容旧参数名 n/m，返回 TRIX 与 MATRIX。"""
        period = int(n or period or 12)
        matrix_period = int(m or 9)
        ema1 = np.array(TechnicalAnalysis._calculate_ema_numpy(closes, period))
        ema2 = np.array(TechnicalAnalysis._calculate_ema_numpy(ema1.tolist(), period))
        ema3 = np.array(TechnicalAnalysis._calculate_ema_numpy(ema2.tolist(), period))
        trix = np.zeros(len(closes))
        for i in range(1, len(closes)):
            if ema3[i - 1] != 0:
                trix[i] = (ema3[i] - ema3[i - 1]) / ema3[i - 1] * 100
        matrix = np.array(TechnicalAnalysis._calculate_sma_numpy(trix.tolist(), matrix_period))
        return {"TRIX": trix.tolist(), "MATRIX": matrix.tolist()}

    @staticmethod
    def calculate_dma_indicator(
        closes: List[float], short: int = 10, long_period: int = 50, m: int = 10
    ) -> Dict[str, List[float]]:
        """计算 DMA 指标，返回 DIF 与 AMA，并兼容旧字段 DIFMA。"""
        short_ma = np.array(TechnicalAnalysis._calculate_sma_numpy(closes, short))
        long_ma = np.array(TechnicalAnalysis._calculate_sma_numpy(closes, long_period))
        dif = short_ma - long_ma
        ama = np.array(TechnicalAnalysis._calculate_sma_numpy(dif.tolist(), m))
        ama_list = ama.tolist()
        return {"DIF": dif.tolist(), "AMA": ama_list, "DIFMA": ama_list}

    @staticmethod
    def calculate_expma(closes: List[float], n1: int = 12, n2: int = 50) -> Dict[str, List[float]]:
        """计算 EXPMA 指标，返回两条指数均线，并兼容旧字段 EXP1/EXP2。"""
        exp1 = TechnicalAnalysis._calculate_ema_numpy(closes, n1)
        exp2 = TechnicalAnalysis._calculate_ema_numpy(closes, n2)
        return {
            "EXPMA1": exp1,
            "EXPMA2": exp2,
            "EXP1": exp1,
            "EXP2": exp2,
        }

    @staticmethod
    def calculate_dmi(
        highs: List[float], lows: List[float], closes: List[float], n: int = 14, m: int = 6
    ) -> Dict[str, List[float]]:
        """计算 DMI 指标，返回 PDI/MDI/ADX/ADXR。"""
        n_bars = len(closes)
        if n_bars < max(2, n):
            return {"PDI": [], "MDI": [], "ADX": [], "ADXR": []}

        pdi = np.zeros(n_bars)
        mdi = np.zeros(n_bars)
        dx = np.zeros(n_bars)

        tr = np.zeros(n_bars)
        plus_dm = np.zeros(n_bars)
        minus_dm = np.zeros(n_bars)
        for i in range(1, n_bars):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0
            minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

        for i in range(n, n_bars):
            tr_n = np.sum(tr[i - n + 1:i + 1])
            pdm_n = np.sum(plus_dm[i - n + 1:i + 1])
            mdm_n = np.sum(minus_dm[i - n + 1:i + 1])
            if tr_n > 0:
                pdi[i] = pdm_n / tr_n * 100
                mdi[i] = mdm_n / tr_n * 100
            if (pdi[i] + mdi[i]) > 0:
                dx[i] = abs(pdi[i] - mdi[i]) / (pdi[i] + mdi[i]) * 100

        adx = np.array(TechnicalAnalysis._calculate_sma_numpy(dx.tolist(), m))
        adxr = np.zeros(n_bars)
        for i in range(m, n_bars):
            adxr[i] = (adx[i] + adx[i - m]) / 2

        return {"PDI": pdi.tolist(), "MDI": mdi.tolist(), "ADX": adx.tolist(), "ADXR": adxr.tolist()}

    @staticmethod
    def calculate_cr_indicator(
        highs: List[float], lows: List[float], closes: List[float], n: int = 26
    ) -> Dict[str, List[float]]:
        """计算 CR 指标，返回 CR 与 MA1/MA2/MA3/MA4。"""
        n_bars = len(closes)
        cr = np.zeros(n_bars)
        mid = (_float_array(highs) + _float_array(lows)) / 2
        for i in range(1, n_bars):
            start = max(1, i - n + 1)
            up_sum = np.sum(np.maximum(0, _float_array(highs[start:i + 1]) - mid[start - 1:i]))
            down_sum = np.sum(np.maximum(0, mid[start - 1:i] - _float_array(lows[start:i + 1])))
            if down_sum > 0:
                cr[i] = up_sum / down_sum * 100

        return {
            "CR": cr.tolist(),
            "MA1": np.array(TechnicalAnalysis._calculate_sma_numpy(cr.tolist(), 5)).tolist(),
            "MA2": np.array(TechnicalAnalysis._calculate_sma_numpy(cr.tolist(), 10)).tolist(),
            "MA3": np.array(TechnicalAnalysis._calculate_sma_numpy(cr.tolist(), 20)).tolist(),
            "MA4": np.array(TechnicalAnalysis._calculate_sma_numpy(cr.tolist(), 40)).tolist(),
        }

    @staticmethod
    def calculate_vr_indicator(
        closes: List[float], volumes: List[float], n: int = 26, m: int = 6
    ) -> Dict[str, List[float]]:
        """计算 VR 指标，返回 VR 序列，并兼容旧字段 MAVR。"""
        n_bars = len(closes)
        vr = np.zeros(n_bars)
        close_arr = _float_array(closes)
        vol_arr = _float_array(volumes)

        for i in range(1, n_bars):
            start = max(1, i - n + 1)
            av = bv = cv = 0.0
            for j in range(start, i + 1):
                if close_arr[j] > close_arr[j - 1]:
                    av += vol_arr[j]
                elif close_arr[j] < close_arr[j - 1]:
                    bv += vol_arr[j]
                else:
                    cv += vol_arr[j]
            denominator = bv + 0.5 * cv
            if denominator > 0:
                vr[i] = (av + 0.5 * cv) / denominator * 100

        vr_list = vr.tolist()
        mavr = TechnicalAnalysis._calculate_sma_numpy(vr_list, m)
        return {"VR": vr_list, "MAVR": mavr}


# 全局实例
technical_analysis = TechnicalAnalysis()
