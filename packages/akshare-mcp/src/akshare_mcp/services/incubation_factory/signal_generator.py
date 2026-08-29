"""孵化工厂 · 信号生成模块。

负责根据策略 DSL 规则为孵化中的策略生成当日交易信号。
信号生成后交由 ForwardVerifier 验证前向收益。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SignalGenerator:
    """策略信号生成器。

    根据策略的 DSL 规则和当日市场数据，生成交易信号。
    生成的信号会被记录到 strategy_signals 表，
    并在后续由 ForwardVerifier 验证其前向收益。
    """

    async def generate(
        self,
        db: Any,
        strategy: dict[str, Any],
        *,
        signal_date: Optional[date] = None,
    ) -> dict[str, Any]:
        """
        为策略生成当日信号。

        流程：
        1. 加载策略 DSL 参数
        2. 获取目标股票的最新行情
        3. 执行 DSL 规则引擎生成信号
        4. 记录信号到数据库
        5. 同步信号到 paper_orders（模拟下单）

        Args:
            db: 数据库连接
            strategy: 策略记录
            signal_date: 信号日期（默认今天）

        Returns:
            信号生成结果摘要
        """
        sid = str(strategy.get("id") or "").strip()
        if not sid:
            return self._empty_result(sid)

        today = signal_date or date.today()

        # 使用已有的 StrategyIncubationService.sync_signals_to_orders
        # 它内部会：
        #   1. 编译策略 DSL
        #   2. 获取目标股票行情
        #   3. 执行规则引擎
        #   4. 生成信号并写入 DB
        #   5. 同步到 paper_orders
        try:
            from ..incubation import get_strategy_incubation_service

            incubation_service = get_strategy_incubation_service()
            sync_result = await incubation_service.sync_signals_to_orders(
                db, strategy, today
            )

            signals_generated = int(sync_result.get("signals_generated") or 0)
            orders_created = int(
                sync_result.get("orders_created")
                or sync_result.get("created_count")
                or 0
            )
            errors = list(sync_result.get("errors") or [])

            result = {
                "strategy_id": sid,
                "signal_date": str(today),
                "signals_generated": signals_generated,
                "orders_created": orders_created,
                "errors": errors,
                "sync_result": sync_result,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

            if signals_generated > 0:
                logger.debug(
                    "SignalGenerator: %s generated %d signals, %d orders on %s",
                    sid,
                    signals_generated,
                    orders_created,
                    today,
                )

            return result

        except Exception as exc:
            logger.warning(
                "SignalGenerator: failed for %s on %s: %s", sid, today, exc
            )
            return {
                "strategy_id": sid,
                "signal_date": str(today),
                "signals_generated": 0,
                "orders_created": 0,
                "errors": [str(exc)],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

    async def generate_batch(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
        *,
        signal_date: Optional[date] = None,
    ) -> dict[str, Any]:
        """
        批量为策略生成信号。

        Args:
            db: 数据库连接
            strategies: 策略列表
            signal_date: 信号日期

        Returns:
            批量生成结果摘要
        """
        today = signal_date or date.today()
        total_signals = 0
        total_orders = 0
        generated_count = 0
        error_count = 0
        results: dict[str, dict[str, Any]] = {}

        for strategy in strategies:
            sid = str(strategy.get("id") or "").strip()
            if not sid:
                continue

            result = await self.generate(db, strategy, signal_date=today)
            results[sid] = result

            signals = int(result.get("signals_generated") or 0)
            orders = int(result.get("orders_created") or 0)
            errors = list(result.get("errors") or [])

            total_signals += signals
            total_orders += orders
            if signals > 0:
                generated_count += 1
            if errors:
                error_count += 1

        return {
            "signal_date": str(today),
            "total_strategies": len(strategies),
            "generated_count": generated_count,
            "error_count": error_count,
            "total_signals": total_signals,
            "total_orders": total_orders,
            "results": results,
        }

    def _empty_result(self, strategy_id: str) -> dict[str, Any]:
        return {
            "strategy_id": strategy_id,
            "signal_date": str(date.today()),
            "signals_generated": 0,
            "orders_created": 0,
            "errors": ["invalid strategy_id"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
