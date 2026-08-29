"""PR-D1: 模拟盘每日调度器。

集成到已有的 StrategyFactoryScheduler 循环中，在每个 cycle 结束后：
1. 对所有 incubating 策略生成当日信号
2. 自动下单到模拟盘
3. 计算 NAV 快照
4. 评估是否达到 promotion 标准

触发时机：策略工厂 cycle 完成后（盘后 19:00 左右）
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class PaperTradingScheduler:
    """模拟盘每日调度器 — 嵌入策略工厂 cycle 末尾。"""

    def __init__(self, db: Any):
        self._db = db

    async def run_daily_paper_trading_cycle(
        self,
        *,
        signal_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """执行一轮完整的模拟盘日循环。

        Returns:
            dict with processed_count, signal_count, order_count, promotion_candidates.
        """
        from .paper_trading_bridge import PaperTradingBridge

        bridge = PaperTradingBridge(self._db)
        signal_date = signal_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. 加载所有 incubating 策略
        strategies = await self._load_incubating_strategies()
        if not strategies:
            return {
                "signal_date": signal_date,
                "processed_count": 0,
                "signal_count": 0,
                "order_count": 0,
                "promotion_candidates": [],
            }

        signal_count = 0
        order_count = 0
        errors: list[str] = []
        promotion_candidates: list[dict[str, Any]] = []

        for strategy in strategies:
            strategy_id = str(strategy.get("id") or "").strip()
            if not strategy_id:
                continue

            # 2. 确保有 paper_account
            await bridge.on_strategy_incubated(strategy)

            # 3. 获取 K 线数据
            target_symbols = list(
                (strategy.get("params") or {}).get("target_symbols")
                or strategy.get("target_symbols")
                or []
            )
            if not target_symbols:
                continue

            code = str(target_symbols[0]).strip()
            klines = await self._get_recent_klines(code, limit=120)
            if not klines or len(klines) < 30:
                continue

            # 4. 生成信号并下单
            try:
                result = await bridge.run_daily_signals(
                    strategy, klines, signal_date=signal_date
                )
                if result.get("signal") and result["signal"] != 0:
                    signal_count += 1
                if result.get("order_result", {}).get("placed"):
                    order_count += 1
            except Exception as exc:
                errors.append(f"{strategy_id}: {exc}")
                continue

            # 5. 评估 promotion（仅对运行 ≥ 30 天的策略）
            try:
                promo = await bridge.evaluate_promotion(strategy)
                if promo.get("eligible"):
                    promotion_candidates.append({
                        "strategy_id": strategy_id,
                        "name": strategy.get("name"),
                        **promo,
                    })
            except Exception as exc:
                logger.debug("PaperTradingScheduler: promotion eval failed for %s: %s", strategy_id, exc)

        # 6. 计算 NAV 快照
        await self._snapshot_all_navs(signal_date)

        result = {
            "signal_date": signal_date,
            "processed_count": len(strategies),
            "signal_count": signal_count,
            "order_count": order_count,
            "promotion_candidates": promotion_candidates,
            "errors": errors[:5],
        }
        logger.info(
            "PaperTradingScheduler: date=%s, processed=%d, signals=%d, orders=%d, promotions=%d",
            signal_date, len(strategies), signal_count, order_count, len(promotion_candidates),
        )
        return result

    async def _load_incubating_strategies(self) -> list[dict[str, Any]]:
        """加载所有 incubating 状态的策略。"""
        try:
            # 优先使用 list_strategies 方法（strategy-factory adapter 标准接口）
            list_strategies = getattr(self._db, "list_strategies", None)
            if callable(list_strategies):
                rows = await list_strategies(status="incubating", limit=50)
                return [dict(r) for r in (rows or [])]
            # Fallback: 直接 SQL
            fetch = getattr(self._db, "fetch", None)
            if callable(fetch):
                rows = await fetch(
                    "SELECT * FROM strategies WHERE status = 'incubating' ORDER BY updated_at DESC LIMIT 50"
                )
                return [dict(r) for r in (rows or [])]
            return []
        except Exception as exc:
            logger.error("PaperTradingScheduler: load strategies failed: %s", exc)
            return []

    async def _get_recent_klines(self, code: str, limit: int = 120) -> list[dict[str, Any]]:
        """获取最近 N 日 K 线。"""
        get_klines = getattr(self._db, "get_klines", None)
        if callable(get_klines):
            try:
                return list(await get_klines(code, limit=limit) or [])
            except Exception:
                pass
        # Fallback: 直接查表
        try:
            rows = await self._db.fetch(
                "SELECT * FROM kline_1d WHERE code = ? ORDER BY time DESC LIMIT ?",
                code, limit,
            )
            return [dict(r) for r in reversed(rows or [])]
        except Exception:
            return []

    async def _snapshot_all_navs(self, nav_date: str) -> None:
        """为所有活跃的策略模拟盘账户计算 NAV 快照。"""
        try:
            accounts = await self._db.fetch(
                "SELECT id, current_capital FROM paper_accounts "
                "WHERE account_type = 'strategy_incubation' AND status = 'active'"
            )
        except Exception:
            return

        for acct in (accounts or []):
            account_id = str(acct.get("id") or "")
            if not account_id:
                continue
            try:
                # 获取持仓市值
                positions = await self._db.fetch(
                    "SELECT stock_code, quantity, current_price FROM paper_positions "
                    "WHERE account_id = ?",
                    account_id,
                )
                market_value = sum(
                    float(p.get("quantity") or 0) * float(p.get("current_price") or 0)
                    for p in (positions or [])
                )
                cash = float(acct.get("current_capital") or 0)
                total_value = cash + market_value

                # 计算日收益率
                prev_nav = await self._db.fetchrow(
                    "SELECT total_value FROM paper_nav "
                    "WHERE account_id = ? AND nav_date < ? "
                    "ORDER BY nav_date DESC LIMIT 1",
                    account_id, nav_date,
                )
                prev_value = float(prev_nav.get("total_value") or total_value) if prev_nav else total_value
                daily_return = (total_value - prev_value) / prev_value if prev_value > 0 else 0.0

                await self._db.execute(
                    """INSERT OR REPLACE INTO paper_nav
                       (account_id, nav_date, total_value, cash, market_value, daily_return)
                       VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    account_id, nav_date, total_value, cash, market_value, daily_return,
                )
            except Exception as exc:
                logger.debug("PaperTradingScheduler: NAV snapshot failed for %s: %s", account_id, exc)


# 便捷入口：供策略工厂 cycle 末尾调用
async def run_paper_trading_cycle(db: Any, *, signal_date: str | None = None) -> dict[str, Any]:
    """便捷函数：执行一轮模拟盘日循环。"""
    scheduler = PaperTradingScheduler(db)
    return await scheduler.run_daily_paper_trading_cycle(signal_date=signal_date)


from typing import Optional  # noqa: E402

__all__ = ["PaperTradingScheduler", "run_paper_trading_cycle"]
