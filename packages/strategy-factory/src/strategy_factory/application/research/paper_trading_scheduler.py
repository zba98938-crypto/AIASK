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
from datetime import datetime
from typing import Any, Optional

from .paper_trading_bridge import _MARKET_TZ, _closed_bars, _is_closed_day, _number, _target_symbols, _trading_day
from .daily_prediction_writer import DailyPredictionWriter
from ..selective_gate import SelectiveGate
from datetime import timedelta

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

        When ``STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED`` is on, each generated
        signal first writes the next-session prediction so that orders and
        predictions share one identity; when
        ``STRATEGY_FACTORY_SELECTIVE_GATE_ENABLED`` is on, orders are placed only
        for predictions the calibration gate marked ``ordered``. With the gate
        on and no active calibration model, every name abstains — the intended
        fail-closed state, reported in ``selective_gate`` rather than as an error.
        """
        from .paper_trading_bridge import PaperTradingBridge
        from ..hitrate_toggles import (
            daily_prediction_writing_enabled,
            selective_gate_enabled,
            selective_gate_threshold,
        )

        bridge = PaperTradingBridge(self._db)
        write_predictions = daily_prediction_writing_enabled()
        gate_on = selective_gate_enabled()
        writer = DailyPredictionWriter() if write_predictions else None
        gate = SelectiveGate(threshold=selective_gate_threshold()) if gate_on else None
        gate_summary: dict[str, Any] = {
            "enabled": gate_on,
            "predictions_written": 0,
            "ordered": 0,
            "orders_placed": 0,
            "abstained": 0,
            "reason_counts": {},
        }

        # 1. 加载所有 incubating 策略
        strategies = await self._load_incubating_strategies()
        if not strategies:
            return {
                "signal_date": signal_date,
                "processed_count": 0,
                "signal_count": 0,
                "order_count": 0,
                "promotion_candidates": [],
                "selective_gate": gate_summary,
            }

        strategies_by_id = {
            str(s.get("id") or "").strip(): s for s in strategies if str(s.get("id") or "").strip()
        }

        signal_count = 0
        order_count = 0
        errors: list[str] = []
        promotion_candidates: list[dict[str, Any]] = []
        signal_dates: set[str] = set()
        processed_symbol_count = 0
        written_predictions: list[dict[str, Any]] = []

        for strategy in strategies:
            strategy_id = str(strategy.get("id") or "").strip()
            if not strategy_id:
                continue

            # 2. 确保有 paper_account
            account = await bridge.on_strategy_incubated(strategy)
            if not account.get("success"):
                errors.append(f"{strategy_id}: paper_account_unavailable")
                continue

            # 3. 获取 K 线数据
            strategy_dates: set[str] = set()
            for code in _target_symbols(strategy):
                processed_symbol_count += 1
                # Each symbol needs its own history, close price and signal date.
                try:
                    klines = await self._get_recent_klines(code, limit=120)
                    if len(klines) < 30:
                        errors.append(f"{strategy_id}:{code}: insufficient_closed_bars")
                        continue
                    result = await bridge.run_daily_signals(
                        strategy,
                        klines,
                        signal_date=signal_date,
                        code=code,
                        place_orders=not gate_on,
                    )
                    if not result.get("success"):
                        errors.append(f"{strategy_id}:{code}: {result.get('error')}")
                        continue
                    strategy_dates.add(result["signal_date"])
                    if result.get("signal"):
                        signal_count += 1
                    if writer is not None:
                        written = await writer.write(
                            self._db,
                            strategy=strategy,
                            code=code,
                            klines=klines,
                            signal=int(result.get("signal") or 0),
                        )
                        if written.get("status") == "ok":
                            gate_summary["predictions_written"] += 1
                            written_predictions.append(written)
                        elif written.get("status") == "degraded":
                            errors.append(f"{strategy_id}:{code}: prediction_write_degraded:{written.get('reason')}")
                    if gate_on:
                        # The order decision is made per session below, not per
                        # signal: the gate needs the whole session's predictions.
                        continue
                    if result.get("order_result", {}).get("placed"):
                        order_count += 1
                except Exception as exc:
                    errors.append(f"{strategy_id}:{code}: {exc}")

            signal_dates.update(strategy_dates)
            # Do not fabricate a weekend/today snapshot when all histories are stale
            # or missing. Snapshot only the account associated with these observations.
            if strategy_dates and account.get("account_id"):
                await self._snapshot_all_navs(max(strategy_dates), account_ids={str(account["account_id"])})

            # 5. 只返回 paper promotion 候选；不切换状态，不执行实盘交易。
            try:
                promo = await bridge.evaluate_promotion(strategy)
                if promo.get("eligible"):
                    promotion_candidates.append({
                        "strategy_id": strategy_id,
                        "name": strategy.get("name"),
                        **promo,
                    })
            except Exception as exc:
                errors.append(f"{strategy_id}: promotion_evaluation_failed")
                logger.warning("PaperTradingScheduler: promotion eval failed for %s: %s", strategy_id, exc)

        effective_date = str(signal_date).strip() if signal_date is not None else (max(signal_dates) if signal_dates else None)

        # 4b. One gate pass per session, after every strategy has written its
        # predictions. Deciding inside the per-strategy loop would only see the
        # strategies processed so far. ``bridge`` is the executor: it turns each
        # ``ordered`` decision into a real paper order and returns the order id
        # the gate writes back to ``prediction_executions.paper_order_id``.
        #
        # The gate's reference date is the session the predictions target, not
        # the closed bar that produced them. The gate searches for predictions
        # whose as-of falls in the 7-day window *before* its reference date
        # (strict "<"), so passing the closed-bar date would exclude the
        # just-written prediction. The reference date is read straight from
        # the predictions written this session — no kline refetching.
        if gate_on and gate is not None:
            gate_date = None
            for payload in written_predictions:
                target = _trading_day(payload.get("target_trading_date"))
                if target is not None and (gate_date is None or target > gate_date):
                    gate_date = target
            if gate_date is None:
                # No prediction was written this session; fall back to
                # effective_date + 1 day, then to the wall clock.
                if effective_date is not None:
                    ed = _trading_day(effective_date)
                    gate_date = ed + timedelta(days=1) if ed else None
                if gate_date is None:
                    gate_date = datetime.now(_MARKET_TZ).date()
            outcome = await gate.evaluate_session(
                self._db,
                signal_date=gate_date,
                strategies=strategies_by_id,
                executor=bridge,
            )
            placed = int(outcome.get("orders_placed") or 0)
            gate_summary["ordered"] = int(outcome.get("ordered") or 0)
            gate_summary["orders_placed"] = placed
            gate_summary["abstained"] = int(outcome.get("abstained") or 0)
            gate_summary["reason_counts"] = dict(outcome.get("reason_counts") or {})
            gate_summary["matched_predictions"] = int(outcome.get("matched_predictions") or 0)
            order_count += placed

        result = {
            "signal_date": effective_date,
            "signal_dates": sorted(signal_dates),
            "processed_count": len(strategies),
            "processed_symbol_count": processed_symbol_count,
            "signal_count": signal_count,
            "order_count": order_count,
            "promotion_candidates": promotion_candidates,
            "error_count": len(errors),
            "errors": errors[:5],
            "selective_gate": gate_summary,
        }
        logger.info(
            "PaperTradingScheduler: date=%s, processed=%d, signals=%d, orders=%d, promotions=%d",
            effective_date, len(strategies), signal_count, order_count, len(promotion_candidates),
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
                return _closed_bars(list(await get_klines(code, limit=limit) or []))
            except Exception as _swallowed:
                logger.warning("packages/strategy-factory/src/strategy_factory/application/research/paper_trading_scheduler.py:149: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass
        # Fallback: 直接查表
        try:
            rows = await self._db.fetch(
                "SELECT * FROM kline_1d WHERE code = ? ORDER BY time DESC LIMIT ?",
                code, limit,
            )
            return _closed_bars([dict(r) for r in (rows or [])])
        except Exception:
            return []

    async def _snapshot_all_navs(self, nav_date: str, *, account_ids: Optional[set[str]] = None) -> None:
        """为所有活跃的策略模拟盘账户计算 NAV 快照。"""
        day = _trading_day(nav_date)
        now = datetime.now(_MARKET_TZ)
        # Current cash/holdings are not a historical ledger snapshot. Never backdate
        # today's state onto a stale history's last day (or overwrite Friday on Sunday).
        if day is None or day != now.date() or not _is_closed_day(day, now):
            return
        nav_date = day.isoformat()
        try:
            accounts = await self._db.fetch(
                "SELECT id, current_capital FROM paper_accounts "
                "WHERE account_type = 'strategy_incubation' AND status = 'active'"
            )
        except Exception as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/application/research/paper_trading_scheduler.py:168: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return

        for acct in (accounts or []):
            account_id = str(acct.get("id") or "")
            if not account_id or (account_ids is not None and account_id not in account_ids):
                continue
            try:
                # 获取持仓市值
                positions = await self._db.fetch(
                    "SELECT stock_code, quantity, current_price FROM paper_positions "
                    "WHERE account_id = ?",
                    account_id,
                )
                market_value = 0.0
                for position in (positions or []):
                    quantity = _number(position.get("quantity"))
                    bars = await self._get_recent_klines(str(position.get("stock_code") or ""))
                    price = next((_number(bar.get("close")) for bar in bars if bar["date"] == nav_date), None)
                    if quantity is None or quantity < 0 or price is None or price <= 0:
                        raise ValueError("missing_closed_position_valuation")
                    market_value += quantity * price
                cash = _number(acct.get("current_capital"))
                if cash is None:
                    raise ValueError("missing_cash_balance")
                total_value = cash + market_value
                if total_value <= 0:
                    raise ValueError("nonpositive_nav")

                # 计算日收益率
                prev_nav = await self._db.fetchrow(
                    "SELECT total_value FROM paper_nav "
                    "WHERE account_id = ? AND nav_date < ? "
                    "ORDER BY nav_date DESC LIMIT 1",
                    account_id, nav_date,
                )
                prev_value = _number(prev_nav.get("total_value")) if prev_nav else None
                daily_return = (total_value - prev_value) / prev_value if prev_value is not None and prev_value > 0 else None

                await self._db.execute(
                    """INSERT OR REPLACE INTO paper_nav
                       (account_id, nav_date, total_value, cash, market_value, daily_return)
                       VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    account_id, nav_date, total_value, cash, market_value, daily_return,
                )
            except Exception as exc:
                logger.warning("PaperTradingScheduler: NAV snapshot failed for %s: %s", account_id, exc)


# 便捷入口：供策略工厂 cycle 末尾调用
async def run_paper_trading_cycle(db: Any, *, signal_date: str | None = None) -> dict[str, Any]:
    """便捷函数：执行一轮模拟盘日循环。"""
    scheduler = PaperTradingScheduler(db)
    return await scheduler.run_daily_paper_trading_cycle(signal_date=signal_date)


__all__ = ["PaperTradingScheduler", "run_paper_trading_cycle"]
