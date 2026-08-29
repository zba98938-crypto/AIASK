"""Pure Python helpers for formula-style screening fallbacks."""

from __future__ import annotations

try:
    import akshare as ak
except Exception:  # pragma: no cover - optional dependency
    ak = None

from ..data_source import data_source
from ..utils import normalize_code, safe_float, safe_int


def _get_kline_from_akshare(stock_code: str, period: str, count: int) -> list:
    """Use AkShare as a synchronous fallback for formula screening."""
    if ak is None or period not in ("daily", "weekly", "monthly"):
        return []

    try:
        df = ak.stock_zh_a_hist(symbol=stock_code, period=period, adjust="qfq")
        if df is None or df.empty:
            return []

        results = []
        for _, row in df.tail(count).iterrows():
            results.append(
                {
                    "date": str(row.get("日期") or row.get("date") or ""),
                    "open": safe_float(row.get("开盘") or row.get("open")),
                    "close": safe_float(row.get("收盘") or row.get("close")),
                    "high": safe_float(row.get("最高") or row.get("high")),
                    "low": safe_float(row.get("最低") or row.get("low")),
                    "volume": safe_int(row.get("成交量") or row.get("volume")),
                    "amount": safe_float(row.get("成交额") or row.get("amount")),
                    "change_pct": safe_float(row.get("涨跌幅") or row.get("pct_chg")),
                    "source": "akshare",
                }
            )
        return results
    except Exception:
        return []


def get_kline_for_formula_fallback(stock_code: str, period: str, count: int) -> list:
    """Load K-line data for pure-Python formula fallbacks."""
    code = normalize_code(stock_code)

    need_aggregate = period in ("1w", "weekly", "1M", "monthly")
    if need_aggregate:
        multiplier = 7 if period in ("1w", "weekly") else 31
        daily_count = min(count * multiplier, 2000)
        daily_klines = data_source.get_kline(code, "daily", daily_count)
        if not daily_klines:
            daily_klines = _get_kline_from_akshare(code, "daily", daily_count)
        if not daily_klines:
            return []
        return aggregate_daily_klines(daily_klines, period)

    normalized_period = "daily" if period in ("1d", "daily") else period
    rows = data_source.get_kline(code, normalized_period, count)
    if rows:
        return rows
    return _get_kline_from_akshare(code, normalized_period, count)


def aggregate_daily_klines(daily_klines: list, period: str) -> list:
    """Aggregate daily bars into weekly or monthly bars."""
    from datetime import datetime

    if not daily_klines:
        return []

    sorted_klines = sorted(daily_klines, key=lambda row: str(row.get("date", "")))

    def group_key(date_str: str) -> str:
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return date_str
        if period in ("1w", "weekly"):
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        return f"{dt.year}-{dt.month:02d}"

    grouped: dict[str, list] = {}
    for row in sorted_klines:
        grouped.setdefault(group_key(str(row.get("date", ""))), []).append(row)

    result = []
    for bars in grouped.values():
        if not bars:
            continue
        result.append(
            {
                "date": str(bars[-1].get("date", "")),
                "open": bars[0].get("open"),
                "close": bars[-1].get("close"),
                "high": max((bar.get("high") or 0) for bar in bars),
                "low": min((bar.get("low") or float("inf")) for bar in bars),
                "volume": sum((bar.get("volume") or 0) for bar in bars),
                "amount": sum((bar.get("amount") or 0) for bar in bars),
                "source": bars[0].get("source", "aggregated"),
            }
        )

    return result


def get_default_formula_stock_pool() -> list[str]:
    """Return a bounded default stock pool for pure-Python screening fallbacks.

    优先 tqcenter (HS300, market='23')；失败回 AkShare CSINDEX；最后兜底硬编码。
    """
    # 0. tqcenter 主路径
    try:
        from ..data_source import data_source
        items = data_source.get_tdx_stock_list("23", 0)
        codes: list[str] = []
        for item in items[:50]:
            full = item.get("full_code") or item.get("code") or ""
            bare = full.split(".")[0] if "." in full else full
            if bare:
                codes.append(bare)
        if codes:
            return codes
    except Exception:
        pass

    try:
        import akshare as ak

        df = ak.index_stock_cons_csindex(symbol="000300")
        if df is not None and not df.empty:
            column = "成分券代码" if "成分券代码" in df.columns else df.columns[0]
            return df[column].tolist()[:50]
    except Exception:
        pass

    return [
        "600519",
        "000001",
        "600036",
        "601318",
        "000858",
        "600276",
        "601166",
        "000333",
        "600030",
        "601398",
        "600900",
        "601012",
        "600809",
        "000568",
        "002714",
        "601888",
        "600887",
        "000651",
        "601668",
        "600585",
    ]
