"""
性能监控模块

提供缓存命中率、API调用统计、响应时间监控等功能
支持持久化存储和历史数据分析
"""

import time
import json
import sqlite3
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from threading import Lock
from typing import Dict, List, Optional
from datetime import datetime

from ..utils import safe_stderr_print


@dataclass
class PerformanceStats:
    """性能统计数据"""
    
    # 缓存统计
    cache_hits: int = 0
    cache_misses: int = 0
    
    # API调用统计
    api_calls: int = 0
    api_errors: int = 0
    
    # 响应时间统计（毫秒）
    response_times: List[float] = field(default_factory=list)
    
    # 限流统计
    rate_limit_waits: int = 0
    total_wait_time: float = 0.0  # 秒
    
    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率"""
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0
    
    @property
    def avg_response_time(self) -> float:
        """平均响应时间（毫秒）"""
        return sum(self.response_times) / len(self.response_times) if self.response_times else 0.0
    
    @property
    def p50_response_time(self) -> float:
        """P50响应时间（毫秒）"""
        if not self.response_times:
            return 0.0
        sorted_times = sorted(self.response_times)
        return sorted_times[len(sorted_times) // 2]
    
    @property
    def p95_response_time(self) -> float:
        """P95响应时间（毫秒）"""
        if not self.response_times:
            return 0.0
        sorted_times = sorted(self.response_times)
        idx = int(len(sorted_times) * 0.95)
        return sorted_times[idx] if idx < len(sorted_times) else sorted_times[-1]
    
    @property
    def p99_response_time(self) -> float:
        """P99响应时间（毫秒）"""
        if not self.response_times:
            return 0.0
        sorted_times = sorted(self.response_times)
        idx = int(len(sorted_times) * 0.99)
        return sorted_times[idx] if idx < len(sorted_times) else sorted_times[-1]


class PerformanceMonitor:
    """性能监控器"""
    
    def __init__(self, max_response_times: int = 1000, db_path: Optional[str] = None):
        """
        初始化性能监控器
        
        Args:
            max_response_times: 保留的响应时间样本数量（避免内存无限增长）
            db_path: SQLite 数据库路径，用于持久化统计数据
        """
        self._stats: Dict[str, PerformanceStats] = defaultdict(PerformanceStats)
        self._lock = Lock()
        self._max_response_times = max_response_times
        self._db_path = db_path
        self._db_conn: Optional[sqlite3.Connection] = None
        
        if db_path:
            self._init_database()
    
    def record_cache_hit(self, key: str):
        """记录缓存命中"""
        with self._lock:
            self._stats[key].cache_hits += 1
    
    def record_cache_miss(self, key: str):
        """记录缓存未命中"""
        with self._lock:
            self._stats[key].cache_misses += 1
    
    def record_api_call(self, key: str):
        """记录API调用"""
        with self._lock:
            self._stats[key].api_calls += 1
    
    def record_api_error(self, key: str):
        """记录API错误"""
        with self._lock:
            self._stats[key].api_errors += 1
    
    def record_response_time(self, key: str, time_ms: float):
        """
        记录响应时间
        
        Args:
            key: 统计键
            time_ms: 响应时间（毫秒）
        """
        with self._lock:
            stats = self._stats[key]
            stats.response_times.append(time_ms)
            # 限制样本数量，保留最新的
            if len(stats.response_times) > self._max_response_times:
                stats.response_times = stats.response_times[-self._max_response_times:]
    
    def record_rate_limit_wait(self, key: str, wait_time: float):
        """
        记录限流等待
        
        Args:
            key: 统计键
            wait_time: 等待时间（秒）
        """
        with self._lock:
            stats = self._stats[key]
            stats.rate_limit_waits += 1
            stats.total_wait_time += wait_time
    
    def get_stats(self, key: str) -> Optional[PerformanceStats]:
        """获取指定键的统计数据"""
        with self._lock:
            return self._stats.get(key)
    
    def get_all_stats(self) -> Dict[str, PerformanceStats]:
        """获取所有统计数据"""
        with self._lock:
            return dict(self._stats)
    
    def reset_stats(self, key: Optional[str] = None):
        """
        重置统计数据
        
        Args:
            key: 指定键，如果为None则重置所有
        """
        with self._lock:
            if key:
                if key in self._stats:
                    self._stats[key] = PerformanceStats()
            else:
                self._stats.clear()
    
    def _init_database(self):
        """初始化 SQLite 数据库"""
        if not self._db_path:
            return
        
        # 确保目录存在
        db_dir = Path(self._db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        
        self._db_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        cursor = self._db_conn.cursor()
        
        # 创建统计快照表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS performance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                cache_hits INTEGER,
                cache_misses INTEGER,
                api_calls INTEGER,
                api_errors INTEGER,
                avg_response_time REAL,
                p50_response_time REAL,
                p95_response_time REAL,
                p99_response_time REAL,
                rate_limit_waits INTEGER,
                total_wait_time REAL
            )
        """)
        
        # 创建索引
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_key_time 
            ON performance_snapshots(key, timestamp)
        """)
        
        self._db_conn.commit()
    
    def save_snapshot(self, key: Optional[str] = None):
        """
        保存当前统计快照到数据库
        
        Args:
            key: 指定键，如果为None则保存所有
        """
        if not self._db_conn:
            return
        
        timestamp = datetime.now().isoformat()
        
        with self._lock:
            stats_to_save = {}
            if key:
                if key in self._stats:
                    stats_to_save[key] = self._stats[key]
            else:
                stats_to_save = dict(self._stats)
        
        cursor = self._db_conn.cursor()
        for name, stats in stats_to_save.items():
            cursor.execute("""
                INSERT INTO performance_snapshots (
                    key, timestamp, cache_hits, cache_misses, api_calls, api_errors,
                    avg_response_time, p50_response_time, p95_response_time, p99_response_time,
                    rate_limit_waits, total_wait_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name, timestamp, stats.cache_hits, stats.cache_misses,
                stats.api_calls, stats.api_errors,
                stats.avg_response_time, stats.p50_response_time,
                stats.p95_response_time, stats.p99_response_time,
                stats.rate_limit_waits, stats.total_wait_time
            ))
        
        self._db_conn.commit()
    
    def get_historical_stats(self, 
                            key: str, 
                            start_time: Optional[str] = None,
                            end_time: Optional[str] = None,
                            limit: int = 100) -> List[Dict]:
        """
        获取历史统计数据
        
        Args:
            key: 统计键
            start_time: 开始时间（ISO格式）
            end_time: 结束时间（ISO格式）
            limit: 返回记录数量限制
            
        Returns:
            历史统计记录列表
        """
        if not self._db_conn:
            return []
        
        cursor = self._db_conn.cursor()
        
        query = "SELECT * FROM performance_snapshots WHERE key = ?"
        params = [key]
        
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time)
        
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time)
        
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        
        cursor.execute(query, params)
        
        columns = [desc[0] for desc in cursor.description]
        results = []
        for row in cursor.fetchall():
            results.append(dict(zip(columns, row)))
        
        return results
    
    def analyze_trends(self, key: str, hours: int = 24) -> Dict:
        """
        分析性能趋势
        
        Args:
            key: 统计键
            hours: 分析最近多少小时的数据
            
        Returns:
            趋势分析结果
        """
        if not self._db_conn:
            return {}
        
        from datetime import timedelta
        start_time = (datetime.now() - timedelta(hours=hours)).isoformat()
        
        records = self.get_historical_stats(key, start_time=start_time, limit=1000)
        
        if not records:
            return {}
        
        # 计算趋势
        cache_hit_rates = []
        avg_response_times = []
        api_error_rates = []
        
        for record in records:
            total_cache = record['cache_hits'] + record['cache_misses']
            if total_cache > 0:
                cache_hit_rates.append(record['cache_hits'] / total_cache)
            
            if record['avg_response_time']:
                avg_response_times.append(record['avg_response_time'])
            
            if record['api_calls'] > 0:
                api_error_rates.append(record['api_errors'] / record['api_calls'])
        
        import numpy as np
        
        return {
            'key': key,
            'period_hours': hours,
            'sample_count': len(records),
            'cache_hit_rate': {
                'mean': float(np.mean(cache_hit_rates)) if cache_hit_rates else 0,
                'std': float(np.std(cache_hit_rates)) if cache_hit_rates else 0,
                'min': float(np.min(cache_hit_rates)) if cache_hit_rates else 0,
                'max': float(np.max(cache_hit_rates)) if cache_hit_rates else 0,
            },
            'response_time': {
                'mean': float(np.mean(avg_response_times)) if avg_response_times else 0,
                'std': float(np.std(avg_response_times)) if avg_response_times else 0,
                'min': float(np.min(avg_response_times)) if avg_response_times else 0,
                'max': float(np.max(avg_response_times)) if avg_response_times else 0,
            },
            'error_rate': {
                'mean': float(np.mean(api_error_rates)) if api_error_rates else 0,
                'std': float(np.std(api_error_rates)) if api_error_rates else 0,
                'min': float(np.min(api_error_rates)) if api_error_rates else 0,
                'max': float(np.max(api_error_rates)) if api_error_rates else 0,
            }
        }
    
    def generate_report(self, output_path: Optional[str] = None) -> str:
        """
        生成性能报告
        
        Args:
            output_path: 报告输出路径，如果为None则返回字符串
            
        Returns:
            报告内容
        """
        report_lines = []
        report_lines.append("=" * 80)
        report_lines.append("性能监控报告")
        report_lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report_lines.append("=" * 80)
        
        with self._lock:
            stats_dict = dict(self._stats)
        
        for name, stats in stats_dict.items():
            report_lines.append(f"\n【{name}】")
            
            if stats.cache_hits > 0 or stats.cache_misses > 0:
                report_lines.append(f"  缓存命中率: {stats.cache_hit_rate:.1%} "
                                  f"(命中: {stats.cache_hits}, 未命中: {stats.cache_misses})")
            
            if stats.api_calls > 0:
                error_rate = stats.api_errors / stats.api_calls
                report_lines.append(f"  API调用: {stats.api_calls} 次 "
                                  f"(错误: {stats.api_errors}, 错误率: {error_rate:.1%})")
            
            if stats.response_times:
                report_lines.append(f"  响应时间: 平均 {stats.avg_response_time:.1f}ms, "
                                  f"P50 {stats.p50_response_time:.1f}ms, "
                                  f"P95 {stats.p95_response_time:.1f}ms, "
                                  f"P99 {stats.p99_response_time:.1f}ms")
            
            if stats.rate_limit_waits > 0:
                avg_wait = stats.total_wait_time / stats.rate_limit_waits
                report_lines.append(f"  限流等待: {stats.rate_limit_waits} 次, "
                                  f"总等待 {stats.total_wait_time:.2f}s, "
                                  f"平均 {avg_wait*1000:.1f}ms")
            
            # 添加趋势分析
            if self._db_conn:
                trends = self.analyze_trends(name, hours=24)
                if trends:
                    report_lines.append(f"  24小时趋势:")
                    report_lines.append(f"    缓存命中率: {trends['cache_hit_rate']['mean']:.1%} "
                                      f"(±{trends['cache_hit_rate']['std']:.1%})")
                    report_lines.append(f"    平均响应时间: {trends['response_time']['mean']:.1f}ms "
                                      f"(±{trends['response_time']['std']:.1f}ms)")
        
        report_lines.append("\n" + "=" * 80)
        
        report = "\n".join(report_lines)
        
        if output_path:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(report)
        
        return report
    
    def close(self):
        """关闭数据库连接"""
        if self._db_conn:
            self._db_conn.close()
            self._db_conn = None
    
    def print_summary(self, key: Optional[str] = None):
        """
        打印统计摘要
        
        Args:
            key: 指定键，如果为None则打印所有
        """
        with self._lock:
            if key:
                stats_dict = {key: self._stats.get(key)} if key in self._stats else {}
            else:
                stats_dict = dict(self._stats)
        
        if not stats_dict:
            safe_stderr_print("无统计数据")
            return
        
        safe_stderr_print("\n" + "=" * 80)
        safe_stderr_print("性能监控统计")
        safe_stderr_print("=" * 80)
        
        for name, stats in stats_dict.items():
            if not stats:
                continue
            
            safe_stderr_print(f"\n【{name}】")
            
            # 缓存统计
            if stats.cache_hits > 0 or stats.cache_misses > 0:
                safe_stderr_print(
                    f"  缓存命中率: {stats.cache_hit_rate:.1%} "
                    f"(命中: {stats.cache_hits}, 未命中: {stats.cache_misses})"
                )
            
            # API调用统计
            if stats.api_calls > 0:
                error_rate = stats.api_errors / stats.api_calls if stats.api_calls > 0 else 0
                safe_stderr_print(
                    f"  API调用: {stats.api_calls} 次 "
                    f"(错误: {stats.api_errors}, 错误率: {error_rate:.1%})"
                )
            
            # 响应时间统计
            if stats.response_times:
                safe_stderr_print(
                    f"  响应时间: 平均 {stats.avg_response_time:.1f}ms, "
                    f"P50 {stats.p50_response_time:.1f}ms, "
                    f"P95 {stats.p95_response_time:.1f}ms, "
                    f"P99 {stats.p99_response_time:.1f}ms"
                )
            
            # 限流统计
            if stats.rate_limit_waits > 0:
                avg_wait = stats.total_wait_time / stats.rate_limit_waits
                safe_stderr_print(
                    f"  限流等待: {stats.rate_limit_waits} 次, "
                    f"总等待 {stats.total_wait_time:.2f}s, "
                    f"平均 {avg_wait*1000:.1f}ms"
                )
        
        safe_stderr_print("\n" + "=" * 80)


# 全局监控器实例
_global_monitor: Optional[PerformanceMonitor] = None


def get_monitor() -> PerformanceMonitor:
    """获取全局监控器实例"""
    global _global_monitor
    if _global_monitor is None:
        _global_monitor = PerformanceMonitor()
    return _global_monitor


def monitor_function(key: str):
    """
    函数监控装饰器
    
    自动记录函数执行时间和调用次数
    
    Args:
        key: 监控键名
    
    Example:
        @monitor_function("my_api")
        def my_function():
            # 函数逻辑
            pass
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            monitor = get_monitor()
            start = time.time()
            
            try:
                result = func(*args, **kwargs)
                elapsed_ms = (time.time() - start) * 1000
                monitor.record_response_time(key, elapsed_ms)
                return result
            except Exception as e:
                monitor.record_api_error(key)
                raise
        
        return wrapper
    return decorator


# 便捷函数
def record_cache_hit(key: str):
    """记录缓存命中"""
    get_monitor().record_cache_hit(key)


def record_cache_miss(key: str):
    """记录缓存未命中"""
    get_monitor().record_cache_miss(key)


def record_api_call(key: str):
    """记录API调用"""
    get_monitor().record_api_call(key)


def record_api_error(key: str):
    """记录API错误"""
    get_monitor().record_api_error(key)


def record_response_time(key: str, time_ms: float):
    """记录响应时间"""
    get_monitor().record_response_time(key, time_ms)


def record_rate_limit_wait(key: str, wait_time: float):
    """记录限流等待"""
    get_monitor().record_rate_limit_wait(key, wait_time)


def print_stats(key: Optional[str] = None):
    """打印统计信息"""
    get_monitor().print_summary(key)


def reset_stats(key: Optional[str] = None):
    """重置统计信息"""
    get_monitor().reset_stats(key)
