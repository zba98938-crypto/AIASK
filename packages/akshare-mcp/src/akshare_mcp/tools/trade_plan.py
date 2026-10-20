"""交易计划生成器 — 信号融合 + 场景化交易方案。

设计原则:
  分析可用 10 个指标，但开仓/平仓判据最多 3 个信号:
    主信号 (1): 趋势方向 — MACD方向 + 均线排列
    触发信号 (1): 入场时机 — RSI/关键位/K线形态
    确认信号 (1): 成交量/资金流
"""
from __future__ import annotations


# --- trade_plan_parts/parsers.py ---
import logging
import math
from typing import Any

import numpy as np

from ..services.technical_analysis import TechnicalAnalysis as _TA
from ..services import pattern_recognition
from ..storage import get_db
from ..utils import fail, ok, resolve_existing_security_code_async

from .key_levels import compute_key_levels
from .stop_levels import compute_stop_levels

logger = logging.getLogger(__name__)


# ── P0: 统一阈值常量 ──────────────────────────────────────────────

_RSI_OVERSOLD = 30
_RSI_OVERBOUGHT = 70
_RSI_NEUTRAL_LOW = 40
_RSI_NEUTRAL_HIGH = 60

# ── P3-5: A 股交易成本参数 ───────────────────────────────────────
_COMMISSION_RATE = 0.00025   # 佣金 万2.5（双向）
_STAMP_TAX_RATE = 0.0005     # 印花税 0.05%（仅卖出）
_TRANSFER_FEE_RATE = 0.00001 # 过户费 0.001%（双向）
_MIN_SLIPPAGE_RATE = 0.001   # 最低滑点 0.1%

# ── 信号判定 ──────────────────────────────────────────────────────


def _judge_trend(
    closes: list[float],
    macd_data: dict,
    ma20: list[float],
    ma60: list[float],
) -> dict:
    """主信号: 趋势方向判定。

    P1 增强: 加入 DIF/DEA 金叉死叉 + 均线斜率判断，减少 neutral。
    """
    if not closes or len(closes) < 60:
        return {"direction": "neutral", "name": "数据不足", "detail": "K 线不足 60 根"}

    current = closes[-1]
    ma20_val = ma20[-1] if ma20 and ma20[-1] > 0 else 0
    ma60_val = ma60[-1] if ma60 and ma60[-1] > 0 else 0

    # MACD 柱线
    macd_hist = macd_data.get("histogram", [])
    valid_hist = [h for h in (macd_hist or []) if h is not None]
    macd_positive = valid_hist[-1] > 0 if valid_hist else False
    macd_rising = (
        len(valid_hist) >= 2 and valid_hist[-1] > valid_hist[-2]
    ) if valid_hist else False

    # P1: DIF/DEA 金叉死叉
    dif = [v for v in (macd_data.get("macd", []) or []) if v is not None]
    dea = [v for v in (macd_data.get("signal", []) or []) if v is not None]
    golden_cross = False
    death_cross = False
    if len(dif) >= 2 and len(dea) >= 2:
        golden_cross = dif[-2] < dea[-2] and dif[-1] >= dea[-1]
        death_cross = dif[-2] > dea[-2] and dif[-1] <= dea[-1]

    # P1: 均线斜率 (5日变化率)
    ma20_slope = 0.0
    if len(ma20) >= 5 and ma20[-5] and ma20[-5] > 0:
        ma20_slope = (ma20[-1] - ma20[-5]) / ma20[-5] * 100

    # 均线排列
    bullish_alignment = current > ma20_val > ma60_val > 0
    bearish_alignment = 0 < current < ma20_val and ma20_val < ma60_val

    # ── 强趋势 ──
    if bullish_alignment and macd_positive:
        return {
            "direction": "bullish",
            "name": "MACD 多头 + 均线多头排列",
            "detail": f"收盘 {current:.2f} > MA20 {ma20_val:.2f} > MA60 {ma60_val:.2f}，MACD 柱线为正",
        }
    if bearish_alignment and not macd_positive:
        return {
            "direction": "bearish",
            "name": "MACD 空头 + 均线空头排列",
            "detail": f"收盘 {current:.2f} < MA20 {ma20_val:.2f} < MA60 {ma60_val:.2f}，MACD 柱线为负",
        }

    # ── P1: DIF/DEA 交叉信号（可以在均线不完全排列时给出方向） ──
    if golden_cross:
        return {
            "direction": "bullish_weak",
            "name": "MACD 金叉",
            "detail": f"DIF 上穿 DEA，MACD 柱线{'为正' if macd_positive else '即将转正'}，均线斜率 {ma20_slope:+.2f}%",
        }
    if death_cross:
        return {
            "direction": "bearish_weak",
            "name": "MACD 死叉",
            "detail": f"DIF 下穿 DEA，MACD 柱线{'为负' if not macd_positive else '即将转负'}，均线斜率 {ma20_slope:+.2f}%",
        }

    # ── 弱趋势 ──
    if macd_positive and macd_rising:
        return {
            "direction": "bullish_weak",
            "name": "MACD 转多但均线未确认",
            "detail": f"MACD 柱线为正且上升，MA20 斜率 {ma20_slope:+.2f}%",
        }
    if not macd_positive and not macd_rising:
        return {
            "direction": "bearish_weak",
            "name": "MACD 偏空且动能下降",
            "detail": f"MACD 柱线为负且下降，MA20 斜率 {ma20_slope:+.2f}%",
        }

    # ── P1: 用均线斜率和价格位置进一步判断，减少 neutral ──
    if ma20_slope > 0.5 and current > ma20_val:
        return {
            "direction": "bullish_weak",
            "name": "均线上升趋势",
            "detail": f"MA20 斜率 {ma20_slope:+.2f}%，价格在均线上方",
        }
    if ma20_slope < -0.5 and current < ma20_val:
        return {
            "direction": "bearish_weak",
            "name": "均线下降趋势",
            "detail": f"MA20 斜率 {ma20_slope:+.2f}%，价格在均线下方",
        }

    return {
        "direction": "neutral",
        "name": "趋势不明确",
        "detail": f"MACD 与均线信号矛盾，MA20 斜率 {ma20_slope:+.2f}%，方向不清",
    }


def _judge_trigger(
    rsi_value: float,
    current_price: float,
    levels: list[dict],
    patterns: list[dict],
) -> dict:
    """触发信号: 入场时机判定。P0: 使用统一 RSI 阈值常量。"""
    near_support = None
    for lv in levels:
        if lv["type"] == "support" and lv.get("strength", 0) >= 2:
            dist = abs(current_price - lv["price"]) / current_price
            if dist < 0.03:
                near_support = lv
                break

    near_resistance = None
    for lv in levels:
        if lv["type"] == "resistance" and lv.get("strength", 0) >= 2:
            dist = abs(current_price - lv["price"]) / current_price
            if dist < 0.03:
                near_resistance = lv
                break

    bullish_pattern = None
    for p in (patterns or []):
        if p.get("bullish") in (True, "True", "true") and p.get("reliability") in ("high", "medium"):
            bullish_pattern = p
            break

    rsi_oversold = rsi_value < _RSI_OVERSOLD
    rsi_overbought = rsi_value > _RSI_OVERBOUGHT
    rsi_neutral_low = _RSI_OVERSOLD <= rsi_value < _RSI_NEUTRAL_LOW

    if near_support and rsi_oversold:
        return {
            "status": "triggered",
            "name": f"RSI 超卖({rsi_value:.0f}) + 接近支撑位 {near_support['price']}",
            "action": "可入场",
        }
    if near_support and bullish_pattern:
        return {
            "status": "triggered",
            "name": f"支撑位 {near_support['price']} + {bullish_pattern.get('name', '看涨形态')}",
            "action": "可入场",
        }
    if rsi_oversold and bullish_pattern:
        return {
            "status": "triggered",
            "name": f"RSI 超卖({rsi_value:.0f}) + {bullish_pattern.get('name', '看涨形态')}",
            "action": "可入场",
        }

    if rsi_overbought and near_resistance:
        return {
            "status": "exit_signal",
            "name": f"RSI 超买({rsi_value:.0f}) + 接近阻力位 {near_resistance['price']}",
            "action": "不宜追涨，等回调",
        }
    if rsi_overbought:
        return {
            "status": "wait",
            "name": f"RSI 超买({rsi_value:.0f})，短期过热",
            "action": f"等 RSI 回落至 {_RSI_NEUTRAL_HIGH} 以下再考虑",
        }

    if near_support:
        return {
            "status": "near_ready",
            "name": f"接近支撑位 {near_support['price']}，等确认信号",
            "action": "关注 K 线形态或放量企稳",
        }

    if rsi_neutral_low:
        return {
            "status": "near_ready",
            "name": f"RSI 中性偏低({rsi_value:.0f})，可关注",
            "action": "等价格触及支撑位再入场",
        }

    return {
        "status": "wait",
        "name": f"RSI {rsi_value:.0f}，无明确触发条件",
        "action": "观望",
    }


def _judge_confirmation(fund_flow: dict | None, avg_volume_20: float, latest_volume: float) -> dict:
    """确认信号: 资金流/成交量。"""
    main_inflow = 0
    if fund_flow:
        main_inflow = float(fund_flow.get("mainNetInflow", 0) or 0)

    volume_ratio = latest_volume / avg_volume_20 if avg_volume_20 > 0 else 1.0

    if main_inflow > 0 and volume_ratio > 1.3:
        return {
            "status": "confirmed",
            "name": f"主力净流入 {main_inflow / 1e4:+.0f} 万 + 放量 (量比 {volume_ratio:.1f})",
        }
    if main_inflow > 0:
        return {
            "status": "confirmed",
            "name": f"主力净流入 {main_inflow / 1e4:+.0f} 万",
        }
    if volume_ratio > 1.5:
        return {
            "status": "partial",
            "name": f"放量 (量比 {volume_ratio:.1f}) 但主力方向不明",
        }
    if main_inflow < 0:
        return {
            "status": "unconfirmed",
            "name": f"主力净流出 {main_inflow / 1e4:+.0f} 万，信号减弱",
        }
    return {
        "status": "neutral",
        "name": "资金面无明确方向",
    }


def _compute_confidence(
    trend: dict,
    trigger: dict,
    confirm: dict,
    *,
    data_staleness_days: int = 0,
    calibration_factor: float = 1.0,
    kline_count: int = 250,
    historical_hit_rate: float | None = None,
    cross_validation: dict | None = None,
    num_scenarios: int = 3,
    num_levels: int = 0,
    num_conflicts: int = 0,
    closes: list[float] | None = None,
    macd_hist: list | None = None,
    rsi_value: float = 50.0,
    fund_flow_direction: str = "neutral",
) -> tuple[float, dict]:
    """策略可信度 0.0-1.0: 衡量本次策略分析的可靠程度，与市场方向无关。

    四维度评分:
        data_quality   (25%): 数据新鲜度、价格校准、数据完整性
        signal_clarity (30%): 信号一致性、信号强度、冲突数量
        validation     (25%): 交叉验证、历史命中率
        completeness   (20%): 场景覆盖、风控完整、关键价位
    """
    # ═══════════════════════════════════════════════
    # 维度1: 数据质量 (25%)
    # ═══════════════════════════════════════════════
    if data_staleness_days <= 3:
        freshness = 1.0
    elif data_staleness_days <= 7:
        freshness = 0.90
    elif data_staleness_days <= 14:
        freshness = 0.70
    elif data_staleness_days <= 30:
        freshness = 0.50
    else:
        freshness = 0.30

    cal_dev = abs(calibration_factor - 1.0)
    if cal_dev < 0.01:
        calibration = 1.0
    elif cal_dev < 0.02:
        calibration = 0.90
    elif cal_dev < 0.05:
        calibration = 0.70
    else:
        calibration = 0.40

    if kline_count >= 200:
        data_completeness = 1.0
    elif kline_count >= 120:
        data_completeness = 0.85
    elif kline_count >= 60:
        data_completeness = 0.65
    else:
        data_completeness = 0.40

    dq_score = (freshness + calibration + data_completeness) / 3.0

    # ═══════════════════════════════════════════════
    # 维度2: 信号清晰度 (30%) — 不看方向，只看是否清晰一致
    # ═══════════════════════════════════════════════

    # 收集各子信号的方向: +1=看多, -1=看空, 0=中性
    signal_dirs: list[int] = []

    # MACD 方向
    valid_hist = [h for h in (macd_hist or []) if h is not None]
    if valid_hist:
        signal_dirs.append(1 if valid_hist[-1] > 0 else -1)

    # 均线方向 (通过 trend 结果推断)
    td = trend["direction"]
    if td in ("bullish", "bullish_weak"):
        signal_dirs.append(1)
    elif td in ("bearish", "bearish_weak"):
        signal_dirs.append(-1)
    else:
        signal_dirs.append(0)

    # RSI 方向
    if rsi_value > 55:
        signal_dirs.append(1)
    elif rsi_value < 45:
        signal_dirs.append(-1)
    else:
        signal_dirs.append(0)

    # 资金流方向
    if fund_flow_direction == "bullish":
        signal_dirs.append(1)
    elif fund_flow_direction == "bearish":
        signal_dirs.append(-1)
    else:
        signal_dirs.append(0)

    # 一致性: 计算非零信号中多数方向占比
    non_zero = [d for d in signal_dirs if d != 0]
    if non_zero:
        majority = max(
            sum(1 for d in non_zero if d > 0),
            sum(1 for d in non_zero if d < 0),
        )
        consistency = majority / len(non_zero)
    else:
        consistency = 0.5

    # 信号强度: 趋势越清晰(无论多空)，强度越高
    strength_map = {
        "bullish": 1.0, "bearish": 1.0,
        "bullish_weak": 0.75, "bearish_weak": 0.75,
        "neutral": 0.40,
    }
    trend_strength = strength_map.get(td, 0.40)

    trigger_strength_map = {
        "triggered": 1.0, "near_ready": 0.75,
        "wait": 0.50, "exit_signal": 0.80,
    }
    ts = trigger["status"]
    trig_strength = trigger_strength_map.get(ts, 0.50)

    signal_strength = (trend_strength + trig_strength) / 2.0

    # 冲突惩罚
    if num_conflicts == 0:
        conflict_free = 1.0
    elif num_conflicts == 1:
        conflict_free = 0.80
    elif num_conflicts == 2:
        conflict_free = 0.60
    else:
        conflict_free = 0.40

    sc_score = (consistency + signal_strength + conflict_free) / 3.0

    # ═══════════════════════════════════════════════
    # 维度3: 验证得分 (25%)
    # ═══════════════════════════════════════════════

    # 交叉验证一致性
    if cross_validation:
        cv_rec = cross_validation.get("recommendation", "")
        cv_dir = "buy" if cv_rec == "buy" else "sell" if cv_rec == "sell" else "hold"
        plan_dir = (
            "buy" if td in ("bullish", "bullish_weak")
            else "sell" if td in ("bearish", "bearish_weak")
            else "hold"
        )
        if cv_dir == plan_dir:
            cross_val_score = 1.0
        elif cv_dir == "hold" or plan_dir == "hold":
            cross_val_score = 0.75
        else:
            cross_val_score = 0.40
    else:
        cross_val_score = 0.50

    # 历史命中率
    if historical_hit_rate is not None:
        if historical_hit_rate >= 0.60:
            hit_rate_score = 1.0
        elif historical_hit_rate >= 0.50:
            hit_rate_score = 0.85
        elif historical_hit_rate >= 0.40:
            hit_rate_score = 0.70
        else:
            hit_rate_score = 0.50
    else:
        hit_rate_score = 0.50

    vd_score = (cross_val_score + hit_rate_score) / 2.0

    # P3: 信号稳定性和衰减度可在外部修正 vd_score（通过返回的 breakdown 触发）

    # ═══════════════════════════════════════════════
    # 维度4: 分析完整性 (20%)
    # ═══════════════════════════════════════════════
    scenario_score = 1.0 if num_scenarios >= 3 else (0.70 if num_scenarios >= 2 else 0.40)
    level_score = 1.0 if num_levels >= 4 else (0.70 if num_levels >= 2 else 0.40)
    cp_score = (scenario_score + level_score) / 2.0

    # ═══════════════════════════════════════════════
    # 加权合成
    # ═══════════════════════════════════════════════
    W_DQ, W_SC, W_VD, W_CP = 0.25, 0.30, 0.25, 0.20
    final = dq_score * W_DQ + sc_score * W_SC + vd_score * W_VD + cp_score * W_CP
    final = round(min(1.0, max(0.0, final)), 2)

    breakdown = {
        "data_quality": {
            "score": round(dq_score, 2),
            "freshness": freshness,
            "calibration": calibration,
            "completeness": data_completeness,
            "staleness_days": data_staleness_days,
        },
        "signal_clarity": {
            "score": round(sc_score, 2),
            "consistency": round(consistency, 2),
            "strength": round(signal_strength, 2),
            "conflict_free": conflict_free,
            "signal_directions": signal_dirs,
        },
        "validation": {
            "score": round(vd_score, 2),
            "cross_validation": round(cross_val_score, 2),
            "historical_hit_rate": historical_hit_rate,
            "hit_rate_score": hit_rate_score,
        },
        "completeness": {
            "score": round(cp_score, 2),
            "scenarios": scenario_score,
            "levels": level_score,
        },
        "weights": {"data_quality": W_DQ, "signal_clarity": W_SC, "validation": W_VD, "completeness": W_CP},
        "final": final,
    }

    return final, breakdown


def _determine_direction(trend: dict, trigger: dict, confidence: float) -> str:
    """最终方向判定。"""
    td = trend["direction"]
    ts = trigger["status"]

    if ts == "exit_signal":
        return "wait_pullback"
    if td in ("bullish", "bullish_weak") and ts == "triggered":
        return "buy"
    if td in ("bullish", "bullish_weak") and ts in ("wait",):
        return "wait_pullback"
    if td in ("bearish", "bearish_weak"):
        return "avoid"
    if ts == "near_ready":
        return "watch"
    return "wait"


def _determine_regime(closes: list[float]) -> str:
    """简单市场环境判定（向后兼容）。P3 新增 _detect_regime_advanced 提供细粒度。"""
    if len(closes) < 20:
        return "未知"
    ret_20 = (closes[-1] - closes[-20]) / closes[-20]
    if ret_20 > 0.05:
        return "偏多"
    if ret_20 < -0.05:
        return "偏空"
    return "震荡"

# --- trade_plan_parts/payloads.py ---
# ── 场景生成 ──────────────────────────────────────────────────────


def _estimate_scenario_probabilities(
    closes: list[float],
    current_price: float,
    supports: list[dict],
    resistances: list[dict],
    atr_14: float,
) -> dict[str, float]:
    """P1: 用历史数据估算场景概率，替代硬编码常数。"""
    if len(closes) < 60:
        return {"pullback": 0.40, "breakout": 0.25, "deep_pullback": 0.20}

    # 统计历史上价格触及支撑/阻力后的表现
    n = len(closes)
    pullback_count = 0
    breakout_count = 0
    deep_drop_count = 0
    total_windows = 0

    sup_price = supports[0]["price"] if supports else current_price * 0.97
    res_price = resistances[0]["price"] if resistances else current_price * 1.03
    deep_sup = supports[1]["price"] if len(supports) >= 2 else current_price * 0.95

    for i in range(max(60, n - 120), n - 5):
        c = closes[i]
        future_5d = closes[i + 1: i + 6]
        if not future_5d:
            continue
        total_windows += 1
        future_max = max(future_5d)
        future_min = min(future_5d)

        dist_to_sup = (c - sup_price) / c if c > 0 else 0.1
        dist_to_res = (res_price - c) / c if c > 0 else 0.1

        if dist_to_sup < 0.03 and future_max > c:
            pullback_count += 1
        if future_max > res_price and dist_to_res < 0.08:
            breakout_count += 1
        if future_min < deep_sup:
            deep_drop_count += 1

    if total_windows > 0:
        p_pull = round(max(0.15, min(0.60, pullback_count / total_windows)), 2)
        p_break = round(max(0.10, min(0.45, breakout_count / total_windows)), 2)
        p_deep = round(max(0.05, min(0.35, deep_drop_count / total_windows)), 2)
    else:
        p_pull, p_break, p_deep = 0.40, 0.25, 0.20

    # ATR 适应：高波动时降低突破概率（假突破多），提升深度回调概率
    atr_pct = atr_14 / current_price if current_price > 0 else 0.03
    if atr_pct > 0.05:
        p_break = round(p_break * 0.8, 2)
        p_deep = round(min(0.35, p_deep * 1.2), 2)

    return {"pullback": p_pull, "breakout": p_break, "deep_pullback": p_deep}


async def _build_scenario_with_stop(
    code: str,
    entry_price: float,
    capital: float,
    risk_per_trade: float,
    atr_14: float,
    klines: list[dict] | None,
    levels: list[dict] | None,
    max_position_pct: float = 0.30,
    hit_rate_detail: dict | None = None,
) -> tuple[dict, dict, dict, dict | None]:
    """P0+P3: 统一止损/止盈/仓位计算 + 凯利公式辅助仓位建议。"""
    stop_result = await compute_stop_levels(
        code, entry_price, capital=capital, risk_per_trade=risk_per_trade,
        klines=klines, levels=levels,
    )
    sl_data = stop_result.get("data", {}) if stop_result.get("success") else {}
    sl_info = sl_data.get("stop_loss", {})
    tp_info = sl_data.get("take_profit", {})
    pos_info = sl_data.get("position_sizing", {})

    stop_price = sl_info.get("recommended", round(entry_price - atr_14 * 2, 2))
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share < 0.01:
        risk_per_share = atr_14 * 2

    shares = pos_info.get("max_shares", 0)
    if not shares and capital > 0 and risk_per_share > 0:
        risk_budget = capital * risk_per_trade
        shares = min(
            (math.floor(risk_budget / risk_per_share) // 100) * 100,
            (math.floor(capital * max_position_pct / entry_price) // 100) * 100,
        )

    method = sl_info.get("method", f"ATR(14)×2 = {round(atr_14 * 2, 2)}")

    # P3-1: 凯利公式辅助仓位
    kelly = None
    if hit_rate_detail:
        hp = hit_rate_detail.get("by_holding_period", {}).get("10d", {})
        hr = hp.get("hit_rate")
        avg_return = hp.get("avg_return")
        if hr is not None and avg_return is not None and hr > 0:
            avg_win = abs(avg_return) if avg_return > 0 else abs(avg_return) * 1.5
            avg_loss = risk_per_share / entry_price if entry_price > 0 else 0.03
            kelly = _kelly_position_sizing(hr, avg_win, avg_loss, capital, entry_price, 0.5, max_position_pct)
            if kelly.get("applicable") and kelly["kelly_shares"] > 0:
                shares = min(shares, kelly["kelly_shares"])

    return (
        {"price": stop_price, "method": method, "risk_per_share": risk_per_share},
        {"tp_2x": tp_info.get("tp_2x"), "tp_3x": tp_info.get("tp_3x")},
        {"shares": shares, "position_pct": pos_info.get("position_pct", round(shares * entry_price / capital * 100, 2) if shares and capital else 0)},
        kelly,
    )


async def _build_scenarios(
    code: str,
    current_price: float,
    levels: list[dict],
    capital: float,
    risk_per_trade: float,
    direction: str,
    atr_14: float,
    klines: list[dict] | None = None,
    closes: list[float] | None = None,
    hit_rate_detail: dict | None = None,
) -> list[dict]:
    """P0+P1+P3: 场景化入场方案 — 统一止损 + 数据驱动概率 + 凯利仓位 + 交易成本。"""
    scenarios = []

    supports = sorted(
        [lv for lv in levels if lv["type"] == "support" and lv["price"] < current_price],
        key=lambda x: x["price"],
        reverse=True,
    )
    resistances = sorted(
        [lv for lv in levels if lv["type"] == "resistance" and lv["price"] > current_price],
        key=lambda x: x["price"],
    )

    # P1: 数据驱动概率
    probs = _estimate_scenario_probabilities(
        closes or [], current_price, supports, resistances, atr_14,
    )

    # ── 场景 A: 回调至最近支撑位买入 ──
    if supports and direction in ("wait_pullback", "watch", "buy"):
        target_support = supports[0]
        entry = target_support["price"]

        sl, tp, pos, kelly = await _build_scenario_with_stop(
            code, entry, capital, risk_per_trade, atr_14, klines, levels, 0.30,
            hit_rate_detail=hit_rate_detail,
        )
        risk_per_share = sl["risk_per_share"]
        shares = pos["shares"]

        tp1 = resistances[0]["price"] if resistances else (tp["tp_2x"] or round(entry * 1.10, 2))
        tp2 = resistances[1]["price"] if len(resistances) > 1 else (tp["tp_3x"] or round(entry * 1.15, 2))
        rr1 = round(max(0, (tp1 - entry) / risk_per_share), 2) if risk_per_share > 0 else 0
        rr2 = round(max(0, (tp2 - entry) / risk_per_share), 2) if risk_per_share > 0 else 0

        # P3-5: 交易成本
        costs_tp1 = _calc_trading_costs(entry, tp1, shares, atr_14)
        costs_sl = _calc_trading_costs(entry, sl["price"], shares, atr_14)

        scenario = {
            "id": "A",
            "name": "回调买入（推荐）",
            "probability": probs["pullback"],
            "probability_source": "历史统计",
            "condition": f"价格回调至 {entry} 附近 ({', '.join(target_support.get('sources', [])[:3])})",
            "entry": {"price": entry, "shares": shares, "amount": round(shares * entry, 2) if shares else 0, "position_pct": pos["position_pct"]},
            "stop_loss": {"price": sl["price"], "method": sl["method"], "max_loss": round(shares * risk_per_share, 2) if shares else 0, "max_loss_pct": round(shares * risk_per_share / capital * 100, 2) if shares and capital else 0},
            "take_profit": [
                {"price": tp1, "action": "减仓 50%", "reason": "阻力位", "rr_ratio": rr1},
                {"price": tp2, "action": "清仓", "reason": "上方阻力位", "rr_ratio": rr2},
            ],
            "if_wrong": target_support.get("breach_action", f"跌破 {sl['price']} 止损离场"),
            "trading_costs": {
                "tp1_net_pnl": costs_tp1["net_pnl"], "tp1_cost": costs_tp1["total_cost"],
                "sl_net_pnl": costs_sl["net_pnl"], "sl_cost": costs_sl["total_cost"],
                "roundtrip_cost_pct": costs_tp1["cost_pct"],
            },
        }
        if kelly:
            scenario["kelly_sizing"] = kelly
        scenarios.append(scenario)

    # ── 场景 B: 强势突破最近阻力位 ──
    if resistances and direction in ("wait_pullback", "watch", "buy"):
        target_res = resistances[0]
        entry = round(target_res["price"] + atr_14 * 0.1, 2)

        sl, tp, pos, kelly = await _build_scenario_with_stop(
            code, entry, capital, risk_per_trade, atr_14, klines, levels, 0.25,
            hit_rate_detail=hit_rate_detail,
        )
        risk_per_share = sl["risk_per_share"]
        shares = pos["shares"]

        tp1 = resistances[1]["price"] if len(resistances) > 1 else (tp["tp_2x"] or round(entry * 1.08, 2))
        tp2 = tp["tp_3x"] or round(entry * 1.15, 2)
        rr1 = round(max(0, (tp1 - entry) / risk_per_share), 2) if risk_per_share > 0 else 0
        rr2 = round(max(0, (tp2 - entry) / risk_per_share), 2) if risk_per_share > 0 else 0

        costs_tp1 = _calc_trading_costs(entry, tp1, shares, atr_14)
        costs_sl = _calc_trading_costs(entry, sl["price"], shares, atr_14)

        scenario = {
            "id": "B",
            "name": "强势突破追涨",
            "probability": probs["breakout"],
            "probability_source": "历史统计",
            "condition": f"放量突破 {target_res['price']}（{target_res.get('confirmation', '需放量确认')}）",
            "entry": {"price": entry, "shares": shares, "amount": round(shares * entry, 2) if shares else 0, "position_pct": pos["position_pct"]},
            "stop_loss": {"price": sl["price"], "method": sl["method"], "max_loss": round(shares * risk_per_share, 2) if shares else 0, "max_loss_pct": round(shares * risk_per_share / capital * 100, 2) if shares and capital else 0},
            "take_profit": [
                {"price": tp1, "action": "减仓 50%", "reason": "上方阻力位", "rr_ratio": rr1},
                {"price": tp2, "action": "清仓", "reason": "趋势目标位", "rr_ratio": rr2},
            ],
            "if_wrong": target_res.get("breach_action", f"跌回 {sl['price']} 下方止损"),
            "trading_costs": {
                "tp1_net_pnl": costs_tp1["net_pnl"], "tp1_cost": costs_tp1["total_cost"],
                "sl_net_pnl": costs_sl["net_pnl"], "sl_cost": costs_sl["total_cost"],
                "roundtrip_cost_pct": costs_tp1["cost_pct"],
            },
        }
        if kelly:
            scenario["kelly_sizing"] = kelly
        scenarios.append(scenario)

    # ── 场景 C: 深度回调至更低支撑 ──
    if len(supports) >= 2 and direction in ("wait_pullback", "watch", "buy", "avoid"):
        deep_support = supports[1]
        entry = deep_support["price"]

        sl, tp, pos, kelly = await _build_scenario_with_stop(
            code, entry, capital, risk_per_trade, atr_14, klines, levels, 0.20,
            hit_rate_detail=hit_rate_detail,
        )
        risk_per_share = sl["risk_per_share"]
        shares = pos["shares"]

        tp1 = supports[0]["price"] if supports else (tp["tp_2x"] or round(entry * 1.08, 2))
        tp2_price = resistances[0]["price"] if resistances else round(current_price * 1.05, 2)
        rr1 = round(max(0, (tp1 - entry) / risk_per_share), 2) if risk_per_share > 0 else 0
        rr2 = round(max(0, (tp2_price - entry) / risk_per_share), 2) if risk_per_share > 0 else 0

        costs_tp1 = _calc_trading_costs(entry, tp1, shares, atr_14)
        costs_sl = _calc_trading_costs(entry, sl["price"], shares, atr_14)

        scenario = {
            "id": "C",
            "name": "深度回调抄底",
            "probability": probs["deep_pullback"],
            "probability_source": "历史统计",
            "condition": f"跌至 {entry} 附近且缩量企稳 ({', '.join(deep_support.get('sources', [])[:2])})",
            "entry": {"price": entry, "shares": shares, "amount": round(shares * entry, 2) if shares else 0, "position_pct": pos["position_pct"]},
            "stop_loss": {"price": sl["price"], "method": sl["method"], "max_loss": round(shares * risk_per_share, 2) if shares else 0, "max_loss_pct": round(shares * risk_per_share / capital * 100, 2) if shares and capital else 0},
            "take_profit": [
                {"price": tp1, "action": "减仓 50%", "reason": "反弹至上方支撑", "rr_ratio": rr1},
                {"price": tp2_price, "action": "清仓", "reason": "反弹至首个阻力位", "rr_ratio": rr2},
            ],
            "if_wrong": deep_support.get("breach_action", f"跌破 {sl['price']} 止损离场"),
            "trading_costs": {
                "tp1_net_pnl": costs_tp1["net_pnl"], "tp1_cost": costs_tp1["total_cost"],
                "sl_net_pnl": costs_sl["net_pnl"], "sl_cost": costs_sl["total_cost"],
                "roundtrip_cost_pct": costs_tp1["cost_pct"],
            },
        }
        if kelly:
            scenario["kelly_sizing"] = kelly
        scenarios.append(scenario)

    return scenarios


# ── 主入口 ────────────────────────────────────────────────────────


async def _get_fund_flow_safe(code: str) -> dict | None:
    """安全获取资金流数据。"""
    try:
        import asyncio
        from .fund_flow import get_stock_fund_flow
        result = await asyncio.to_thread(get_stock_fund_flow, code)
        if isinstance(result, dict) and result.get("success"):
            return result.get("data")
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/trade_plan.py:818: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass
    return None


def _normalize_price(price: float, factor: float) -> float:
    """将复权价转换为实际市场价格。"""
    return round(price * factor, 2)


def _normalize_output(result: dict, factor: float) -> dict:
    """对整个输出结构中的绝对价格字段应用归一化因子，并重算仓位。"""
    if abs(factor - 1.0) < 0.005:
        return result

    cap = result.get("position_management", {}).get("_capital", 0)
    risk_pct = result.get("position_management", {}).get("max_loss_per_trade_pct", 2.0) / 100
    max_pos_pct = result.get("position_management", {}).get("max_position_pct", 30) / 100

    result["current_price"] = _normalize_price(result["current_price"], factor)

    for lv in result.get("key_levels", []):
        old_price = lv["price"]
        lv["price"] = _normalize_price(old_price, factor)
        for field in ("confirmation", "breach_action"):
            if field in lv and isinstance(lv[field], str):
                lv[field] = _replace_prices_in_text(lv[field], factor)

    for sc in result.get("scenarios", []):
        entry = sc.get("entry", {})
        sl = sc.get("stop_loss", {})

        if "price" in entry:
            entry["price"] = _normalize_price(entry["price"], factor)
        if "price" in sl:
            sl["price"] = _normalize_price(sl["price"], factor)

        e_price = entry.get("price", 0)
        s_price = sl.get("price", 0)
        risk_per_share = abs(e_price - s_price) if e_price and s_price else 0

        if cap > 0 and e_price > 0 and risk_per_share > 0:
            risk_budget = cap * risk_pct
            by_risk = math.floor(risk_budget / risk_per_share)
            by_cap = math.floor(cap * max_pos_pct / e_price)
            shares = min(by_risk, by_cap)
            shares = (shares // 100) * 100
            entry["shares"] = shares
            entry["amount"] = round(shares * e_price, 2)
            entry["position_pct"] = round(entry["amount"] / cap * 100, 2)
            sl["max_loss"] = round(shares * risk_per_share, 2)
            sl["max_loss_pct"] = round(sl["max_loss"] / cap * 100, 2)

        for tp in sc.get("take_profit", []):
            if "price" in tp:
                tp["price"] = _normalize_price(tp["price"], factor)
                if risk_per_share > 0 and e_price > 0:
                    tp["rr_ratio"] = round(abs(tp["price"] - e_price) / risk_per_share, 2)

        if "if_wrong" in sc and isinstance(sc["if_wrong"], str):
            sc["if_wrong"] = _replace_prices_in_text(sc["if_wrong"], factor)
        if "condition" in sc and isinstance(sc["condition"], str):
            sc["condition"] = _replace_prices_in_text(sc["condition"], factor)

    snap = result.get("indicators_snapshot", {})
    for f in ("ma20", "ma60", "atr_14"):
        if snap.get(f) is not None:
            snap[f] = _normalize_price(snap[f], factor)

    return result


def _replace_prices_in_text(text: str, factor: float) -> str:
    """替换文本中出现的价格数字（>10 的浮点数）为归一化后的值。"""
    import re
    def _repl(m):
        val = float(m.group(0))
        if val > 10:
            return f"{val * factor:.2f}"
        return m.group(0)
    return re.sub(r'\d+\.\d+', _repl, text)


def _select_signal_name(rsi_value: float, macd_hist: list) -> str:
    """根据当前指标状态选择最匹配的历史信号名。"""
    valid_hist = [h for h in (macd_hist or []) if h is not None]
    macd_positive = valid_hist[-1] > 0 if valid_hist else False

    if rsi_value < _RSI_OVERSOLD and macd_positive:
        return "rsi_oversold_and_macd_golden"
    if rsi_value < _RSI_OVERSOLD:
        return "rsi_oversold"
    if macd_positive:
        return "ma_bullish_alignment"
    return "ma_bearish_alignment"


async def _get_historical_hit_rate(
    code: str, rsi_value: float, macd_hist: list,
) -> float | None:
    """尝试获取当前信号组合的 10 日历史胜率。"""
    try:
        from ..services.data_pipeline import compute_signal_hit_rate

        db = get_db()
        klines = await db.get_klines(code, limit=500)
        if not klines or len(klines) < 60:
            return None

        signal = _select_signal_name(rsi_value, macd_hist)
        report = compute_signal_hit_rate(klines, signal=signal, forward_days=[5, 10, 20])
        r10 = report.get("forward_returns", {}).get("10d", {})
        if r10.get("reliable") and r10.get("hit_rate") is not None:
            return float(r10["hit_rate"])
    except Exception as e:
        logger.debug("历史胜率获取失败: %s", e)
    return None


async def _get_hit_rate_detail(
    code: str, rsi_value: float, macd_hist: list,
) -> dict | None:
    """P1: 深化信号命中率 — 分持有期、平均收益、regime 分层。"""
    try:
        from ..services.data_pipeline import compute_signal_hit_rate

        db = get_db()
        klines = await db.get_klines(code, limit=500)
        if not klines or len(klines) < 60:
            return None

        signal = _select_signal_name(rsi_value, macd_hist)
        report = compute_signal_hit_rate(klines, signal=signal, forward_days=[5, 10, 20])

        detail = {
            "signal": signal,
            "sample_count": report.get("sample_count", 0),
            "by_holding_period": {},
            "by_regime": {},
        }

        for fd_key, stats in report.get("forward_returns", {}).items():
            detail["by_holding_period"][fd_key] = {
                "hit_rate": stats.get("hit_rate"),
                "avg_return": stats.get("avg_return"),
                "samples": stats.get("samples", 0),
                "reliable": stats.get("reliable", False),
            }

        for regime, buckets in report.get("by_regime", {}).items():
            regime_summary = {}
            for fd_key, stats in buckets.items():
                regime_summary[fd_key] = {
                    "hit_rate": stats.get("hit_rate"),
                    "avg_return": stats.get("avg_return"),
                    "samples": stats.get("samples", 0),
                }
            detail["by_regime"][regime] = regime_summary

        return detail if detail["sample_count"] > 0 else None
    except Exception as e:
        logger.debug("命中率详情获取失败: %s", e)
    return None


# ── P2: 量化增强辅助函数 ──────────────────────────────────────────


async def _get_similar_patterns(code: str, klines: list[dict]) -> dict | None:
    """P2: 调用相似形态分析，获取历史相似走势后的收益分布。"""
    try:
        from .quant import _build_similar_pattern_report
        report = _build_similar_pattern_report(klines, window_days=20, top_n=5, forward_days=[5, 10, 20])
        if report.get("matches"):
            agg = report.get("aggregate_prediction", {})
            return {
                "match_count": len(report["matches"]),
                "avg_correlation": round(np.mean([m.get("correlation", 0) for m in report["matches"]]), 3),
                "forward_prediction": {
                    k: {"avg_return": round(v.get("mean_return", 0) * 100, 2), "positive_pct": round(v.get("positive_pct", 0) * 100, 1)}
                    for k, v in agg.items()
                },
            }
    except Exception as e:
        logger.debug("相似形态分析失败: %s", e)
    return None


async def _get_factor_snapshot(code: str, closes: list[float]) -> dict | None:
    """P2: 计算动量/波动率因子的分位数，提供量化信号强度依据。"""
    try:
        if len(closes) < 60:
            return None

        # 动量因子: 20日收益率
        mom_20 = (closes[-1] - closes[-20]) / closes[-20] * 100 if closes[-20] > 0 else 0
        # 60日收益率
        mom_60 = (closes[-1] - closes[-60]) / closes[-60] * 100 if closes[-60] > 0 else 0

        # 波动率: 20日收益率标准差
        returns_20 = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(len(closes) - 20, len(closes)) if closes[i - 1] > 0]
        vol_20 = float(np.std(returns_20) * 100) if returns_20 else 0

        # 计算动量因子在历史中的分位数
        hist_mom = []
        for i in range(60, len(closes)):
            m = (closes[i] - closes[i - 20]) / closes[i - 20] * 100 if closes[i - 20] > 0 else 0
            hist_mom.append(m)
        mom_pct = round(sum(1 for m in hist_mom if m < mom_20) / len(hist_mom) * 100, 1) if hist_mom else 50

        hist_vol = []
        for i in range(40, len(closes)):
            r = [(closes[j] - closes[j - 1]) / closes[j - 1] for j in range(i - 19, i + 1) if closes[j - 1] > 0]
            if r:
                hist_vol.append(float(np.std(r) * 100))
        vol_pct = round(sum(1 for v in hist_vol if v < vol_20) / len(hist_vol) * 100, 1) if hist_vol else 50

        return {
            "momentum_20d": round(mom_20, 2),
            "momentum_60d": round(mom_60, 2),
            "momentum_percentile": mom_pct,
            "volatility_20d": round(vol_20, 2),
            "volatility_percentile": vol_pct,
            "momentum_signal": "强势" if mom_pct > 80 else "偏强" if mom_pct > 60 else "中性" if mom_pct > 40 else "偏弱" if mom_pct > 20 else "弱势",
            "volatility_signal": "高波动" if vol_pct > 80 else "中高" if vol_pct > 60 else "正常" if vol_pct > 40 else "偏低" if vol_pct > 20 else "低波动",
        }
    except Exception as e:
        logger.debug("因子快照获取失败: %s", e)
    return None


def _compute_var(closes: list[float], capital: float, position_pct: float = 0.30) -> dict | None:
    """P2: 计算 VaR 和极端场景损失估算。"""
    try:
        if len(closes) < 60:
            return None

        returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(returns) < 30:
            return None

        arr = np.array(returns)
        position_value = capital * position_pct

        var_95 = round(float(np.percentile(arr, 5)) * position_value, 2)
        var_99 = round(float(np.percentile(arr, 1)) * position_value, 2)
        max_daily_loss = round(float(np.min(arr)) * position_value, 2)

        # 连续下跌极端场景: 最近 250 天内最大 5 日回撤
        max_5d_drawdown = 0
        for i in range(4, len(closes)):
            dd = (closes[i] - max(closes[max(0, i - 4): i + 1])) / max(closes[max(0, i - 4): i + 1])
            if dd < max_5d_drawdown:
                max_5d_drawdown = dd

        return {
            "daily_var_95": abs(var_95),
            "daily_var_99": abs(var_99),
            "worst_daily_loss": abs(max_daily_loss),
            "max_5d_drawdown_pct": round(abs(max_5d_drawdown) * 100, 2),
            "extreme_scenario_loss": round(abs(max_5d_drawdown) * position_value, 2),
            "position_value": round(position_value, 2),
        }
    except Exception as e:
        logger.debug("VaR 计算失败: %s", e)
    return None

# --- trade_plan_parts/formatters.py ---
# ── P3-1: 凯利公式仓位管理 ─────────────────────────────────────


def _kelly_position_sizing(
    hit_rate: float,
    avg_win: float,
    avg_loss: float,
    capital: float,
    entry_price: float,
    fraction: float = 0.5,
    max_position_pct: float = 0.30,
) -> dict:
    """用分数凯利公式计算最优仓位。

    f* = (b*p - q) / b，其中 b = avg_win / avg_loss, p = 胜率, q = 1-p。
    实战使用 fraction (默认 ½ Kelly) 降低波动风险。
    """
    if avg_loss <= 0 or hit_rate <= 0 or entry_price <= 0:
        return {"kelly_raw": 0, "kelly_adjusted": 0, "kelly_shares": 0,
                "method": "数据不足", "applicable": False}

    b = avg_win / avg_loss
    q = 1 - hit_rate
    kelly_pct = (b * hit_rate - q) / b

    if kelly_pct <= 0:
        return {"kelly_raw": round(kelly_pct, 4), "kelly_adjusted": 0,
                "kelly_shares": 0, "method": "Kelly 为负，不建议建仓",
                "applicable": False}

    adj_pct = min(kelly_pct * fraction, max_position_pct)
    amount = capital * adj_pct
    shares = (math.floor(amount / entry_price) // 100) * 100

    return {
        "kelly_raw": round(kelly_pct, 4),
        "kelly_adjusted": round(adj_pct, 4),
        "kelly_shares": shares,
        "method": f"{fraction:.0%} Kelly",
        "win_loss_ratio": round(b, 2),
        "note": f"基于胜率{hit_rate:.0%}、盈亏比{b:.2f}计算",
        "applicable": True,
    }


# ── P3-2: 多维度市场状态检测 ───────────────────────────────────


def _detect_regime_advanced(
    closes: list[float], volumes: list[float],
) -> dict:
    """三维分类矩阵: 动量×波动率×量能。

    输出 6 种细粒度状态替代原始的"偏多/偏空/震荡"。
    """
    if len(closes) < 30 or len(volumes) < 20:
        return {"regime": "unknown", "momentum": 0, "volatility": 0,
                "volume_trend": 0, "label": "数据不足"}

    # 维度1: 动量 — 30 日收益率
    ret_30 = (closes[-1] - closes[-30]) / closes[-30] * 100

    # 维度2: 波动率 — 20 日年化波动率
    daily_ret = np.diff(closes[-21:]) / np.array(closes[-21:-1])
    vol_ann = float(np.std(daily_ret) * np.sqrt(250) * 100)

    # 维度3: 量能趋势 — 5 日均量 vs 20 日均量
    avg_vol_5 = float(np.mean(volumes[-5:]))
    avg_vol_20 = float(np.mean(volumes[-20:]))
    vol_trend = ((avg_vol_5 - avg_vol_20) / avg_vol_20 * 100) if avg_vol_20 > 0 else 0

    # 分类
    is_volatile = vol_ann > 30
    if ret_30 > 5:
        regime = "bull_volatile" if is_volatile else "bull_calm"
    elif ret_30 < -5:
        regime = "bear_volatile" if is_volatile else "bear_calm"
    else:
        regime = "range_volatile" if is_volatile else "range_calm"

    # 量能标签
    if vol_trend > 30:
        volume_label = "放量"
    elif vol_trend < -30:
        volume_label = "缩量"
    else:
        volume_label = "正常"

    _REGIME_LABELS = {
        "bull_calm": "温和上涨",
        "bull_volatile": "剧烈上涨",
        "bear_calm": "温和下跌",
        "bear_volatile": "剧烈下跌",
        "range_calm": "低波震荡",
        "range_volatile": "高波震荡",
    }

    return {
        "regime": regime,
        "label": _REGIME_LABELS.get(regime, regime),
        "momentum_30d": round(ret_30, 2),
        "volatility_ann": round(vol_ann, 2),
        "volume_trend": round(vol_trend, 2),
        "volume_label": volume_label,
    }


def _regime_adjusted_params(regime: str) -> dict:
    """根据市场状态调整策略参数。"""
    defaults = {"max_position_pct": 0.30, "atr_multiplier": 2.0, "risk_per_trade": 0.02}
    adjustments = {
        "bull_calm":     {"max_position_pct": 0.30, "atr_multiplier": 2.0, "risk_per_trade": 0.02},
        "bull_volatile": {"max_position_pct": 0.20, "atr_multiplier": 2.5, "risk_per_trade": 0.015},
        "bear_calm":     {"max_position_pct": 0.15, "atr_multiplier": 1.5, "risk_per_trade": 0.01},
        "bear_volatile": {"max_position_pct": 0.10, "atr_multiplier": 3.0, "risk_per_trade": 0.01},
        "range_calm":    {"max_position_pct": 0.25, "atr_multiplier": 2.0, "risk_per_trade": 0.02},
        "range_volatile":{"max_position_pct": 0.20, "atr_multiplier": 2.5, "risk_per_trade": 0.015},
    }
    return adjustments.get(regime, defaults)


# ── P3-3: 滚动窗口验证（WFA Lite）─────────────────────────────


def _rolling_window_validation(
    closes: list[float], signal: str, klines: list[dict], window_size: int = 50,
) -> dict | None:
    """将数据分为多个窗口，验证信号在不同时期的稳定性。"""
    try:
        from ..services.data_pipeline import compute_signal_hit_rate

        n = len(klines)
        if n < window_size * 2:
            return None

        window_results = []
        for start in range(0, n - window_size + 1, window_size):
            end = start + window_size
            window_klines = klines[start:end]
            if len(window_klines) < 30:
                continue
            report = compute_signal_hit_rate(window_klines, signal=signal, forward_days=[10])
            r10 = report.get("forward_returns", {}).get("10d", {})
            if r10.get("samples", 0) >= 3 and r10.get("hit_rate") is not None:
                window_results.append(float(r10["hit_rate"]))

        if len(window_results) < 2:
            return None

        stability = round(1.0 - float(np.std(window_results)), 3)
        mean_hr = round(float(np.mean(window_results)), 3)

        if stability >= 0.85:
            verdict = "非常稳定"
        elif stability >= 0.70:
            verdict = "稳定"
        elif stability >= 0.55:
            verdict = "一般"
        else:
            verdict = "不稳定（过拟合风险）"

        return {
            "windows": len(window_results),
            "hit_rates": [round(r, 3) for r in window_results],
            "stability": stability,
            "mean_hit_rate": mean_hr,
            "verdict": verdict,
        }
    except Exception as e:
        logger.debug("滚动窗口验证失败: %s", e)
    return None


# ── P3-4: 信号衰减检测 ────────────────────────────────────────


def _detect_signal_decay(
    klines: list[dict], signal: str, recent_window: int = 60,
) -> dict | None:
    """对比近期 vs 全样本的信号命中率，检测信号是否衰减。"""
    try:
        from ..services.data_pipeline import compute_signal_hit_rate

        if len(klines) < recent_window + 30:
            return None

        full_report = compute_signal_hit_rate(klines, signal=signal, forward_days=[10])
        recent_report = compute_signal_hit_rate(klines[-recent_window:], signal=signal, forward_days=[10])

        full_r10 = full_report.get("forward_returns", {}).get("10d", {})
        recent_r10 = recent_report.get("forward_returns", {}).get("10d", {})

        full_hr = full_r10.get("hit_rate")
        recent_hr = recent_r10.get("hit_rate")
        full_samples = full_r10.get("samples", 0)
        recent_samples = recent_r10.get("samples", 0)

        if full_hr is None or full_hr <= 0 or full_samples < 5:
            return None

        decay_ratio = round(recent_hr / full_hr, 3) if recent_hr is not None else 0

        warning = None
        if decay_ratio < 0.5:
            warning = f"信号严重衰减：近{recent_window}日命中率仅为历史的{decay_ratio:.0%}，建议降低仓位或切换信号"
        elif decay_ratio < 0.7:
            warning = f"信号有所衰减：近{recent_window}日命中率下降{(1 - decay_ratio):.0%}，需谨慎"

        return {
            "full_sample_hr": full_hr,
            "full_sample_n": full_samples,
            "recent_hr": recent_hr,
            "recent_n": recent_samples,
            "decay_ratio": decay_ratio,
            "warning": warning,
        }
    except Exception as e:
        logger.debug("信号衰减检测失败: %s", e)
    return None


# ── P3-5: A 股交易成本建模 ────────────────────────────────────


def _calc_trading_costs(
    entry_price: float,
    exit_price: float,
    shares: int,
    atr_14: float = 0,
) -> dict:
    """计算 A 股单次完整交易（买入+卖出）的全部成本。"""
    if shares <= 0 or entry_price <= 0:
        return {"total_cost": 0, "cost_pct": 0, "gross_pnl": 0, "net_pnl": 0}

    slippage_rate = max(_MIN_SLIPPAGE_RATE, (atr_14 * 0.1 / entry_price) if atr_14 > 0 and entry_price > 0 else _MIN_SLIPPAGE_RATE)

    buy_amount = entry_price * shares
    sell_amount = exit_price * shares

    buy_commission = buy_amount * _COMMISSION_RATE
    buy_transfer = buy_amount * _TRANSFER_FEE_RATE
    buy_slippage = buy_amount * slippage_rate

    sell_commission = sell_amount * _COMMISSION_RATE
    sell_stamp_tax = sell_amount * _STAMP_TAX_RATE
    sell_transfer = sell_amount * _TRANSFER_FEE_RATE
    sell_slippage = sell_amount * slippage_rate

    total_cost = (buy_commission + buy_transfer + buy_slippage
                  + sell_commission + sell_stamp_tax + sell_transfer + sell_slippage)

    gross_pnl = (exit_price - entry_price) * shares
    net_pnl = gross_pnl - total_cost

    return {
        "total_cost": round(total_cost, 2),
        "cost_pct": round(total_cost / buy_amount * 100, 3) if buy_amount > 0 else 0,
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "breakdown": {
            "buy_cost": round(buy_commission + buy_transfer + buy_slippage, 2),
            "sell_cost": round(sell_commission + sell_stamp_tax + sell_transfer + sell_slippage, 2),
            "slippage_rate": round(slippage_rate * 100, 3),
        },
    }


async def _cross_validate_signal(code: str, capital: float) -> dict | None:
    """交叉验证: 调用 should_i_buy 获取独立判断。"""
    try:
        from ._decision_buy import should_i_buy

        result = await should_i_buy(code=code, investment_style="balanced")
        if isinstance(result, dict) and result.get("success"):
            d = result.get("data", {})
            return {
                "recommendation": d.get("recommendation"),
                "buy_probability": d.get("decision_probability", {}).get("buy_probability"),
                "score": d.get("score"),
                "confidence": d.get("confidence"),
            }
    except Exception as e:
        logger.debug("交叉验证失败: %s", e)
    return None


async def _get_realtime_price(code: str) -> float | None:
    """安全获取实时价格用于价格校准。"""
    try:
        from ...services.market_data_access import FALLBACK_DB_ONLY, get_quote_snapshot_response
        rt = await get_quote_snapshot_response(code, fallback_mode=FALLBACK_DB_ONLY)
        if isinstance(rt, dict) and rt.get("success"):
            p = float(rt.get("data", {}).get("price", 0))
            return p if p > 0 else None
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/trade_plan.py:1380: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None

# --- trade_plan_parts/actions.py ---
async def generate_plan(
    code: str,
    capital: float = 1_000_000,
    risk_per_trade: float = 0.02,
    style: str = "balanced",
) -> dict[str, Any]:
    """生成完整交易计划。"""
    from .db_freshness import ensure_fresh_klines, _calc_staleness

    klines, freshness_info = await ensure_fresh_klines(code, limit=250)

    if not klines or len(klines) < 60:
        return fail("K 线数据不足 60 根，无法生成交易计划")

    klines.sort(key=lambda k: k.get("date", ""))

    closes = [float(k.get("close", 0) or 0) for k in klines]
    highs = [float(k.get("high", 0) or 0) for k in klines]
    lows = [float(k.get("low", 0) or 0) for k in klines]
    volumes = [float(k.get("volume", 0) or 0) for k in klines]
    current_price = closes[-1]
    latest_volume = volumes[-1]
    avg_vol_20 = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))

    # ── Step 1: 关键价位 — 共享 klines 避免重复获取
    klines_for_levels = klines[-120:] if len(klines) > 120 else klines
    kl_result = await compute_key_levels(code, lookback_days=120, klines=klines_for_levels)
    levels = []
    if kl_result.get("success") and kl_result.get("data"):
        levels = kl_result["data"].get("levels", [])

    # ── Step 2: 技术指标
    ma20 = _TA.calculate_sma(closes, 20)
    ma60 = _TA.calculate_sma(closes, 60)
    macd_data = _TA.calculate_macd(closes)
    macd_hist = macd_data.get("histogram", [])
    rsi_data = _TA.calculate_rsi(closes)
    # F-N04-1: value 可能为 None(warmup 不足)；trade_plan 上游已要求充足K线，
    # 这里兜底中性值 50，避免 None 进入下游 trigger/hit_rate 计算。
    rsi_value = rsi_data.get("value")
    if rsi_value is None:
        rsi_value = 50.0
    atr_series = _TA.calculate_atr(highs, lows, closes, period=14)
    atr_14 = atr_series[-1] if atr_series and atr_series[-1] > 0 else 0

    # ── Step 3: K 线形态
    patterns = []
    try:
        patterns = pattern_recognition.detect_patterns(klines)
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/trade_plan.py:1433: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass

    # ── Step 4: 资金流
    fund_flow = await _get_fund_flow_safe(code)

    # ── Step 5: 数据质量评估
    days_stale = freshness_info.get("staleness_days", _calc_staleness(klines))

    # ── Step 6: 信号融合 (核心) — P1: _judge_trend 使用完整 macd_data
    trend = _judge_trend(closes, macd_data, ma20, ma60)
    trigger = _judge_trigger(rsi_value, current_price, levels, patterns)
    confirm = _judge_confirmation(fund_flow, avg_vol_20, latest_volume)

    # P3-2: 多维度市场状态检测
    regime_detail = _detect_regime_advanced(closes, volumes)
    regime_params = _regime_adjusted_params(regime_detail["regime"])

    # P1: 深化历史验证 — 分持有期、平均收益、regime 分层
    hist_hit_rate = await _get_historical_hit_rate(code, rsi_value, macd_hist)
    hit_rate_detail = await _get_hit_rate_detail(code, rsi_value, macd_hist)

    # 冲突检测 (在 _compute_confidence 之前，因为冲突数量影响信号清晰度)
    conflicts = []
    if trend["direction"] in ("bullish", "bullish_weak") and rsi_value > _RSI_OVERBOUGHT:
        conflicts.append("趋势看多但 RSI 超买，短期过热，等回调更安全")
    if trend["direction"] in ("bearish", "bearish_weak") and trigger["status"] == "triggered":
        conflicts.append("趋势偏空但触发了看涨信号，可能是反弹而非反转，需谨慎")
    if confirm["status"] == "unconfirmed":
        conflicts.append("主力净流出，即使信号触发也需降低仓位")

    # 资金流方向
    fund_dir = "neutral"
    if confirm["status"] == "confirmed":
        fund_dir = "bullish"
    elif confirm["status"] == "unconfirmed":
        fund_dir = "bearish"

    # 交叉验证 (提前到这里，以便传入 _compute_confidence)
    cross_val = await _cross_validate_signal(code, capital)
    if cross_val:
        buy_prob = cross_val.get("buy_probability")
        if buy_prob is not None and buy_prob < 0.10 and trend["direction"] in ("bullish", "bullish_weak"):
            conflicts.append(
                f"交叉验证警告: should_i_buy 买入概率仅 {buy_prob:.1%}，"
                f"与当前趋势方向存在分歧"
            )

    confidence, confidence_breakdown = _compute_confidence(
        trend, trigger, confirm,
        data_staleness_days=days_stale,
        calibration_factor=1.0,
        kline_count=len(klines),
        historical_hit_rate=hist_hit_rate,
        cross_validation=cross_val,
        num_scenarios=3,
        num_levels=len(levels),
        num_conflicts=len(conflicts),
        closes=closes,
        macd_hist=macd_hist,
        rsi_value=rsi_value,
        fund_flow_direction=fund_dir,
    )
    direction = _determine_direction(trend, trigger, confidence)

    # 风格调整
    if style == "conservative" and direction == "buy":
        direction = "watch"
        confidence = round(confidence * 0.85, 2)
    elif style == "aggressive" and direction == "watch":
        direction = "buy"
        confidence = round(min(confidence * 1.1, 1.0), 2)

    # P3-2: 应用 regime 自适应参数
    effective_risk = regime_params.get("risk_per_trade", risk_per_trade)
    effective_max_pos = regime_params.get("max_position_pct", 0.30)

    # ── Step 6b: 场景化方案 — P0+P1+P3: 统一止损 + 数据驱动概率 + 凯利仓位 + 交易成本
    scenarios = await _build_scenarios(
        code, current_price, levels, capital, effective_risk, direction, atr_14,
        klines=klines_for_levels, closes=closes, hit_rate_detail=hit_rate_detail,
    )

    regime = _determine_regime(closes)

    stock_name = klines[-1].get("name", "") if klines else ""

    # ── P2: 量化增强分析（并行执行，不阻塞主流程）
    similar_patterns = await _get_similar_patterns(code, klines)
    factor_snapshot = await _get_factor_snapshot(code, closes)
    var_analysis = _compute_var(closes, capital, effective_max_pos)

    # ── P3-3: 滚动窗口验证 — 信号稳定性检测
    signal_name = _select_signal_name(rsi_value, macd_hist)
    signal_stability = _rolling_window_validation(closes, signal_name, klines)

    # ── P3-4: 信号衰减检测
    signal_decay = _detect_signal_decay(klines, signal_name)

    # ── 数据质量警告
    data_quality_warnings: list[str] = []
    if days_stale > 90:
        data_quality_warnings.append(
            f"K线数据严重过期（{days_stale}天），技术指标可能不反映当前市场状态"
        )
    elif days_stale > 30:
        data_quality_warnings.append(
            f"K线数据较旧（{days_stale}天），指标可信度降低"
        )
    elif days_stale > 7:
        data_quality_warnings.append(
            f"K线数据有延迟（{days_stale}天），建议关注最新行情"
        )

    if hist_hit_rate is not None and hist_hit_rate < 0.30:
        data_quality_warnings.append(
            f"当前信号组合的历史胜率较低({hist_hit_rate:.0%})，历史验证不充分"
        )
    elif hist_hit_rate is None:
        data_quality_warnings.append(
            "当前信号组合缺乏历史数据验证，置信度已折减"
        )

    # P3-3: 信号稳定性警告
    if signal_stability and signal_stability.get("stability", 1) < 0.55:
        data_quality_warnings.append(
            f"信号稳定性差（{signal_stability['stability']:.2f}），不同时期表现波动大，过拟合风险较高"
        )
        confidence = round(confidence * 0.90, 2)

    # P3-4: 信号衰减警告
    if signal_decay and signal_decay.get("warning"):
        data_quality_warnings.append(signal_decay["warning"])
        decay_ratio = signal_decay.get("decay_ratio", 1.0)
        if decay_ratio < 0.5:
            confidence = round(confidence * 0.85, 2)
        elif decay_ratio < 0.7:
            confidence = round(confidence * 0.92, 2)

    # 冲突检测中加入数据新鲜度冲突
    if days_stale > 30:
        conflicts.append(
            f"K线数据距今{days_stale}天，RSI/MACD等指标基于历史数据，仅供趋势参考"
        )

    result = {
        "code": code,
        "name": stock_name,
        "current_price": round(current_price, 2),
        "market_regime": regime,
        "regime_detail": regime_detail,
        "regime_adaptive_params": regime_params,
        "direction": direction,
        "confidence": confidence,
        "confidence_breakdown": confidence_breakdown,
        "data_quality": {
            "kline_last_date": klines[-1].get("date", "unknown") if klines else "unknown",
            "staleness_days": days_stale,
            "freshness": (
                "fresh" if days_stale <= 3
                else "acceptable" if days_stale <= 7
                else "stale" if days_stale <= 30
                else "severely_stale"
            ),
            "warnings": data_quality_warnings if data_quality_warnings else None,
        },
        "signal_summary": {
            "primary": {
                "name": trend["name"],
                "direction": trend["direction"],
                "detail": trend["detail"],
            },
            "trigger": {
                "name": trigger["name"],
                "status": trigger["status"],
                "action": trigger["action"],
            },
            "confirmation": {
                "name": confirm["name"],
                "status": confirm["status"],
            },
            "conflicts": conflicts if conflicts else None,
        },
        "cross_validation": cross_val,
        "scenarios": scenarios,
        "key_levels": levels[:8],
        "position_management": {
            "max_position_pct": round(effective_max_pos * 100, 1),
            "max_loss_per_trade_pct": round(effective_risk * 100, 2),
            "risk_budget_per_trade": round(capital * effective_risk, 2),
            "style": style,
            "regime_adjusted": regime_detail["regime"] != "unknown",
            "_capital": capital,
        },
        "indicators_snapshot": {
            "rsi": round(rsi_value, 1),
            "macd_histogram": round(float(macd_hist[-1]), 4) if macd_hist and macd_hist[-1] is not None else None,
            "ma20": round(ma20[-1], 2) if ma20 else None,
            "ma60": round(ma60[-1], 2) if ma60 else None,
            "atr_14": round(atr_14, 2),
            "atr_pct": round(atr_14 / current_price * 100, 2) if current_price > 0 else None,
            "volume_ratio": round(latest_volume / avg_vol_20, 2) if avg_vol_20 > 0 else None,
        },
        "hit_rate_detail": hit_rate_detail,
        "signal_stability": signal_stability,
        "signal_decay": signal_decay,
        "similar_patterns": similar_patterns,
        "factor_snapshot": factor_snapshot,
        "risk_analysis": var_analysis,
        "daily_checklist": [
            "开盘前: 检查隔夜消息面，确认计划是否需要调整",
            "盘中: 关注关键价位的量价配合",
            "尾盘: 记录当日高低价，若持仓则更新追踪止损位",
        ],
    }

    # ── Step 7: 价格归一化 — 用实时价校准复权偏移
    rt_price = await _get_realtime_price(code)
    price_factor = 1.0
    price_warning = None
    kline_date = klines[-1].get("date", "unknown") if klines else "unknown"

    if rt_price and current_price > 0:
        price_factor = rt_price / current_price
        if abs(price_factor - 1.0) > 0.02:
            result = _normalize_output(result, price_factor)
            price_warning = (
                f"K 线收盘价({current_price:.2f}, {kline_date})与实时价({rt_price:.2f})"
                f"偏差 {abs(price_factor - 1) * 100:.1f}%，已自动校准至实际价格"
            )

        # 用实际校准偏差重新计算 data_quality.calibration 子分
        cal_dev = abs(price_factor - 1.0)
        if cal_dev < 0.01:
            cal_sub = 1.0
        elif cal_dev < 0.02:
            cal_sub = 0.90
        elif cal_dev < 0.05:
            cal_sub = 0.70
        else:
            cal_sub = 0.40
        bd = result.get("confidence_breakdown", {})
        dq = bd.get("data_quality", {})
        old_cal = dq.get("calibration", 1.0)
        if abs(cal_sub - old_cal) > 0.01:
            dq["calibration"] = cal_sub
            new_dq_score = round((dq.get("freshness", 1.0) + cal_sub + dq.get("completeness", 1.0)) / 3.0, 2)
            dq["score"] = new_dq_score
            W = bd.get("weights", {})
            new_final = (
                new_dq_score * W.get("data_quality", 0.25)
                + bd.get("signal_clarity", {}).get("score", 0) * W.get("signal_clarity", 0.30)
                + bd.get("validation", {}).get("score", 0) * W.get("validation", 0.25)
                + bd.get("completeness", {}).get("score", 0) * W.get("completeness", 0.20)
            )
            new_final = round(min(1.0, max(0.0, new_final)), 2)
            bd["final"] = new_final
            result["confidence"] = new_final

    result["price_calibration"] = {
        "kline_close": round(current_price, 2),
        "realtime_price": round(rt_price, 2) if rt_price else None,
        "factor": round(price_factor, 4),
        "calibrated": abs(price_factor - 1.0) > 0.02,
        "warning": price_warning,
        "kline_date": kline_date,
    }

    result["position_management"].pop("_capital", None)

    return ok(result)


# ── MCP 注册 ──────────────────────────────────────────────────────


def register(mcp):
    """注册交易计划生成工具。"""

    @mcp.tool()
    async def generate_trade_plan(
        code: str,
        capital: float = 1_000_000,
        risk_per_trade: float = 0.02,
        style: str = "balanced",
    ):
        """生成完整交易计划（信号融合 + 场景化方案）

        一次调用完成多源信号融合，输出:
        - 方向判断 + 置信度
        - 3 个信号 (主信号/触发信号/确认信号) 的简洁结论
        - 场景化入场方案 (每个带条件/入场价/止损/止盈/若错则...)
        - 关键价位 (带突破/跌破操作建议)
        - 仓位管理参数

        Args:
            code: 股票代码
            capital: 可用资金 (元，默认 100 万)
            risk_per_trade: 单笔风险占比 (默认 0.02 即 2%)
            style: aggressive (激进) / balanced (均衡) / conservative (保守)
        """
        normalized_code, _, error = await resolve_existing_security_code_async(code=code)
        if error:
            return fail(error)
        return await generate_plan(
            normalized_code, capital=capital, risk_per_trade=risk_per_trade, style=style,
        )

