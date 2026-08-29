"""Quant engine: data helpers, prefetch, factor calculation, trading cost functions."""

import asyncio
import time
from typing import Any, Dict, List, Optional

import numpy as np

from ..services.slippage import SlippageCalculator, SlippageModelType
from ..services.factor_calculator import factor_calculator
from ..services.factor_analysis import FactorAnalyzer as ICFactorAnalyzer
from ..storage import get_db

from .quant_definitions import (
    DEFAULT_FACTOR_LOOKBACK,
    MARKET_CAP_KEYS,
    PROFIT_GROWTH_KEYS,
    REVENUE_GROWTH_KEYS,
    SUPPORTED_FACTORS,
    _PERF_STAGE_KEYS,
    _QUANT_BATCH_FETCH_ENABLED,
    _QUANT_PREFETCH_CONCURRENCY,
    _normalize_factor_name,
    _safe_float,
)


# ── Slippage model map ──

_SLIPPAGE_MODEL_MAP = {
    "fixed": SlippageModelType.FIXED,
    "volume_based": SlippageModelType.VOLUME_BASED,
    "market_impact": SlippageModelType.MARKET_IMPACT,
}


# ── Performance tracking helpers ──

def _new_run_cache() -> Dict[str, Any]:
    return {
        "panels": {},
        "stats": {
            "panel_cache_hits": 0,
            "panel_cache_misses": 0,
            "duplicate_reads": 0,
        },
    }


def _new_perf_tracker(enabled: bool = True) -> Dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "timings": {k: 0.0 for k in _PERF_STAGE_KEYS},
    }


def _perf_add(perf: Dict[str, Any], stage: str, elapsed: float) -> None:
    if not isinstance(perf, dict):
        return
    if not bool(perf.get("enabled", False)):
        return
    if stage not in perf.get("timings", {}):
        return
    perf["timings"][stage] = float(perf["timings"][stage]) + max(0.0, float(elapsed))


# ── Market panel construction ──

def _get_or_build_market_panel(
    run_cache: Dict[str, Any],
    code: str,
    klines: List[Dict[str, Any]],
    *,
    chronological: bool,
    include_volume: bool = False,
    include_returns: bool = False,
) -> Dict[str, Any]:
    cache = run_cache.setdefault("panels", {})
    stats_meta = run_cache.setdefault(
        "stats",
        {"panel_cache_hits": 0, "panel_cache_misses": 0, "duplicate_reads": 0},
    )
    key = f"{str(code)}|c={int(chronological)}|v={int(include_volume)}|r={int(include_returns)}"

    cached = cache.get(key)
    if isinstance(cached, dict):
        stats_meta["panel_cache_hits"] = int(stats_meta.get("panel_cache_hits", 0)) + 1
        stats_meta["duplicate_reads"] = int(stats_meta.get("duplicate_reads", 0)) + 1
        return cached

    arrs = ICFactorAnalyzer.klines_to_ndarrays(
        klines=klines,
        chronological=chronological,
        include_volume=include_volume,
        include_returns=include_returns,
    )
    closes_arr = arrs.get("closes")
    if not isinstance(closes_arr, np.ndarray):
        closes_arr = np.asarray(closes_arr or [], dtype=np.float64)
    closes_arr = closes_arr.astype(np.float64, copy=False)

    panel = {
        "closes_arr": closes_arr,
        "closes": closes_arr.tolist(),
    }

    if include_volume:
        volumes_arr = arrs.get("volumes")
        if not isinstance(volumes_arr, np.ndarray):
            volumes_arr = np.asarray(volumes_arr or [], dtype=np.float64)
        volumes_arr = volumes_arr.astype(np.float64, copy=False)
        if volumes_arr.shape[0] != closes_arr.shape[0]:
            volumes_arr = np.zeros(closes_arr.shape[0], dtype=np.float64)
        panel["volumes_arr"] = volumes_arr
        panel["volumes"] = volumes_arr.tolist()

    if include_returns:
        returns_arr = arrs.get("returns")
        if not isinstance(returns_arr, np.ndarray):
            returns_arr = np.asarray(returns_arr or [], dtype=np.float64)
        returns_arr = returns_arr.astype(np.float64, copy=False)
        panel["returns_arr"] = returns_arr
        panel["returns"] = returns_arr.tolist()

    cache[key] = panel
    stats_meta["panel_cache_misses"] = int(stats_meta.get("panel_cache_misses", 0)) + 1
    return panel


# ── Perf breakdown builder ──

def _build_perf_breakdown(
    perf: Dict[str, Any],
    *,
    prefetch_meta: Optional[Dict[str, Any]] = None,
    run_cache: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not bool((perf or {}).get("enabled", False)):
        return None

    timings_raw = (perf or {}).get("timings", {})
    timings = {k: round(float(timings_raw.get(k, 0.0)), 6) for k in _PERF_STAGE_KEYS}
    total_seconds = round(float(sum(timings.values())), 6)

    prefetch = prefetch_meta or {}
    memo_hits = prefetch.get("memo_hits") if isinstance(prefetch.get("memo_hits"), dict) else {}
    run_stats = (run_cache or {}).get("stats") if isinstance(run_cache, dict) else {}
    run_stats = run_stats if isinstance(run_stats, dict) else {}

    kline_req = int(prefetch.get("kline_batch_hits", 0)) + int(prefetch.get("kline_single_fetches", 0))
    stock_req = int(prefetch.get("stock_info_fetches", 0))
    fin_req = int(prefetch.get("financial_fetches", 0))
    total_req = int(kline_req + stock_req + fin_req)

    return {
        "enabled": True,
        "timings": timings,
        "total_seconds": total_seconds,
        "data_stats": {
            "request_counts": {
                "kline_requests": int(kline_req),
                "stock_info_requests": int(stock_req),
                "financial_requests": int(fin_req),
                "total_requests": int(total_req),
            },
            "batch_hits": {
                "kline_batch_hits": int(prefetch.get("kline_batch_hits", 0)),
                "kline_batch_used": bool(prefetch.get("kline_batch_used", False)),
            },
            "repeated_reads": {
                "prefetch_memo_hits": int(sum(int(v or 0) for v in memo_hits.values())) if memo_hits else 0,
                "panel_cache_hits": int(run_stats.get("panel_cache_hits", 0)),
                "duplicate_reads": int(run_stats.get("duplicate_reads", 0)),
            },
        },
    }


# ── Financial data helpers ──

_FACTOR_MIN_HISTORY: Dict[str, int] = {
    "momentum": 2,
    "trend": 3,
    "reversal": 2,
    "volatility": 4,
    "mom_1d": 2,
    "mom_5d": 6,
    "mom_10d": 11,
    "mom_60d": 61,
    "rsi_6": 7,
    "rsi_14": 15,
    "macd_signal": 26,
    "macd_histogram": 26,
    "willr_14": 14,
    "cci_20": 20,
    "mfi_14": 15,
    "stoch_k": 14,
    "stoch_d": 16,
    "roc_10": 11,
    "roc_20": 21,
    "vol_5d": 6,
    "vol_10d": 11,
    "vol_60d": 61,
    "atr_14": 15,
    "atr_20": 21,
    "bollinger_width": 20,
    "downside_vol": 21,
    "volume_ratio": 21,
    "obv_slope": 20,
    "vwap_deviation": 20,
    "turnover_5d": 20,
    "turnover_20d": 21,
}


def _minimum_factor_history(factor: str) -> int:
    factor_name = _normalize_factor_name(factor)
    return int(_FACTOR_MIN_HISTORY.get(factor_name, 2))


def _latest_financial_row(financials: Any) -> Optional[Dict[str, Any]]:
    if isinstance(financials, list):
        for item in financials:
            if isinstance(item, dict):
                return item
        return None
    if isinstance(financials, dict):
        return financials
    return None


def _extract_profit_growth(financial: Dict[str, Any]) -> float:
    for key in ("profit_growth", "profit_growth_yoy", "net_profit_growth", "revenue_growth"):
        val = _safe_float(financial.get(key), 0.0)
        if val != 0.0:
            return val
    return 0.0


def _first_valid_float(
    payload: Optional[Dict[str, Any]],
    keys: Any,
    *,
    positive_only: bool = False,
) -> Optional[float]:
    if not isinstance(payload, dict):
        return None

    for key in keys:
        if key not in payload:
            continue
        raw = payload.get(key)
        if raw is None:
            continue
        val = _safe_float(raw, float("nan"))
        if not np.isfinite(val):
            continue
        if positive_only and val <= 0:
            continue
        return float(val)

    return None


def _extract_style_exposures(
    stock_info: Optional[Dict[str, Any]],
    financial: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """从 stock_info/financial 中提取行业、市值、beta 风格暴露（有则用，无则降级）。"""
    info = stock_info or {}
    fin = financial or {}

    industry = info.get("industry") or fin.get("industry")

    market_cap = None
    for key in (
        "market_cap",
        "total_market_cap",
        "total_mv",
        "circ_mv",
        "float_market_cap",
        "mkt_cap",
    ):
        market_cap = _safe_float(info.get(key), 0.0)
        if market_cap > 0:
            break
        market_cap = _safe_float(fin.get(key), 0.0)
        if market_cap > 0:
            break
    if market_cap is not None and market_cap <= 0:
        market_cap = None

    beta = None
    for key in ("beta", "beta_1y", "beta_250d", "beta_60d"):
        candidate = info.get(key, fin.get(key))
        if candidate is not None:
            beta = _safe_float(candidate, 0.0)
            break

    return {
        "industry": industry,
        "market_cap": market_cap,
        "beta": beta,
    }


# ── Tradability helpers ──

def _limit_ratio_from_code(code: str) -> float:
    c = str(code or "").strip()
    for prefix in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
        if c.startswith(prefix):
            c = c[len(prefix):]
            break
    if c.startswith("300") or c.startswith("301") or c.startswith("688"):
        return 0.20
    return 0.10


def _build_tradability_mask_local(
    closes: np.ndarray,
    volumes: Optional[np.ndarray] = None,
    *,
    code: str = "",
    is_st: bool = False,
) -> np.ndarray:
    n = len(closes)
    mask = np.ones(n, dtype=bool)
    if n == 0:
        return mask

    if volumes is not None and len(volumes) == n:
        mask &= volumes > 0

    limit_ratio = 0.05 if is_st else _limit_ratio_from_code(code)
    tolerance = 0.002
    for i in range(1, n):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        change = (closes[i] - prev) / prev
        if change >= limit_ratio - tolerance or change <= -(limit_ratio - tolerance):
            mask[i] = False
    return mask


# ── Trading cost computation ──

def _compute_trade_return_with_costs(
    *,
    entry_price: float,
    exit_price: float,
    entry_volume: float,
    exit_volume: float,
    commission: float,
    slippage: float,
    slippage_calc: Optional[SlippageCalculator],
) -> Optional[Dict[str, float]]:
    if entry_price <= 0 or exit_price <= 0:
        return None

    commission = max(0.0, float(commission or 0.0))
    slippage = max(0.0, float(slippage or 0.0))
    order_size = 1.0

    if slippage_calc is not None:
        buy_slip = slippage_calc.calculate(
            price=float(entry_price),
            volume=float(entry_volume),
            order_size=order_size,
            is_buy=True,
        )
        sell_slip = slippage_calc.calculate(
            price=float(exit_price),
            volume=float(exit_volume),
            order_size=order_size,
            is_buy=False,
        )
        buy_exec = float(buy_slip.get("execution_price", entry_price))
        sell_exec = float(sell_slip.get("execution_price", exit_price))
        slip_buy_rate = max(0.0, (buy_exec - entry_price) / entry_price) if entry_price > 0 else 0.0
        slip_sell_rate = max(0.0, (exit_price - sell_exec) / exit_price) if exit_price > 0 else 0.0
        impact_cost_rate = float(slip_buy_rate + slip_sell_rate)
    else:
        buy_exec = float(entry_price * (1 + slippage))
        sell_exec = float(exit_price * (1 - slippage))
        impact_cost_rate = float(max(0.0, slippage * 2.0))

    net_entry = buy_exec * (1 + commission)
    net_exit = sell_exec * (1 - commission)
    if net_entry <= 0:
        return None

    net_return = float(net_exit / net_entry - 1.0)
    return {
        "net_return": net_return,
        "impact_cost_rate": impact_cost_rate,
        "transaction_cost_rate": float(impact_cost_rate + commission * 2.0),
    }


# ── Prefetch helpers ──

def _normalize_codes_for_prefetch(codes: List[Any]) -> List[str]:
    seen = set()
    normalized: List[str] = []
    for code in codes or []:
        code_str = str(code or "").strip()
        if not code_str or code_str in seen:
            continue
        seen.add(code_str)
        normalized.append(code_str)
    return normalized


async def _prefetch_market_data(
    db: Any,
    codes: List[Any],
    *,
    need_financials: bool,
    kline_limit: int,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    fetch_concurrency: Optional[int] = None,
    memo: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """P0: 批量优先 + 并发回退的统一预取层，减少重复 DB 查询。"""
    code_list = _normalize_codes_for_prefetch(codes)
    cache = memo if isinstance(memo, dict) else {}
    concurrency = max(1, min(int(fetch_concurrency or _QUANT_PREFETCH_CONCURRENCY), 20))

    result: Dict[str, Dict[str, Any]] = {code: {} for code in code_list}
    meta: Dict[str, Any] = {
        "codes": len(code_list),
        "batch_enabled": bool(_QUANT_BATCH_FETCH_ENABLED),
        "fetch_concurrency": int(concurrency),
        "kline_batch_used": False,
        "kline_batch_hits": 0,
        "kline_single_fetches": 0,
        "stock_info_fetches": 0,
        "financial_fetches": 0,
        "memo_hits": {"klines": 0, "stock_info": 0, "financial": 0},
    }

    for code in code_list:
        cached = cache.get(code)
        if not isinstance(cached, dict):
            continue
        for key in ("klines", "stock_info", "financial"):
            if key in cached:
                result[code][key] = cached.get(key)
                meta["memo_hits"][key] += 1

    # 1) Kline：优先批量接口
    missing_klines = [c for c in code_list if "klines" not in result[c]]
    if missing_klines and _QUANT_BATCH_FETCH_ENABLED:
        batch_method = getattr(db, "get_klines_batch", None)
        if callable(batch_method):
            try:
                limit_arg = int(kline_limit) if int(kline_limit or 0) > 0 else None
                batch_rows = await batch_method(missing_klines, start_date, end_date, limit_arg)
                meta["kline_batch_used"] = True
                rows_dict = batch_rows if isinstance(batch_rows, dict) else {}
                for code in missing_klines:
                    rows = rows_dict.get(code, [])
                    if isinstance(rows, list) and rows:
                        result[code]["klines"] = rows
                        meta["kline_batch_hits"] += 1
            except Exception:
                pass

    # 2) Kline：并发回退
    missing_klines = [c for c in code_list if "klines" not in result[c]]
    if missing_klines:
        semaphore = asyncio.Semaphore(concurrency)

        async def _fetch_kline_one(code: str) -> tuple[str, List[Dict[str, Any]]]:
            async with semaphore:
                try:
                    if start_date is not None or end_date is not None:
                        rows = await db.get_klines(code, start_date, end_date, kline_limit)
                    else:
                        rows = await db.get_klines(code, limit=kline_limit)
                except Exception:
                    rows = []
                return code, rows if isinstance(rows, list) else []

        batch = await asyncio.gather(*[_fetch_kline_one(code) for code in missing_klines], return_exceptions=True)
        for item in batch:
            if isinstance(item, Exception):
                continue
            code, rows = item
            result[code]["klines"] = rows
            meta["kline_single_fetches"] += 1

    # 3) stock_info 并发获取（每个 run 内 memo 复用）
    missing_infos = [c for c in code_list if "stock_info" not in result[c]]
    if missing_infos:
        semaphore = asyncio.Semaphore(concurrency)

        async def _fetch_info_one(code: str) -> tuple[str, Any]:
            async with semaphore:
                try:
                    payload = await db.get_stock_info(code)
                except Exception:
                    payload = None
                return code, payload

        batch = await asyncio.gather(*[_fetch_info_one(code) for code in missing_infos], return_exceptions=True)
        for item in batch:
            if isinstance(item, Exception):
                continue
            code, payload = item
            result[code]["stock_info"] = payload
            meta["stock_info_fetches"] += 1

    # 4) financial 并发获取（仅在因子需要时）
    if need_financials:
        missing_fin = [c for c in code_list if "financial" not in result[c]]
        if missing_fin:
            semaphore = asyncio.Semaphore(concurrency)

            async def _fetch_fin_one(code: str) -> tuple[str, Optional[Dict[str, Any]]]:
                async with semaphore:
                    try:
                        payload = _latest_financial_row(await db.get_financials(code, limit=1))
                    except Exception:
                        payload = None
                    return code, payload

            batch = await asyncio.gather(*[_fetch_fin_one(code) for code in missing_fin], return_exceptions=True)
            for item in batch:
                if isinstance(item, Exception):
                    continue
                code, payload = item
                result[code]["financial"] = payload
                meta["financial_fetches"] += 1

    # 5) 标准化输出并写回 memo（同请求内复用）
    for code in code_list:
        code_data = result.setdefault(code, {})
        if "klines" not in code_data or not isinstance(code_data.get("klines"), list):
            code_data["klines"] = []
        if "stock_info" not in code_data:
            code_data["stock_info"] = None
        if "financial" not in code_data:
            code_data["financial"] = None

        bucket = cache.setdefault(code, {})
        bucket["klines"] = code_data["klines"]
        bucket["stock_info"] = code_data["stock_info"]
        bucket["financial"] = code_data["financial"]

    return {"data": result, "meta": meta}


# ── EMA helper ──

def _ema(data: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average."""
    alpha = 2.0 / (period + 1)
    result = np.empty_like(data)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result


# ── Factor value calculation engine ──

def _calculate_factor_value(
    factor: str,
    closes: list,
    financial: Optional[Dict[str, Any]] = None,
    stock_info: Optional[Dict[str, Any]] = None,
    period: int = DEFAULT_FACTOR_LOOKBACK,
) -> Optional[float]:
    factor_name = _normalize_factor_name(factor)

    if factor_name == "momentum":
        if len(closes) < 2:
            return None
        lookback = max(2, min(int(period), len(closes)))
        return float(factor_calculator.calculate_momentum(closes, period=lookback))

    if factor_name == "volatility":
        if len(closes) < 4:
            return None
        lookback = max(3, min(int(period), len(closes) - 1))
        try:
            vol = float(factor_calculator.calculate_volatility(closes, period=lookback))
            if np.isfinite(vol):
                return vol
        except Exception:
            vol = None

        # Fallback: realized volatility over log-return approximation window.
        window = np.array(closes[-(lookback + 1) :], dtype=np.float64)
        if window.size < 3:
            return None
        prev = window[:-1]
        curr = window[1:]
        valid = prev > 0
        if int(np.sum(valid)) < 2:
            return None
        rets = (curr[valid] - prev[valid]) / prev[valid]
        if rets.size < 2:
            return None
        return float(np.std(rets, ddof=1) * np.sqrt(252.0))

    if factor_name == "trend":
        if len(closes) < 3:
            return None
        lookback = max(3, min(int(period), len(closes)))
        return float(factor_calculator.calculate_trend_factor(closes, period=lookback))

    if factor_name == "reversal":
        if len(closes) < 2:
            return None
        lookback = max(2, min(int(period), len(closes), 10))
        return float(factor_calculator.calculate_reversal(closes, period=lookback))

    if factor_name == "value":
        if not financial:
            return None
        pe = _safe_float(financial.get("pe_ratio"), 0.0)
        pb = _safe_float(financial.get("pb_ratio"), 0.0)
        ps = _safe_float(financial.get("ps_ratio"), 0.0)
        if pe <= 0 and pb <= 0 and ps <= 0:
            return None
        return float(factor_calculator.calculate_value_factor(pe, pb, ps if ps > 0 else None))

    if factor_name == "quality":
        if not financial:
            return None
        roe = _safe_float(financial.get("roe"), 0.0)
        debt_ratio = _safe_float(financial.get("debt_ratio"), 1.0)
        growth = _extract_profit_growth(financial)
        return float(factor_calculator.calculate_quality_factor(roe, debt_ratio, growth if growth != 0 else None))

    if factor_name == "growth":
        if not financial:
            return None
        revenue_growth = _first_valid_float(financial, REVENUE_GROWTH_KEYS)
        profit_growth = _first_valid_float(financial, PROFIT_GROWTH_KEYS)
        if revenue_growth is None and profit_growth is None:
            return None
        return float(factor_calculator.calculate_growth_factor(revenue_growth, profit_growth))

    if factor_name == "size":
        market_cap = _first_valid_float(stock_info, MARKET_CAP_KEYS, positive_only=True)
        if market_cap is None:
            market_cap = _first_valid_float(financial, MARKET_CAP_KEYS, positive_only=True)
        if market_cap is None:
            return None
        return float(factor_calculator.calculate_size_factor(market_cap))

    # ── 短周期动量 ──
    if factor_name in ("mom_1d", "mom_5d", "mom_10d", "mom_60d"):
        period_map = {"mom_1d": 1, "mom_5d": 5, "mom_10d": 10, "mom_60d": 60}
        p = period_map[factor_name]
        if len(closes) < p + 1:
            return None
        return float((closes[-1] - closes[-(p + 1)]) / closes[-(p + 1)]) if closes[-(p + 1)] > 0 else None

    # ── RSI ──
    if factor_name in ("rsi_14", "rsi_6"):
        rsi_period = 14 if factor_name == "rsi_14" else 6
        if len(closes) < rsi_period + 1:
            return None
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0) for d in deltas[-rsi_period:]]
        losses = [max(-d, 0) for d in deltas[-rsi_period:]]
        avg_gain = sum(gains) / rsi_period
        avg_loss = sum(losses) / rsi_period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100.0 - 100.0 / (1.0 + rs))

    # ── MACD ──
    if factor_name in ("macd_signal", "macd_histogram"):
        if len(closes) < 26:
            return None
        arr = np.array(closes, dtype=np.float64)
        ema12 = _ema(arr, 12)
        ema26 = _ema(arr, 26)
        dif = ema12 - ema26
        dea = _ema(dif, 9)
        if factor_name == "macd_signal":
            return float(dea[-1])
        return float((dif[-1] - dea[-1]) * 2)

    # ── Williams %R ──
    if factor_name == "willr_14":
        if len(closes) < 14:
            return None
        window = closes[-14:]
        highest = max(window)
        lowest = min(window)
        if highest == lowest:
            return 0.0
        return float((highest - closes[-1]) / (highest - lowest) * -100)

    # ── CCI ──
    if factor_name == "cci_20":
        if len(closes) < 20:
            return None
        window = closes[-20:]
        tp = sum(window) / len(window)
        md = sum(abs(c - tp) for c in window) / len(window)
        if md == 0:
            return 0.0
        return float((closes[-1] - tp) / (0.015 * md))

    # ── Stochastic K/D ──
    if factor_name in ("stoch_k", "stoch_d"):
        if len(closes) < 14:
            return None
        window = closes[-14:]
        lowest = min(window)
        highest = max(window)
        if highest == lowest:
            return 50.0
        k = (closes[-1] - lowest) / (highest - lowest) * 100
        if factor_name == "stoch_k":
            return float(k)
        # D = 3-day SMA of K (approximate with last 3 K values)
        if len(closes) < 16:
            return float(k)
        k_vals = []
        for i in range(3):
            w = closes[-(14 + i):-i] if i > 0 else closes[-14:]
            lo, hi = min(w), max(w)
            k_vals.append((w[-1] - lo) / (hi - lo) * 100 if hi != lo else 50.0)
        return float(sum(k_vals) / len(k_vals))

    # ── ROC ──
    if factor_name in ("roc_10", "roc_20"):
        p = 10 if factor_name == "roc_10" else 20
        if len(closes) < p + 1:
            return None
        prev = closes[-(p + 1)]
        return float((closes[-1] - prev) / prev * 100) if prev > 0 else None

    # ── MFI (simplified, uses closes as proxy) ──
    if factor_name == "mfi_14":
        if len(closes) < 15:
            return None
        pos_flow = 0.0
        neg_flow = 0.0
        for i in range(-14, 0):
            if closes[i] > closes[i - 1]:
                pos_flow += closes[i]
            else:
                neg_flow += abs(closes[i])
        if neg_flow == 0:
            return 100.0
        mfr = pos_flow / neg_flow
        return float(100.0 - 100.0 / (1.0 + mfr))

    # ── 波动率变体 ──
    if factor_name in ("vol_5d", "vol_10d", "vol_60d"):
        period_map = {"vol_5d": 5, "vol_10d": 10, "vol_60d": 60}
        p = period_map[factor_name]
        if len(closes) < p + 1:
            return None
        window = np.array(closes[-(p + 1):], dtype=np.float64)
        prev = window[:-1]
        curr = window[1:]
        valid = prev > 0
        if int(np.sum(valid)) < 2:
            return None
        rets = (curr[valid] - prev[valid]) / prev[valid]
        return float(np.std(rets, ddof=1) * np.sqrt(252.0))

    # ── ATR (simplified, uses close-to-close) ──
    if factor_name in ("atr_14", "atr_20"):
        p = 14 if factor_name == "atr_14" else 20
        if len(closes) < p + 1:
            return None
        trs = [abs(closes[i] - closes[i - 1]) for i in range(len(closes) - p, len(closes))]
        return float(sum(trs) / len(trs))

    # ── Bollinger Width ──
    if factor_name == "bollinger_width":
        if len(closes) < 20:
            return None
        window = closes[-20:]
        ma = sum(window) / 20
        std = (sum((c - ma) ** 2 for c in window) / 20) ** 0.5
        if ma == 0:
            return 0.0
        return float(4 * std / ma)

    # ── Downside Vol ──
    if factor_name == "downside_vol":
        if len(closes) < 21:
            return None
        window = np.array(closes[-21:], dtype=np.float64)
        rets = (window[1:] - window[:-1]) / np.where(window[:-1] > 0, window[:-1], 1.0)
        neg_rets = rets[rets < 0]
        if len(neg_rets) < 2:
            return 0.0
        return float(np.std(neg_rets, ddof=1) * np.sqrt(252.0))

    # ── Volume-based (simplified, use closes as proxy) ──
    if factor_name == "volume_ratio":
        # FIX-7: 访问 closes[i-1]，最旧的 i=-20 需要 closes[-21]，故需 21 根
        if len(closes) < 21:
            return None
        vol5 = sum(abs(closes[i] - closes[i - 1]) for i in range(-5, 0)) / 5
        vol20 = sum(abs(closes[i] - closes[i - 1]) for i in range(-20, 0)) / 20
        return float(vol5 / vol20) if vol20 > 0 else None

    if factor_name == "obv_slope":
        # FIX-7: 循环内访问 closes[i-1]，i 从 -19 起最旧需 closes[-20]，需 20 根（含 i-1）
        if len(closes) < 20:
            return None
        obv = 0.0
        obv_series = [0.0]
        for i in range(-19, 0):
            if closes[i] > closes[i - 1]:
                obv += 1
            elif closes[i] < closes[i - 1]:
                obv -= 1
            obv_series.append(obv)
        x = np.arange(len(obv_series), dtype=np.float64)
        y = np.array(obv_series, dtype=np.float64)
        slope = float(np.polyfit(x, y, 1)[0])
        return slope

    if factor_name == "vwap_deviation":
        if len(closes) < 20:
            return None
        vwap = sum(closes[-20:]) / 20
        return float((closes[-1] - vwap) / vwap) if vwap > 0 else None

    if factor_name in ("turnover_5d", "turnover_20d"):
        p = 5 if factor_name == "turnover_5d" else 20
        # FIX-7: 访问 closes[i-1]，最旧的 i=-p 需要 closes[-(p+1)]，故需 p+1 根
        if len(closes) < p + 1:
            return None
        changes = [abs(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(-p, 0) if closes[i - 1] > 0]
        return float(sum(changes) / len(changes)) if changes else None

    # ── 基本面单指标 ──
    if factor_name == "pe_ttm":
        if not financial:
            return None
        return _safe_float(financial.get("pe_ratio"), None)

    if factor_name == "pb_mrq":
        if not financial:
            return None
        return _safe_float(financial.get("pb_ratio"), None)

    if factor_name == "ps_ttm":
        if not financial:
            return None
        return _safe_float(financial.get("ps_ratio"), None)

    if factor_name == "roe_ttm":
        if not financial:
            return None
        return _safe_float(financial.get("roe"), None)

    if factor_name == "roa_ttm":
        if not financial:
            return None
        return _safe_float(financial.get("roa"), None)

    if factor_name == "gross_margin":
        if not financial:
            return None
        return _safe_float(financial.get("gross_margin"), None)

    if factor_name == "net_margin":
        if not financial:
            return None
        return _safe_float(financial.get("net_margin"), None)

    if factor_name == "debt_to_equity":
        if not financial:
            return None
        return _safe_float(financial.get("debt_ratio"), None)

    if factor_name == "revenue_growth_yoy":
        if not financial:
            return None
        return _first_valid_float(financial, REVENUE_GROWTH_KEYS)

    if factor_name == "dividend_yield":
        if not financial:
            return None
        for key in ("dividend_yield", "div_yield", "dps_yield"):
            val = _safe_float(financial.get(key), 0.0)
            if val > 0:
                return val
        return None

    # ── 另类因子 (placeholder — return None, computed externally) ──
    if factor_name in ("sentiment_score", "capital_flow", "north_flow", "institutional_flow", "event_intensity"):
        return None

    return None
