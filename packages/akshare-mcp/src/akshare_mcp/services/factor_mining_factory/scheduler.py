"""因子挖掘工厂调度器 — 继承现有 FactorScheduler 接口，渐进替换。

调度策略：
- 每日 18:00: 常规挖掘周期（LLM + GP + Rule 混合）
- 每日 06:00: 因子池维护（衰减检测 + 退役）
- 每周日 02:00: 深度进化优化（GP 大种群）
- 每月 1 日 03:00: Meta-Learner 分析
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 默认运行时间
DEFAULT_MINING_TIME = time(18, 0)
DEFAULT_MAINTENANCE_TIME = time(6, 0)


class FactorMiningFactoryScheduler:
    """因子挖掘工厂调度器。

    保持与现有 FactorScheduler 兼容的接口（start/stop/run_once）。
    """

    def __init__(
        self,
        *,
        mining_time: time = DEFAULT_MINING_TIME,
        maintenance_time: time = DEFAULT_MAINTENANCE_TIME,
    ):
        self.mining_time = mining_time
        self.maintenance_time = maintenance_time
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_mining_at: Optional[datetime] = None
        self._last_maintenance_at: Optional[datetime] = None
        self._consecutive_errors = 0

    def start(self):
        """启动调度器（非阻塞）。"""
        if self._running:
            logger.warning("FactorMiningFactoryScheduler already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="factor-mining-factory-scheduler")
        logger.info("FactorMiningFactoryScheduler started (mining=%s, maintenance=%s)",
                    self.mining_time, self.maintenance_time)

    def stop(self):
        """停止调度器。"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("FactorMiningFactoryScheduler stopped")

    async def shutdown(self, grace_sec: float = 3.0) -> None:
        """Stop the scheduler and wait for the background task before loop exit."""
        self._running = False
        task = self._task
        self._task = None
        if task is None:
            logger.info("FactorMiningFactoryScheduler stopped")
            return
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, grace_sec))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        else:
            with suppress(asyncio.CancelledError):
                await task
        logger.info("FactorMiningFactoryScheduler stopped")

    async def run_once(self) -> dict[str, Any]:
        """执行一次完整的挖掘周期（兼容 FactorScheduler.run_once）。"""
        from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
        from strategy_factory.runtime.factor_mining import get_factor_mining_runtime

        ensure_default_runtime_services()
        runtime = get_factor_mining_runtime()
        result = await runtime.run_once(trigger="manual")
        self._last_mining_at = datetime.now(timezone.utc)
        return result

    async def _loop(self):
        """主调度循环。"""
        while self._running:
            try:
                now = datetime.now()

                # 计算下一个事件时间
                next_mining = datetime.combine(now.date(), self.mining_time)
                if next_mining <= now:
                    next_mining += timedelta(days=1)

                next_maintenance = datetime.combine(now.date(), self.maintenance_time)
                if next_maintenance <= now:
                    next_maintenance += timedelta(days=1)

                # 选择最近的事件
                next_event = min(next_mining, next_maintenance)
                wait_seconds = (next_event - now).total_seconds()

                logger.info("FactorMiningFactoryScheduler: next event in %.0fs at %s",
                            wait_seconds, next_event)
                await asyncio.sleep(wait_seconds)

                if not self._running:
                    break

                # 执行对应任务
                if next_event == next_mining:
                    await self._run_mining()
                else:
                    await self._run_maintenance()

                self._consecutive_errors = 0

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._consecutive_errors += 1
                backoff = min(60 * (2 ** (self._consecutive_errors - 1)), 3600)
                logger.error("FactorMiningFactoryScheduler error (#%d, backoff %.0fs): %s",
                             self._consecutive_errors, backoff, exc, exc_info=True)
                await asyncio.sleep(backoff)

    async def _run_mining(self):
        """执行挖掘周期。"""
        from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
        from strategy_factory.runtime.factor_mining import get_factor_mining_runtime

        ensure_default_runtime_services()
        runtime = get_factor_mining_runtime()
        result = await runtime.run_once(trigger="scheduled")
        self._last_mining_at = datetime.now(timezone.utc)
        logger.info("FactorMiningFactoryScheduler: mining completed: admitted=%d pool=%d",
                    result.get("admitted_count", 0), result.get("pool_size", 0))

    async def _run_maintenance(self):
        """执行维护任务。"""
        from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
        from strategy_factory.runtime.factor_mining import get_factor_mining_runtime

        ensure_default_runtime_services()
        runtime = get_factor_mining_runtime()
        result = await runtime.run_maintenance()
        self._last_maintenance_at = datetime.now(timezone.utc)
        logger.info("FactorMiningFactoryScheduler: maintenance completed: pool=%d",
                    result.get("pool_size", 0))


# 全局单例
_scheduler: Optional[FactorMiningFactoryScheduler] = None


def get_factor_mining_factory_scheduler() -> FactorMiningFactoryScheduler:
    """获取调度器单例。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = FactorMiningFactoryScheduler()
    return _scheduler
