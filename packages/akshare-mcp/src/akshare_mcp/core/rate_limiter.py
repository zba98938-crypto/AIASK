"""
请求限流器（令牌桶算法）
防止触发数据源反爬虫机制
"""

import time
import asyncio
from typing import Optional, Dict


class RateLimiter:
    """
    令牌桶限流器
    
    特点：
    - 令牌桶算法
    - 支持突发流量
    - 线程安全
    """
    
    def __init__(self, rate: float = 10.0, capacity: Optional[int] = None):
        """
        Args:
            rate: 每秒生成的令牌数
            capacity: 桶容量（最大令牌数），默认为 rate * 2
        """
        self.rate = rate
        self.capacity = capacity if capacity is not None else int(rate * 2)
        self.tokens = float(self.capacity)
        self.last_update = time.time()
    
    def _refill(self):
        """补充令牌"""
        now = time.time()
        elapsed = now - self.last_update
        
        # 补充令牌
        self.tokens = min(
            self.capacity,
            self.tokens + elapsed * self.rate
        )
        self.last_update = now
    
    def acquire(self, tokens: int = 1, blocking: bool = True) -> bool:
        """
        获取令牌
        
        Args:
            tokens: 需要的令牌数
            blocking: 是否阻塞等待
        
        Returns:
            是否成功获取令牌
        """
        self._refill()
        
        # 检查是否有足够令牌
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        
        # 非阻塞模式直接返回
        if not blocking:
            return False
        
        # 阻塞等待
        wait_time = (tokens - self.tokens) / self.rate
        time.sleep(wait_time)
        self.tokens = 0
        return True
    
    def try_acquire(self, tokens: int = 1) -> bool:
        """尝试获取令牌（非阻塞）"""
        return self.acquire(tokens, blocking=False)


# 全局限流器注册表
_limiters: Dict[str, RateLimiter] = {}


def get_limiter(name: str = "default", rate: Optional[float] = None, 
                max_calls: Optional[int] = None, period: float = 1.0) -> RateLimiter:
    """
    获取或创建限流器
    
    Args:
        name: 限流器名称
        rate: 每秒请求数（与 max_calls/period 二选一）
        max_calls: 时间窗口内最大请求数
        period: 时间窗口（秒）
    
    Returns:
        RateLimiter 实例
    
    Example:
        # 方式1：使用 rate 参数
        limiter = get_limiter("api1", rate=5.0)  # 5次/秒
        
        # 方式2：使用 max_calls 和 period
        limiter = get_limiter("api2", max_calls=10, period=1.0)  # 10次/秒
    """
    # 如果限流器已存在，直接返回
    if name in _limiters:
        return _limiters[name]
    
    # 计算 rate
    if rate is not None:
        actual_rate = rate
    elif max_calls is not None:
        actual_rate = max_calls / period
    else:
        actual_rate = 10.0  # 默认 10次/秒
    
    # 创建新限流器
    limiter = RateLimiter(rate=actual_rate)
    _limiters[name] = limiter
    return limiter


# 全局默认限流器
_global_limiter = get_limiter("default", rate=10.0)


def rate_limit(tokens: int = 1):
    """
    限流装饰器
    
    Example:
        @rate_limit(tokens=1)
        def fetch_data():
            return api_call()
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            _global_limiter.acquire(tokens)
            return func(*args, **kwargs)
        return wrapper
    return decorator
