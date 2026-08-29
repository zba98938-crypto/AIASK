"""Trade Risk Guard — 交易风控规则引擎。

在交易操作执行前进行多层风控检查：
  1. 单笔金额限制
  2. 日累计金额限制
  3. 日交易次数限制
  4. 持仓集中度限制
  5. 交易时段限制
  6. ST 股票限制
  7. 涨跌停追涨限制

所有交易操作必须通过 ActionIntentStore 确认后才能执行。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Any

from .numeric import bounded_float, bounded_int


# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# ---------------------------------------------------------------------------

@dataclass
class TradeRiskConfig:
    """交易风控配置。"""

    max_single_order_amount: float = field(default_factory=lambda: bounded_float(os.getenv("TRADE_MAX_SINGLE_AMOUNT"), default=100000.0, minimum=0.0))
    max_daily_trade_count: int = field(default_factory=lambda: bounded_int(os.getenv("TRADE_MAX_DAILY_COUNT"), default=50, minimum=1))
    max_daily_trade_amount: float = field(default_factory=lambda: bounded_float(os.getenv("TRADE_MAX_DAILY_AMOUNT"), default=500000.0, minimum=0.0))
    max_position_ratio: float = field(default_factory=lambda: bounded_float(os.getenv("TRADE_MAX_POSITION_RATIO"), default=0.3, minimum=0.0, maximum=1.0))
    forbidden_hours_start: int = field(default_factory=lambda: bounded_int(os.getenv("TRADE_FORBIDDEN_HOURS_START"), default=15, minimum=0, maximum=23))
    forbidden_hours_end: int = field(default_factory=lambda: bounded_int(os.getenv("TRADE_FORBIDDEN_HOURS_END"), default=9, minimum=0, maximum=23))
    allow_st_stocks: bool = os.getenv("TRADE_ALLOW_ST", "").strip().lower() in {"1", "true"}
    allow_limit_chase: bool = os.getenv("TRADE_ALLOW_LIMIT_CHASE", "").strip().lower() in {"1", "true"}
    require_confirmation: bool = True
    confirmation_timeout_seconds: int = field(default_factory=lambda: bounded_int(os.getenv("TRADE_CONFIRM_TIMEOUT"), default=300, minimum=1))


# ---------------------------------------------------------------------------
# Risk check result
# ---------------------------------------------------------------------------

@dataclass
class RiskCheckResult:
    """风控检查结果。"""

    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    blocked_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    risk_score: float = 0.0  # 0.0 (safe) to 1.0 (max risk)

    def add_check(self, name: str, passed: bool, detail: str = ""):
        self.checks.append({"name": name, "passed": passed, "detail": detail})
        if not passed:
            self.passed = False
            if not self.blocked_reason:
                self.blocked_reason = f"风控拦截: {name} - {detail}"

    def add_warning(self, warning: str):
        self.warnings.append(warning)


# ---------------------------------------------------------------------------
# Daily trade tracker
# ---------------------------------------------------------------------------

class DailyTradeTracker:
    """跟踪当日交易统计。"""

    def __init__(self):
        self._date: str = ""
        self._count: int = 0
        self._amount: float = 0.0
        self._orders: list[dict[str, Any]] = []

    def _ensure_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._date != today:
            self._date = today
            self._count = 0
            self._amount = 0.0
            self._orders.clear()

    @property
    def daily_count(self) -> int:
        self._ensure_today()
        return self._count

    @property
    def daily_amount(self) -> float:
        self._ensure_today()
        return self._amount

    def record_order(self, symbol: str, amount: float, direction: str):
        self._ensure_today()
        self._count += 1
        self._amount += abs(amount)
        self._orders.append({
            "symbol": symbol,
            "amount": amount,
            "direction": direction,
            "time": datetime.now().isoformat(),
        })

    def summary(self) -> dict[str, Any]:
        self._ensure_today()
        return {
            "date": self._date,
            "count": self._count,
            "total_amount": self._amount,
            "orders": len(self._orders),
        }


# ---------------------------------------------------------------------------
# Trade Risk Guard
# ---------------------------------------------------------------------------

class TradeRiskGuard:
    """交易风控守卫 — 在交易执行前进行多层检查。"""

    def __init__(self, config: TradeRiskConfig | None = None):
        self.config = config or TradeRiskConfig()
        self._tracker = DailyTradeTracker()

    def check_order(
        self,
        *,
        symbol: str,
        direction: str,
        price: float,
        quantity: int,
        account_balance: float | None = None,
        current_positions: list[dict[str, Any]] | None = None,
        stock_name: str = "",
    ) -> RiskCheckResult:
        """对一笔委托进行全面风控检查。"""
        result = RiskCheckResult(passed=True)
        order_amount = price * quantity

        # 1. 单笔金额检查
        if order_amount > self.config.max_single_order_amount:
            result.add_check(
                "单笔金额限制",
                False,
                f"委托金额 {order_amount:.0f} 元超过上限 {self.config.max_single_order_amount:.0f} 元",
            )
        else:
            result.add_check("单笔金额限制", True, f"{order_amount:.0f} / {self.config.max_single_order_amount:.0f}")

        # 2. 日累计金额检查
        projected_daily = self._tracker.daily_amount + order_amount
        if projected_daily > self.config.max_daily_trade_amount:
            result.add_check(
                "日累计金额限制",
                False,
                f"日累计将达 {projected_daily:.0f} 元，超过上限 {self.config.max_daily_trade_amount:.0f} 元",
            )
        else:
            result.add_check("日累计金额限制", True, f"{projected_daily:.0f} / {self.config.max_daily_trade_amount:.0f}")

        # 3. 日交易次数检查
        if self._tracker.daily_count >= self.config.max_daily_trade_count:
            result.add_check(
                "日交易次数限制",
                False,
                f"已交易 {self._tracker.daily_count} 次，达到上限 {self.config.max_daily_trade_count}",
            )
        else:
            result.add_check("日交易次数限制", True, f"{self._tracker.daily_count + 1} / {self.config.max_daily_trade_count}")

        # 4. 交易时段检查
        now = datetime.now()
        hour = now.hour
        if hour >= self.config.forbidden_hours_start or hour < self.config.forbidden_hours_end:
            result.add_check(
                "交易时段限制",
                False,
                f"当前时间 {now.strftime('%H:%M')} 不在允许交易时段内",
            )
        else:
            result.add_check("交易时段限制", True, f"当前时间 {now.strftime('%H:%M')}")

        # 5. ST 股票检查
        if not self.config.allow_st_stocks and "ST" in stock_name.upper():
            result.add_check("ST 股票限制", False, f"禁止交易 ST 股票: {stock_name}")
        else:
            result.add_check("ST 股票限制", True)

        # 6. 持仓集中度检查（如果有持仓数据）
        if account_balance and current_positions and direction == "buy":
            total_value = account_balance
            for pos in current_positions:
                total_value += pos.get("market_value", 0)

            # 计算买入后该股票的持仓占比
            existing_value = 0.0
            for pos in current_positions:
                if pos.get("symbol") == symbol:
                    existing_value = pos.get("market_value", 0)
                    break
            projected_ratio = (existing_value + order_amount) / total_value if total_value > 0 else 0

            if projected_ratio > self.config.max_position_ratio:
                result.add_check(
                    "持仓集中度限制",
                    False,
                    f"买入后 {symbol} 占比将达 {projected_ratio:.1%}，超过上限 {self.config.max_position_ratio:.0%}",
                )
            else:
                result.add_check("持仓集中度限制", True, f"占比 {projected_ratio:.1%}")
        else:
            result.add_check("持仓集中度限制", True, "无持仓数据，跳过")

        # 计算综合风险分数
        failed_count = sum(1 for c in result.checks if not c["passed"])
        result.risk_score = min(1.0, failed_count / max(len(result.checks), 1))

        # 添加警告
        if order_amount > self.config.max_single_order_amount * 0.8:
            result.add_warning(f"委托金额接近单笔上限 ({order_amount:.0f}/{self.config.max_single_order_amount:.0f})")
        if self._tracker.daily_count > self.config.max_daily_trade_count * 0.8:
            result.add_warning("日交易次数接近上限")

        return result

    def record_executed_order(self, symbol: str, amount: float, direction: str):
        """记录已执行的委托（用于累计统计）。"""
        self._tracker.record_order(symbol, amount, direction)

    def daily_summary(self) -> dict[str, Any]:
        """获取当日交易统计摘要。"""
        return {
            **self._tracker.summary(),
            "config": {
                "max_single_amount": self.config.max_single_order_amount,
                "max_daily_amount": self.config.max_daily_trade_amount,
                "max_daily_count": self.config.max_daily_trade_count,
                "max_position_ratio": self.config.max_position_ratio,
            },
        }
