"""决策工具 — 投资分析数据汇聚（纯数据模式）。"""

import statistics

from ..storage import get_db
from ..services.factor_calculator import factor_calculator
from ..utils import ok, fail, resolve_existing_security_code_async


# ═══════════════════════════════════════════════════════════════════
#  Phase 2: 纯数据汇聚工具 — AI 自行推理，MCP 只提供数据原材料
# ═══════════════════════════════════════════════════════════════════

async def get_investment_analysis(code: str) -> dict:
    """
    投资分析数据汇聚（纯数据模式，不做评分/推荐）。

    返回结构化的多维度数据，供 AI 自行分析判断：
    - 价格上下文（当前价、多周期涨跌幅、52周高低点位置）
    - 估值数据（PE/PB 及历史分位）
    - 基本面指标（ROE/负债率/增速）
    - 技术面指标（RSI/MACD/均线排列/支撑阻力）
    - 动量因子
    - 风险指标（波动率/最大回撤）
    """
    try:
        code, _, error = await resolve_existing_security_code_async(code=code)
        if error:
            return fail(error)
        db = get_db()
        result = {}
        info = None

        # ── 1. K 线与价格上下文 ──
        klines = await db.get_klines(code, limit=250)
        if not klines:
            return fail("无 K 线数据")

        closes = [float(k.get('close', 0)) for k in klines]
        highs = [float(k.get('high', 0)) for k in klines]
        lows = [float(k.get('low', 0)) for k in klines]
        volumes = [float(k.get('volume', 0)) for k in klines]
        current_price = closes[-1] if closes else 0

        def _pct_change(data, days):
            if len(data) <= days:
                return None
            old = float(data[-(days + 1)])
            latest = float(data[-1])
            return round((latest - old) / old * 100, 2) if old > 0 else None

        high_52w = max(highs[:min(250, len(highs))]) if highs else 0
        low_52w = min(lows[:min(250, len(lows))]) if lows else 0
        position_52w = None
        if high_52w > low_52w > 0:
            position_52w = round((current_price - low_52w) / (high_52w - low_52w) * 100, 1)

        result["price_context"] = {
            "current_price": current_price,
            "change_1d_pct": _pct_change(closes, 1),
            "change_5d_pct": _pct_change(closes, 5),
            "change_20d_pct": _pct_change(closes, 20),
            "change_60d_pct": _pct_change(closes, 60),
            "high_52w": high_52w,
            "low_52w": low_52w,
            "position_in_52w_range_pct": position_52w,
            "analysis_date": klines[-1].get('date', '') if klines else '',
        }

        # ── 2. 估值数据 ──
        valuation = {}
        try:
            info = await db.get_stock_info(code)
            if info:
                for key in ['pe', 'pb', 'ps', 'total_mv', 'circ_mv', 'pe_ratio', 'pb_ratio', 'market_cap']:
                    val = info.get(key)
                    if val is not None:
                        try:
                            normalized_key = {
                                'pe_ratio': 'pe',
                                'pb_ratio': 'pb',
                                'market_cap': 'total_mv',
                            }.get(key, key)
                            valuation[normalized_key] = round(float(val), 2)
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass
        result["basic_info"] = {
            "code": code,
            "name": (info or {}).get("name") or (info or {}).get("stock_name", ""),
            "industry": (info or {}).get("industry", ""),
            "market_cap": valuation.get("total_mv"),
            "list_date": (info or {}).get("list_date"),
        }
        if (info or {}).get("industry") and hasattr(db, 'acquire'):
            try:
                async with db.acquire() as conn:
                    peer_rows = await conn.fetch(
                        """SELECT pe_ratio, pb_ratio FROM stocks WHERE industry = $1 AND code <> $2 AND pe_ratio IS NOT NULL""",
                        info["industry"],
                        code,
                    )
                peer_pes = [float(row['pe_ratio']) for row in peer_rows if row.get('pe_ratio')]
                peer_pbs = [float(row['pb_ratio']) for row in peer_rows if row.get('pb_ratio')]
                if peer_pes:
                    valuation["industry_median_pe"] = round(float(statistics.median(peer_pes)), 2)
                    if valuation.get("pe"):
                        valuation["industry_relative_pe"] = round(float(valuation["pe"]) / float(valuation["industry_median_pe"]), 4)
                if peer_pbs:
                    valuation["industry_median_pb"] = round(float(statistics.median(peer_pbs)), 2)
            except Exception:
                pass
        result["valuation"] = valuation

        # ── 3. 基本面指标 ──
        fundamentals = {}
        try:
            fin = None
            fin_rows = []
            if hasattr(db, 'get_financial_data'):
                fin = await db.get_financial_data(code)
            if not fin and hasattr(db, 'get_financials'):
                fin_rows = await db.get_financials(code)
                fin = fin_rows[0] if fin_rows else None
            if fin:
                aliases = {
                    'roe': ['roe'],
                    'roa': ['roa'],
                    'debt_ratio': ['debt_ratio'],
                    'gross_margin': ['gross_margin'],
                    'revenue_yoy': ['revenue_yoy', 'revenue_growth'],
                    'profit_yoy': ['profit_yoy', 'profit_growth'],
                    'eps': ['eps'],
                    'bps': ['bps', 'bvps'],
                }
                for key, candidates in aliases.items():
                    val = next((fin.get(name) for name in candidates if fin.get(name) is not None), None)
                    if val is not None:
                        try:
                            fundamentals[key] = round(float(val), 2)
                        except (ValueError, TypeError):
                            pass
                fundamentals["report_count"] = len(fin_rows) if isinstance(fin_rows, list) else 1
        except Exception:
            pass
        result["fundamentals"] = fundamentals

        # ── 4. 技术面指标（计算值，无评分） ──
        technical = {}
        if len(closes) >= 15:
            try:
                technical["rsi_14"] = round(factor_calculator.calculate_rsi(closes, 14), 2)
            except Exception:
                pass
            try:
                technical["rsi_6"] = round(factor_calculator.calculate_rsi(closes, 6), 2)
            except Exception:
                pass
        if len(closes) >= 26:
            try:
                technical["macd"] = round(factor_calculator.calculate_macd(closes), 4)
            except Exception:
                pass
            try:
                technical["macd_signal"] = round(factor_calculator.calculate_macd_signal(closes), 4)
            except Exception:
                pass
            try:
                technical["macd_hist"] = round(factor_calculator.calculate_macd_hist(closes), 4)
            except Exception:
                pass

        # 均线排列
        ma_data = {}
        for period in [5, 10, 20, 60, 120]:
            if len(closes) >= period:
                import numpy as np
                ma_data[f"ma{period}"] = round(float(np.mean(closes[-period:])), 2)
        technical["moving_averages"] = ma_data
        if ma_data.get("ma20") and ma_data.get("ma60"):
            if current_price > ma_data["ma20"] > ma_data["ma60"]:
                technical["ma_alignment"] = "bullish"
            elif current_price < ma_data["ma20"] < ma_data["ma60"]:
                technical["ma_alignment"] = "bearish"
            else:
                technical["ma_alignment"] = "mixed"

        # 支撑/阻力（近 60 日高低点）
        if len(highs) >= 60 and len(lows) >= 60:
            technical["resistance_60d"] = max(highs[-60:])
            technical["support_60d"] = min(lows[-60:])

        result["technical"] = technical

        # ── 5. 动量因子 ──
        momentum = {}
        for days, label in [(5, "5d"), (10, "10d"), (20, "20d"), (60, "60d")]:
            if len(closes) > days:
                try:
                    momentum[f"mom_{label}"] = round(
                        factor_calculator.calculate_momentum(closes, days), 4
                    )
                except Exception:
                    pass
        market_regime = "neutral"
        if momentum.get("mom_20d") is not None:
            if momentum["mom_20d"] >= 0.05:
                market_regime = "bullish"
            elif momentum["mom_20d"] <= -0.05:
                market_regime = "bearish"
        momentum["market_regime"] = market_regime
        result["momentum"] = momentum

        # ── 6. 风险指标 ──
        risk = {}
        if len(closes) >= 21:
            try:
                risk["volatility_20d"] = round(
                    factor_calculator.calculate_volatility(closes, 20), 4
                )
            except Exception:
                pass
        if len(closes) >= 61:
            try:
                risk["volatility_60d"] = round(
                    factor_calculator.calculate_volatility(closes, 60), 4
                )
            except Exception:
                pass
        # 最大回撤（近 250 日）
        if len(closes) >= 20:
            import numpy as np
            cum_prices = np.array(closes[-min(250, len(closes)):])
            peak = np.maximum.accumulate(cum_prices)
            dd = (peak - cum_prices) / peak
            risk["max_drawdown_250d"] = round(float(np.max(dd)), 4)
        if len(closes) >= 21:
            import numpy as np
            recent_prices = np.array(closes[-21:])
            rets = np.diff(recent_prices) / np.maximum(recent_prices[:-1], 1e-12)
            neg_rets = rets[rets < 0]
            if len(neg_rets) > 0:
                risk["downside_volatility_20d"] = round(float(np.std(neg_rets)), 4)

        result["risk"] = risk

        return ok(result)

    except Exception as e:
        return fail(str(e))
