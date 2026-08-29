"""Risk exposure action handler for risk_manager."""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

from ._risk_manager_support import (
    _dedupe_chain,
    _extract_holding_code,
    _extract_holding_shares,
    _get_financials_with_fallback,
    _get_klines_with_fallback,
    _get_stock_info_with_fallback,
    _load_portfolio_holdings,
)
from .risk_mgr_helpers import (
    _classify_size_bucket,
    _first_float,
    _format_pct,
    _liquidity_level,
    _parse_codes_weights,
    _safe_float,
    _safe_portfolio_id,
)


async def _handle_risk_exposure(
    *,
    db: Any,
    kwargs: dict[str, Any],
    ok: Callable[..., dict],
    fail: Callable[..., dict],
) -> dict:
    source_chain = ["risk_manager"]
    portfolio_id = _safe_portfolio_id(kwargs.get("portfolio_id"))
    input_mode = "portfolio_id"
    position_rows: list[dict[str, Any]] = []
    lookback_days = int(kwargs.get("lookback_days", 20) or 20)
    lookback_days = max(5, min(120, lookback_days))
    monitor_points = int(kwargs.get("monitor_points", lookback_days) or lookback_days)
    monitor_points = max(5, min(60, monitor_points))
    max_participation_rate = float(kwargs.get("max_participation_rate", 0.2) or 0.2)
    max_participation_rate = max(0.01, min(0.5, max_participation_rate))

    if portfolio_id is not None:
        async with db.acquire() as conn:
            holdings = await _load_portfolio_holdings(conn, portfolio_id)
        source_chain.append("db.holdings")

        if not holdings:
            return ok(
                {
                    "message": "empty portfolio, add holdings first",
                    "quick_start": {
                        "step1": 'portfolio_manager(action="add_holding", portfolio_id="xxx", code="600519", shares=100)',
                        "step2": 'risk_manager(action="risk_exposure", portfolio_id="xxx")',
                    },
                },
                source_chain=_dedupe_chain(source_chain),
            )

        for holding in holdings:
            code = _extract_holding_code(holding)
            shares = _extract_holding_shares(holding)
            stock_info, one_info_chain = await _get_stock_info_with_fallback(db, code)
            source_chain.extend(one_info_chain)
            klines, one_kline_chain = await _get_klines_with_fallback(db, code, max(lookback_days, monitor_points, 2))
            source_chain.extend(one_kline_chain)
            if not klines:
                continue

            financial_row = await _load_financial_row(db, code, source_chain)
            current_price = float(klines[-1]["close"])
            current_value = shares * current_price
            sector = stock_info.get("industry", "unknown") if stock_info else "unknown"
            position_rows.append(
                _build_position_row(
                    code=code,
                    current_value=float(current_value),
                    current_price=float(current_price),
                    shares_proxy=float(shares),
                    sector=sector,
                    stock_info=stock_info,
                    financial_row=financial_row,
                    klines=klines,
                    lookback_days=lookback_days,
                    monitor_points=monitor_points,
                )
            )
    else:
        codes, weights, parse_error = _parse_codes_weights(kwargs)
        if parse_error:
            return fail(parse_error, source_chain=source_chain)
        if not codes:
            return fail("portfolio_id or codes+weights required", source_chain=source_chain)

        input_mode = "codes_weights"
        source_chain.append("input.codes_weights")
        portfolio_value = float(kwargs.get("portfolio_value", 1_000_000) or 1_000_000)
        for code, weight in zip(codes, weights):
            stock_info, one_info_chain = await _get_stock_info_with_fallback(db, code)
            source_chain.extend(one_info_chain)
            klines, one_kline_chain = await _get_klines_with_fallback(db, code, max(lookback_days, monitor_points, 2))
            source_chain.extend(one_kline_chain)
            if not klines:
                continue

            financial_row = await _load_financial_row(db, code, source_chain)
            current_price = float(klines[-1]["close"])
            current_value = float(weight * portfolio_value)
            shares_proxy = (current_value / current_price) if current_price > 0 else 0.0
            sector = stock_info.get("industry", "unknown") if stock_info else "unknown"
            position_rows.append(
                _build_position_row(
                    code=code,
                    current_value=current_value,
                    current_price=float(current_price),
                    shares_proxy=float(shares_proxy),
                    sector=sector,
                    stock_info=stock_info,
                    financial_row=financial_row,
                    klines=klines,
                    lookback_days=lookback_days,
                    monitor_points=monitor_points,
                )
            )

    if not position_rows:
        return fail("no positions or quotes available for exposure analysis", source_chain=_dedupe_chain(source_chain))

    total_value = float(sum(item["value"] for item in position_rows))
    sector_totals: dict[str, float] = {}
    stock_exposure: list[dict[str, Any]] = []

    for item in position_rows:
        code = item["code"]
        value = float(item["value"])
        sector = item["sector"]
        sector_totals[sector] = sector_totals.get(sector, 0.0) + value
        stock_exposure.append(
            {
                "code": code,
                "name": item.get("name", code),
                "value": value,
                "weight": "0%",
                "sector": sector,
                "liquidity_level": "unknown",
            }
        )

    if total_value > 0:
        for row in stock_exposure:
            row["weight"] = f"{(row['value'] / total_value * 100):.2f}%"

    sector_exposure = {
        sector: (f"{(value / total_value * 100):.2f}%" if total_value > 0 else "0%")
        for sector, value in sector_totals.items()
    }

    max_weight = (max(item["value"] for item in stock_exposure) / total_value) if total_value > 0 else 0.0
    hhi = sum((item["value"] / total_value) ** 2 for item in stock_exposure) if total_value > 0 else 0.0
    effective_positions = (1.0 / hhi) if hhi > 0 else 0.0
    top3_weight = (
        sum(sorted((item["value"] / total_value for item in stock_exposure), reverse=True)[:3]) if total_value > 0 else 0.0
    )
    sector_hhi = sum((value / total_value) ** 2 for value in sector_totals.values()) if total_value > 0 else 0.0
    if max_weight > 0.3:
        concentration_level = "high"
        concentration_desc = "single-stock concentration is too high"
    elif max_weight > 0.2:
        concentration_level = "medium"
        concentration_desc = "single-stock concentration is relatively high"
    else:
        concentration_level = "low"
        concentration_desc = "holdings are reasonably diversified"

    size_bucket_weights = {"large": 0.0, "mid": 0.0, "small": 0.0, "unknown": 0.0}
    beta_weight_num = 0.0
    beta_weight_den = 0.0
    pe_weight_num = 0.0
    pe_weight_den = 0.0
    pb_weight_num = 0.0
    pb_weight_den = 0.0
    roe_weight_num = 0.0
    roe_weight_den = 0.0
    debt_weight_num = 0.0
    debt_weight_den = 0.0

    for item in position_rows:
        value = float(item["value"])
        weight = (value / total_value) if total_value > 0 else 0.0
        bucket = _classify_size_bucket(item.get("market_cap"))
        size_bucket_weights[bucket] = size_bucket_weights.get(bucket, 0.0) + weight

        beta = item.get("beta")
        if beta is not None:
            beta_weight_num += weight * float(beta)
            beta_weight_den += weight

        pe = item.get("pe")
        if pe is not None and pe > 0:
            pe_weight_num += weight * float(pe)
            pe_weight_den += weight

        pb = item.get("pb")
        if pb is not None and pb > 0:
            pb_weight_num += weight * float(pb)
            pb_weight_den += weight

        roe = item.get("roe")
        if roe is not None:
            roe_weight_num += weight * float(roe)
            roe_weight_den += weight

        debt_ratio = item.get("debt_ratio")
        if debt_ratio is not None:
            debt_weight_num += weight * float(debt_ratio)
            debt_weight_den += weight

    weighted_beta = (beta_weight_num / beta_weight_den) if beta_weight_den > 0 else None
    weighted_pe = (pe_weight_num / pe_weight_den) if pe_weight_den > 0 else None
    weighted_pb = (pb_weight_num / pb_weight_den) if pb_weight_den > 0 else None
    weighted_roe = (roe_weight_num / roe_weight_den) if roe_weight_den > 0 else None
    weighted_debt = (debt_weight_num / debt_weight_den) if debt_weight_den > 0 else None

    if weighted_pe is None:
        valuation_tilt = "unknown"
    elif weighted_pe <= 15:
        valuation_tilt = "value"
    elif weighted_pe >= 30:
        valuation_tilt = "growth"
    else:
        valuation_tilt = "balanced"

    liquidity_rows: list[dict[str, Any]] = []
    weighted_days_to_exit = 0.0
    weighted_days_den = 0.0
    illiquid_weight = 0.0
    for item in position_rows:
        value = float(item["value"])
        weight = (value / total_value) if total_value > 0 else 0.0
        avg_daily_amount = float(item.get("avg_daily_amount", 0.0) or 0.0)
        capacity = avg_daily_amount * max_participation_rate
        days_to_exit = (value / capacity) if capacity > 0 else None
        level = _liquidity_level(days_to_exit)
        if level in {"medium", "high"}:
            illiquid_weight += weight
        if days_to_exit is not None:
            weighted_days_to_exit += weight * days_to_exit
            weighted_days_den += weight

        liquidity_rows.append(
            {
                "code": item["code"],
                "name": item.get("name", item["code"]),
                "avg_daily_amount": float(avg_daily_amount),
                "days_to_exit": round(float(days_to_exit), 2) if days_to_exit is not None else None,
                "level": level,
            }
        )

    for row in stock_exposure:
        match = next((item for item in liquidity_rows if item["code"] == row["code"]), None)
        if match:
            row["liquidity_level"] = match["level"]

    portfolio_days_to_exit = weighted_days_to_exit / weighted_days_den if weighted_days_den > 0 else None
    if portfolio_days_to_exit is None:
        liquidity_level = "unknown"
    elif portfolio_days_to_exit > 5 or illiquid_weight > 0.35:
        liquidity_level = "high"
    elif portfolio_days_to_exit > 2 or illiquid_weight > 0.2:
        liquidity_level = "medium"
    else:
        liquidity_level = "low"

    daily_monitor = []
    max_series_len = max((len(item.get("price_series", [])) for item in position_rows), default=0)
    for index in range(min(monitor_points, max_series_len)):
        daily_values = []
        day_label = None
        total_capacity = 0.0
        for item in position_rows:
            series = item.get("price_series", [])
            if index >= len(series):
                continue
            day, day_close = series[index]
            if day_close <= 0 or item.get("current_price", 0.0) <= 0:
                continue
            day_label = day_label or day
            base_value = float(item["value"])
            scaled_value = base_value * float(day_close / item["current_price"])
            daily_values.append(scaled_value)
            total_capacity += float(item.get("avg_daily_amount", 0.0) or 0.0) * max_participation_rate

        if not daily_values:
            continue
        total_day_value = float(sum(daily_values))
        hhi_day = sum((value / total_day_value) ** 2 for value in daily_values) if total_day_value > 0 else 0.0
        top3_day = (
            sum(sorted((value / total_day_value for value in daily_values), reverse=True)[:3]) if total_day_value > 0 else 0.0
        )
        liquidity_coverage = (total_capacity / total_day_value) if total_day_value > 0 else 0.0
        daily_monitor.append(
            {
                "date": day_label or f"t-{index}",
                "hhi": float(hhi_day),
                "top3_weight_pct": _format_pct(top3_day),
                "effective_positions": float(1.0 / hhi_day) if hhi_day > 0 else 0.0,
                "liquidity_coverage_pct": _format_pct(liquidity_coverage),
            }
        )

    stock_exposure.sort(key=lambda item: item["value"], reverse=True)
    liquidity_rows.sort(key=lambda item: (item["days_to_exit"] is None, -(item["days_to_exit"] or 0.0)))

    return ok(
        {
            "portfolio_id": portfolio_id,
            "input_mode": input_mode,
            "total_value": total_value,
            "stock_exposure": stock_exposure[:10],
            "sector_exposure": sector_exposure,
            "concentration_risk": {
                "level": concentration_level,
                "max_weight": f"{max_weight * 100:.2f}%",
                "description": concentration_desc,
            },
            "diversification": {
                "stock_count": len(stock_exposure),
                "sector_count": len(sector_exposure),
                "recommendation": "consider more holdings" if len(stock_exposure) < 10 else "holding count is reasonable",
            },
            "explainability": {
                "hhi": float(hhi),
                "effective_positions": float(effective_positions),
                "top3_weight_pct": f"{top3_weight * 100:.2f}%",
                "sector_hhi": float(sector_hhi),
            },
            "risk_dashboard": {
                "data_window": {
                    "lookback_days": lookback_days,
                    "monitor_points": monitor_points,
                    "max_participation_rate": max_participation_rate,
                },
                "industry_concentration": {
                    "sector_count": len(sector_exposure),
                    "sector_hhi": float(sector_hhi),
                    "top_sector": max(sector_totals, key=sector_totals.get) if sector_totals else "unknown",
                    "top_sector_weight_pct": _format_pct(max(sector_totals.values()) / total_value) if total_value > 0 else "0.00%",
                },
                "style_exposure": {
                    "beta_weighted": round(float(weighted_beta), 4) if weighted_beta is not None else None,
                    "size_bucket_weights": {key: _format_pct(value) for key, value in size_bucket_weights.items()},
                    "valuation_tilt": valuation_tilt,
                    "weighted_pe": round(float(weighted_pe), 2) if weighted_pe is not None else None,
                    "weighted_pb": round(float(weighted_pb), 2) if weighted_pb is not None else None,
                    "weighted_roe": round(float(weighted_roe), 4) if weighted_roe is not None else None,
                    "weighted_debt_ratio": round(float(weighted_debt), 4) if weighted_debt is not None else None,
                },
                "liquidity_risk": {
                    "level": liquidity_level,
                    "portfolio_days_to_exit": round(float(portfolio_days_to_exit), 2) if portfolio_days_to_exit is not None else None,
                    "illiquid_weight_pct": _format_pct(float(illiquid_weight)),
                    "positions": liquidity_rows[:10],
                },
                "daily_monitor": {
                    "as_of": daily_monitor[0]["date"] if daily_monitor else None,
                    "series_count": len(daily_monitor),
                    "series": daily_monitor,
                },
            },
        },
        source_chain=_dedupe_chain(source_chain),
    )


async def _load_financial_row(db: Any, code: str, source_chain: list[str]) -> dict[str, Any] | None:
    try:
        financials, one_fin_chain = await _get_financials_with_fallback(db, code)
        source_chain.extend(one_fin_chain)
        if isinstance(financials, list) and financials:
            return financials[0]
        if isinstance(financials, dict):
            return financials
    except Exception:
        return None
    return None


def _build_position_row(
    *,
    code: str,
    current_value: float,
    current_price: float,
    shares_proxy: float,
    sector: str,
    stock_info: dict[str, Any],
    financial_row: dict[str, Any] | None,
    klines: list[dict[str, Any]],
    lookback_days: int,
    monitor_points: int,
) -> dict[str, Any]:
    market_cap = _first_float(
        ["market_cap", "total_market_cap", "total_mv", "circ_mv", "float_market_cap", "mkt_cap"],
        stock_info,
        financial_row,
        positive_only=True,
    )
    beta = _first_float(["beta", "beta_1y", "beta_250d", "beta_60d"], stock_info, financial_row)
    pe = _first_float(["pe_ratio", "pe", "ttm_pe"], stock_info, financial_row, positive_only=True)
    pb = _first_float(["pb_ratio", "pb", "ttm_pb"], stock_info, financial_row, positive_only=True)
    roe = _first_float(["roe", "roe_ttm"], financial_row, stock_info)
    debt_ratio = _first_float(["debt_ratio", "debt_to_asset"], financial_row, stock_info)

    recent_klines = klines[-lookback_days:]
    amount_samples = []
    for row in recent_klines:
        close_px = _safe_float(row.get("close"), 0.0) or 0.0
        volume = _safe_float(row.get("volume"), 0.0) or 0.0
        amount = _safe_float(row.get("amount"), None)
        amount_samples.append(amount if amount is not None and amount > 0 else close_px * volume)
    avg_daily_amount = float(np.mean(amount_samples)) if amount_samples else 0.0

    monitor_klines = klines[-monitor_points:]
    price_series = []
    for row in monitor_klines:
        close_px = _safe_float(row.get("close"), 0.0) or 0.0
        if close_px <= 0:
            continue
        price_series.append((str(row.get("date", "")), float(close_px)))

    return {
        "code": code,
        "name": stock_info.get("stock_name", code) if stock_info else code,
        "value": float(current_value),
        "sector": sector,
        "current_price": float(current_price),
        "shares_proxy": float(shares_proxy),
        "market_cap": market_cap,
        "beta": beta,
        "pe": pe,
        "pb": pb,
        "roe": roe,
        "debt_ratio": debt_ratio,
        "avg_daily_amount": float(avg_daily_amount),
        "price_series": price_series,
    }


__all__ = ["_handle_risk_exposure"]
