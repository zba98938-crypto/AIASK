"""
智能缓存模块

提供数据预加载、智能预测、自适应TTL等高级功能
"""

import time
import threading
from typing import Dict, List, Optional, Callable, Any, Tuple
from collections import defaultdict, deque
from dataclasses import dataclass
import heapq

from ..utils import safe_stderr_print


@dataclass
class AccessPattern:
    """访问模式统计"""
    key: str
    access_count: int = 0
    last_access_time: float = 0
    access_times: deque = None  # 最近访问时间队列
    avg_interval: float = 0  # 平均访问间隔
    
    def __post_init__(self):
        if self.access_times is None:
            self.access_times = deque(maxlen=100)  # 保留最近100次访问
    
    def record_access(self):
        """记录一次访问"""
        current_time = time.time()
        self.access_count += 1
        self.access_times.append(current_time)
        self.last_access_time = current_time
        
        # 更新平均访问间隔
        if len(self.access_times) >= 2:
            intervals = []
            for i in range(1, len(self.access_times)):
                intervals.append(self.access_times[i] - self.access_times[i-1])
            self.avg_interval = sum(intervals) / len(intervals)
    
    def predict_next_access(self) -> float:
        """预测下次访问时间"""
        if self.avg_interval > 0:
            return self.last_access_time + self.avg_interval
        return float('inf')


class SmartCache:
    """智能缓存管理器"""
    
    def __init__(self,
                 base_cache: Any,
                 preload_func: Optional[Callable] = None,
                 max_preload_items: int = 100):
        """
        初始化智能缓存
        
        Args:
            base_cache: 基础缓存实例
            preload_func: 预加载函数
            max_preload_items: 最大预加载项数
        """
        self.base_cache = base_cache
        self.preload_func = preload_func
        self.max_preload_items = max_preload_items
        
        # 访问模式统计
        self.access_patterns: Dict[str, AccessPattern] = {}
        self._lock = threading.Lock()
        
        # 预加载队列（优先级队列）
        self.preload_queue: List[Tuple[float, str]] = []
        
        # 后台预加载线程
        self.preload_thread: Optional[threading.Thread] = None
        self.preload_running = False
    
    def get(self, key: str, fetch_func: Optional[Callable] = None) -> Optional[Any]:
        """
        获取缓存数据（带访问模式记录）
        
        Args:
            key: 缓存键
            fetch_func: 获取数据的函数（缓存未命中时调用）
            
        Returns:
            缓存数据
        """
        # 记录访问模式
        with self._lock:
            if key not in self.access_patterns:
                self.access_patterns[key] = AccessPattern(key=key)
            self.access_patterns[key].record_access()
        
        # 从基础缓存获取
        value = self.base_cache.get(key)
        
        if value is None and fetch_func:
            value = fetch_func()
            if value is not None:
                self.base_cache.set(key, value)
        
        # 触发预测性预加载
        self._schedule_predictive_preload()
        
        return value
    
    def get_adaptive_ttl(self, key: str, default_ttl: int) -> int:
        """
        获取自适应TTL
        
        根据访问频率动态调整TTL：
        - 高频访问：延长TTL
        - 低频访问：缩短TTL
        
        Args:
            key: 缓存键
            default_ttl: 默认TTL（秒）
            
        Returns:
            调整后的TTL
        """
        with self._lock:
            pattern = self.access_patterns.get(key)
        
        if not pattern or pattern.access_count < 2:
            return default_ttl
        
        # 根据访问频率调整
        if pattern.avg_interval > 0:
            # 访问间隔小于默认TTL的一半，延长TTL
            if pattern.avg_interval < default_ttl / 2:
                return int(default_ttl * 1.5)
            # 访问间隔大于默认TTL的2倍，缩短TTL
            elif pattern.avg_interval > default_ttl * 2:
                return int(default_ttl * 0.7)
        
        return default_ttl
    
    def preload_hot_data(self, hot_keys: List[str]):
        """
        预加载热门数据
        
        Args:
            hot_keys: 热门键列表
        """
        if not self.preload_func:
            return
        
        for key in hot_keys[:self.max_preload_items]:
            # 检查是否已缓存
            if self.base_cache.get(key) is None:
                try:
                    data = self.preload_func(key)
                    if data is not None:
                        self.base_cache.set(key, data)
                except Exception as e:
                    safe_stderr_print(f"预加载失败 {key}: {e}")
    
    def get_hot_keys(self, top_n: int = 50) -> List[str]:
        """
        获取热门键（按访问频率排序）
        
        Args:
            top_n: 返回前N个
            
        Returns:
            热门键列表
        """
        with self._lock:
            patterns = list(self.access_patterns.values())
        
        # 按访问次数排序
        patterns.sort(key=lambda p: p.access_count, reverse=True)
        
        return [p.key for p in patterns[:top_n]]
    
    def _schedule_predictive_preload(self):
        """调度预测性预加载"""
        if not self.preload_func:
            return
        
        with self._lock:
            # 找出即将被访问的键
            current_time = time.time()
            candidates = []
            
            for key, pattern in self.access_patterns.items():
                if pattern.access_count < 3:  # 至少3次访问才能预测
                    continue
                
                next_access = pattern.predict_next_access()
                # 如果预测在未来5分钟内会被访问，且当前未缓存
                if 0 < next_access - current_time < 300:
                    if self.base_cache.get(key) is None:
                        heapq.heappush(candidates, (next_access, key))
            
            # 更新预加载队列
            self.preload_queue = candidates[:self.max_preload_items]
        
        # 启动后台预加载
        if self.preload_queue and not self.preload_running:
            self._start_background_preload()
    
    def _start_background_preload(self):
        """启动后台预加载线程"""
        if self.preload_thread and self.preload_thread.is_alive():
            return
        
        self.preload_running = True
        self.preload_thread = threading.Thread(target=self._background_preload_worker)
        self.preload_thread.daemon = True
        self.preload_thread.start()
    
    def _background_preload_worker(self):
        """后台预加载工作线程"""
        while self.preload_running and self.preload_queue:
            with self._lock:
                if not self.preload_queue:
                    break
                _, key = heapq.heappop(self.preload_queue)
            
            # 检查是否已缓存
            if self.base_cache.get(key) is not None:
                continue
            
            # 预加载数据
            try:
                data = self.preload_func(key)
                if data is not None:
                    self.base_cache.set(key, data)
                    safe_stderr_print(f"后台预加载成功: {key}")
            except Exception as e:
                safe_stderr_print(f"后台预加载失败 {key}: {e}")
            
            # 避免过于频繁
            time.sleep(0.1)
        
        self.preload_running = False
    
    def get_statistics(self) -> Dict:
        """获取统计信息"""
        with self._lock:
            total_accesses = sum(p.access_count for p in self.access_patterns.values())
            
            return {
                'total_keys': len(self.access_patterns),
                'total_accesses': total_accesses,
                'hot_keys': self.get_hot_keys(10),
                'preload_queue_size': len(self.preload_queue),
                'preload_running': self.preload_running
            }
    
    def stop_preload(self):
        """停止预加载"""
        self.preload_running = False
        if self.preload_thread:
            self.preload_thread.join(timeout=5)


class AdaptiveRateLimiter:
    """自适应限流器"""
    
    def __init__(self,
                 initial_rate: float = 10.0,
                 min_rate: float = 1.0,
                 max_rate: float = 50.0,
                 adjustment_factor: float = 0.1):
        """
        初始化自适应限流器
        
        Args:
            initial_rate: 初始速率（次/秒）
            min_rate: 最小速率
            max_rate: 最大速率
            adjustment_factor: 调整因子（0-1）
        """
        self.current_rate = initial_rate
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.adjustment_factor = adjustment_factor
        
        # 统计信息
        self.success_count = 0
        self.error_count = 0
        self.response_times = deque(maxlen=100)
        
        self._lock = threading.Lock()
        self._last_adjust_time = time.time()
    
    def record_success(self, response_time: float):
        """
        记录成功请求
        
        Args:
            response_time: 响应时间（秒）
        """
        with self._lock:
            self.success_count += 1
            self.response_times.append(response_time)
            self._adjust_rate()
    
    def record_error(self):
        """记录错误请求"""
        with self._lock:
            self.error_count += 1
            self._adjust_rate()
    
    def _adjust_rate(self):
        """调整速率"""
        current_time = time.time()
        
        # 每10秒调整一次
        if current_time - self._last_adjust_time < 10:
            return
        
        total_requests = self.success_count + self.error_count
        if total_requests == 0:
            return
        
        error_rate = self.error_count / total_requests
        
        # 根据错误率调整
        if error_rate > 0.1:  # 错误率超过10%，降低速率
            self.current_rate = max(
                self.min_rate,
                self.current_rate * (1 - self.adjustment_factor)
            )
            safe_stderr_print(f"降低速率至 {self.current_rate:.1f} 次/秒（错误率: {error_rate:.1%}）")
        
        elif error_rate < 0.01 and self.response_times:  # 错误率低且响应快，提高速率
            avg_response_time = sum(self.response_times) / len(self.response_times)
            if avg_response_time < 0.5:  # 平均响应时间小于0.5秒
                self.current_rate = min(
                    self.max_rate,
                    self.current_rate * (1 + self.adjustment_factor)
                )
                safe_stderr_print(f"提高速率至 {self.current_rate:.1f} 次/秒（响应时间: {avg_response_time:.2f}s）")
        
        # 重置计数器
        self.success_count = 0
        self.error_count = 0
        self._last_adjust_time = current_time
    
    def get_current_rate(self) -> float:
        """获取当前速率"""
        with self._lock:
            return self.current_rate
