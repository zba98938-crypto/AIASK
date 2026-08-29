"""数据源整合优化模块 — 统一数据访问层。

功能：
1. 统一数据访问层 - 先查缓存，再调用 API
2. 市场数据自动同步到 SQLite
3. 智能缓存策略 - 根据数据类型设置不同 TTL

注：本模块提供 ``DataSyncService`` (底层数据访问 + 缓存)。
自动定时同步由 ``DataSyncScheduler`` (services/data_sync_scheduler.py) 驱动。
MCP 按需同步工具由 ``data_sync_manager`` (tools/managers/data_sync_manager.py) 提供。
全量/增量同步请使用统一脚本 ``scripts/db_sync.py``。
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

from ..storage import get_db
from ..cache import cache
from ..data_source import data_source


logger = logging.getLogger(__name__)


# 数据源优先级配置
DATA_SOURCE_PRIORITY = [
    "data_source",   # 1. 统一数据源入口
    "tushare_pro",   # 2. Tushare Pro (数据全面)
    "eastmoney",     # 3. 东方财富直接 API (全市场行情/板块资金流)
]

# 缓存 TTL 配置 (秒)
CACHE_TTL = {
    "realtime_quote": 5,       # 实时行情 5秒
    "kline_intraday": 60,      # 分钟K线 1分钟
    "kline_daily": 3600,       # 日K线 1小时
    "trading_dates": 86400,    # 交易日历 24小时
    "ipo_info": 3600,          # IPO信息 1小时
    "cb_info": 3600,           # 可转债信息 1小时
    "gb_info": 86400,          # 股本数据 24小时
    "financial": 86400,        # 财务数据 24小时
}
API_FETCH_TIMEOUT_SECONDS = float(os.getenv("DATA_SYNC_API_FETCH_TIMEOUT_SECONDS", "10"))
BATCH_SYNC_CONCURRENCY = max(1, int(os.getenv("DATA_SYNC_BATCH_CONCURRENCY", "4")))
BATCH_SYNC_PER_CODE_TIMEOUT_SECONDS = float(os.getenv("DATA_SYNC_BATCH_PER_CODE_TIMEOUT_SECONDS", "12"))


class DataSyncService:
    """数据同步服务"""
    
    def __init__(self, dead_letter_dir: Optional[str] = None):
        self._db = None
        self._sync_lock = None
        self._lock_loop = None  # 跟踪 lock 绑定的事件循环

        # 落库任务队列与 worker（受控后台任务）
        self._save_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._save_workers: List[asyncio.Task] = []
        self._workers_started = False
        self._stopping = False

        # 重试配置
        self._max_retry = 3
        self._retry_backoff_base = 0.5
        self._flush_timeout_seconds = 30

        # dead-letter 配置（持久化失败任务）
        cache_dir = getattr(cache, "cache_dir", ".mcp_cache")
        default_dlq_dir = Path(cache_dir) / "dead_letters"
        self._dead_letter_dir = Path(dead_letter_dir) if dead_letter_dir else default_dlq_dir
        self._dead_letter_file = self._dead_letter_dir / "kline_save_failures.jsonl"

        # 任务追踪指标
        self._metrics: Dict[str, float] = {
            "pending": 0,
            "success": 0,
            "fail": 0,
            "retry": 0,
            "lag": 0.0,
            "dead_letter": 0,
        }

    def _get_sync_lock(self) -> asyncio.Lock:
        """懒加载 asyncio.Lock，确保在当前事件循环内创建"""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        
        if self._sync_lock is None or self._lock_loop is not current_loop:
            self._sync_lock = asyncio.Lock()
            self._lock_loop = current_loop
        return self._sync_lock
    
    async def get_db(self):
        """获取数据库连接（SQLiteAdapter 内部自动处理事件循环变更）"""
        if self._db is None:
            self._db = get_db()
        # 不需要手动 initialize()，acquire() 内部会自动处理
        return self._db

    async def _ensure_workers_started(self, worker_count: int = 1) -> None:
        """确保后台落库 worker 已启动（惰性启动）。"""
        if self._workers_started:
            return
        for idx in range(worker_count):
            task = asyncio.create_task(self._save_worker(idx))
            self._save_workers.append(task)
        self._workers_started = True
        logger.info("[DataSync] save workers started", extra={"worker_count": worker_count})

    async def _enqueue_save_task(self, stock_code: str, klines: List[Dict]) -> None:
        """入队异步落库任务，替代裸 create_task 放飞。"""
        if self._stopping:
            logger.warning("[DataSync] service stopping, skip enqueue", extra={"code": stock_code})
            return

        await self._ensure_workers_started()
        item = {
            "stock_code": stock_code,
            "klines": klines,
            "retry": 0,
            "enqueued_at": time.time(),
        }
        await self._save_queue.put(item)
        self._metrics["pending"] = self._save_queue.qsize()

    async def _save_worker(self, worker_id: int) -> None:
        """消费落库队列的后台 worker。"""
        logger.info("[DataSync] save worker online", extra={"worker_id": worker_id})
        while True:
            item = await self._save_queue.get()
            try:
                if item.get("_stop"):
                    logger.info("[DataSync] save worker stopping", extra={"worker_id": worker_id})
                    return

                lag = max(0.0, time.time() - float(item.get("enqueued_at", time.time())))
                self._metrics["lag"] = lag
                await self._save_klines_with_retry(item)
            finally:
                self._save_queue.task_done()
                self._metrics["pending"] = self._save_queue.qsize()

    async def _save_klines_with_retry(self, item: Dict[str, Any]) -> None:
        """保存失败自动重试（指数退避），最终失败写入 dead-letter。"""
        stock_code = item["stock_code"]
        klines = item["klines"]

        for attempt in range(item.get("retry", 0), self._max_retry + 1):
            try:
                await self._save_klines_to_db(stock_code, klines)
                self._metrics["success"] += 1
                return
            except Exception as e:
                if attempt < self._max_retry:
                    self._metrics["retry"] += 1
                    backoff = self._retry_backoff_base * (2 ** attempt)
                    logger.warning(
                        "[DataSync] save failed, retrying",
                        extra={
                            "code": stock_code,
                            "attempt": attempt + 1,
                            "max_retry": self._max_retry,
                            "backoff": backoff,
                            "error": str(e),
                        },
                    )
                    await asyncio.sleep(backoff)
                    continue

                self._metrics["fail"] += 1
                logger.error(
                    "[DataSync] save failed after retries",
                    extra={"code": stock_code, "max_retry": self._max_retry, "error": str(e)},
                )
                self._persist_dead_letter(item=item, error=e)
                return

    def _persist_dead_letter(self, item: Dict[str, Any], error: Exception) -> None:
        """将最终失败的任务写入 jsonl。"""
        record = {
            "kind": "save_failure",
            "stock_code": item.get("stock_code"),
            "retry": item.get("retry", 0),
            "enqueued_at": item.get("enqueued_at"),
            "failed_at": time.time(),
            "error": str(error),
            "klines_count": len(item.get("klines") or []),
            "sample_dates": [
                str(k.get("date"))
                for k in (item.get("klines") or [])[:3]
                if isinstance(k, dict)
            ],
        }

        self._append_dead_letter_record(record)

    def _append_dead_letter_record(self, record: Dict[str, Any]) -> None:
        """追加 dead-letter 记录。"""

        try:
            os.makedirs(self._dead_letter_dir, exist_ok=True)
            with open(self._dead_letter_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._metrics["dead_letter"] += 1
        except Exception as dlq_err:
            logger.error(
                "[DataSync] persist dead letter failed",
                extra={"error": str(dlq_err), "path": str(self._dead_letter_file)},
            )

    def record_rejected_klines(
        self,
        *,
        stock_code: Optional[str],
        rejected_rows: List[Dict[str, Any]],
        source: str = "kline_validation",
    ) -> None:
        """记录被 DQA 拒绝的 K 线行，避免静默丢失。"""
        rejected = [dict(item or {}) for item in list(rejected_rows or []) if isinstance(item, dict)]
        if not rejected:
            return
        record = {
            "kind": "validation_rejection",
            "source": source,
            "stock_code": str(stock_code or ""),
            "failed_at": time.time(),
            "rejected_count": len(rejected),
            "sample_dates": [
                str((item.get("row") or {}).get("date") or "")
                for item in rejected[:5]
                if isinstance(item.get("row"), dict)
            ],
            "rejections": [
                {
                    "index": item.get("index"),
                    "reason": item.get("reason"),
                    "date": (item.get("row") or {}).get("date") if isinstance(item.get("row"), dict) else None,
                    "code": (item.get("row") or {}).get("code") if isinstance(item.get("row"), dict) else None,
                }
                for item in rejected[:20]
            ],
        }
        self._append_dead_letter_record(record)

    def get_dead_letters(self, limit: int = 20) -> Dict[str, Any]:
        """读取最近 dead-letter 记录。"""
        limit = max(1, int(limit or 20))
        if not self._dead_letter_file.exists():
            return {
                "success": True,
                "path": str(self._dead_letter_file),
                "count": 0,
                "records": [],
            }

        records: List[Dict[str, Any]] = []
        try:
            with open(self._dead_letter_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue

            if len(records) > limit:
                records = records[-limit:]

            return {
                "success": True,
                "path": str(self._dead_letter_file),
                "count": len(records),
                "records": records,
            }
        except Exception as e:
            return {
                "success": False,
                "path": str(self._dead_letter_file),
                "count": 0,
                "records": [],
                "error": str(e),
            }

    def clear_dead_letters(self) -> Dict[str, Any]:
        """清空 dead-letter 文件。"""
        removed = 0
        try:
            if self._dead_letter_file.exists():
                self._dead_letter_file.unlink()
                removed = 1
            self._metrics["dead_letter"] = 0
            return {
                "success": True,
                "removed": removed,
                "path": str(self._dead_letter_file),
            }
        except Exception as e:
            return {
                "success": False,
                "removed": removed,
                "path": str(self._dead_letter_file),
                "error": str(e),
            }

    def get_sync_metrics(self) -> Dict[str, Any]:
        """返回当前同步指标（pending/success/fail/retry/lag/dead_letter）。"""
        metrics: Dict[str, Any] = dict(self._metrics)
        metrics["pending"] = self._save_queue.qsize()
        metrics["dead_letter_path"] = str(self._dead_letter_file)
        return metrics

    async def flush(self, timeout_seconds: Optional[float] = None) -> bool:
        """等待所有已入队任务完成。"""
        timeout = timeout_seconds if timeout_seconds is not None else self._flush_timeout_seconds
        try:
            await asyncio.wait_for(self._save_queue.join(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "[DataSync] flush timeout",
                extra={"timeout": timeout, "pending": self._save_queue.qsize()},
            )
            return False

    async def shutdown(self) -> None:
        """优雅停机：flush 队列并停止 worker。"""
        self._stopping = True
        await self.flush()

        if not self._workers_started:
            return

        for _ in self._save_workers:
            await self._save_queue.put({"_stop": True})

        await asyncio.gather(*self._save_workers, return_exceptions=True)
        self._save_workers.clear()
        self._workers_started = False
    
    def _build_kline_cache_key_v2(
        self,
        stock_code: str,
        period: str,
        start_date: str,
        end_date: str,
        limit: int,
    ) -> str:
        """标准化缓存键（namespace + version）。"""
        return f"kline:v2:{stock_code}:{period}:{start_date}:{end_date}:{limit}"

    def _build_kline_cache_key_legacy(
        self,
        stock_code: str,
        period: str,
        start_date: str,
        end_date: str,
        limit: int,
    ) -> str:
        """历史缓存键（兼容读取）。"""
        return f"kline_{stock_code}_{period}_{start_date}_{end_date}_{limit}"

    async def get_kline_with_cache(
        self,
        stock_code: str,
        period: str = "daily",
        start_date: str = "",
        end_date: str = "",
        limit: int = 100,
        use_cache: bool = True
    ) -> dict:
        """
        获取K线数据（带缓存）

        数据流向:
        1. 先查 SimpleCache (短期缓存)
        2. 再查 SQLite (持久缓存)
        3. 缓存未命中则调用 API
        4. 获取后自动写入缓存
        """
        cache_key_v2 = self._build_kline_cache_key_v2(stock_code, period, start_date, end_date, limit)
        cache_key_legacy = self._build_kline_cache_key_legacy(stock_code, period, start_date, end_date, limit)
        ttl = CACHE_TTL.get("kline_daily", 3600)

        # 1. 查询 SimpleCache（优先新key，兼容旧key并回填）
        if use_cache:
            cached = cache.get(cache_key_v2, ttl)
            if not cached:
                cached = cache.get(cache_key_legacy, ttl)
                if cached:
                    cache.set(cache_key_v2, cached)
            if cached:
                data = self._filter_and_enrich_klines(cached, start_date, end_date, limit)
                return {"success": True, "data": data, "source": "simple_cache"}

        # 2. 查询 SQLite
        if use_cache:
            try:
                db = await self.get_db()
                db_data = await db.get_klines(stock_code, start_date, end_date, limit)
                if db_data:
                    data = self._filter_and_enrich_klines(db_data, start_date, end_date, limit)
                    # 写入 SimpleCache（统一使用新key）
                    cache.set(cache_key_v2, data)
                    return {"success": True, "data": data, "source": "sqlite"}
            except Exception as e:
                logger.warning(
                    "[DataSync] SQLite query failed",
                    extra={"code": stock_code, "error": str(e)},
                )

        # 3. 调用 API 获取数据（放到线程中执行，避免阻塞事件循环）
        api_error: Optional[str] = None
        try:
            api_data = await asyncio.wait_for(
                asyncio.to_thread(data_source.get_kline, stock_code, period, limit),
                timeout=API_FETCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            api_data = []
            api_error = f"获取K线数据超时（>{API_FETCH_TIMEOUT_SECONDS}s）"
            logger.warning(
                "[DataSync] API kline fetch timed out",
                extra={"code": stock_code, "period": period, "limit": limit, "timeout": API_FETCH_TIMEOUT_SECONDS},
            )
        except Exception as e:
            api_data = []
            api_error = str(e)
            logger.warning(
                "[DataSync] API kline fetch failed",
                extra={"code": stock_code, "period": period, "limit": limit, "error": api_error},
            )

        if api_data:
            # 4. 日期过滤 + 补充 change_pct
            data = self._filter_and_enrich_klines(api_data, start_date, end_date, limit)

            # 5. 写入缓存（统一使用新key）
            cache.set(cache_key_v2, data)

            # 6. 入队异步写入 SQLite（受控队列 + worker）
            await self._enqueue_save_task(stock_code, data)

            return {"success": True, "data": data, "source": "api"}

        return {
            "success": False,
            "data": [],
            "source": "api_timeout" if api_error and "超时" in api_error else "none",
            "message": api_error or "获取K线数据失败",
        }
    
    def _filter_and_enrich_klines(self, klines: list, start_date: str, end_date: str, limit: int) -> list:
        """过滤日期范围 + 补充 change_pct 字段"""
        if not klines:
            return klines
        
        # 日期过滤
        filtered = klines
        if start_date:
            sd = start_date.replace('-', '')
            filtered = [k for k in filtered if str(k.get('date', '')).replace('-', '') >= sd]
        if end_date:
            ed = end_date.replace('-', '')
            filtered = [k for k in filtered if str(k.get('date', '')).replace('-', '') <= ed]
        
        # 限制数量
        if limit and len(filtered) > limit:
            filtered = filtered[-limit:]
        
        # 补充 change_pct（涨跌幅）
        for i, k in enumerate(filtered):
            if k.get('change_pct') is None:
                close = k.get('close')
                pre_close = None
                if i > 0:
                    pre_close = filtered[i - 1].get('close')
                elif k.get('pre_close') is not None:
                    pre_close = k.get('pre_close')
                if close is not None and pre_close is not None and pre_close != 0:
                    k['change_pct'] = round((close - pre_close) / pre_close * 100, 2)
                else:
                    k['change_pct'] = 0
        
        return filtered
    
    async def _save_klines_to_db(self, stock_code: str, klines: List[Dict]):
        """保存K线数据到 SQLite（单次尝试，异常上抛给重试层）。"""
        db = await self.get_db()
        await db.save_klines(stock_code, klines)
    
    async def sync_trading_dates(self, year: int = None) -> dict:
        """
        同步交易日历到 SQLite
        """
        if year is None:
            year = datetime.now().year
        
        start_date = f"{year}0101"
        end_date = f"{year}1231"
        
        result = data_source.get_trading_dates(
            start_time=start_date,
            end_time=end_date
        )
        
        if result.get("success"):
            dates = result["data"]
            # 过滤确保只返回指定年份的数据
            dates = [d for d in dates if str(d).startswith(str(year))]
            
            # 判断是否为当前年份且数据不完整
            current_year = datetime.now().year
            is_current_year = (year == current_year)
            note = ""
            if is_current_year and len(dates) < 200:
                note = f"当前年份数据截至今日，后续交易日将随时间推移自动更新"
            elif year > current_year:
                note = f"{year}年为未来年份，交易日历可能尚未完全发布"
            
            msg = f"同步 {year} 年交易日历成功，共 {len(dates)} 个交易日"
            if note:
                msg += f"（{note}）"
            
            return {
                "success": True,
                "year": year,
                "dates": dates,
                "count": len(dates),
                "source": result.get("source"),
                "message": msg,
                "note": note if note else None,
            }
        
        return result
    
    async def sync_stock_klines(
        self,
        codes: List[str],
        start_date: str = "",
        end_date: str = "",
        period: str = "daily"
    ) -> dict:
        """
        批量同步股票K线数据
        """
        async with self._get_sync_lock():
            results = {"success": 0, "failed": 0, "errors": []}
            semaphore = asyncio.Semaphore(BATCH_SYNC_CONCURRENCY)

            async def _sync_one(code: str) -> dict:
                async with semaphore:
                    try:
                        result = await asyncio.wait_for(
                            self.get_kline_with_cache(
                                stock_code=code,
                                period=period,
                                start_date=start_date,
                                end_date=end_date,
                                use_cache=False,  # 强制从 API 获取
                            ),
                            timeout=BATCH_SYNC_PER_CODE_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        return {
                            "code": code,
                            "success": False,
                            "error": f"批量同步单标的超时（>{BATCH_SYNC_PER_CODE_TIMEOUT_SECONDS}s）",
                        }
                    except Exception as e:
                        return {"code": code, "success": False, "error": str(e)}
                    if result.get("success"):
                        return {"code": code, "success": True}
                    return {
                        "code": code,
                        "success": False,
                        "error": result.get("message") or result.get("error") or "获取K线数据失败",
                    }

            outcomes = await asyncio.gather(*[_sync_one(code) for code in codes])
            for outcome in outcomes:
                if outcome.get("success"):
                    results["success"] += 1
                else:
                    results["failed"] += 1
                    results["errors"].append({"code": outcome.get("code"), "error": outcome.get("error")})
            
            return {
                "success": True,
                "data": results,
                "message": f"同步完成: 成功 {results['success']}, 失败 {results['failed']}"
            }


# 全局实例
data_sync_service = DataSyncService()
