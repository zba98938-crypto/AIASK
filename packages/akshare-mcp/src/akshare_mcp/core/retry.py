"""
错误重试和多数据源降级
"""

import time
from functools import wraps
from typing import Callable, List, Optional, Any


def retry_with_fallback(
    max_attempts: int = 3,
    backoff: float = 1.0,
    exceptions: tuple = (Exception,)
):
    """
    重试装饰器（带指数退避）
    
    Args:
        max_attempts: 最大重试次数
        backoff: 初始退避时间（秒）
        exceptions: 需要重试的异常类型
    
    Example:
        @retry_with_fallback(max_attempts=3, backoff=1.0)
        def fetch_data():
            return api_call()
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception: Optional[Exception] = None
            
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    
                    # 最后一次尝试不等待
                    if attempt < max_attempts - 1:
                        wait_time = backoff * (2 ** attempt)
                        time.sleep(wait_time)
            
            # 所有尝试都失败
            if last_exception:
                raise last_exception
            
            raise RuntimeError("All retry attempts failed")
        
        return wrapper
    return decorator


class MultiSourceFetcher:
    """
    多数据源获取器（支持降级）
    
    Example:
        fetcher = MultiSourceFetcher([
            lambda code: fetch_from_akshare(code),
            lambda code: fetch_from_tushare(code),
            lambda code: fetch_from_sina(code),
        ])
        result = fetcher.fetch("000001")
    """
    
    def __init__(self, sources: List[Callable]):
        """
        Args:
            sources: 数据源函数列表（按优先级排序）
        """
        self.sources = sources
    
    def fetch(self, *args, **kwargs) -> Any:
        """
        依次尝试各个数据源
        
        Returns:
            第一个成功的结果
        
        Raises:
            RuntimeError: 所有数据源都失败
        """
        last_error: Optional[Exception] = None
        
        for i, source in enumerate(self.sources):
            try:
                result = source(*args, **kwargs)
                if result is not None:
                    return result
            except Exception as e:
                last_error = e
                # 继续尝试下一个数据源
                continue
        
        # 所有数据源都失败
        if last_error:
            raise RuntimeError(f"All data sources failed: {last_error}")
        
        raise RuntimeError("All data sources returned None")


def with_timeout(timeout: float):
    """
    超时装饰器
    
    Args:
        timeout: 超时时间（秒）
    
    Note:
        简化版本，实际使用中可以配合ThreadPoolExecutor
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            import signal
            
            def timeout_handler(signum, frame):
                raise TimeoutError(f"Function timed out after {timeout}s")
            
            # 设置超时信号（仅Unix系统）
            try:
                signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(int(timeout))
                
                result = func(*args, **kwargs)
                
                signal.alarm(0)  # 取消超时
                return result
            except AttributeError:
                # Windows系统不支持SIGALRM，直接执行
                return func(*args, **kwargs)
        
        return wrapper
    return decorator
