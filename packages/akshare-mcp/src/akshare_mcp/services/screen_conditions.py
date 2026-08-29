"""
选股条件库 - 所有内置选股条件的实现

每个条件函数签名: (klines: list[dict], params: dict) -> bool
klines 按时间升序排列，klines[-1] 是最新一天
"""

import numpy as np
from .screen_engine import engine
from .technical_analysis import TechnicalAnalysis

ta = TechnicalAnalysis()


# ============== 辅助函数 ==============

def _closes(klines):
    return [k['close'] for k in klines]

def _highs(klines):
    return [k['high'] for k in klines]

def _lows(klines):
    return [k['low'] for k in klines]

def _volumes(klines):
    return [k.get('volume', 0) or 0 for k in klines]

def _sma(data, n):
    if len(data) < n:
        return [0.0] * len(data)
    arr = np.array(data, dtype=float)
    result = np.zeros(len(data))
    for i in range(n - 1, len(data)):
        result[i] = np.mean(arr[i - n + 1:i + 1])
    return result.tolist()

def _cross_above(fast, slow, idx=-1):
    return fast[idx - 1] <= slow[idx - 1] and fast[idx] > slow[idx]

def _cross_below(fast, slow, idx=-1):
    return fast[idx - 1] >= slow[idx - 1] and fast[idx] < slow[idx]

def _change_pct(klines, i=-1):
    c = klines[i]['close']
    pc = klines[i - 1]['close']
    return (c - pc) / pc if pc else 0


# ============== 趋势类条件 ==============

@engine.register('upn', '连续N日上涨', 'trend', '收盘价连续N天高于前一天', {'n': 3})
def cond_upn(klines, params):
    n = params.get('n', 3)
    if len(klines) < n + 1:
        return False
    for i in range(-n, 0):
        if klines[i]['close'] <= klines[i - 1]['close']:
            return False
    return True

@engine.register('downn', '连续N日下跌', 'trend', '收盘价连续N天低于前一天', {'n': 3})
def cond_downn(klines, params):
    n = params.get('n', 3)
    if len(klines) < n + 1:
        return False
    for i in range(-n, 0):
        if klines[i]['close'] >= klines[i - 1]['close']:
            return False
    return True

@engine.register('ma_bull', '均线多头排列', 'trend', 'MA5>MA10>MA20>MA60', {}, min_klines=60)
def cond_ma_bull(klines, params):
    closes = _closes(klines)
    ma5 = _sma(closes, 5)[-1]
    ma10 = _sma(closes, 10)[-1]
    ma20 = _sma(closes, 20)[-1]
    ma60 = _sma(closes, 60)[-1]
    return ma5 > ma10 > ma20 > ma60 > 0

@engine.register('ma_bear', '均线空头排列', 'trend', 'MA5<MA10<MA20<MA60', {}, min_klines=60)
def cond_ma_bear(klines, params):
    closes = _closes(klines)
    ma5 = _sma(closes, 5)[-1]
    ma10 = _sma(closes, 10)[-1]
    ma20 = _sma(closes, 20)[-1]
    ma60 = _sma(closes, 60)[-1]
    return 0 < ma5 < ma10 < ma20 < ma60

@engine.register('price_above_ma', '站上均线', 'trend', '收盘价>MA(N)', {'n': 20})
def cond_price_above_ma(klines, params):
    n = params.get('n', 20)
    closes = _closes(klines)
    ma = _sma(closes, n)[-1]
    return closes[-1] > ma > 0

@engine.register('price_below_ma', '跌破均线', 'trend', '收盘价<MA(N)', {'n': 20})
def cond_price_below_ma(klines, params):
    n = params.get('n', 20)
    closes = _closes(klines)
    ma = _sma(closes, n)[-1]
    return 0 < closes[-1] < ma

@engine.register('golden_cross_ma', '均线金叉', 'trend', '短期均线上穿长期均线', {'short': 5, 'long': 20})
def cond_golden_cross_ma(klines, params):
    closes = _closes(klines)
    short_ma = _sma(closes, params.get('short', 5))
    long_ma = _sma(closes, params.get('long', 20))
    return _cross_above(short_ma, long_ma)

@engine.register('death_cross_ma', '均线死叉', 'trend', '短期均线下穿长期均线', {'short': 5, 'long': 20})
def cond_death_cross_ma(klines, params):
    closes = _closes(klines)
    short_ma = _sma(closes, params.get('short', 5))
    long_ma = _sma(closes, params.get('long', 20))
    return _cross_below(short_ma, long_ma)

@engine.register('new_high', '创N日新高', 'trend', '收盘价为N日内最高', {'n': 20})
def cond_new_high(klines, params):
    n = params.get('n', 20)
    closes = _closes(klines)
    if len(closes) < n:
        return False
    return closes[-1] >= max(closes[-n:])

@engine.register('new_low', '创N日新低', 'trend', '收盘价为N日内最低', {'n': 20})
def cond_new_low(klines, params):
    n = params.get('n', 20)
    closes = _closes(klines)
    if len(closes) < n:
        return False
    return closes[-1] <= min(closes[-n:])

@engine.register('breakout_high', '突破前高', 'trend', '收盘价突破N日内最高价', {'n': 60}, min_klines=60)
def cond_breakout_high(klines, params):
    n = params.get('n', 60)
    highs = _highs(klines)
    closes = _closes(klines)
    if len(highs) < n + 1:
        return False
    prev_high = max(highs[-n - 1:-1])
    return closes[-1] > prev_high

@engine.register('trend_up', '上升趋势', 'trend', '近N日高点和低点均抬升', {'n': 20}, min_klines=20)
def cond_trend_up(klines, params):
    n = params.get('n', 20)
    recent = klines[-n:]
    mid = n // 2
    first_half_high = max(k['high'] for k in recent[:mid])
    second_half_high = max(k['high'] for k in recent[mid:])
    first_half_low = min(k['low'] for k in recent[:mid])
    second_half_low = min(k['low'] for k in recent[mid:])
    return second_half_high > first_half_high and second_half_low > first_half_low


# ============== 技术指标类条件 ==============

@engine.register('macd_golden_cross', 'MACD金叉', 'indicator', 'DIF上穿DEA',
                 {'fast': 12, 'slow': 26, 'signal': 9}, min_klines=35)
def cond_macd_golden_cross(klines, params):
    closes = _closes(klines)
    result = ta.calculate_macd(closes, params.get('fast', 12),
                               params.get('slow', 26), params.get('signal', 9))
    dif, dea = result['macd'], result['signal']
    return _cross_above(dif, dea)

@engine.register('macd_death_cross', 'MACD死叉', 'indicator', 'DIF下穿DEA',
                 {'fast': 12, 'slow': 26, 'signal': 9}, min_klines=35)
def cond_macd_death_cross(klines, params):
    closes = _closes(klines)
    result = ta.calculate_macd(closes, params.get('fast', 12),
                               params.get('slow', 26), params.get('signal', 9))
    dif, dea = result['macd'], result['signal']
    return _cross_below(dif, dea)

@engine.register('macd_above_zero', 'MACD零轴上方', 'indicator', 'DIF和DEA均>0', {}, min_klines=35)
def cond_macd_above_zero(klines, params):
    closes = _closes(klines)
    result = ta.calculate_macd(closes)
    return result['macd'][-1] > 0 and result['signal'][-1] > 0

@engine.register('macd_divergence_bull', 'MACD底背离', 'indicator',
                 '价格新低但MACD不创新低', {'lookback': 60}, min_klines=60)
def cond_macd_divergence_bull(klines, params):
    lb = params.get('lookback', 60)
    closes = _closes(klines)
    result = ta.calculate_macd(closes)
    dif = result['macd']
    recent_closes = closes[-lb:]
    recent_dif = dif[-lb:]
    mid = lb // 2
    price_low1 = min(recent_closes[:mid])
    price_low2 = min(recent_closes[mid:])
    dif_low1 = min(recent_dif[:mid])
    dif_low2 = min(recent_dif[mid:])
    return price_low2 < price_low1 and dif_low2 > dif_low1

@engine.register('macd_divergence_bear', 'MACD顶背离', 'indicator',
                 '价格新高但MACD不创新高', {'lookback': 60}, min_klines=60)
def cond_macd_divergence_bear(klines, params):
    lb = params.get('lookback', 60)
    closes = _closes(klines)
    result = ta.calculate_macd(closes)
    dif = result['macd']
    recent_closes = closes[-lb:]
    recent_dif = dif[-lb:]
    mid = lb // 2
    price_high1 = max(recent_closes[:mid])
    price_high2 = max(recent_closes[mid:])
    dif_high1 = max(recent_dif[:mid])
    dif_high2 = max(recent_dif[mid:])
    return price_high2 > price_high1 and dif_high2 < dif_high1

@engine.register('kdj_golden_cross', 'KDJ金叉', 'indicator', 'K线上穿D线',
                 {'n': 9, 'm1': 3, 'm2': 3})
def cond_kdj_golden_cross(klines, params):
    result = ta.calculate_kdj(_highs(klines), _lows(klines), _closes(klines),
                              params.get('n', 9), params.get('m1', 3), params.get('m2', 3))
    return _cross_above(result['k'], result['d'])

@engine.register('kdj_death_cross', 'KDJ死叉', 'indicator', 'K线下穿D线',
                 {'n': 9, 'm1': 3, 'm2': 3})
def cond_kdj_death_cross(klines, params):
    result = ta.calculate_kdj(_highs(klines), _lows(klines), _closes(klines),
                              params.get('n', 9), params.get('m1', 3), params.get('m2', 3))
    return _cross_below(result['k'], result['d'])

@engine.register('kdj_oversold', 'KDJ超卖', 'indicator', 'J值<0或K<20', {'threshold': 20})
def cond_kdj_oversold(klines, params):
    result = ta.calculate_kdj(_highs(klines), _lows(klines), _closes(klines))
    threshold = params.get('threshold', 20)
    return result['j'][-1] < 0 or result['k'][-1] < threshold

@engine.register('kdj_overbought', 'KDJ超买', 'indicator', 'J值>100或K>80', {'threshold': 80})
def cond_kdj_overbought(klines, params):
    result = ta.calculate_kdj(_highs(klines), _lows(klines), _closes(klines))
    threshold = params.get('threshold', 80)
    return result['j'][-1] > 100 or result['k'][-1] > threshold

@engine.register('rsi_oversold', 'RSI超卖', 'indicator', 'RSI<阈值',
                 {'period': 14, 'threshold': 30})
def cond_rsi_oversold(klines, params):
    closes = _closes(klines)
    period = params.get('period', 14)
    threshold = params.get('threshold', 30)
    rsi_result = ta.calculate_rsi(closes, period)
    rsi_val = rsi_result.get('value') if isinstance(rsi_result, dict) else None
    if rsi_val is None:  # F-N04-1: warmup 不足，不视为超卖命中
        return False
    return rsi_val < threshold

@engine.register('rsi_overbought', 'RSI超买', 'indicator', 'RSI>阈值',
                 {'period': 14, 'threshold': 70})
def cond_rsi_overbought(klines, params):
    closes = _closes(klines)
    period = params.get('period', 14)
    threshold = params.get('threshold', 70)
    rsi_result = ta.calculate_rsi(closes, period)
    rsi_val = rsi_result.get('value') if isinstance(rsi_result, dict) else None
    if rsi_val is None:  # F-N04-1: warmup 不足，不视为超买命中
        return False
    return rsi_val > threshold

@engine.register('boll_squeeze', '布林带收窄', 'indicator', '带宽<历史均值的指定比例',
                 {'n': 20, 'std': 2, 'ratio': 0.5}, min_klines=40)
def cond_boll_squeeze(klines, params):
    closes = _closes(klines)
    n = params.get('n', 20)
    std = params.get('std', 2)
    ratio = params.get('ratio', 0.5)
    result = ta.calculate_bollinger_bands(closes, n, float(std))
    upper, lower, middle = result['upper'], result['lower'], result['middle']
    bandwidths = [(u - l) / m if m else 0 for u, l, m in zip(upper, lower, middle)]
    recent_bw = bandwidths[-1]
    avg_bw = np.mean(bandwidths[-40:]) if len(bandwidths) >= 40 else np.mean(bandwidths)
    return recent_bw < avg_bw * ratio

@engine.register('boll_breakout_upper', '突破布林上轨', 'indicator', '收盘价>布林上轨',
                 {'n': 20, 'std': 2})
def cond_boll_breakout_upper(klines, params):
    closes = _closes(klines)
    result = ta.calculate_bollinger_bands(closes, params.get('n', 20), float(params.get('std', 2)))
    return closes[-1] > result['upper'][-1]

@engine.register('boll_breakout_lower', '跌破布林下轨', 'indicator', '收盘价<布林下轨',
                 {'n': 20, 'std': 2})
def cond_boll_breakout_lower(klines, params):
    closes = _closes(klines)
    result = ta.calculate_bollinger_bands(closes, params.get('n', 20), float(params.get('std', 2)))
    return closes[-1] < result['lower'][-1]

@engine.register('dmi_trend_strong', 'DMI趋势增强', 'indicator', 'ADX>25且+DI>-DI',
                 {'n': 14, 'adx_threshold': 25}, min_klines=30)
def cond_dmi_trend_strong(klines, params):
    result = ta.calculate_dmi(_highs(klines), _lows(klines), _closes(klines),
                              params.get('n', 14))
    adx_threshold = params.get('adx_threshold', 25)
    return result['ADX'][-1] > adx_threshold and result['PDI'][-1] > result['MDI'][-1]


# ============== 量价关系类条件 ==============

@engine.register('volume_breakout', '放量突破', 'volume',
                 '成交量>N日均量M倍且涨幅>P%',
                 {'n': 20, 'multiplier': 2.0, 'min_pct': 2}, requires_volume=True)
def cond_volume_breakout(klines, params):
    n = params.get('n', 20)
    m = params.get('multiplier', 2.0)
    p = params.get('min_pct', 2)
    volumes = _volumes(klines)
    if len(volumes) < n + 1:
        return False
    avg_vol = np.mean(volumes[-n - 1:-1])
    today_vol = volumes[-1]
    pct = _change_pct(klines) * 100
    return today_vol > avg_vol * m and pct > p

@engine.register('volume_shrink', '缩量整理', 'volume', '成交量<N日均量的指定比例',
                 {'n': 20, 'ratio': 0.5}, requires_volume=True)
def cond_volume_shrink(klines, params):
    n = params.get('n', 20)
    ratio = params.get('ratio', 0.5)
    volumes = _volumes(klines)
    if len(volumes) < n + 1:
        return False
    avg_vol = np.mean(volumes[-n - 1:-1])
    return volumes[-1] < avg_vol * ratio

@engine.register('volume_price_up', '量价齐升', 'volume', '连续N日量增价涨',
                 {'n': 3}, requires_volume=True)
def cond_volume_price_up(klines, params):
    n = params.get('n', 3)
    if len(klines) < n + 1:
        return False
    for i in range(-n, 0):
        if klines[i]['close'] <= klines[i - 1]['close']:
            return False
        if (klines[i].get('volume', 0) or 0) <= (klines[i - 1].get('volume', 0) or 0):
            return False
    return True

@engine.register('volume_price_diverge', '量价背离', 'volume', '价格上涨但成交量萎缩',
                 {'n': 5}, requires_volume=True)
def cond_volume_price_diverge(klines, params):
    n = params.get('n', 5)
    if len(klines) < n + 1:
        return False
    closes = _closes(klines)
    volumes = _volumes(klines)
    return closes[-1] > closes[-n - 1] and volumes[-1] < volumes[-n - 1]

@engine.register('low_volume_bottom', '地量见底', 'volume', '成交量为N日内最低且处于价格低位',
                 {'n': 60}, requires_volume=True, min_klines=60)
def cond_low_volume_bottom(klines, params):
    n = params.get('n', 60)
    volumes = _volumes(klines)
    closes = _closes(klines)
    if len(volumes) < n:
        return False
    recent_vols = volumes[-n:]
    recent_closes = closes[-n:]
    is_lowest_vol = volumes[-1] <= min(recent_vols)
    price_range = max(recent_closes) - min(recent_closes)
    price_pct = (closes[-1] - min(recent_closes)) / price_range if price_range else 0.5
    return is_lowest_vol and price_pct < 0.3

@engine.register('volume_ratio_high', '量比大于N', 'volume', '今日成交量/5日均量>N',
                 {'ratio': 2.0}, requires_volume=True)
def cond_volume_ratio_high(klines, params):
    ratio = params.get('ratio', 2.0)
    volumes = _volumes(klines)
    if len(volumes) < 6:
        return False
    avg5 = np.mean(volumes[-6:-1])
    return (volumes[-1] / avg5) > ratio if avg5 > 0 else False


# ============== K线形态类条件 ==============

@engine.register('pattern_hammer', '锤头线', 'pattern', '底部反转信号')
def cond_pattern_hammer(klines, params):
    k = klines[-1]
    body = abs(k['close'] - k['open'])
    lower_shadow = min(k['open'], k['close']) - k['low']
    upper_shadow = k['high'] - max(k['open'], k['close'])
    if body == 0:
        return False
    return lower_shadow > body * 2 and upper_shadow < body * 0.5

@engine.register('pattern_shooting_star', '流星线', 'pattern', '顶部反转信号')
def cond_pattern_shooting_star(klines, params):
    k = klines[-1]
    body = abs(k['close'] - k['open'])
    upper_shadow = k['high'] - max(k['open'], k['close'])
    lower_shadow = min(k['open'], k['close']) - k['low']
    if body == 0:
        return False
    return upper_shadow > body * 2 and lower_shadow < body * 0.5

@engine.register('pattern_engulfing_bull', '看涨吞没', 'pattern', '阳线完全包住前一阴线')
def cond_pattern_engulfing_bull(klines, params):
    if len(klines) < 2:
        return False
    prev, curr = klines[-2], klines[-1]
    prev_bear = prev['close'] < prev['open']
    curr_bull = curr['close'] > curr['open']
    engulf = curr['open'] <= prev['close'] and curr['close'] >= prev['open']
    return prev_bear and curr_bull and engulf

@engine.register('pattern_engulfing_bear', '看跌吞没', 'pattern', '阴线完全包住前一阳线')
def cond_pattern_engulfing_bear(klines, params):
    if len(klines) < 2:
        return False
    prev, curr = klines[-2], klines[-1]
    prev_bull = prev['close'] > prev['open']
    curr_bear = curr['close'] < curr['open']
    engulf = curr['open'] >= prev['close'] and curr['close'] <= prev['open']
    return prev_bull and curr_bear and engulf

@engine.register('pattern_morning_star', '早晨之星', 'pattern', '三根K线底部反转')
def cond_pattern_morning_star(klines, params):
    if len(klines) < 3:
        return False
    k1, k2, k3 = klines[-3], klines[-2], klines[-1]
    hl1 = k1['high'] - k1['low']
    bear1 = k1['close'] < k1['open'] and abs(k1['close'] - k1['open']) > hl1 * 0.3 if hl1 else False
    hl2 = k2['high'] - k2['low']
    small2 = abs(k2['close'] - k2['open']) < hl2 * 0.3 if hl2 else False
    hl3 = k3['high'] - k3['low']
    bull3 = k3['close'] > k3['open'] and abs(k3['close'] - k3['open']) > hl3 * 0.3 if hl3 else False
    mid1 = (k1['open'] + k1['close']) / 2
    return bear1 and small2 and bull3 and k3['close'] > mid1

@engine.register('pattern_evening_star', '黄昏之星', 'pattern', '三根K线顶部反转')
def cond_pattern_evening_star(klines, params):
    if len(klines) < 3:
        return False
    k1, k2, k3 = klines[-3], klines[-2], klines[-1]
    hl1 = k1['high'] - k1['low']
    bull1 = k1['close'] > k1['open'] and abs(k1['close'] - k1['open']) > hl1 * 0.3 if hl1 else False
    hl2 = k2['high'] - k2['low']
    small2 = abs(k2['close'] - k2['open']) < hl2 * 0.3 if hl2 else False
    hl3 = k3['high'] - k3['low']
    bear3 = k3['close'] < k3['open'] and abs(k3['close'] - k3['open']) > hl3 * 0.3 if hl3 else False
    mid1 = (k1['open'] + k1['close']) / 2
    return bull1 and small2 and bear3 and k3['close'] < mid1

@engine.register('pattern_three_white', '红三兵', 'pattern', '连续三根阳线逐步走高')
def cond_pattern_three_white(klines, params):
    if len(klines) < 3:
        return False
    for i in range(-3, 0):
        if klines[i]['close'] <= klines[i]['open']:
            return False
    return (klines[-1]['close'] > klines[-2]['close'] > klines[-3]['close'] and
            klines[-1]['open'] > klines[-2]['open'] > klines[-3]['open'])

@engine.register('pattern_three_black', '三只乌鸦', 'pattern', '连续三根阴线逐步走低')
def cond_pattern_three_black(klines, params):
    if len(klines) < 3:
        return False
    for i in range(-3, 0):
        if klines[i]['close'] >= klines[i]['open']:
            return False
    return (klines[-1]['close'] < klines[-2]['close'] < klines[-3]['close'] and
            klines[-1]['open'] < klines[-2]['open'] < klines[-3]['open'])

@engine.register('pattern_doji', '十字星', 'pattern', '开盘价≈收盘价', {'threshold': 0.003})
def cond_pattern_doji(klines, params):
    k = klines[-1]
    threshold = params.get('threshold', 0.003)
    body_ratio = abs(k['close'] - k['open']) / k['open'] if k['open'] else 0
    has_shadow = (k['high'] - k['low']) > abs(k['close'] - k['open']) * 3
    return body_ratio < threshold and has_shadow

@engine.register('pattern_long_lower_shadow', '长下影线', 'pattern', '下影线>实体N倍', {'ratio': 2.0})
def cond_pattern_long_lower_shadow(klines, params):
    k = klines[-1]
    ratio = params.get('ratio', 2.0)
    body = abs(k['close'] - k['open'])
    lower_shadow = min(k['open'], k['close']) - k['low']
    return lower_shadow > body * ratio if body > 0 else False


# ============== A股特色条件 ==============

def _is_limit_up(kline, prev_kline, is_st=False):
    threshold = 0.048 if is_st else 0.098
    if prev_kline['close'] == 0:
        return False
    pct = (kline['close'] - prev_kline['close']) / prev_kline['close']
    return pct >= threshold

def _is_limit_down(kline, prev_kline, is_st=False):
    threshold = -0.048 if is_st else -0.098
    if prev_kline['close'] == 0:
        return False
    pct = (kline['close'] - prev_kline['close']) / prev_kline['close']
    return pct <= threshold

@engine.register('limit_up', '涨停', 'astock', '涨幅≥9.8%', {'is_st': False})
def cond_limit_up(klines, params):
    if len(klines) < 2:
        return False
    return _is_limit_up(klines[-1], klines[-2], params.get('is_st', False))

@engine.register('limit_down', '跌停', 'astock', '跌幅≤-9.8%', {'is_st': False})
def cond_limit_down(klines, params):
    if len(klines) < 2:
        return False
    return _is_limit_down(klines[-1], klines[-2], params.get('is_st', False))

@engine.register('limit_up_open', '涨停打开', 'astock', '最高价涨停但收盘未涨停')
def cond_limit_up_open(klines, params):
    if len(klines) < 2:
        return False
    prev_close = klines[-2]['close']
    if prev_close == 0:
        return False
    high_pct = (klines[-1]['high'] - prev_close) / prev_close
    close_pct = (klines[-1]['close'] - prev_close) / prev_close
    return high_pct >= 0.098 and close_pct < 0.098

@engine.register('continuous_limit_up', '连板', 'astock', '连续N日涨停', {'n': 2})
def cond_continuous_limit_up(klines, params):
    n = params.get('n', 2)
    if len(klines) < n + 1:
        return False
    for i in range(-n, 0):
        if not _is_limit_up(klines[i], klines[i - 1]):
            return False
    return True

@engine.register('first_limit_up', '首板', 'astock', '今日涨停且前一日未涨停')
def cond_first_limit_up(klines, params):
    if len(klines) < 3:
        return False
    return _is_limit_up(klines[-1], klines[-2]) and not _is_limit_up(klines[-2], klines[-3])

@engine.register('limit_up_volume_shrink', '缩量涨停', 'astock', '涨停且成交量<前日50%',
                 {}, requires_volume=True)
def cond_limit_up_volume_shrink(klines, params):
    if len(klines) < 2:
        return False
    if not _is_limit_up(klines[-1], klines[-2]):
        return False
    today_vol = klines[-1].get('volume', 0) or 0
    prev_vol = klines[-2].get('volume', 0) or 0
    return today_vol < prev_vol * 0.5 if prev_vol > 0 else False

@engine.register('t_board', 'T字板', 'astock', '开盘涨停盘中打开后回封')
def cond_t_board(klines, params):
    if len(klines) < 2:
        return False
    k = klines[-1]
    prev_close = klines[-2]['close']
    if prev_close == 0:
        return False
    limit_price = prev_close * 1.1
    open_at_limit = abs(k['open'] - limit_price) / prev_close < 0.005
    close_at_limit = abs(k['close'] - limit_price) / prev_close < 0.005
    low_below_limit = k['low'] < limit_price * 0.99
    return open_at_limit and close_at_limit and low_below_limit

@engine.register('one_line_board', '一字板', 'astock', '开盘=收盘=最高=最低=涨停价')
def cond_one_line_board(klines, params):
    if len(klines) < 2:
        return False
    k = klines[-1]
    prev_close = klines[-2]['close']
    if prev_close == 0:
        return False
    limit_price = prev_close * 1.1
    tolerance = prev_close * 0.002
    return (abs(k['open'] - limit_price) < tolerance and
            abs(k['close'] - limit_price) < tolerance and
            abs(k['high'] - limit_price) < tolerance and
            abs(k['low'] - limit_price) < tolerance)

@engine.register('big_yang', '大阳线', 'astock', '涨幅>N%的阳线', {'min_pct': 5})
def cond_big_yang(klines, params):
    if len(klines) < 2:
        return False
    min_pct = params.get('min_pct', 5)
    pct = _change_pct(klines) * 100
    return pct > min_pct and klines[-1]['close'] > klines[-1]['open']

@engine.register('big_yin', '大阴线', 'astock', '跌幅>N%的阴线', {'min_pct': 5})
def cond_big_yin(klines, params):
    if len(klines) < 2:
        return False
    min_pct = params.get('min_pct', 5)
    pct = _change_pct(klines) * 100
    return pct < -min_pct and klines[-1]['close'] < klines[-1]['open']

@engine.register('gap_up', '跳空高开', 'astock', '今日开盘价>昨日最高价', {'min_gap_pct': 1})
def cond_gap_up(klines, params):
    if len(klines) < 2:
        return False
    min_gap = params.get('min_gap_pct', 1) / 100
    gap = (klines[-1]['open'] - klines[-2]['high']) / klines[-2]['close']
    return gap > min_gap

@engine.register('gap_down', '跳空低开', 'astock', '今日开盘价<昨日最低价', {'min_gap_pct': 1})
def cond_gap_down(klines, params):
    if len(klines) < 2:
        return False
    min_gap = params.get('min_gap_pct', 1) / 100
    gap = (klines[-2]['low'] - klines[-1]['open']) / klines[-2]['close']
    return gap > min_gap


@engine.register('turnover_rate', '换手率筛选', 'volume',
                 '换手率在指定范围内（需K线含turnoverRate字段）',
                 {'min': 3.0, 'max': 12.0}, requires_volume=True)
def cond_turnover_rate(klines, params):
    min_rate = params.get('min', 3.0)
    max_rate = params.get('max', 12.0)
    k = klines[-1]
    tr = k.get('turnoverRate') or k.get('turnover_rate')
    if tr is None:
        # 无换手率字段时，用量比近似估算（量比>2 约等于换手率偏高）
        volumes = _volumes(klines)
        if len(volumes) < 6:
            return False
        avg5 = np.mean(volumes[-6:-1])
        ratio = volumes[-1] / avg5 if avg5 > 0 else 0
        # 粗略映射：量比 1 ≈ 换手率 3%，量比 2 ≈ 6%，量比 4 ≈ 12%
        estimated = ratio * 3.0
        return min_rate <= estimated <= max_rate
    return min_rate <= float(tr) <= max_rate


# ============== 注册组合策略 ==============

engine.register_composite(
    'strong_breakout', '强势突破', '放量+突破前高+MACD金叉',
    [{'id': 'volume_breakout'}, {'id': 'breakout_high'}, {'id': 'macd_golden_cross'}],
    logic='AND'
)

engine.register_composite(
    'bottom_reversal', '底部反转', 'RSI超卖+锤头线+量比放大',
    [{'id': 'rsi_oversold'}, {'id': 'pattern_hammer'}, {'id': 'volume_ratio_high'}],
    logic='AND'
)

engine.register_composite(
    'trend_following', '趋势跟踪', '均线多头+MACD零轴上+量价齐升',
    [{'id': 'ma_bull'}, {'id': 'macd_above_zero'}, {'id': 'volume_price_up'}],
    logic='AND'
)

engine.register_composite(
    'vcp', 'VCP缩量整理', '布林收窄+缩量+站上均线',
    [{'id': 'boll_squeeze'}, {'id': 'volume_shrink'}, {'id': 'price_above_ma'}],
    logic='AND'
)

engine.register_composite(
    'momentum_burst', '动量爆发', '连涨+放量+KDJ金叉',
    [{'id': 'upn'}, {'id': 'volume_breakout'}, {'id': 'kdj_golden_cross'}],
    logic='AND'
)

engine.register_composite(
    'oversold_bounce', '超跌反弹', '连跌+RSI超卖+十字星',
    [{'id': 'downn'}, {'id': 'rsi_oversold'}, {'id': 'pattern_doji'}],
    logic='AND'
)

engine.register_composite(
    'macd_bull_start', 'MACD多头启动', 'MACD金叉+零轴上方+均线金叉',
    [{'id': 'macd_golden_cross'}, {'id': 'macd_above_zero'}, {'id': 'golden_cross_ma'}],
    logic='AND'
)

engine.register_composite(
    'limit_up_next_day', '涨停次日策略', '首板+缩量涨停',
    [{'id': 'first_limit_up'}, {'id': 'limit_up_volume_shrink'}],
    logic='AND'
)

engine.register_composite(
    'gap_breakout', '跳空突破', '跳空高开+放量+创新高',
    [{'id': 'gap_up'}, {'id': 'volume_breakout'}, {'id': 'new_high'}],
    logic='AND'
)

engine.register_composite(
    'divergence_buy', '背离买入', 'MACD底背离+KDJ超卖',
    [{'id': 'macd_divergence_bull'}, {'id': 'kdj_oversold'}],
    logic='AND'
)
