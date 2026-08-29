"""关键价位引擎 — 多方法投票计算支撑/阻力位。

算法:
  1. Pivot Point (经典)
  2. 成交密集区 (量价分布核密度估计)
  3. 斐波那契回撤 (最近一波趋势)
  4. 前高前低聚类 (zigzag 拐点 + 聚类)
  5. 均线关键位 (MA5/MA10/MA20/MA60)

每个价位附带 strength (多方法汇聚强度) 和 breach_action (突破/跌破操作建议)。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

import numpy as np

from ..services.technical_analysis import TechnicalAnalysis as _TA
from ..storage import get_db
from ..utils import fail, ok, resolve_existing_security_code_async

logger = logging.getLogger(__name__)

_CLUSTER_TOLERANCE = 0.01  # 价位聚类容差 ±1%


def _extract_ohlcv(klines: list[dict]) -> dict[str, np.ndarray]:
    """从 K 线中提取 OHLCV numpy 数组。"""
    o = np.array([float(k.get("open", 0) or 0) for k in klines], dtype=np.float64)
    h = np.array([float(k.get("high", 0) or 0) for k in klines], dtype=np.float64)
    l = np.array([float(k.get("low", 0) or 0) for k in klines], dtype=np.float64)
    c = np.array([float(k.get("close", 0) or 0) for k in klines], dtype=np.float64)
    v = np.array([float(k.get("volume", 0) or 0) for k in klines], dtype=np.float64)
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


# ── 算法 1: Pivot Point ──────────────────────────────────────────────


def _calc_pivot_levels(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> list[dict]:
    """经典 Pivot Point: 取最近一根 K 线的 HLC 计算。"""
    hi, lo, cl = float(h[-1]), float(l[-1]), float(c[-1])
    pivot = (hi + lo + cl) / 3
    r1 = 2 * pivot - lo
    s1 = 2 * pivot - hi
    r2 = pivot + (hi - lo)
    s2 = pivot - (hi - lo)

    levels = []
    for price, label in [(s2, "S2"), (s1, "S1"), (r1, "R1"), (r2, "R2")]:
        levels.append({
            "price": round(price, 2),
            "type": "support" if label.startswith("S") else "resistance",
            "source": f"pivot_{label}",
        })
    return levels


# ── 算法 2: 成交密集区 ──────────────────────────────────────────────


def _calc_volume_clusters(
    c: np.ndarray, v: np.ndarray, *, n_bins: int = 50
) -> list[dict]:
    """用量价分布直方图找成交密集区 (简化版 KDE)。"""
    if len(c) < 20 or v.sum() == 0:
        return []

    price_min, price_max = float(c.min()), float(c.max())
    if price_max <= price_min:
        return []

    bin_edges = np.linspace(price_min, price_max, n_bins + 1)
    vol_profile = np.zeros(n_bins)

    for price, vol in zip(c, v):
        idx = int((price - price_min) / (price_max - price_min) * (n_bins - 1))
        idx = max(0, min(idx, n_bins - 1))
        vol_profile[idx] += vol

    # 找成交量峰值 (局部最大值)
    threshold = np.percentile(vol_profile, 75)
    levels = []
    for i in range(1, n_bins - 1):
        if (
            vol_profile[i] > threshold
            and vol_profile[i] >= vol_profile[i - 1]
            and vol_profile[i] >= vol_profile[i + 1]
        ):
            center = (bin_edges[i] + bin_edges[i + 1]) / 2
            current_price = float(c[-1])
            levels.append({
                "price": round(center, 2),
                "type": "support" if center < current_price else "resistance",
                "source": "volume_cluster",
            })
    return levels


# ── 算法 3: 斐波那契回撤 ──────────────────────────────────────────


def _calc_fibonacci_levels(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> list[dict]:
    """基于最近一波显著趋势的斐波那契回撤。"""
    if len(c) < 20:
        return []

    recent_high = float(h[-60:].max()) if len(h) >= 60 else float(h.max())
    recent_low = float(l[-60:].min()) if len(l) >= 60 else float(l.min())

    high_idx = int(np.argmax(h[-60:])) if len(h) >= 60 else int(np.argmax(h))
    low_idx = int(np.argmin(l[-60:])) if len(l) >= 60 else int(np.argmin(l))

    diff = recent_high - recent_low
    if diff < recent_low * 0.03:
        return []

    current = float(c[-1])
    fib_ratios = [0.236, 0.382, 0.5, 0.618, 0.786]
    levels = []

    if high_idx > low_idx:
        # 上涨趋势后回调: 从高点往下算回撤
        for ratio in fib_ratios:
            price = recent_high - diff * ratio
            levels.append({
                "price": round(price, 2),
                "type": "support" if price < current else "resistance",
                "source": f"fibonacci_{ratio}",
            })
    else:
        # 下跌趋势后反弹: 从低点往上算回撤
        for ratio in fib_ratios:
            price = recent_low + diff * ratio
            levels.append({
                "price": round(price, 2),
                "type": "support" if price < current else "resistance",
                "source": f"fibonacci_{ratio}",
            })

    return levels


# ── 算法 4: 前高前低聚类 (zigzag) ─────────────────────────────────


def _calc_swing_levels(
    h: np.ndarray, l: np.ndarray, c: np.ndarray, *, threshold: float = 0.05
) -> list[dict]:
    """用 zigzag 简化法识别显著拐点并聚类。"""
    if len(c) < 20:
        return []

    # 简化 zigzag: 找局部极值点
    swing_points: list[float] = []
    window = max(5, len(c) // 20)

    for i in range(window, len(c) - window):
        local_h = h[i - window : i + window + 1]
        local_l = l[i - window : i + window + 1]
        if h[i] == local_h.max():
            swing_points.append(float(h[i]))
        if l[i] == local_l.min():
            swing_points.append(float(l[i]))

    if not swing_points:
        return []

    # 聚类: 把相距 ±1% 的拐点合并
    swing_points.sort()
    clusters: list[list[float]] = [[swing_points[0]]]
    for p in swing_points[1:]:
        if (p - clusters[-1][-1]) / clusters[-1][-1] < _CLUSTER_TOLERANCE:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    current = float(c[-1])
    levels = []
    for cluster in clusters:
        center = np.mean(cluster)
        count = len(cluster)
        if count >= 2:
            levels.append({
                "price": round(float(center), 2),
                "type": "support" if center < current else "resistance",
                "source": f"swing_cluster(x{count})",
            })

    return levels


# ── 算法 5: 均线关键位 ────────────────────────────────────────────


def _calc_ma_levels(closes: list[float]) -> list[dict]:
    """MA5/MA10/MA20/MA60 的当前值。"""
    ta = _TA
    current = closes[-1] if closes else 0
    levels = []

    for period in (5, 10, 20, 60):
        if len(closes) < period:
            continue
        sma = ta.calculate_sma(closes, period)
        if sma and sma[-1] > 0:
            ma_val = round(sma[-1], 2)
            levels.append({
                "price": ma_val,
                "type": "support" if ma_val < current else "resistance",
                "source": f"MA{period}",
            })

    return levels


# ── 多方法投票与汇聚 ──────────────────────────────────────────────


_METHOD_WEIGHTS = {
    "pivot": 1.0,
    "volume_cluster": 1.5,
    "fibonacci": 1.0,
    "swing_cluster": 1.2,
    "MA": 0.8,
}


def _get_method_weight(source: str) -> float:
    for prefix, weight in _METHOD_WEIGHTS.items():
        if source.startswith(prefix):
            return weight
    return 1.0


def _merge_and_vote(
    all_levels: list[dict], current_price: float, *, tolerance: float = _CLUSTER_TOLERANCE
) -> list[dict]:
    """把不同算法的原始价位按 ±tolerance 汇聚，叠加强度。"""
    if not all_levels:
        return []

    all_levels.sort(key=lambda x: x["price"])
    clusters: list[list[dict]] = [[all_levels[0]]]

    for lv in all_levels[1:]:
        ref = np.mean([x["price"] for x in clusters[-1]])
        if abs(lv["price"] - ref) / max(ref, 1e-9) < tolerance:
            clusters[-1].append(lv)
        else:
            clusters.append([lv])

    result = []
    for cluster in clusters:
        price = round(float(np.mean([x["price"] for x in cluster])), 2)
        raw_strength = sum(_get_method_weight(x["source"]) for x in cluster)
        strength = min(5, max(1, round(raw_strength)))
        sources = [x["source"] for x in cluster]
        ltype = "support" if price < current_price else "resistance"
        distance_pct = round((price - current_price) / current_price * 100, 2)

        result.append({
            "price": price,
            "type": ltype,
            "strength": strength,
            "sources": sources,
            "distance_pct": distance_pct,
        })

    return result


def _add_breach_actions(levels: list[dict], current_price: float) -> list[dict]:
    """为每个关键位生成突破/跌破后的操作建议。"""
    for lv in levels:
        price = lv["price"]
        strength = lv["strength"]

        if lv["type"] == "support":
            breach_pct = round(price * 0.02, 2)
            breach_price = round(price - breach_pct, 2)
            if strength >= 4:
                lv["confirmation"] = f"需缩量企稳 2 日或放量反弹突破 {round(price * 1.02, 2)}"
                lv["breach_action"] = (
                    f"放量跌破 {breach_price} 确认破位→止损离场；"
                    f"缩量假破次日收回 {price} 以上→可补仓"
                )
            elif strength >= 2:
                lv["confirmation"] = f"收盘价站稳 {price} 上方即为有效"
                lv["breach_action"] = (
                    f"跌破 {breach_price} 后观察 1 日，"
                    f"无法收回则减仓；快速收回则视为洗盘"
                )
            else:
                lv["confirmation"] = "弱支撑，需其他信号配合"
                lv["breach_action"] = f"跌破 {breach_price} 需关注下一档支撑"
        else:
            breach_price = round(price * 1.02, 2)
            if strength >= 4:
                lv["confirmation"] = f"需放量突破且收盘站上 {price}，日成交额需高于 20 日均值 1.3 倍"
                lv["breach_action"] = (
                    f"有效突破后回踩不破 {round(price * 0.98, 2)} 可加仓；"
                    f"冲高回落收于 {price} 下方→阻力有效，暂不追涨"
                )
            elif strength >= 2:
                lv["confirmation"] = f"收盘价需站上 {price}"
                lv["breach_action"] = (
                    f"突破 {price} 后可轻仓跟进，止损设 {round(price * 0.97, 2)}"
                )
            else:
                lv["confirmation"] = "弱阻力，突破概率较高"
                lv["breach_action"] = f"突破后关注上一档阻力"

    return levels


# ── 主入口 ────────────────────────────────────────────────────────




async def _get_realtime_price_for_calibration(code: str) -> float | None:
    """安全获取实时价格用于价格校准。"""
    try:
        from ..services.market_data_access import FALLBACK_DB_ONLY, get_quote_snapshot_response
        rt = await get_quote_snapshot_response(code, fallback_mode=FALLBACK_DB_ONLY)
        if isinstance(rt, dict) and rt.get("success"):
            p = float(rt.get("data", {}).get("price", 0))
            return p if p > 0 else None
    except Exception:
        return None


def _calibrate_levels(result: dict, factor: float) -> dict:
    """对所有价位应用校准因子。"""
    import re

    def _replace(text: str) -> str:
        def _repl(m):
            val = float(m.group(0))
            return f"{val * factor:.2f}" if val > 10 else m.group(0)
        return re.sub(r'\d+\.\d+', _repl, text)

    result["current_price"] = round(result["current_price"] * factor, 2)

    for lv in result.get("levels", []):
        lv["price"] = round(lv["price"] * factor, 2)
        lv["distance_pct"] = round(
            (lv["price"] - result["current_price"]) / result["current_price"] * 100, 2,
        )
        for field in ("confirmation", "breach_action"):
            if field in lv and isinstance(lv[field], str):
                lv[field] = _replace(lv[field])

    tr = result.get("trading_range", {})
    if tr.get("nearest_support") is not None:
        tr["nearest_support"] = round(tr["nearest_support"] * factor, 2)
    if tr.get("nearest_resistance") is not None:
        tr["nearest_resistance"] = round(tr["nearest_resistance"] * factor, 2)

    return result


async def compute_key_levels(
    code: str,
    *,
    lookback_days: int = 120,
    klines: list[dict] | None = None,
) -> dict[str, Any]:
    """计算关键价位，返回结构化支撑/阻力清单。

    Args:
        klines: 外部传入的 K 线数据，传入时跳过内部获取，确保多工具共享同一份数据。
    """
    externally_provided = klines is not None

    if klines is None:
        from .db_freshness import ensure_fresh_klines

        klines, _fi = await ensure_fresh_klines(code, limit=lookback_days)

    if not klines or len(klines) < 20:
        return fail("K 线数据不足，无法计算关键价位")

    klines.sort(key=lambda k: k.get("date", ""))
    data = _extract_ohlcv(klines)
    closes_list = [float(c) for c in data["close"]]
    current_price = closes_list[-1]

    raw_levels: list[dict] = []
    raw_levels.extend(_calc_pivot_levels(data["high"], data["low"], data["close"]))
    raw_levels.extend(_calc_volume_clusters(data["close"], data["volume"]))
    raw_levels.extend(_calc_fibonacci_levels(data["high"], data["low"], data["close"]))
    raw_levels.extend(_calc_swing_levels(data["high"], data["low"], data["close"]))
    raw_levels.extend(_calc_ma_levels(closes_list))

    merged = _merge_and_vote(raw_levels, current_price)

    merged = [
        lv for lv in merged if abs(lv["distance_pct"]) <= 15
    ]

    merged.sort(key=lambda x: x["price"])
    _add_breach_actions(merged, current_price)

    supports = [lv for lv in merged if lv["type"] == "support"]
    resistances = [lv for lv in merged if lv["type"] == "resistance"]

    nearest_support = max(supports, key=lambda x: x["price"]) if supports else None
    nearest_resistance = min(resistances, key=lambda x: x["price"]) if resistances else None

    kline_date = klines[-1].get("date", "unknown") if klines else "unknown"

    output = {
        "code": code,
        "current_price": round(current_price, 2),
        "levels": merged,
        "trading_range": {
            "nearest_support": nearest_support["price"] if nearest_support else None,
            "nearest_resistance": nearest_resistance["price"] if nearest_resistance else None,
            "support_count": len(supports),
            "resistance_count": len(resistances),
        },
        "kline_count": len(klines),
        "kline_date": kline_date,
        "methods_used": ["pivot", "volume_cluster", "fibonacci", "swing_cluster", "MA"],
    }

    # 独立调用时自动价格校准（外部传入 klines 时由调用方负责）
    if not externally_provided:
        rt_price = await _get_realtime_price_for_calibration(code)
        if rt_price and current_price > 0:
            factor = rt_price / current_price
            if abs(factor - 1.0) > 0.02:
                output = _calibrate_levels(output, factor)
                output["price_calibration"] = {
                    "kline_close": round(current_price, 2),
                    "realtime_price": round(rt_price, 2),
                    "factor": round(factor, 4),
                    "calibrated": True,
                    "kline_date": kline_date,
                }
            else:
                output["price_calibration"] = {
                    "calibrated": False,
                    "kline_date": kline_date,
                }
        else:
            output["price_calibration"] = {
                "calibrated": False,
                "kline_date": kline_date,
            }

    return ok(output)


# ── MCP 注册 ──────────────────────────────────────────────────────


def register(mcp):
    """注册关键价位工具。"""

    @mcp.tool()
    async def get_key_levels(
        code: str,
        lookback_days: int = 120,
    ):
        """计算股票的支撑/阻力关键价位（多方法投票）

        返回分级支撑/阻力位，每个价位附带:
        - strength: 1-5 强度 (多算法汇聚越多越强)
        - sources: 来源算法列表
        - confirmation: 确认条件
        - breach_action: 突破/跌破后的操作建议

        算法: Pivot Point + 成交密集区 + 斐波那契回撤 + 前高前低聚类 + 均线关键位

        Args:
            code: 股票代码
            lookback_days: 回看天数 (默认120)
        """
        normalized_code, _, error = await resolve_existing_security_code_async(code=code)
        if error:
            return fail(error)
        return await compute_key_levels(normalized_code, lookback_days=lookback_days)
