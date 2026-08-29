"""
向量化技术指标计算模块

使用 NumPy 进行向量化计算，大幅提升性能
支持批量计算多个股票的技术指标
"""

import numpy as np
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass


@dataclass
class IndicatorResult:
    """指标计算结果"""
    values: np.ndarray
    signal: Optional[str] = None  # 'buy', 'sell', 'hold'
    metadata: Optional[Dict] = None


class VectorizedIndicators:
    """向量化技术指标计算器"""
    
    @staticmethod
    def sma(data: np.ndarray, period: int) -> np.ndarray:
        """
        简单移动平均 (Simple Moving Average)
        
        Args:
            data: 价格数据数组
            period: 周期
            
        Returns:
            SMA 值数组
        """
        if len(data) < period:
            return np.array([])
        
        # 使用卷积计算移动平均，比循环快 10-50 倍
        weights = np.ones(period) / period
        sma = np.convolve(data, weights, mode='valid')
        
        # 前面补充 NaN 以保持长度一致
        result = np.full(len(data), np.nan)
        result[period-1:] = sma
        
        return result
    
    @staticmethod
    def ema(data: np.ndarray, period: int) -> np.ndarray:
        """
        指数移动平均 (Exponential Moving Average)
        
        Args:
            data: 价格数据数组
            period: 周期
            
        Returns:
            EMA 值数组
        """
        if len(data) < period:
            return np.array([])
        
        alpha = 2 / (period + 1)
        ema = np.zeros_like(data)
        ema[0] = data[0]
        
        # 向量化计算 EMA
        for i in range(1, len(data)):
            ema[i] = alpha * data[i] + (1 - alpha) * ema[i-1]
        
        # 前 period-1 个值设为 NaN
        ema[:period-1] = np.nan
        
        return ema
    
    @staticmethod
    def rsi(data: np.ndarray, period: int = 14) -> IndicatorResult:
        """
        相对强弱指标 (Relative Strength Index)
        
        Args:
            data: 价格数据数组
            period: 周期，默认 14
            
        Returns:
            RSI 值和交易信号
        """
        if len(data) < period + 1:
            return IndicatorResult(values=np.array([]))
        
        # 计算价格变化
        deltas = np.diff(data)
        
        # 分离涨跌
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        # 计算平均涨跌
        avg_gains = np.zeros(len(data))
        avg_losses = np.zeros(len(data))
        
        # 初始平均值
        avg_gains[period] = np.mean(gains[:period])
        avg_losses[period] = np.mean(losses[:period])
        
        # 指数移动平均
        for i in range(period + 1, len(data)):
            avg_gains[i] = (avg_gains[i-1] * (period - 1) + gains[i-1]) / period
            avg_losses[i] = (avg_losses[i-1] * (period - 1) + losses[i-1]) / period
        
        # 计算 RSI
        rs = np.where(avg_losses != 0, avg_gains / avg_losses, 0)
        rsi = 100 - (100 / (1 + rs))
        
        # 前 period 个值设为 NaN
        rsi[:period] = np.nan
        
        # 生成交易信号
        last_rsi = rsi[-1]
        signal = None
        if not np.isnan(last_rsi):
            if last_rsi < 30:
                signal = 'buy'
            elif last_rsi > 70:
                signal = 'sell'
            else:
                signal = 'hold'
        
        return IndicatorResult(
            values=rsi,
            signal=signal,
            metadata={'period': period, 'last_value': last_rsi}
        )
    
    @staticmethod
    def macd(data: np.ndarray, 
             fast_period: int = 12, 
             slow_period: int = 26, 
             signal_period: int = 9) -> IndicatorResult:
        """
        MACD 指标 (Moving Average Convergence Divergence)
        
        Args:
            data: 价格数据数组
            fast_period: 快线周期，默认 12
            slow_period: 慢线周期，默认 26
            signal_period: 信号线周期，默认 9
            
        Returns:
            MACD 值、信号线、柱状图和交易信号
        """
        # 检查数据量是否足够
        min_required = slow_period + signal_period
        if len(data) < min_required:
            return IndicatorResult(
                values=np.array([]),
                signal=None,
                metadata={
                    'error': f'数据不足：需要至少{min_required}天，当前{len(data)}天',
                    'macd': np.array([]),
                    'signal_line': np.array([]),
                    'histogram': np.array([]),
                    'fast_period': fast_period,
                    'slow_period': slow_period,
                    'signal_period': signal_period
                }
            )
        
        # 计算快慢 EMA
        ema_fast = VectorizedIndicators.ema(data, fast_period)
        ema_slow = VectorizedIndicators.ema(data, slow_period)
        
        # MACD 线 = 快线 - 慢线
        macd_line = ema_fast - ema_slow
        
        # 修复：直接对MACD线计算信号线，保持数组长度一致
        signal_line_full = np.full(len(data), np.nan)
        
        # 从slow_period-1开始有有效的MACD值
        valid_start = slow_period - 1
        if valid_start + signal_period <= len(data):
            # 对有效的MACD值计算EMA作为信号线
            macd_valid = macd_line[valid_start:]
            # 过滤掉NaN值
            macd_valid_clean = macd_valid[~np.isnan(macd_valid)]
            if len(macd_valid_clean) >= signal_period:
                signal_values = VectorizedIndicators.ema(macd_valid_clean, signal_period)
                # 将信号线值放回正确的位置
                signal_start_idx = valid_start + (len(macd_valid) - len(macd_valid_clean))
                signal_line_full[signal_start_idx:signal_start_idx + len(signal_values)] = signal_values
        
        # 柱状图 = MACD - 信号线
        histogram = macd_line - signal_line_full
        
        # 生成交易信号（金叉死叉）
        signal = None
        if len(histogram) >= 2:
            last_hist = histogram[-1]
            prev_hist = histogram[-2]
            if not np.isnan(last_hist) and not np.isnan(prev_hist):
                if last_hist > 0 and prev_hist < 0:
                    signal = 'buy'  # 金叉
                elif last_hist < 0 and prev_hist > 0:
                    signal = 'sell'  # 死叉
                else:
                    signal = 'hold'
        
        return IndicatorResult(
            values=histogram,
            signal=signal,
            metadata={
                'macd': macd_line,
                'signal_line': signal_line_full,
                'histogram': histogram,
                'fast_period': fast_period,
                'slow_period': slow_period,
                'signal_period': signal_period
            }
        )
    
    @staticmethod
    def kdj(high: np.ndarray, 
            low: np.ndarray, 
            close: np.ndarray, 
            period: int = 9, 
            k_period: int = 3, 
            d_period: int = 3) -> IndicatorResult:
        """
        KDJ 指标 (Stochastic Oscillator)
        
        Args:
            high: 最高价数组
            low: 最低价数组
            close: 收盘价数组
            period: 周期，默认 9
            k_period: K 值平滑周期，默认 3
            d_period: D 值平滑周期，默认 3
            
        Returns:
            K, D, J 值和交易信号
        """
        if len(close) < period:
            return IndicatorResult(values=np.array([]))
        
        # 计算 RSV (Raw Stochastic Value)
        rsv = np.zeros(len(close))
        for i in range(period - 1, len(close)):
            period_high = np.max(high[i-period+1:i+1])
            period_low = np.min(low[i-period+1:i+1])
            if period_high != period_low:
                rsv[i] = (close[i] - period_low) / (period_high - period_low) * 100
            else:
                rsv[i] = 50
        
        # 计算 K 值（RSV 的移动平均）
        k = np.zeros(len(close))
        k[period-1] = rsv[period-1]
        for i in range(period, len(close)):
            k[i] = (k[i-1] * (k_period - 1) + rsv[i]) / k_period
        
        # 计算 D 值（K 的移动平均）
        d = np.zeros(len(close))
        d[period-1] = k[period-1]
        for i in range(period, len(close)):
            d[i] = (d[i-1] * (d_period - 1) + k[i]) / d_period
        
        # 计算 J 值
        j = 3 * k - 2 * d
        
        # 前 period-1 个值设为 NaN
        k[:period-1] = np.nan
        d[:period-1] = np.nan
        j[:period-1] = np.nan
        
        # 生成交易信号
        last_j = j[-1]
        signal = None
        if not np.isnan(last_j):
            if last_j < 20:
                signal = 'buy'
            elif last_j > 80:
                signal = 'sell'
            else:
                signal = 'hold'
        
        return IndicatorResult(
            values=j,
            signal=signal,
            metadata={
                'k': k,
                'd': d,
                'j': j,
                'period': period
            }
        )
    
    @staticmethod
    def bollinger_bands(data: np.ndarray, 
                       period: int = 20, 
                       std_dev: float = 2.0) -> IndicatorResult:
        """
        布林带 (Bollinger Bands)
        
        Args:
            data: 价格数据数组
            period: 周期，默认 20
            std_dev: 标准差倍数，默认 2.0
            
        Returns:
            上轨、中轨、下轨
        """
        if len(data) < period:
            return IndicatorResult(values=np.array([]))
        
        # 中轨 = SMA
        middle = VectorizedIndicators.sma(data, period)
        
        # 计算滚动标准差
        std = np.full(len(data), np.nan)
        for i in range(period - 1, len(data)):
            std[i] = np.std(data[i-period+1:i+1])
        
        # 上下轨
        upper = middle + std_dev * std
        lower = middle - std_dev * std
        
        # 生成交易信号
        last_close = data[-1]
        last_upper = upper[-1]
        last_lower = lower[-1]
        last_middle = middle[-1]
        
        signal = None
        if not np.isnan(last_upper) and not np.isnan(last_lower):
            if last_close < last_lower:
                signal = 'buy'  # 价格触及下轨
            elif last_close > last_upper:
                signal = 'sell'  # 价格触及上轨
            else:
                signal = 'hold'
        
        return IndicatorResult(
            values=middle,
            signal=signal,
            metadata={
                'upper': upper,
                'middle': middle,
                'lower': lower,
                'period': period,
                'std_dev': std_dev
            }
        )
    
    @staticmethod
    def batch_calculate(prices_dict: Dict[str, np.ndarray],
                       indicators: List[str],
                       **kwargs) -> Dict[str, Dict[str, IndicatorResult]]:
        """
        批量计算多个股票的技术指标
        
        Args:
            prices_dict: {股票代码: 价格数组} 字典
            indicators: 指标列表，如 ['sma', 'rsi', 'macd']
            **kwargs: 指标参数
            
        Returns:
            {股票代码: {指标名: 结果}} 嵌套字典
        """
        results = {}
        
        for code, prices in prices_dict.items():
            code_results = {}
            
            for indicator in indicators:
                if indicator == 'sma':
                    period = kwargs.get('sma_period', 20)
                    values = VectorizedIndicators.sma(prices, period)
                    code_results[indicator] = IndicatorResult(values=values)
                
                elif indicator == 'ema':
                    period = kwargs.get('ema_period', 20)
                    values = VectorizedIndicators.ema(prices, period)
                    code_results[indicator] = IndicatorResult(values=values)
                
                elif indicator == 'rsi':
                    period = kwargs.get('rsi_period', 14)
                    code_results[indicator] = VectorizedIndicators.rsi(prices, period)
                
                elif indicator == 'macd':
                    fast = kwargs.get('macd_fast', 12)
                    slow = kwargs.get('macd_slow', 26)
                    signal = kwargs.get('macd_signal', 9)
                    code_results[indicator] = VectorizedIndicators.macd(
                        prices, fast, slow, signal
                    )
                
                elif indicator == 'boll':
                    period = kwargs.get('boll_period', 20)
                    std_dev = kwargs.get('boll_std', 2.0)
                    code_results[indicator] = VectorizedIndicators.bollinger_bands(
                        prices, period, std_dev
                    )
            
            results[code] = code_results
        
        return results


# 便捷函数
def calculate_sma(data: Union[List, np.ndarray], period: int) -> np.ndarray:
    """计算 SMA"""
    return VectorizedIndicators.sma(np.array(data), period)


def calculate_ema(data: Union[List, np.ndarray], period: int) -> np.ndarray:
    """计算 EMA"""
    return VectorizedIndicators.ema(np.array(data), period)


def calculate_rsi(data: Union[List, np.ndarray], period: int = 14) -> IndicatorResult:
    """计算 RSI"""
    return VectorizedIndicators.rsi(np.array(data), period)


def calculate_macd(data: Union[List, np.ndarray],
                  fast: int = 12,
                  slow: int = 26,
                  signal: int = 9) -> IndicatorResult:
    """计算 MACD"""
    return VectorizedIndicators.macd(np.array(data), fast, slow, signal)


def calculate_kdj(high: Union[List, np.ndarray],
                 low: Union[List, np.ndarray],
                 close: Union[List, np.ndarray],
                 period: int = 9) -> IndicatorResult:
    """计算 KDJ"""
    return VectorizedIndicators.kdj(
        np.array(high),
        np.array(low),
        np.array(close),
        period
    )


def calculate_bollinger(data: Union[List, np.ndarray],
                       period: int = 20,
                       std_dev: float = 2.0) -> IndicatorResult:
    """计算布林带"""
    return VectorizedIndicators.bollinger_bands(np.array(data), period, std_dev)
