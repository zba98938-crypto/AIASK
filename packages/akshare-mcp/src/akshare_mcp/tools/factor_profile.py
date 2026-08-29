"""因子画像工具。"""

from typing import Any

import numpy as np
from scipy import stats as sp_stats

from ..services.data_pipeline import build_cross_section_summary, compute_signal_hit_rate, normalize_klines
from ..storage import get_db
from ..services.factor_calculator import factor_calculator
from ..utils import ok, fail


# ── 因子名 → 计算逻辑映射 ──

def _compute_rsi_14(closes):
    return factor_calculator.calculate_rsi(closes, period=14, as_series=True)

def _compute_rsi_6(closes):
    return factor_calculator.calculate_rsi(closes, period=6, as_series=True)

def _compute_macd(closes):
    return factor_calculator.calculate_macd(closes, as_series=True)

def _compute_momentum_5d(closes):
    return factor_calculator.calculate_momentum(closes, period=5, as_series=True)

def _compute_momentum_20d(closes):
    return factor_calculator.calculate_momentum(closes, period=20, as_series=True)

def _compute_momentum_60d(closes):
    return factor_calculator.calculate_momentum(closes, period=60, as_series=True)


_FACTOR_REGISTRY = {
    "rsi": _compute_rsi_14,
    "rsi_14": _compute_rsi_14,
    "rsi_6": _compute_rsi_6,
    "macd": _compute_macd,
    "momentum": _compute_momentum_20d,
    "momentum_5d": _compute_momentum_5d,
    "momentum_20d": _compute_momentum_20d,
    "momentum_60d": _compute_momentum_60d,
}

_OVERSOLD_SIGNAL_MAP = {
    "rsi": {"signal": "rsi_oversold", "signal_params": {"period": 14, "threshold": 30}},
    "rsi_14": {"signal": "rsi_oversold", "signal_params": {"period": 14, "threshold": 30}},
    "rsi_6": {"signal": "rsi_oversold", "signal_params": {"period": 6, "threshold": 20}},
}


# ── 辅助计算 ──

def _percentile_of(series: np.ndarray, current: float, lookback: int) -> float | None:
    """计算 current 在 series[-lookback:] 中的百分位"""
    valid = series[-lookback:] if len(series) >= lookback else series
    valid = valid[~np.isnan(valid)]
    if len(valid) < 10:
        return None
    return round(float(sp_stats.percentileofscore(valid, current, kind="rank")), 1)


def _compute_trend(series: np.ndarray, window: int = 10) -> str:
    """用线性回归斜率判断趋势方向"""
    tail = series[-window:]
    tail = tail[~np.isnan(tail)]
    if len(tail) < 5:
        return "unknown"
    x = np.arange(len(tail), dtype=np.float64)
    slope, _, _, _, _ = sp_stats.linregress(x, tail)
    # 阈值：斜率的绝对值相对于序列标准差
    std = float(np.std(tail))
    if std < 1e-12:
        return "stable"
    normalized_slope = slope / std
    if normalized_slope > 0.15:
        return "strengthening"
    elif normalized_slope < -0.15:
        return "weakening"
    return "stable"


def _rolling_zscore(series: np.ndarray, window: int = 60) -> float | None:
    """计算滚动 z-score：(current - mean_window) / std_window"""
    valid = series[~np.isnan(series)]
    if len(valid) < 10:
        return None
    recent = valid[-window:] if len(valid) >= window else valid
    mean = float(np.mean(recent))
    std = float(np.std(recent))
    if std < 1e-12:
        return 0.0
    current = float(valid[-1])
    return round((current - mean) / std, 4)


def _build_factor_profile(series: np.ndarray, total_len: int) -> dict:
    """从因子序列构建画像 dict"""
    valid = series[~np.isnan(series)]
    if len(valid) == 0:
        return {
            "current": None,
            "series_30d": None,
            "percentile_1y": None,
            "percentile_3y": None,
            "trend": "unknown",
            "rolling_zscore": None,
            "industry_rank": None,
            "industry_total": None,
            "market_percentile": None,
            "historical_oversold_recovery": None,
        }

    current = round(float(valid[-1]), 4)
    # 近 30 日序列
    series_30d = valid[-30:].tolist() if len(valid) >= 1 else []
    series_30d = [round(v, 4) for v in series_30d]

    return {
        "current": current,
        "series_30d": series_30d,
        "percentile_1y": _percentile_of(series, current, lookback=250),
        "percentile_3y": _percentile_of(series, current, lookback=750),
        "trend": _compute_trend(series),
        "rolling_zscore": _rolling_zscore(series),
        "industry_rank": None,
        "industry_total": None,
        "market_percentile": None,
        "historical_oversold_recovery": None,
    }


def _series_last_value(series: np.ndarray) -> float | None:
    valid = series[~np.isnan(series)] if isinstance(series, np.ndarray) else np.array([], dtype=np.float64)
    if len(valid) == 0:
        return None
    return float(valid[-1])


def _factor_error_payload(message: str) -> dict:
    return {
        "current": None,
        "series_30d": None,
        "percentile_1y": None,
        "percentile_3y": None,
        "trend": "unknown",
        "rolling_zscore": None,
        "industry_rank": None,
        "industry_total": None,
        "market_percentile": None,
        "historical_oversold_recovery": None,
        "error": message,
    }


async def _fetch_peer_codes(db, code: str, industry: str) -> tuple[list[str], list[str]]:
    if not hasattr(db, "acquire"):
        return [], []
    try:
        async with db.acquire() as conn:
            # P3-B4 fix: stocks 表实际列名为 stock_code 而非 code(对话式复测发现)
            # 历史问题:silent OperationalError 导致 industry_total=1(应为 ~37 酿酒行业)
            # 修复:column rename + SELECT alias 让上层 row["code"] 仍然兼容
            market_rows = await conn.fetch(
                """SELECT stock_code AS code FROM stocks
                   WHERE stock_code <> $1
                   ORDER BY market_cap DESC NULLS LAST LIMIT 80""",
                code,
            )
            industry_rows = []
            if industry:
                industry_rows = await conn.fetch(
                    """SELECT stock_code AS code FROM stocks
                       WHERE industry = $1 AND stock_code <> $2
                       ORDER BY market_cap DESC NULLS LAST LIMIT 40""",
                    industry,
                    code,
                )
        industry_codes_db = [row["code"] for row in industry_rows]
        market_codes_db = [row["code"] for row in market_rows]

        # P3-5.12 fix: industry_total 取自 search.search_stocks(诊断报告 §5.12)
        # P3-B4 fix: 列名修复 + search_stocks 是 register_*() 闭包内的 @mcp.tool,
        #            module 级 import 拿不到。这里直接查 db 即可,无需走 MCP 工具调用
        # 历史问题:db.stocks.industry 字段填充不全(尤其新股),导致 industry_total 偏小
        # 修复:db 拿不到时降级到直接 SQL count 同行业作为 industry_total 上限参考
        if industry and len(industry_codes_db) < 5:
            try:
                async with db.acquire() as conn2:
                    # 不限制 LIMIT,只查代码不查全字段(避免大查询)
                    extra_rows = await conn2.fetch(
                        """SELECT stock_code AS code FROM stocks
                           WHERE industry LIKE $1 AND stock_code <> $2
                           ORDER BY market_cap DESC NULLS LAST LIMIT 50""",
                        f"%{industry}%",
                        code,
                    )
                extra_codes = [row["code"] for row in extra_rows if row["code"] not in industry_codes_db]
                if extra_codes:
                    industry_codes_db = (industry_codes_db + extra_codes)[:50]
            except Exception:
                pass

        return industry_codes_db, market_codes_db
    except Exception:
        return [], []


async def _collect_peer_factor_values(db, codes: list[str], lookback_days: int, compute_fn) -> list[float]:
    values = []
    for peer_code in codes:
        try:
            peer_klines = await db.get_klines(peer_code, limit=max(lookback_days, 250))
            ordered = normalize_klines(peer_klines)
            closes = [float(item.get("close", 0) or 0) for item in ordered if item.get("close") is not None]
            if len(closes) < 30:
                continue
            current = _series_last_value(compute_fn(closes))
            if current is not None and np.isfinite(current):
                values.append(float(current))
        except Exception:
            continue
    return values


# ── MCP 工具注册 ──

def register(mcp):
    """注册因子画像工具"""

    @mcp.tool()
    async def get_factor_profile(
        code: str,
        factors: str = "rsi,macd,momentum",
        lookback_days: int = 250,
    ):
        """
        因子画像 — 返回因子当前值 + 时间序列 + 历史分位 + 趋势 + z-score

        为 AI 提供完整的因子运行上下文，辅助投资决策推理。

        Args:
            code: 股票代码（如 000001, 600519）
            factors: 需要画像的因子列表（逗号分隔），可选值:
                rsi / rsi_14, rsi_6, macd, momentum / momentum_20d,
                momentum_5d, momentum_60d
            lookback_days: K 线回溯天数（默认 250，约 1 年交易日）

        Returns:
            dict: 包含每个因子的 current / series_30d / percentile_1y /
                  percentile_3y / trend / rolling_zscore

        Examples:
            get_factor_profile(code="000001")
            get_factor_profile(code="600519", factors="rsi,macd,momentum_60d")
        """
        try:
            db = get_db()
            klines = await db.get_klines(code, limit=max(lookback_days, 250))
            if not klines or len(klines) < 30:
                return fail(f"K 线数据不足（需要至少 30 条，实际 {len(klines) if klines else 0} 条）")

            ordered_klines = normalize_klines(klines)
            closes = [float(k.get("close", 0) or 0) for k in ordered_klines]
            stock_info = None
            try:
                stock_info = await db.get_stock_info(code)
            except Exception:
                stock_info = None
            industry = (stock_info or {}).get("industry", "")
            industry_codes, market_codes = await _fetch_peer_codes(db, code, industry)

            # 解析请求的因子列表
            requested = [f.strip().lower() for f in factors.split(",") if f.strip()]
            if not requested:
                requested = ["rsi", "macd", "momentum"]

            result_factors = {}
            for factor_name in requested:
                compute_fn = _FACTOR_REGISTRY.get(factor_name)
                if compute_fn is None:
                    result_factors[factor_name] = _factor_error_payload(f"不支持的因子: {factor_name}")
                    continue

                try:
                    series = compute_fn(closes)
                    profile = _build_factor_profile(series, len(closes))
                    current = profile.get("current")
                    industry_summary = build_cross_section_summary(
                        current,
                        await _collect_peer_factor_values(db, industry_codes, lookback_days, compute_fn),
                    )
                    market_summary = build_cross_section_summary(
                        current,
                        await _collect_peer_factor_values(db, market_codes, lookback_days, compute_fn),
                    )
                    profile["industry_rank"] = industry_summary.get("rank")
                    profile["industry_total"] = industry_summary.get("total")
                    profile["market_percentile"] = market_summary.get("percentile")
                    oversold_meta = _OVERSOLD_SIGNAL_MAP.get(factor_name)
                    if oversold_meta:
                        hit_rate = compute_signal_hit_rate(
                            ordered_klines,
                            signal=oversold_meta["signal"],
                            forward_days=[5, 10],
                            signal_params=oversold_meta["signal_params"],
                        )
                        profile["historical_oversold_recovery"] = {
                            "sample_count": hit_rate.get("sample_count", 0),
                            "5d": hit_rate.get("forward_returns", {}).get("5d", {}),
                            "10d": hit_rate.get("forward_returns", {}).get("10d", {}),
                        }
                    result_factors[factor_name] = profile
                except Exception as e:
                    result_factors[factor_name] = _factor_error_payload(str(e))

            return ok({
                "code": code,
                "kline_count": len(closes),
                "industry": industry,
                "factors": result_factors,
            })

        except Exception as e:
            return fail(str(e))
