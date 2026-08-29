"""Market temperature analytics.

The service is intentionally pure: callers provide stock snapshots and optional
industry metadata, and the service returns a deterministic read-only summary.
Runtime tools can decide whether those rows come from SQLite, TDX, AKShare, or
fixtures.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from math import isfinite
from typing import Any, Iterable, Mapping

CONTRACT_VERSION = "market_temperature.v1"


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _date_key(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    return text[:10]


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def _first_value(row: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _industry_lookup(industry_rows: Iterable[Mapping[str, Any]] | None) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for item in industry_rows or []:
        row = dict(item or {})
        code = _safe_text(
            _first_value(row, ("code", "industry_code", "block_code", "sw_code"))
        )
        name = _safe_text(
            _first_value(row, ("name", "industry", "industry_name", "block_name"))
        )
        if code and name:
            lookup[code] = name
    return lookup


def normalize_stock_snapshot(
    row: Mapping[str, Any],
    *,
    industry_names: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Normalize one stock snapshot into the market-temperature contract."""

    payload = dict(row or {})
    code = _safe_text(_first_value(payload, ("code", "stock_code", "symbol", "ts_code")))
    if not code:
        return None

    close = _safe_float(_first_value(payload, ("close", "price", "last")))
    pct_change = _safe_float(
        _first_value(payload, ("pct_change", "change_pct", "pct_chg", "changePercent"))
    )
    ma20 = _safe_float(_first_value(payload, ("ma20", "sma20", "MA20")))
    amount = _safe_float(_first_value(payload, ("amount", "turnover_amount", "成交额")))
    turnover = _safe_float(_first_value(payload, ("turnover", "turnover_rate")))
    market_cap = _safe_float(
        _first_value(payload, ("marketCap", "market_cap", "mkt_cap", "total_mv"))
    )
    industry_code = _safe_text(
        _first_value(payload, ("industry_code", "block_code", "sw_code"))
    )
    industry_name = _safe_text(
        _first_value(payload, ("industry", "industry_name", "sector", "block_name"))
    )
    if not industry_name and industry_code and industry_names:
        industry_name = _safe_text(industry_names.get(industry_code))

    above_ma20 = None
    if close is not None and ma20 is not None and ma20 > 0:
        above_ma20 = close > ma20

    return {
        "code": code,
        "name": _safe_text(_first_value(payload, ("name", "stock_name"))) or code,
        "date": _date_key(_first_value(payload, ("date", "time", "trade_date"))),
        "close": close,
        "pct_change": pct_change,
        "ma20": ma20,
        "amount": amount,
        "turnover": turnover,
        "market_cap": market_cap,
        "industry_code": industry_code,
        "industry": industry_name or "UNKNOWN",
        "above_ma20": above_ma20,
    }

def latest_rows_by_code(
    stock_rows: Iterable[Mapping[str, Any]],
    *,
    as_of: str | None = None,
    industry_rows: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return the latest valid row per code at or before ``as_of``."""

    industry_names = _industry_lookup(industry_rows)
    as_of_key = _date_key(as_of)
    invalid = 0
    latest: dict[str, dict[str, Any]] = {}
    for row in stock_rows or []:
        normalized = normalize_stock_snapshot(row, industry_names=industry_names)
        if normalized is None:
            invalid += 1
            continue
        row_date = normalized.get("date") or ""
        if as_of_key and row_date and row_date > as_of_key:
            continue
        previous = latest.get(normalized["code"])
        if previous is None or (row_date, normalized["code"]) >= (
            previous.get("date") or "",
            previous["code"],
        ):
            latest[normalized["code"]] = normalized
    return list(latest.values()), invalid


def _weighted_average(rows: list[dict[str, Any]], field: str, weight_field: str) -> float | None:
    weighted_sum = 0.0
    weight_sum = 0.0
    for row in rows:
        value = _safe_float(row.get(field))
        weight = _safe_float(row.get(weight_field))
        if value is None or weight is None or weight <= 0:
            continue
        weighted_sum += value * weight
        weight_sum += weight
    if weight_sum <= 0:
        return None
    return weighted_sum / weight_sum


def _average(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [_safe_float(row.get(field)) for row in rows]
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _temperature_from_components(
    *,
    breadth: float | None,
    avg_pct_change: float | None,
    advance_ratio: float | None,
) -> float:
    breadth_component = 50.0 if breadth is None else _clamp(float(breadth) * 100.0)
    momentum_component = 50.0 if avg_pct_change is None else _clamp(50.0 + float(avg_pct_change) * 8.0)
    advance_component = 50.0 if advance_ratio is None else _clamp(float(advance_ratio) * 100.0)
    return _clamp(breadth_component * 0.6 + momentum_component * 0.3 + advance_component * 0.1)


def _state_for_temperature(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 80:
        return "hot"
    if value >= 65:
        return "warm"
    if value <= 20:
        return "cold"
    if value <= 35:
        return "cool"
    return "neutral"


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    trend_known = [row for row in rows if row.get("above_ma20") is not None]
    above = sum(1 for row in trend_known if bool(row.get("above_ma20")))
    advance = sum(1 for row in rows if (row.get("pct_change") or 0) > 0)
    decline = sum(1 for row in rows if (row.get("pct_change") or 0) < 0)
    flat = total - advance - decline
    avg_pct = _average(rows, "pct_change")
    weighted_pct = _weighted_average(rows, "pct_change", "market_cap")
    breadth = above / len(trend_known) if trend_known else None
    advance_ratio = advance / total if total else None
    temperature = _temperature_from_components(
        breadth=breadth,
        avg_pct_change=weighted_pct if weighted_pct is not None else avg_pct,
        advance_ratio=advance_ratio,
    )
    return {
        "stock_count": total,
        "trend_known_count": len(trend_known),
        "above_ma20_count": above,
        "ma20_breadth": _round(breadth),
        "advance_count": advance,
        "decline_count": decline,
        "flat_count": flat,
        "advance_ratio": _round(advance_ratio),
        "avg_pct_change": _round(avg_pct),
        "weighted_pct_change": _round(weighted_pct),
        "amount": _round(sum(row.get("amount") or 0.0 for row in rows), 2),
        "market_cap": _round(sum(row.get("market_cap") or 0.0 for row in rows), 2),
        "temperature": _round(temperature, 2),
        "state": _state_for_temperature(temperature),
    }


def build_market_temperature_snapshot(
    stock_rows: Iterable[Mapping[str, Any]],
    *,
    industry_rows: Iterable[Mapping[str, Any]] | None = None,
    as_of: str | None = None,
    top_n: int = 5,
    min_industry_size: int = 1,
) -> dict[str, Any]:
    """Build a market and industry temperature snapshot."""

    input_rows = list(stock_rows or [])
    rows, invalid = latest_rows_by_code(input_rows, as_of=as_of, industry_rows=industry_rows)
    trade_dates = sorted({row.get("date") for row in rows if row.get("date")})
    as_of_date = _date_key(as_of) or (trade_dates[-1] if trade_dates else "")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("industry") or "UNKNOWN")].append(row)

    market = _summarize_rows(rows) if rows else {
        "stock_count": 0,
        "trend_known_count": 0,
        "above_ma20_count": 0,
        "ma20_breadth": None,
        "advance_count": 0,
        "decline_count": 0,
        "flat_count": 0,
        "advance_ratio": None,
        "avg_pct_change": None,
        "weighted_pct_change": None,
        "amount": 0.0,
        "market_cap": 0.0,
        "temperature": None,
        "state": "unknown",
    }
    total_market_cap = float(market.get("market_cap") or 0.0)

    industries: list[dict[str, Any]] = []
    for industry_name, industry_rows_for_name in groups.items():
        if len(industry_rows_for_name) < max(1, int(min_industry_size or 1)):
            continue
        summary = _summarize_rows(industry_rows_for_name)
        industry_codes = sorted(
            {row.get("industry_code") for row in industry_rows_for_name if row.get("industry_code")}
        )
        summary.update(
            {
                "code": industry_codes[0] if industry_codes else "",
                "name": industry_name,
                "date": as_of_date,
                "market_cap_weight": _round(
                    (float(summary.get("market_cap") or 0.0) / total_market_cap)
                    if total_market_cap > 0
                    else None
                ),
            }
        )
        industries.append(summary)

    industries.sort(
        key=lambda item: (
            item.get("temperature") is not None,
            float(item.get("temperature") or -1.0),
            float(item.get("market_cap") or 0.0),
        ),
        reverse=True,
    )
    cold = sorted(
        industries,
        key=lambda item: (
            item.get("temperature") is None,
            float(item.get("temperature") if item.get("temperature") is not None else 101.0),
            -float(item.get("market_cap") or 0.0),
        ),
    )

    unknown_industry_count = sum(1 for row in rows if row.get("industry") == "UNKNOWN")
    trend_coverage = (
        float(market.get("trend_known_count") or 0) / float(market.get("stock_count") or 1)
        if market.get("stock_count")
        else 0.0
    )
    warnings: list[str] = []
    if not rows:
        warnings.append("no_valid_stock_rows")
    if trend_coverage < 0.8:
        warnings.append("low_ma20_coverage")
    if unknown_industry_count:
        warnings.append("unknown_industry_rows")

    quality_status = "healthy"
    if not rows:
        quality_status = "empty"
    elif warnings:
        quality_status = "degraded"

    return {
        "contract_version": CONTRACT_VERSION,
        "as_of": as_of_date,
        "market": market,
        "industries": industries,
        "hot_industries": industries[: max(0, int(top_n or 0))],
        "cold_industries": cold[: max(0, int(top_n or 0))],
        "quality": {
            "status": quality_status,
            "warnings": warnings,
            "input_rows": len(input_rows),
            "valid_stock_count": len(rows),
            "invalid_stock_rows": invalid,
            "industry_count": len(industries),
            "unknown_industry_count": unknown_industry_count,
            "trend_coverage": _round(trend_coverage),
            "contract_version": CONTRACT_VERSION,
        },
    }
