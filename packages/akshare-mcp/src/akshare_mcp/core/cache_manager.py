"""
进程内缓存管理器（适合MCP服务）
使用LRU策略和TTL过期机制
"""

import asyncio
import inspect
import time
from collections import OrderedDict
from functools import wraps, lru_cache
from typing import Any, Optional, Callable


class ProcessCache:
    """
    进程内缓存管理器
    
    特点：
    - LRU淘汰策略
    - TTL过期机制
    - 线程安全（通过简单的dict操作）
    - 适合MCP服务的单进程场景
    """
    
    def __init__(self, max_size: int = 1000):
        self.cache = OrderedDict()
        self.ttl_map = {}  # key -> expire_time
        self.max_size = max_size
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存，自动检查过期"""
        # 检查是否过期
        if key in self.ttl_map:
            if time.time() > self.ttl_map[key]:
                self.delete(key)
                return None
        
        # 从缓存获取
        if key in self.cache:
            # LRU: 移到末尾
            self.cache.move_to_end(key)
            return self.cache[key]
        
        return None
    
    def set(self, key: str, value: Any, ttl: int = 300):
        """设置缓存，带TTL"""
        # 更新或添加
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        self.ttl_map[key] = time.time() + ttl
        
        # LRU淘汰
        if len(self.cache) > self.max_size:
            oldest_key = next(iter(self.cache))
            self.delete(oldest_key)
    
    def delete(self, key: str):
        """删除缓存"""
        self.cache.pop(key, None)
        self.ttl_map.pop(key, None)
    
    def clear(self):
        """清空缓存"""
        self.cache.clear()
        self.ttl_map.clear()
    
    def stats(self) -> dict:
        """缓存统计"""
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "usage": len(self.cache) / self.max_size if self.max_size > 0 else 0,
        }


# 全局缓存实例
_global_cache = ProcessCache(max_size=1000)


def cached(ttl: int = 300, key_prefix: str = ""):
    """
    缓存装饰器（支持同步和异步函数）
    
    Args:
        ttl: 缓存过期时间（秒）
        key_prefix: 缓存键前缀
    
    Example:
        @cached(ttl=60, key_prefix="quote")
        def get_quote(symbol: str):
            return fetch_quote(symbol)
        
        @cached(ttl=60, key_prefix="kline")
        async def get_kline_async(code: str):
            return await fetch_kline(code)
    """
    def decorator(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                cache_key = f"{key_prefix}:{func.__name__}:{args}:{kwargs}"
                cached_value = _global_cache.get(cache_key)
                if cached_value is not None:
                    return cached_value
                result = await func(*args, **kwargs)
                skip = False
                if isinstance(result, dict):
                    if not result.get("success", True):
                        skip = True
                    if result.pop("_no_cache", False):
                        skip = True
                if not skip:
                    _global_cache.set(cache_key, result, ttl=ttl)
                return result
            return async_wrapper
        else:
            @wraps(func)
            def wrapper(*args, **kwargs):
                cache_key = f"{key_prefix}:{func.__name__}:{args}:{kwargs}"
                cached_value = _global_cache.get(cache_key)
                if cached_value is not None:
                    return cached_value
                result = func(*args, **kwargs)
                skip = False
                if isinstance(result, dict):
                    if not result.get("success", True):
                        skip = True
                    if result.pop("_no_cache", False):
                        skip = True
                if not skip:
                    _global_cache.set(cache_key, result, ttl=ttl)
                return result
            return wrapper
    return decorator


# 使用Python内置lru_cache的简单示例
@lru_cache(maxsize=500)
def cached_normalize_code(code: str) -> str:
    """缓存的股票代码标准化"""
    return code.strip().zfill(6)


def get_cache_stats() -> dict:
    """获取缓存统计信息"""
    return _global_cache.stats()


def clear_cache():
    """清空缓存"""
    _global_cache.clear()
