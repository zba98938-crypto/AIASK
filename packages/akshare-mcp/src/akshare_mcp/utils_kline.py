"""
K线数据工具函数

统一处理 K线数据格式，确保所有模块都能正确访问 K线数据
"""

from typing import List, Dict, Any, Union


def ensure_kline_dict(kline: Union[Dict, Any]) -> Dict[str, Any]:
    """
    确保 K线数据是字典格式
    
    Args:
        kline: K线数据，可能是字典或 Pydantic 对象
        
    Returns:
        字典格式的 K线数据
    """
    if isinstance(kline, dict):
        return kline
    
    # 处理 Pydantic BaseModel
    if hasattr(kline, 'model_dump'):
        return kline.model_dump()
    
    # 处理旧版 Pydantic
    if hasattr(kline, 'dict'):
        return kline.dict()
    
    # 处理普通对象
    if hasattr(kline, '__dict__'):
        return {
            attr: getattr(kline, attr) 
            for attr in ('date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'source') 
            if hasattr(kline, attr)
        }
    
    # 最后尝试直接转换
    try:
        return dict(kline)
    except (TypeError, ValueError):
        raise ValueError(f"无法将 K线数据转换为字典: {type(kline)}")


def ensure_kline_list(klines: List[Union[Dict, Any]]) -> List[Dict[str, Any]]:
    """
    确保 K线数据列表都是字典格式
    
    Args:
        klines: K线数据列表
        
    Returns:
        字典格式的 K线数据列表
    """
    if not klines:
        return []
    
    return [ensure_kline_dict(k) for k in klines]


def extract_closes(klines: List[Union[Dict, Any]]) -> List[float]:
    """
    从 K线数据中提取收盘价列表
    
    Args:
        klines: K线数据列表
        
    Returns:
        收盘价列表
    """
    klines_dict = ensure_kline_list(klines)
    return [k['close'] for k in klines_dict if k.get('close') is not None]


def extract_ohlcv(klines: List[Union[Dict, Any]]) -> Dict[str, List[float]]:
    """
    从 K线数据中提取 OHLCV 数据
    
    Args:
        klines: K线数据列表
        
    Returns:
        包含 opens, highs, lows, closes, volumes 的字典
    """
    klines_dict = ensure_kline_list(klines)
    
    return {
        'opens': [k.get('open', 0) for k in klines_dict],
        'highs': [k.get('high', 0) for k in klines_dict],
        'lows': [k.get('low', 0) for k in klines_dict],
        'closes': [k.get('close', 0) for k in klines_dict],
        'volumes': [k.get('volume', 0) for k in klines_dict],
    }
