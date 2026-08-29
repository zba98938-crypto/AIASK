"""实时交易约束复核（Realtime Trading Constraints）

把停牌、ST、涨跌停、交易时段、可成交量等约束从静态规则
升级为可在决策时动态调用的复核接口。

使用方式
--------
```python
from akshare_mcp.services.realtime_constraints import (
    TradeConstraintChecker, TradeConstraintContext
)
ctx = TradeConstraintContext(
    code="000858",
    name="五粮液",
    current_price=150.0,
    prev_close=148.5,
    volume_5d_avg=5_000_000,
    is_suspended=False,
    is_st=False,
)
checker = TradeConstraintChecker()
result = checker.check(ctx)
```
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

# ── 常量 ──────────────────────────────────────────────────────────────────────

# A 股涨跌停幅度
LIMIT_PCT_NORMAL = 0.10       # 普通股票 ±10%
LIMIT_PCT_ST = 0.05           # ST 股票 ±5%
LIMIT_PCT_STAR = 0.20         # 科创板/创业板注册制 ±20%
LIMIT_PCT_NEW_LISTING = 0.44  # 新股首日（约 ±44%，实际规则复杂）

# 交易时段（北京时间）
MORNING_OPEN = datetime.time(9, 30)
MORNING_CLOSE = datetime.time(11, 30)
AFTERNOON_OPEN = datetime.time(13, 0)
AFTERNOON_CLOSE = datetime.time(15, 0)

# 参与率上限（防止冲击市场）
DEFAULT_MAX_PARTICIPATION_RATE = 0.30  # 最多参与当日成交量的 30%


# ── 数据类 ───────────────────────────────────────────────────────────────────

@dataclass
class TradeConstraintContext:
    """交易约束检查上下文。"""

    code: str
    name: str = ""
    current_price: float | None = None
    prev_close: float | None = None
    volume_today: float | None = None      # 当日成交量（股）
    volume_5d_avg: float | None = None     # 近 5 日日均成交量（股）
    is_suspended: bool = False             # 是否停牌
    is_st: bool = False                    # 是否 ST/ST*
    is_star_market: bool = False           # 是否科创板（LIMIT_PCT_STAR）
    is_new_listing: bool = False           # 是否新股首日
    market_state: str = "trading"          # trading / call_auction / closed
    check_time: datetime.datetime | None = None   # 检查时间，None 表示当前
    target_shares: float | None = None    # 计划成交量（股）
    max_participation_rate: float = DEFAULT_MAX_PARTICIPATION_RATE


@dataclass
class ConstraintViolation:
    """单条约束违规记录。"""

    code: str           # 约束代码，如 "SUSPENDED" / "LIMIT_UP" / "ST_LIMIT_UP"
    severity: str       # "blocking" / "warning"
    title: str
    detail: str
    suggested_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "suggested_action": self.suggested_action,
        }


@dataclass
class TradeConstraintResult:
    """交易约束检查结果。"""

    stock_code: str
    stock_name: str
    tradeable: bool                  # True = 可交易，False = 有阻断项
    has_blocking: bool
    has_warnings: bool
    violations: list[ConstraintViolation] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)
    warning_reasons: list[str] = field(default_factory=list)
    limit_price_up: float | None = None
    limit_price_down: float | None = None
    max_tradeable_shares: float | None = None
    participation_rate: float | None = None
    check_time: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "tradeable": self.tradeable,
            "has_blocking": self.has_blocking,
            "has_warnings": self.has_warnings,
            "violations": [v.to_dict() for v in self.violations],
            "blocking_reasons": self.blocking_reasons,
            "warning_reasons": self.warning_reasons,
            "limit_price_up": self.limit_price_up,
            "limit_price_down": self.limit_price_down,
            "max_tradeable_shares": self.max_tradeable_shares,
            "participation_rate": self.participation_rate,
            "check_time": self.check_time,
            "summary": self.summary,
        }


# ── 核心检查器 ────────────────────────────────────────────────────────────────

class TradeConstraintChecker:
    """交易约束检查器。

    将各类实时约束逻辑收口到单一入口，便于测试和扩展。
    """

    def check(self, ctx: TradeConstraintContext) -> TradeConstraintResult:
        violations: list[ConstraintViolation] = []
        now = ctx.check_time or datetime.datetime.now()
        check_time_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # 1. 停牌检查
        if ctx.is_suspended:
            violations.append(ConstraintViolation(
                code="SUSPENDED",
                severity="blocking",
                title="股票停牌",
                detail=f"{ctx.code} 当前处于停牌状态，无法交易",
                suggested_action="等待复牌公告后再评估",
            ))

        # 2. 涨跌停检查
        limit_up, limit_down = self._calc_limit_prices(ctx)
        if limit_up is not None and limit_down is not None and ctx.current_price is not None:
            price = float(ctx.current_price)
            if price >= limit_up * 0.999:  # 允许 0.1% 浮动（撮合精度）
                violations.append(ConstraintViolation(
                    code="LIMIT_UP",
                    severity="blocking",
                    title="涨停板",
                    detail=f"当前价 {price:.2f} 已触及涨停价 {limit_up:.2f}，买入可能无法成交",
                    suggested_action="避免追涨停，关注次日开盘机会",
                ))
            elif price <= limit_down * 1.001:
                violations.append(ConstraintViolation(
                    code="LIMIT_DOWN",
                    severity="blocking",
                    title="跌停板",
                    detail=f"当前价 {price:.2f} 已触及跌停价 {limit_down:.2f}，卖出可能无法成交",
                    suggested_action="避免跌停时卖出，关注次日竞价",
                ))

        # 3. ST 风险提示
        if ctx.is_st:
            violations.append(ConstraintViolation(
                code="ST_RISK",
                severity="warning",
                title="ST 股票",
                detail=f"{ctx.code} 为 ST 或 ST* 股票，涨跌停幅度 ±5%，退市风险较高",
                suggested_action="谨慎参与，关注退市风险公告",
            ))

        # 4. 交易时段检查
        trading_ok, session_msg = self._check_trading_session(now)
        if not trading_ok:
            violations.append(ConstraintViolation(
                code="NON_TRADING_HOURS",
                severity="warning",
                title="非交易时段",
                detail=session_msg,
                suggested_action="在交易时段（09:30-11:30, 13:00-15:00）内下单",
            ))

        # 5. 成交量/参与率检查
        participation_rate = None
        max_tradeable = None
        if ctx.target_shares is not None and ctx.volume_5d_avg is not None and ctx.volume_5d_avg > 0:
            participation_rate = float(ctx.target_shares) / float(ctx.volume_5d_avg)
            max_tradeable = float(ctx.max_participation_rate) * float(ctx.volume_5d_avg)
            if participation_rate > ctx.max_participation_rate:
                violations.append(ConstraintViolation(
                    code="HIGH_PARTICIPATION_RATE",
                    severity="warning",
                    title="参与率过高",
                    detail=(
                        f"计划成交 {ctx.target_shares:,.0f} 股，"
                        f"参与率 {participation_rate:.1%} 超过上限 {ctx.max_participation_rate:.1%}，"
                        f"可能对价格产生冲击"
                    ),
                    suggested_action=f"建议分批交易，单日参与率控制在 {ctx.max_participation_rate:.0%} 以内",
                ))
        elif ctx.volume_5d_avg is not None and ctx.volume_5d_avg < 100_000:
            violations.append(ConstraintViolation(
                code="LOW_LIQUIDITY",
                severity="warning",
                title="流动性不足",
                detail=f"近 5 日日均成交量仅 {ctx.volume_5d_avg:,.0f} 股，流动性偏低",
                suggested_action="建议减少仓位或分散时段操作",
            ))

        # 汇总
        blocking = [v for v in violations if v.severity == "blocking"]
        warnings = [v for v in violations if v.severity == "warning"]
        tradeable = len(blocking) == 0

        summary_parts = []
        if not tradeable:
            summary_parts.append(f"❌ 不可交易：{'; '.join(v.title for v in blocking)}")
        if warnings:
            summary_parts.append(f"⚠ 警告：{'; '.join(v.title for v in warnings)}")
        if tradeable and not warnings:
            summary_parts.append("✓ 交易约束检查通过，当前可交易")

        return TradeConstraintResult(
            stock_code=ctx.code,
            stock_name=ctx.name or ctx.code,
            tradeable=tradeable,
            has_blocking=len(blocking) > 0,
            has_warnings=len(warnings) > 0,
            violations=violations,
            blocking_reasons=[v.code for v in blocking],
            warning_reasons=[v.code for v in warnings],
            limit_price_up=limit_up,
            limit_price_down=limit_down,
            max_tradeable_shares=max_tradeable,
            participation_rate=round(participation_rate, 4) if participation_rate is not None else None,
            check_time=check_time_str,
            summary="；".join(summary_parts),
        )

    def _calc_limit_prices(
        self, ctx: TradeConstraintContext
    ) -> tuple[float | None, float | None]:
        """计算涨跌停价格。"""
        if ctx.prev_close is None:
            return None, None

        prev = float(ctx.prev_close)
        if ctx.is_new_listing:
            pct = LIMIT_PCT_NEW_LISTING
        elif ctx.is_star_market:
            pct = LIMIT_PCT_STAR
        elif ctx.is_st:
            pct = LIMIT_PCT_ST
        else:
            pct = LIMIT_PCT_NORMAL

        # A 股价格精度通常为 0.01 元
        up = round(prev * (1 + pct), 2)
        down = round(prev * (1 - pct), 2)
        return up, down

    def _check_trading_session(
        self, now: datetime.datetime
    ) -> tuple[bool, str]:
        """检查是否在交易时段内。"""
        t = now.time()
        in_morning = MORNING_OPEN <= t <= MORNING_CLOSE
        in_afternoon = AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE

        # 周末不交易（粗略判断，实际还需节假日历）
        if now.weekday() >= 5:
            return False, f"当前为周末（{now.strftime('%A')}），A 股不交易"

        if in_morning or in_afternoon:
            return True, "当前在交易时段内"

        if t < MORNING_OPEN:
            return False, f"盘前时段（{t}），集合竞价 09:15 开始，连续竞价 09:30 开始"
        if MORNING_CLOSE < t < AFTERNOON_OPEN:
            return False, f"午休时段（{t}），下午 13:00 开市"
        return False, f"收盘后（{t}），A 股收盘时间为 15:00"


# ── 便捷入口 ──────────────────────────────────────────────────────────────────

_DEFAULT_CHECKER = TradeConstraintChecker()


def check_trade_constraints(
    code: str,
    name: str = "",
    current_price: float | None = None,
    prev_close: float | None = None,
    volume_5d_avg: float | None = None,
    is_suspended: bool = False,
    is_st: bool = False,
    is_star_market: bool = False,
    target_shares: float | None = None,
    check_time: datetime.datetime | None = None,
) -> TradeConstraintResult:
    """检查交易约束的便捷入口函数。"""
    ctx = TradeConstraintContext(
        code=code,
        name=name,
        current_price=current_price,
        prev_close=prev_close,
        volume_5d_avg=volume_5d_avg,
        is_suspended=is_suspended,
        is_st=is_st,
        is_star_market=is_star_market,
        target_shares=target_shares,
        check_time=check_time,
    )
    return _DEFAULT_CHECKER.check(ctx)
