"""
K线形态识别 - 使用ta-lib
"""

from typing import List, Dict, Any
import numpy as np

try:
    import talib
    TALIB_AVAILABLE = True
except ImportError:
    TALIB_AVAILABLE = False
    talib = None


PATTERN_DEFINITIONS = {
    'doji': {'name': '十字星', 'bullish': False, 'reliability': 'medium'},
    'hammer': {'name': '锤头线', 'bullish': True, 'reliability': 'high'},
    'shooting_star': {'name': '流星线', 'bullish': False, 'reliability': 'high'},
    'engulfing': {'name': '吞没形态', 'bullish': None, 'reliability': 'high'},
    'morning_star': {'name': '早晨之星', 'bullish': True, 'reliability': 'high'},
    'evening_star': {'name': '黄昏之星', 'bullish': False, 'reliability': 'high'},
    'three_white_soldiers': {'name': '三白兵', 'bullish': True, 'reliability': 'high'},
    'three_black_crows': {'name': '三只乌鸦', 'bullish': False, 'reliability': 'high'},
}


class PatternRecognition:
    """K线形态识别"""
    
    @staticmethod
    def detect_patterns(klines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """检测K线形态"""
        if not klines or not TALIB_AVAILABLE:
            return []
        
        opens = np.array([k['open'] for k in klines])
        highs = np.array([k['high'] for k in klines])
        lows = np.array([k['low'] for k in klines])
        closes = np.array([k['close'] for k in klines])
        
        results = []
        
        # 十字星
        doji = talib.CDLDOJI(opens, highs, lows, closes)
        if doji[-1] != 0:
            results.append({
                'pattern': 'doji',
                'name': '十字星',
                'detected': True,
                'bullish': False,
                'reliability': 'medium'
            })
        
        # 锤头线
        hammer = talib.CDLHAMMER(opens, highs, lows, closes)
        if hammer[-1] != 0:
            results.append({
                'pattern': 'hammer',
                'name': '锤头线',
                'detected': True,
                'bullish': True,
                'reliability': 'high'
            })
        
        # 流星线
        shooting_star = talib.CDLSHOOTINGSTAR(opens, highs, lows, closes)
        if shooting_star[-1] != 0:
            results.append({
                'pattern': 'shooting_star',
                'name': '流星线',
                'detected': True,
                'bullish': False,
                'reliability': 'high'
            })
        
        # 吞没形态
        engulfing = talib.CDLENGULFING(opens, highs, lows, closes)
        if engulfing[-1] != 0:
            results.append({
                'pattern': 'engulfing',
                'name': '吞没形态',
                'detected': True,
                'bullish': engulfing[-1] > 0,
                'reliability': 'high'
            })
        
        # 早晨之星
        morning_star = talib.CDLMORNINGSTAR(opens, highs, lows, closes)
        if morning_star[-1] != 0:
            results.append({
                'pattern': 'morning_star',
                'name': '早晨之星',
                'detected': True,
                'bullish': True,
                'reliability': 'high'
            })
        
        # 黄昏之星
        evening_star = talib.CDLEVENINGSTAR(opens, highs, lows, closes)
        if evening_star[-1] != 0:
            results.append({
                'pattern': 'evening_star',
                'name': '黄昏之星',
                'detected': True,
                'bullish': False,
                'reliability': 'high'
            })
        
        # 三白兵
        three_white = talib.CDL3WHITESOLDIERS(opens, highs, lows, closes)
        if three_white[-1] != 0:
            results.append({
                'pattern': 'three_white_soldiers',
                'name': '三白兵',
                'detected': True,
                'bullish': True,
                'reliability': 'high'
            })
        
        # 三只乌鸦
        three_black = talib.CDL3BLACKCROWS(opens, highs, lows, closes)
        if three_black[-1] != 0:
            results.append({
                'pattern': 'three_black_crows',
                'name': '三只乌鸦',
                'detected': True,
                'bullish': False,
                'reliability': 'high'
            })
        
        return results
    
    @staticmethod
    def get_available_patterns() -> List[Dict[str, Any]]:
        """获取支持的形态列表"""
        return [
            {
                'pattern': key,
                'name': value['name'],
                'bullish': value['bullish'],
                'reliability': value['reliability']
            }
            for key, value in PATTERN_DEFINITIONS.items()
        ]


pattern_recognition = PatternRecognition()
