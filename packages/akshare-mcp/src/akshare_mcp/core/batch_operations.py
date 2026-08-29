"""
批量操作优化模块

提供批量获取行情、批量计算指标等功能
通过并发控制和结果聚合提升性能
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Callable, Any, Tuple
from dataclasses import dataclass
import time

from ..utils import safe_stderr_print


@dataclass
class BatchResult:
    """批量操作结果"""
    success: Dict[str, Any]  # 成功的结果
    failed: Dict[str, str]   # 失败的结果（错误信息）
    total_time: float        # 总耗时（秒）
    success_count: int       # 成功数量
    failed_count: int        # 失败数量


class BatchOperations:
    """批量操作管理器"""
    
    def __init__(self, max_workers: int = 10, timeout: float = 30.0):
        """
        初始化批量操作管理器
        
        Args:
            max_workers: 最大并发数
            timeout: 单个操作超时时间（秒）
        """
        self.max_workers = max_workers
        self.timeout = timeout
    
    def batch_execute(self,
                     func: Callable,
                     items: List[Any],
                     *args,
                     **kwargs) -> BatchResult:
        """
        批量执行函数
        
        Args:
            func: 要执行的函数
            items: 参数列表
            *args: 额外的位置参数
            **kwargs: 额外的关键字参数
            
        Returns:
            批量操作结果
        """
        start_time = time.time()
        success = {}
        failed = {}
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务
            future_to_item = {
                executor.submit(func, item, *args, **kwargs): item
                for item in items
            }
            
            # 收集结果
            for future in as_completed(future_to_item, timeout=self.timeout):
                item = future_to_item[future]
                try:
                    result = future.result(timeout=self.timeout)
                    success[str(item)] = result
                except Exception as e:
                    failed[str(item)] = str(e)
        
        total_time = time.time() - start_time
        
        return BatchResult(
            success=success,
            failed=failed,
            total_time=total_time,
            success_count=len(success),
            failed_count=len(failed)
        )
    
    def batch_execute_with_retry(self,
                                 func: Callable,
                                 items: List[Any],
                                 max_retries: int = 3,
                                 *args,
                                 **kwargs) -> BatchResult:
        """
        批量执行函数（带重试）
        
        Args:
            func: 要执行的函数
            items: 参数列表
            max_retries: 最大重试次数
            *args: 额外的位置参数
            **kwargs: 额外的关键字参数
            
        Returns:
            批量操作结果
        """
        start_time = time.time()
        success = {}
        failed = {}
        
        # 第一次尝试
        result = self.batch_execute(func, items, *args, **kwargs)
        success.update(result.success)
        
        # 重试失败的项
        retry_items = [item for item in items if str(item) in result.failed]
        
        for retry in range(max_retries):
            if not retry_items:
                break
            
            safe_stderr_print(f"重试第 {retry + 1} 次，剩余 {len(retry_items)} 项...")
            result = self.batch_execute(func, retry_items, *args, **kwargs)
            success.update(result.success)
            
            # 更新需要重试的项
            retry_items = [item for item in retry_items if str(item) not in result.success]
        
        # 最终失败的项
        for item in retry_items:
            failed[str(item)] = "达到最大重试次数"
        
        total_time = time.time() - start_time
        
        return BatchResult(
            success=success,
            failed=failed,
            total_time=total_time,
            success_count=len(success),
            failed_count=len(failed)
        )
    
    def batch_execute_chunked(self,
                             func: Callable,
                             items: List[Any],
                             chunk_size: int = 50,
                             delay_between_chunks: float = 0.5,
                             *args,
                             **kwargs) -> BatchResult:
        """
        分块批量执行（避免一次性请求过多）
        
        Args:
            func: 要执行的函数
            items: 参数列表
            chunk_size: 每块大小
            delay_between_chunks: 块之间的延迟（秒）
            *args: 额外的位置参数
            **kwargs: 额外的关键字参数
            
        Returns:
            批量操作结果
        """
        start_time = time.time()
        all_success = {}
        all_failed = {}
        
        # 分块处理
        for i in range(0, len(items), chunk_size):
            chunk = items[i:i + chunk_size]
            safe_stderr_print(f"处理第 {i//chunk_size + 1} 块，共 {len(chunk)} 项...")
            
            result = self.batch_execute(func, chunk, *args, **kwargs)
            all_success.update(result.success)
            all_failed.update(result.failed)
            
            # 块之间延迟
            if i + chunk_size < len(items):
                time.sleep(delay_between_chunks)
        
        total_time = time.time() - start_time
        
        return BatchResult(
            success=all_success,
            failed=all_failed,
            total_time=total_time,
            success_count=len(all_success),
            failed_count=len(all_failed)
        )


class BatchQuotesFetcher:
    """批量行情获取器"""
    
    def __init__(self, 
                 get_quote_func: Callable,
                 max_workers: int = 10,
                 cache_func: Optional[Callable] = None):
        """
        初始化批量行情获取器
        
        Args:
            get_quote_func: 获取单个行情的函数
            max_workers: 最大并发数
            cache_func: 缓存函数（可选）
        """
        self.get_quote_func = get_quote_func
        self.batch_ops = BatchOperations(max_workers=max_workers)
        self.cache_func = cache_func
    
    def fetch_batch(self, stock_codes: List[str]) -> Dict[str, Any]:
        """
        批量获取行情
        
        Args:
            stock_codes: 股票代码列表
            
        Returns:
            {股票代码: 行情数据} 字典
        """
        # 检查缓存
        uncached_codes = []
        cached_results = {}
        
        if self.cache_func:
            for code in stock_codes:
                cached = self.cache_func(code)
                if cached:
                    cached_results[code] = cached
                else:
                    uncached_codes.append(code)
        else:
            uncached_codes = stock_codes
        
        # 批量获取未缓存的数据
        if uncached_codes:
            result = self.batch_ops.batch_execute_with_retry(
                self.get_quote_func,
                uncached_codes,
                max_retries=2
            )
            
            # 合并结果
            cached_results.update(result.success)
            
            safe_stderr_print(
                f"批量获取完成: 成功 {result.success_count}, "
                f"失败 {result.failed_count}, "
                f"耗时 {result.total_time:.2f}s"
            )
        
        return cached_results


class BatchIndicatorCalculator:
    """批量指标计算器"""
    
    def __init__(self, max_workers: int = 10):
        """
        初始化批量指标计算器
        
        Args:
            max_workers: 最大并发数
        """
        self.batch_ops = BatchOperations(max_workers=max_workers)
    
    def calculate_batch(self,
                       calc_func: Callable,
                       data_dict: Dict[str, Any],
                       **kwargs) -> Dict[str, Any]:
        """
        批量计算指标
        
        Args:
            calc_func: 计算函数
            data_dict: {股票代码: 数据} 字典
            **kwargs: 计算参数
            
        Returns:
            {股票代码: 指标结果} 字典
        """
        def calc_wrapper(code):
            data = data_dict[code]
            return calc_func(data, **kwargs)
        
        result = self.batch_ops.batch_execute(
            calc_wrapper,
            list(data_dict.keys())
        )
        
        safe_stderr_print(
            f"批量计算完成: 成功 {result.success_count}, "
            f"失败 {result.failed_count}, "
            f"耗时 {result.total_time:.2f}s"
        )
        
        return result.success


# 便捷函数
def batch_fetch_quotes(get_quote_func: Callable,
                      stock_codes: List[str],
                      max_workers: int = 10) -> Dict[str, Any]:
    """
    批量获取行情（便捷函数）
    
    Args:
        get_quote_func: 获取单个行情的函数
        stock_codes: 股票代码列表
        max_workers: 最大并发数
        
    Returns:
        {股票代码: 行情数据} 字典
    """
    fetcher = BatchQuotesFetcher(get_quote_func, max_workers=max_workers)
    return fetcher.fetch_batch(stock_codes)


def batch_calculate_indicators(calc_func: Callable,
                               data_dict: Dict[str, Any],
                               max_workers: int = 10,
                               **kwargs) -> Dict[str, Any]:
    """
    批量计算指标（便捷函数）
    
    Args:
        calc_func: 计算函数
        data_dict: {股票代码: 数据} 字典
        max_workers: 最大并发数
        **kwargs: 计算参数
        
    Returns:
        {股票代码: 指标结果} 字典
    """
    calculator = BatchIndicatorCalculator(max_workers=max_workers)
    return calculator.calculate_batch(calc_func, data_dict, **kwargs)
