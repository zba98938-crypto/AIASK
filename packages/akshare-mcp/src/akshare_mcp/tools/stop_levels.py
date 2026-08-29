"""ATR 止损/止盈/仓位计算引擎。

给定入场价，返回:
  - ATR 动态止损位
  - 结构止损位 (最近支撑/阻力)
  - 风险收益比分级止盈位
  - 基于风险预算的建议仓位
"""

from __future__ import annotations

import math
from typing import Any

from ..services.technical_analysis import TechnicalAnalysis as _TA
from ..storage import get_db
from ..utils import fail, ok, resolve_existing_security_code_async
from .key_levels import compute_key_levels


async def compute_stop_levels(
    code: str,
    entry_price: float,
    *,
    direction: str = "long",
    atr_multiplier: float = 2.0,
    capital: float = 0,
    risk_per_trade: float = 0.02,
    klines: list[dict] | None = None,
    levels: list[dict] | None = None,
) -> dict[str, Any]:
    """计算止损/止盈/仓位。

    Args:
        klines: 外部传入的 K 线数据，传入时跳过内部获取。
        levels: 外部传入的关键价位列表，传入时跳过 compute_key_levels 调用。
    """
    # FIX-4: 入口参数校验（K线获取之前），失败显性化而非裸抛/产出负值
    try:
        entry_price = float(entry_price)
    except (TypeError, ValueError):
        return fail("entry_price 必须为数字")
    if not math.isfinite(entry_price) or entry_price <= 0:
        return fail("entry_price 必须为正数（股票入场价不能为 0 或负）")

    direction = str(direction or "long").strip().lower()
    if direction not in {"long", "short"}:
        return fail("direction 必须为 long 或 short")

    try:
        atr_multiplier = float(atr_multiplier)
    except (TypeError, ValueError):
        return fail("atr_multiplier 必须为数字")
    if not math.isfinite(atr_multiplier) or atr_multiplier <= 0:
        return fail("atr_multiplier 必须为正数")

    try:
        capital = float(capital)
    except (TypeError, ValueError):
        return fail("capital 必须为数字")
    if not math.isfinite(capital) or capital < 0:
        return fail("capital 不能为负数")

    try:
        risk_per_trade = float(risk_per_trade)
    except (TypeError, ValueError):
        return fail("risk_per_trade 必须为数字")
    if not math.isfinite(risk_per_trade) or not (0 < risk_per_trade <= 1):
        return fail("risk_per_trade 必须在 (0, 1] 区间（单笔风险占比，如 0.02 表示 2%）")

    if klines is None:
        db = get_db()
        klines = await db.get_klines(code, limit=120)

        if not klines:
            from .market.kline import get_kline
            api_result = await get_kline(code, "daily", 120)
            if api_result.get("success") and api_result.get("data"):
                klines = api_result["data"]

    if not klines or len(klines) < 14:
        return fail("K 线数据不足，无法计算 ATR")

    klines.sort(key=lambda k: k.get("date", ""))
    highs = [float(k.get("high", 0) or 0) for k in klines]
    lows = [float(k.get("low", 0) or 0) for k in klines]
    closes = [float(k.get("close", 0) or 0) for k in klines]

    # ATR 计算
    atr_series = _TA.calculate_atr(highs, lows, closes, period=14)
    atr_14 = atr_series[-1] if atr_series and atr_series[-1] > 0 else 0
    atr_pct = round(atr_14 / entry_price * 100, 2) if entry_price > 0 else 0

    # ATR 止损
    if direction == "long":
        atr_stop = round(entry_price - atr_14 * atr_multiplier, 2)
    else:
        atr_stop = round(entry_price + atr_14 * atr_multiplier, 2)

    # 结构止损: 从关键价位取最近支撑/阻力
    structure_stop = None
    if levels is None:
        try:
            kl_result = await compute_key_levels(code, lookback_days=120, klines=klines)
            if kl_result.get("success") and kl_result.get("data"):
                levels = kl_result["data"].get("levels", [])
        except Exception:
            levels = []

    try:
        if levels:
            if direction == "long":
                supports_below = [
                    lv for lv in levels
                    if lv["type"] == "support" and lv["price"] < entry_price
                ]
                if supports_below:
                    nearest = max(supports_below, key=lambda x: x["price"])
                    structure_stop = round(nearest["price"] * 0.99, 2)
            else:
                resistances_above = [
                    lv for lv in levels
                    if lv["type"] == "resistance" and lv["price"] > entry_price
                ]
                if resistances_above:
                    nearest = min(resistances_above, key=lambda x: x["price"])
                    structure_stop = round(nearest["price"] * 1.01, 2)
    except Exception:
        pass

    # 推荐止损: 取 ATR 止损和结构止损中更保守的
    if direction == "long":
        recommended_stop = max(atr_stop, structure_stop) if structure_stop else atr_stop
    else:
        recommended_stop = min(atr_stop, structure_stop) if structure_stop else atr_stop

    risk_per_share = abs(entry_price - recommended_stop)

    # 止盈: 按风险收益比
    if direction == "long":
        tp_1x = round(entry_price + risk_per_share, 2)
        tp_2x = round(entry_price + risk_per_share * 2, 2)
        tp_3x = round(entry_price + risk_per_share * 3, 2)
    else:
        tp_1x = round(entry_price - risk_per_share, 2)
        tp_2x = round(entry_price - risk_per_share * 2, 2)
        tp_3x = round(entry_price - risk_per_share * 3, 2)

    # 仓位计算
    position = {}
    if capital > 0 and risk_per_share > 0:
        risk_budget = capital * risk_per_trade
        max_shares_by_risk = math.floor(risk_budget / risk_per_share)
        max_shares_by_cap = math.floor(capital * 0.3 / entry_price)
        max_shares = min(max_shares_by_risk, max_shares_by_cap)
        # A 股整手 (100 股)
        max_shares = (max_shares // 100) * 100
        max_amount = round(max_shares * entry_price, 2)
        position = {
            "capital": capital,
            "risk_budget": round(risk_budget, 2),
            "max_shares": max_shares,
            "max_amount": max_amount,
            "position_pct": round(max_amount / capital * 100, 2) if capital > 0 else 0,
        }

    stop_method = "结构止损 (最近支撑位下方 1%)" if structure_stop and recommended_stop == structure_stop else f"ATR(14)×{atr_multiplier}"

    return ok({
        "code": code,
        "entry_price": entry_price,
        "direction": direction,
        "atr_14": round(atr_14, 2),
        "atr_pct": atr_pct,
        "stop_loss": {
            "atr_stop": atr_stop,
            "structure_stop": structure_stop,
            "recommended": recommended_stop,
            "method": stop_method,
            "risk_per_share": round(risk_per_share, 2),
        },
        "take_profit": {
            "tp_1x": tp_1x,
            "tp_2x": tp_2x,
            "tp_3x": tp_3x,
            "labels": ["1:1 风险收益比", "1:2 风险收益比", "1:3 风险收益比"],
        },
        "position_sizing": position or None,
        "trailing_stop": {
            "method": f"最高价回撤 ATR(14)×{atr_multiplier * 0.75:.1f}",
            "initial_trigger": round(entry_price + risk_per_share, 2) if direction == "long" else round(entry_price - risk_per_share, 2),
        },
    })


def register(mcp):
    """注册止损止盈计算工具。"""

    @mcp.tool()
    async def calculate_stop_levels(
        code: str,
        entry_price: float,
        direction: str = "long",
        atr_multiplier: float = 2.0,
        capital: float = 0,
        risk_per_trade: float = 0.02,
    ):
        """计算 ATR 止损/止盈/仓位

        给定入场价，计算:
        - ATR 动态止损 + 结构止损 (关键价位)
        - 1:1 / 1:2 / 1:3 风险收益比止盈位
        - 基于风险预算的建议仓位 (A 股整手)
        - 追踪止盈触发位

        Args:
            code: 股票代码
            entry_price: 入场价格
            direction: long (做多) 或 short (做空)
            atr_multiplier: ATR 倍数 (默认 2.0)
            capital: 总资金 (元)，填 0 则不计算仓位
            risk_per_trade: 单笔风险占比 (默认 0.02 即 2%)
        """
        direction = str(direction or "long").strip().lower()
        if direction not in {"long", "short"}:
            return fail("direction 必须为 long 或 short")
        normalized_code, _, error = await resolve_existing_security_code_async(code=code)
        if error:
            return fail(error)
        return await compute_stop_levels(
            normalized_code,
            entry_price,
            direction=direction,
            atr_multiplier=atr_multiplier,
            capital=capital,
            risk_per_trade=risk_per_trade,
        )
