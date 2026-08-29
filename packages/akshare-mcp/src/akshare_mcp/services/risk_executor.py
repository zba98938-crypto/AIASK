"""风控自动处置执行器 — 在 RiskMonitor 检测之上自动执行风控动作。

触发时机：撮合引擎每次扫描后调用 enforce()。
冷却期：同一账户 60 秒内不重复触发。
所有处置动作写入 order_events 表，标记 source='risk_executor'。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# 冷却期（秒）
COOLDOWN_SECONDS = 60


class RiskAction:
    """风控处置动作记录"""

    def __init__(self, action_type: str, code: str, shares: int, price: float, reason: str):
        self.action_type = action_type  # reduce_position | force_liquidate
        self.code = code
        self.shares = shares
        self.price = price
        self.reason = reason
        self.timestamp = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": self.action_type,
            "code": self.code,
            "shares": self.shares,
            "price": self.price,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
        }


class RiskExecutor:
    """风控自动处置执行器"""

    def __init__(
        self,
        max_position_pct: float = 30.0,
        max_drawdown_pct: float = 20.0,
    ):
        self.max_position_pct = max_position_pct
        self.max_drawdown_pct = max_drawdown_pct
        self._cooldowns: Dict[str, datetime] = {}  # account_id -> last_trigger_time
        self.total_actions = 0

    def _in_cooldown(self, account_id: str) -> bool:
        last = self._cooldowns.get(account_id)
        if last is None:
            return False
        return (datetime.now() - last).total_seconds() < COOLDOWN_SECONDS

    def _set_cooldown(self, account_id: str):
        self._cooldowns[account_id] = datetime.now()

    async def enforce(self, account_id: str) -> List[RiskAction]:
        """检查风控规则并自动执行处置。返回本次触发的处置动作列表。"""
        if self._in_cooldown(account_id):
            return []

        from ..storage import get_db
        db = get_db()
        actions: List[RiskAction] = []

        async with db.acquire() as conn:
            account = await conn.fetchrow(
                "SELECT * FROM paper_accounts WHERE id=$1", account_id
            )
            if not account:
                return []

            positions = await conn.fetch(
                "SELECT * FROM paper_positions WHERE account_id=$1", account_id
            )
            if not positions:
                return []

            # 读取账户风控规则（可能被用户自定义覆盖）
            rules = account.get("risk_rules") or {}
            if isinstance(rules, str):
                try:
                    rules = json.loads(rules)
                except Exception:
                    rules = {}
            if not isinstance(rules, dict):
                rules = {}

            max_pos_pct = float(rules.get("max_position_pct", self.max_position_pct))
            max_dd_pct = float(rules.get("max_drawdown_pct", self.max_drawdown_pct))

            total_value = float(account.get("total_value") or 0)
            initial_capital = float(account.get("initial_capital") or 0)

            if total_value <= 0:
                return []

            # ── 检查 1: 回撤超限 → 全部平仓 ──────────────────
            if initial_capital > 0:
                drawdown_pct = (initial_capital - total_value) / initial_capital * 100
                if drawdown_pct >= max_dd_pct:
                    for pos in positions:
                        qty = int(pos.get("quantity") or 0)
                        if qty <= 0:
                            continue
                        code = pos["stock_code"]
                        price = float(pos.get("current_price") or pos.get("cost_price") or 0)
                        if price <= 0:
                            continue
                        action = await self._execute_sell(
                            db, conn, account_id, code, qty, price,
                            "force_liquidate",
                            f"回撤 {drawdown_pct:.1f}% 超限 {max_dd_pct}%，强制平仓",
                        )
                        if action:
                            actions.append(action)
                    if actions:
                        self._set_cooldown(account_id)
                        self.total_actions += len(actions)
                        if account.get('strategy_id') and hasattr(db, 'save_strategy_runtime_risk_event'):
                            await db.save_strategy_runtime_risk_event({
                                'strategy_id': account.get('strategy_id'),
                                'account_id': account_id,
                                'severity': 'critical',
                                'event_type': 'risk_executor_force_liquidate',
                                'action': 'force_liquidate',
                                'status': 'open',
                                'title': '风险执行器触发强平',
                                'reason': f'触发 {len(actions)} 笔自动平仓',
                                'payload': {'actions': [item.to_dict() for item in actions]},
                            })
                    return actions

            # ── 检查 2: 单持仓超限 → 减仓至上限 ──────────────
            for pos in positions:
                qty = int(pos.get("quantity") or 0)
                price = float(pos.get("current_price") or pos.get("cost_price") or 0)
                if qty <= 0 or price <= 0:
                    continue
                code = pos["stock_code"]
                pos_value = qty * price
                pos_pct = pos_value / total_value * 100

                if pos_pct > max_pos_pct:
                    target_value = total_value * max_pos_pct / 100
                    target_qty = int(target_value / price)
                    reduce_qty = qty - target_qty
                    if reduce_qty >= 100:  # A股最小交易单位
                        reduce_qty = (reduce_qty // 100) * 100
                        action = await self._execute_sell(
                            db, conn, account_id, code, reduce_qty, price,
                            "reduce_position",
                            f"持仓占比 {pos_pct:.1f}% 超限 {max_pos_pct}%，减仓 {reduce_qty} 股",
                        )
                        if action:
                            actions.append(action)

        if actions:
            self._set_cooldown(account_id)
            self.total_actions += len(actions)
            if account.get('strategy_id') and hasattr(db, 'save_strategy_runtime_risk_event'):
                await db.save_strategy_runtime_risk_event({
                    'strategy_id': account.get('strategy_id'),
                    'account_id': account_id,
                    'severity': 'high',
                    'event_type': 'risk_executor_reduce_position',
                    'action': 'reduce_position',
                    'status': 'open',
                    'title': '风险执行器触发减仓',
                    'reason': f'触发 {len(actions)} 笔自动减仓',
                    'payload': {'actions': [item.to_dict() for item in actions]},
                })

        return actions

    async def _execute_sell(
        self, db, conn, account_id: str, code: str, qty: int, price: float,
        action_type: str, reason: str,
    ) -> Optional[RiskAction]:
        """执行卖出并记录审计事件"""
        try:
            from ..tools.managers.paper_trading_manager import _fill_order, _record_order_event
            trade_id, commission = await _fill_order(conn, account_id, code, "sell", qty, price)
            await _record_order_event(
                conn,
                f"risk_{action_type}_{code}",
                action_type,
                account_id=account_id,
                code=code,
                payload={
                    "source": "risk_executor",
                    "trade_id": str(trade_id),
                    "shares": qty,
                    "price": price,
                    "commission": round(commission, 4),
                    "reason": reason,
                },
            )
            logger.info("[RiskExecutor] %s: %s %s x%d @%.2f — %s",
                        action_type, account_id, code, qty, price, reason)
            return RiskAction(action_type, code, qty, price, reason)
        except Exception as e:
            logger.warning("[RiskExecutor] %s failed for %s %s: %s",
                           action_type, account_id, code, e)
            return None

    def status(self) -> Dict[str, Any]:
        return {
            "total_actions": self.total_actions,
            "active_cooldowns": len(self._cooldowns),
            "max_position_pct": self.max_position_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
        }


# Singleton
_executor: Optional[RiskExecutor] = None


def get_risk_executor() -> RiskExecutor:
    global _executor
    if _executor is None:
        _executor = RiskExecutor()
    return _executor
