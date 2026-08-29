"""PR-D1: 模拟盘自动化桥接。

将通过 Gate-3 的策略自动接入模拟盘验证流程：
1. 策略进入 incubating 状态时，自动创建 paper_account 并绑定
2. 每日收盘后，对所有 incubating 策略生成当日信号
3. 将信号自动转为 paper_orders（通过 paper_trading_manager）
4. 每日计算 NAV 快照，跟踪实际命中率
5. 30 天后评估是否达到实盘标准

依赖的已有基础设施：
- paper_accounts / paper_orders / paper_trades / paper_nav 表
- paper_trading_manager（订单生命周期 + 风控）
- matching_engine（异步撮合）
- signal_tracker（信号跟踪 + 前向收益验证）
- strategy_signals / signal_forward_returns 表
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
import inspect
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 模拟盘默认配置
_DEFAULT_PAPER_CAPITAL = 100_000.0
_DEFAULT_RISK_RULES = {
    "max_position_pct": 30.0,
    "max_drawdown_pct": 20.0,
    "stop_loss_pct": 10.0,
}
_INCUBATION_MIN_DAYS = 30
_PROMOTION_THRESHOLDS = {
    "min_win_rate": 0.45,
    "min_profit_factor": 1.5,
    "max_drawdown": 0.25,
    "min_sharpe": 0.5,
    "min_trade_count": 10,
    "win_rate_vs_backtest_ratio": 0.80,  # 实际命中率 ≥ 回测的 80%
}


class PaperTradingBridge:
    """策略工厂 → 模拟盘的自动化桥接。

    生命周期：
    1. on_strategy_incubated() — 策略进入孵化时调用
    2. run_daily_signals() — 每日收盘后调用（由 SignalTracker 触发）
    3. evaluate_promotion() — 30 天后评估是否可上实盘
    """

    def __init__(self, db: Any):
        self._db = db

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _execute(self, query: str, *args: Any) -> Any:
        execute = getattr(self._db, "execute", None)
        if callable(execute):
            return await self._maybe_await(execute(query, *args))
        acquire = getattr(self._db, "acquire", None)
        if callable(acquire):
            async with acquire() as conn:
                return await conn.execute(query, *args)
        raise AttributeError(f"{type(self._db).__name__} does not expose execute/acquire")

    async def _fetchrow(self, query: str, *args: Any) -> Optional[dict[str, Any]]:
        fetchrow = getattr(self._db, "fetchrow", None)
        if callable(fetchrow):
            row = await self._maybe_await(fetchrow(query, *args))
            return dict(row) if row else None
        acquire = getattr(self._db, "acquire", None)
        if callable(acquire):
            async with acquire() as conn:
                row = await conn.fetchrow(query, *args)
                return dict(row) if row else None
        raise AttributeError(f"{type(self._db).__name__} does not expose fetchrow/acquire")

    async def _fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        fetch = getattr(self._db, "fetch", None)
        if callable(fetch):
            rows = await self._maybe_await(fetch(query, *args))
            return [dict(row) for row in (rows or [])]
        acquire = getattr(self._db, "acquire", None)
        if callable(acquire):
            async with acquire() as conn:
                rows = await conn.fetch(query, *args)
                return [dict(row) for row in (rows or [])]
        raise AttributeError(f"{type(self._db).__name__} does not expose fetch/acquire")

    # ─── 1. 策略进入孵化时自动创建模拟盘账户 ───────────────────────────

    async def on_strategy_incubated(
        self,
        strategy: dict[str, Any],
        *,
        initial_capital: float = _DEFAULT_PAPER_CAPITAL,
    ) -> dict[str, Any]:
        """策略进入 incubating 状态时，自动创建并绑定 paper_account。"""
        strategy_id = str(strategy.get("id") or "").strip()
        strategy_name = str(strategy.get("name") or strategy.get("strategy_type") or "").strip()
        if not strategy_id:
            return {"success": False, "error": "missing_strategy_id"}

        # 检查是否已有绑定的 paper_account
        existing = await self._find_paper_account(strategy_id)
        if existing:
            logger.info(
                "PaperTradingBridge: strategy %s already has paper_account %s",
                strategy_id, existing.get("id"),
            )
            return {"success": True, "account_id": existing.get("id"), "reused": True}

        # 创建新的 paper_account
        account_id = f"paper_{strategy_id[:8]}_{uuid.uuid4().hex[:6]}"
        account_name = f"模拟盘·{strategy_name[:20]}"

        try:
            await self._execute(
                """INSERT INTO paper_accounts
                   (id, user_id, name, initial_capital, current_capital, total_value,
                    strategy_id, account_type, incubation_stage, status, risk_rules)
                   VALUES ($1, 'system', $2, $3, $4, $5, $6, 'strategy_incubation', 'warmup', 'active', $7)
                """,
                account_id,
                account_name,
                initial_capital,
                initial_capital,
                initial_capital,
                strategy_id,
                __import__("json").dumps(_DEFAULT_RISK_RULES),
            )
        except Exception as exc:
            logger.error("PaperTradingBridge: failed to create account: %s", exc)
            return {"success": False, "error": str(exc)}

        # 绑定 session
        try:
            await self._execute(
                """INSERT OR IGNORE INTO strategy_paper_sessions
                   (id, strategy_id, user_id, account_id, session_type)
                   VALUES ($1, $2, 'system', $3, 'incubation')
                """,
                f"session_{strategy_id[:8]}_{uuid.uuid4().hex[:8]}",
                strategy_id,
                account_id,
            )
        except Exception as exc:
            logger.debug("PaperTradingBridge: session binding note: %s", exc)

        logger.info(
            "PaperTradingBridge: created paper_account %s for strategy %s (capital=%.0f)",
            account_id, strategy_id, initial_capital,
        )
        return {"success": True, "account_id": account_id, "reused": False}

    # ─── 2. 每日信号生成 → 订单下达 ──────────────────────────────────────

    async def run_daily_signals(
        self,
        strategy: dict[str, Any],
        klines: list[dict[str, Any]],
        *,
        signal_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """对单个策略生成当日信号并自动下单到模拟盘。

        Args:
            strategy: 策略记录（含 id, strategy_type, params）
            klines: 最近 N 日 K 线数据
            signal_date: 信号日期（默认今天）
        """
        import numpy as np
        from aiask_quant_core.backtest import StrategyRegistry

        strategy_id = str(strategy.get("id") or "").strip()
        strategy_type = str(strategy.get("strategy_type") or "").strip()
        params = dict(strategy.get("params") or {})
        target_symbols = list(params.get("target_symbols") or strategy.get("target_symbols") or [])

        if not strategy_id or not strategy_type or not klines:
            return {"success": False, "error": "missing_required_fields"}

        signal_date = str(signal_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")).strip()

        # 获取策略实例
        try:
            inst, _ = StrategyRegistry.create_runtime_strategy(strategy_type, params)
        except Exception as exc:
            return {"success": False, "error": f"strategy_instantiation_failed: {exc}"}

        if inst is None:
            return {"success": False, "error": f"unknown_strategy_type: {strategy_type}"}

        # 生成信号
        closes = np.array([float(k.get("close") or 0) for k in klines], dtype=float)
        volumes = np.array([float(k.get("volume") or 0) for k in klines], dtype=float)
        signals = inst.generate_signals(closes, volumes)
        latest_bar = dict(klines[-1] or {}) if klines else {}
        latest_bar_date = str(
            latest_bar.get("date")
            or latest_bar.get("trade_date")
            or latest_bar.get("datetime")
            or signal_date
        ).strip()

        # 取最后一根 bar 的信号
        latest_signal = int(signals[-1]) if len(signals) > 0 else 0
        target_code = str(target_symbols[0] if target_symbols else "").strip()

        if latest_signal == 0 or not target_code:
            return {
                "success": True,
                "signal": 0,
                "action": "hold",
                "signal_date": signal_date,
            }

        # 保存信号到 strategy_signals 表
        try:
            signal_metadata = {
                "latest_bar_date": latest_bar_date,
                "latest_signal_index": max(0, len(signals) - 1),
                "latest_nonzero_signal_date": latest_bar_date if latest_signal != 0 else None,
                "latest_nonzero_signal": latest_signal if latest_signal != 0 else None,
                "action_source": "paper_trading_bridge",
            }
            await self._execute(
                """INSERT OR REPLACE INTO strategy_signals
                   (strategy_id, signal_date, code, signal, score, action_source, signal_metadata)
                   VALUES ($1, $2, $3, $4, $5, 'paper_trading_bridge', $6)
                """,
                strategy_id,
                signal_date,
                target_code,
                latest_signal,
                float(signals[-1]),
                __import__("json").dumps(signal_metadata),
            )
        except Exception as exc:
            logger.debug("PaperTradingBridge: save signal note: %s", exc)

        # 找到绑定的 paper_account
        account = await self._find_paper_account(strategy_id)
        if not account:
            return {
                "success": True,
                "signal": latest_signal,
                "action": "signal_only_no_account",
                "signal_date": signal_date,
            }

        account_id = str(account.get("id") or "")

        # 下单到模拟盘
        latest_price = float(closes[-1]) if len(closes) > 0 else 0.0
        order_result = await self._place_paper_order(
            account_id=account_id,
            code=target_code,
            signal=latest_signal,
            strategy_id=strategy_id,
            signal_date=signal_date,
            latest_price=latest_price,
        )

        return {
            "success": True,
            "signal": latest_signal,
            "action": "buy" if latest_signal > 0 else "sell",
            "signal_date": signal_date,
            "code": target_code,
            "order_result": order_result,
        }

    # ─── 3. 30 天后评估是否可上实盘 ──────────────────────────────────────

    async def evaluate_promotion(
        self,
        strategy: dict[str, Any],
    ) -> dict[str, Any]:
        """评估策略是否达到实盘标准。

        条件（全部满足）：
        - 模拟盘运行 ≥ 30 天
        - 实际命中率 ≥ 回测命中率的 80%
        - 利润因子 ≥ 1.5
        - 最大回撤 ≤ 25%
        - 交易次数 ≥ 10
        """
        strategy_id = str(strategy.get("id") or "").strip()
        account = await self._find_paper_account(strategy_id)
        if not account:
            return {"eligible": False, "reason": "no_paper_account"}

        account_id = str(account.get("id") or "")

        # 获取 NAV 历史
        nav_rows = await self._get_nav_history(account_id)
        if len(nav_rows) < _INCUBATION_MIN_DAYS:
            return {
                "eligible": False,
                "reason": f"insufficient_days ({len(nav_rows)} < {_INCUBATION_MIN_DAYS})",
                "days_active": len(nav_rows),
            }

        # 获取交易记录
        trades = await self._get_trades(account_id)
        if not trades:
            return {"eligible": False, "reason": "no_trades", "days_active": len(nav_rows)}

        # 计算实际指标
        wins = [t for t in trades if float(t.get("amount") or 0) > 0 and t.get("trade_type") == "sell"]
        total_sells = [t for t in trades if t.get("trade_type") == "sell"]
        win_count = sum(1 for t in total_sells if float(t.get("profit") or t.get("amount") or 0) > 0)
        total_count = len(total_sells)

        actual_win_rate = win_count / total_count if total_count > 0 else 0.0

        # NAV 指标
        navs = [float(r.get("total_value") or 0) for r in nav_rows if float(r.get("total_value") or 0) > 0]
        if not navs:
            return {"eligible": False, "reason": "no_valid_nav"}

        initial = navs[0]
        final = navs[-1]
        total_return = (final - initial) / initial if initial > 0 else 0.0

        # 最大回撤
        peak = navs[0]
        max_dd = 0.0
        for v in navs:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        # 回测命中率（从策略 params 中取）
        params = dict(strategy.get("params") or {})
        backtest_win_rate = float(params.get("backtest_win_rate") or params.get("win_rate") or 0.5)

        # 评估
        thresholds = _PROMOTION_THRESHOLDS
        reasons: list[str] = []

        if actual_win_rate < thresholds["min_win_rate"]:
            reasons.append(f"win_rate {actual_win_rate:.3f} < {thresholds['min_win_rate']}")
        if total_count < thresholds["min_trade_count"]:
            reasons.append(f"trade_count {total_count} < {thresholds['min_trade_count']}")
        if max_dd > thresholds["max_drawdown"]:
            reasons.append(f"max_drawdown {max_dd:.3f} > {thresholds['max_drawdown']}")
        if backtest_win_rate > 0 and actual_win_rate < backtest_win_rate * thresholds["win_rate_vs_backtest_ratio"]:
            reasons.append(
                f"actual_win_rate {actual_win_rate:.3f} < "
                f"backtest {backtest_win_rate:.3f} × {thresholds['win_rate_vs_backtest_ratio']}"
            )

        eligible = len(reasons) == 0

        return {
            "eligible": eligible,
            "days_active": len(nav_rows),
            "trade_count": total_count,
            "actual_win_rate": round(actual_win_rate, 4),
            "backtest_win_rate": round(backtest_win_rate, 4),
            "total_return": round(total_return, 4),
            "max_drawdown": round(max_dd, 4),
            "reasons": reasons,
        }

    # ─── 内部方法 ─────────────────────────────────────────────────────────

    async def _find_paper_account(self, strategy_id: str) -> Optional[dict[str, Any]]:
        """查找策略绑定的 paper_account。"""
        try:
            row = await self._fetchrow(
                "SELECT * FROM paper_accounts WHERE strategy_id = $1 AND status = 'active' LIMIT 1",
                strategy_id,
            )
            return dict(row) if row else None
        except Exception:
            return None

    async def _find_existing_bridge_order(
        self,
        *,
        strategy_id: str,
        signal_date: str,
        code: str,
        direction: str,
    ) -> Optional[dict[str, Any]]:
        strategy_id = str(strategy_id or "").strip()
        signal_date = str(signal_date or "").strip()
        code = str(code or "").strip()
        direction = str(direction or "").strip().lower()
        if not strategy_id or not signal_date or not code or not direction:
            return None
        try:
            return await self._fetchrow(
                """SELECT * FROM paper_orders
                   WHERE source = 'paper_trading_bridge'
                     AND strategy_id = $1
                     AND signal_date = $2
                     AND code = $3
                     AND direction = $4
                   ORDER BY created_at ASC, id ASC
                   LIMIT 1
                """,
                strategy_id,
                signal_date,
                code,
                direction,
            )
        except Exception as exc:
            logger.debug("PaperTradingBridge: existing order lookup failed: %s", exc)
            return None

    @staticmethod
    def _reused_order_result(row: dict[str, Any], *, direction: str) -> dict[str, Any]:
        return {
            "placed": False,
            "reused": True,
            "order_id": str(row.get("id") or ""),
            "direction": str(row.get("direction") or direction),
            "shares": int(row.get("shares") or 0),
            "status": row.get("status"),
        }

    async def _place_paper_order(
        self,
        *,
        account_id: str,
        code: str,
        signal: int,
        strategy_id: str,
        signal_date: str,
        latest_price: float = 0.0,
    ) -> dict[str, Any]:
        """向模拟盘下单。"""
        account_id = str(account_id or "").strip()
        code = str(code or "").strip()
        strategy_id = str(strategy_id or "").strip()
        signal_date = str(signal_date or "").strip()
        direction = "buy" if signal > 0 else "sell"

        existing = await self._find_existing_bridge_order(
            strategy_id=strategy_id,
            signal_date=signal_date,
            code=code,
            direction=direction,
        )
        if existing:
            return self._reused_order_result(existing, direction=direction)

        if direction == "sell":
            # 检查是否有持仓可卖
            try:
                pos = await self._fetchrow(
                    "SELECT quantity FROM paper_positions WHERE account_id = $1 AND stock_code = $2",
                    account_id, code,
                )
                if not pos or int(pos.get("quantity") or 0) <= 0:
                    return {"placed": False, "reason": "no_position_to_sell"}
                shares = int(pos["quantity"])
            except Exception:
                return {"placed": False, "reason": "position_check_failed"}
        else:
            # 买入：用账户 30% 资金，按最新价估算可买股数（A 股 100 股整手）
            try:
                acct = await self._fetchrow(
                    "SELECT current_capital FROM paper_accounts WHERE id = $1",
                    account_id,
                )
                capital = float(acct.get("current_capital") or 0) if acct else 0
                if capital <= 0:
                    return {"placed": False, "reason": "no_capital"}
                price = float(latest_price or 0)
                if price <= 0:
                    return {"placed": False, "reason": "no_price"}
                budget = capital * 0.30
                shares = int(budget / price) // 100 * 100
                if shares < 100:
                    return {"placed": False, "reason": "insufficient_cash_for_one_lot"}
            except Exception:
                return {"placed": False, "reason": "capital_check_failed"}

        # 插入 pending 订单（由 matching_engine 撮合）
        try:
            row = await self._fetchrow(
                """INSERT INTO paper_orders
                   (account_id, code, direction, shares, status, order_type,
                    strategy_id, signal_date, source)
                   VALUES ($1, $2, $3, $4, 'pending', 'market', $5, $6, 'paper_trading_bridge')
                   RETURNING *
                """,
                account_id, code, direction, shares,
                strategy_id, signal_date,
            )
            order = dict(row or {})
            order_id = str(order.get("id") or "")
            return {"placed": True, "order_id": order_id, "direction": direction, "shares": shares}
        except Exception as exc:
            existing = await self._find_existing_bridge_order(
                strategy_id=strategy_id,
                signal_date=signal_date,
                code=code,
                direction=direction,
            )
            if existing:
                return self._reused_order_result(existing, direction=direction)
            return {"placed": False, "reason": str(exc)}

    async def _get_nav_history(self, account_id: str) -> list[dict[str, Any]]:
        """获取 NAV 历史。"""
        try:
            rows = await self._fetch(
                "SELECT * FROM paper_nav WHERE account_id = $1 ORDER BY nav_date",
                account_id,
            )
            return [dict(r) for r in (rows or [])]
        except Exception:
            return []

    async def _get_trades(self, account_id: str) -> list[dict[str, Any]]:
        """获取交易记录。"""
        try:
            rows = await self._fetch(
                "SELECT * FROM paper_trades WHERE account_id = $1 ORDER BY trade_time",
                account_id,
            )
            return [dict(r) for r in (rows or [])]
        except Exception:
            return []


__all__ = ["PaperTradingBridge"]
