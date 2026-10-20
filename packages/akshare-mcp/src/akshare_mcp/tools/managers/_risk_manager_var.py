"""VaR action handler for risk_manager."""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

from ._risk_manager_support import (
    _dedupe_chain,
    _extract_holding_code,
    _extract_holding_shares,
    _get_klines_with_fallback,
    _load_portfolio_holdings,
)
from .risk_mgr_helpers import _empty_var_payload, _parse_codes_weights, _safe_portfolio_id


# Fixed so that the Monte Carlo branch is reproducible: identical inputs must
# yield an identical VaR, otherwise a gate reading it is deciding on noise.
MONTE_CARLO_SEED = 20260101


def _returns_by_date(klines: list[dict[str, Any]]) -> dict[str, float]:
    """Per-trading-day simple returns, keyed by the bar's trade date.

    The previous implementation kept a bare list and later aligned constituents
    by list position, which silently paired the first return of a short history
    with the oldest return of a long one.
    """
    series: dict[str, float] = {}
    previous_price: float | None = None
    for bar in klines:
        try:
            price = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            continue
        day = str(bar.get("date") or bar.get("time") or "")[:10]
        if not day:
            continue
        if previous_price is not None and previous_price > 0:
            series[day] = (price - previous_price) / previous_price
        previous_price = price if price > 0 else None
    return series


async def _handle_calculate_var(
    *,
    db: Any,
    kwargs: dict[str, Any],
    ok: Callable[..., dict],
    fail: Callable[..., dict],
) -> dict:
    source_chain = ["risk_manager"]
    portfolio_id = _safe_portfolio_id(kwargs.get("portfolio_id"))
    confidence = float(kwargs.get("confidence", 0.95) or 0.95)
    confidence = max(0.5, min(0.999, confidence))
    method = str(kwargs.get("method", "historical") or "historical")
    lookback_days = int(kwargs.get("lookback_days", 252) or 252)
    lookback_days = max(30, min(2000, lookback_days))
    portfolio_value_input = float(kwargs.get("portfolio_value", 1_000_000) or 1_000_000)

    returns_data: list[dict[str, Any]] = []
    requested_codes: list[str] = []
    total_value = 0.0
    input_mode = "portfolio_id"

    if portfolio_id is not None:
        async with db.acquire() as conn:
            holdings = await _load_portfolio_holdings(conn, portfolio_id)
        source_chain.append("db.holdings")

        if not holdings:
            return ok(
                _empty_var_payload(portfolio_id, input_mode, confidence, method),
                source_chain=_dedupe_chain(source_chain),
            )

        requested_codes = [_extract_holding_code(holding) for holding in holdings]
        for holding in holdings:
            code = _extract_holding_code(holding)
            shares = _extract_holding_shares(holding)
            klines, one_chain = await _get_klines_with_fallback(db, code, lookback_days)
            source_chain.extend(one_chain)
            prices = [float(item["close"]) for item in klines]
            if len(klines) < 2 or not prices or prices[-1] <= 0:
                continue

            series = _returns_by_date(klines)
            if not series:
                continue

            current_value = shares * prices[-1]
            total_value += current_value
            returns_data.append(
                {
                    "code": code,
                    "returns_by_date": series,
                    "weight": 0.0,
                    "current_value": current_value,
                }
            )

        if total_value > 0:
            for item in returns_data:
                item["weight"] = item["current_value"] / total_value
    else:
        codes, weights, parse_error = _parse_codes_weights(kwargs)
        if parse_error:
            return fail(parse_error, source_chain=source_chain)
        if not codes:
            return fail("portfolio_id or codes+weights required", source_chain=source_chain)

        input_mode = "codes_weights"
        source_chain.append("input.codes_weights")
        total_value = portfolio_value_input
        requested_codes = list(codes)

        for code, weight in zip(codes, weights):
            klines, one_chain = await _get_klines_with_fallback(db, code, lookback_days)
            source_chain.extend(one_chain)
            prices = [float(item["close"]) for item in klines]
            if len(klines) < 2 or not prices or prices[-1] <= 0:
                continue

            series = _returns_by_date(klines)
            if not series:
                continue

            returns_data.append(
                {
                    "code": code,
                    "returns_by_date": series,
                    "weight": float(weight),
                    "current_value": float(weight * total_value),
                }
            )

        if returns_data:
            total_weight = float(sum(item["weight"] for item in returns_data))
            if total_weight > 0:
                for item in returns_data:
                    item["weight"] = item["weight"] / total_weight
                    item["current_value"] = item["weight"] * total_value

    # A missing constituent is not silently dropped and the survivors are no
    # longer renormalised to 100%: that produced a confident VaR for a portfolio
    # nobody holds. It is reported as unmeasurable instead.
    present_codes = {str(item["code"]) for item in returns_data}
    missing_codes = [code for code in requested_codes if code not in present_codes]
    if missing_codes:
        return fail(
            "missing_constituents",
            source_chain=_dedupe_chain(source_chain),
            missing_codes=sorted(set(missing_codes)),
            measured_codes=sorted(present_codes),
        )

    if not returns_data:
        return fail("no available kline data for VaR calculation", source_chain=_dedupe_chain(source_chain))

    common_dates = sorted(
        set.intersection(*(set(item["returns_by_date"]) for item in returns_data))
    )
    if not common_dates:
        return fail(
            "no common trading dates across constituents",
            source_chain=_dedupe_chain(source_chain),
            measured_codes=sorted(present_codes),
        )

    total_weight = float(sum(item["weight"] for item in returns_data))
    if total_weight <= 0:
        return fail("portfolio weights sum to zero", source_chain=_dedupe_chain(source_chain))

    portfolio_returns = np.array(
        [
            sum(
                item["returns_by_date"][day] * (item["weight"] / total_weight)
                for item in returns_data
            )
            for day in common_dates
        ]
    )
    observations = len(common_dates)

    if method == "historical":
        var = float(np.percentile(portfolio_returns, (1 - confidence) * 100))
    elif method == "parametric":
        mean = float(np.mean(portfolio_returns))
        std = float(np.std(portfolio_returns))
        try:
            from scipy import stats

            var = float(stats.norm.ppf(1 - confidence, mean, std))
        except Exception:
            var = float(np.percentile(portfolio_returns, (1 - confidence) * 100))
    else:
        mean = float(np.mean(portfolio_returns))
        std = float(np.std(portfolio_returns))
        # Seeded on purpose. An unseeded draw made VaR a different number on
        # every call with identical inputs, so any gate consuming it could pass
        # or fail on run-to-run luck. `np.random.normal` also draws from the
        # global legacy state, which is shared with anything else in-process.
        simulations = np.random.default_rng(MONTE_CARLO_SEED).normal(mean, std, 10000)
        var = float(np.percentile(simulations, (1 - confidence) * 100))

    var_amount = abs(var * total_value)
    cvar_returns = portfolio_returns[portfolio_returns <= var]
    cvar = float(np.mean(cvar_returns)) if len(cvar_returns) > 0 else var
    cvar_amount = abs(cvar * total_value)

    return ok(
        {
            "portfolio_id": portfolio_id,
            "input_mode": input_mode,
            "method": method,
            "confidence": confidence,
            "total_value": float(total_value),
            "constituents": [item["code"] for item in returns_data],
            "var": {
                "percentage": float(var),
                "amount": float(var_amount),
                "description": f"{confidence * 100:.0f}% confidence 1-day VaR",
            },
            "cvar": {
                "percentage": float(cvar),
                "amount": float(cvar_amount),
                "description": "expected tail loss beyond VaR",
            },
            "volatility": float(np.std(portfolio_returns)),
            "max_drawdown": float(np.min(portfolio_returns)),
            # The aligned sample actually measured, so a consumer can tell a
            # 12-observation VaR from a 252-observation one.
            "observations": int(observations),
            "aligned_from": common_dates[0] if common_dates else None,
            "aligned_to": common_dates[-1] if common_dates else None,
        },
        source_chain=_dedupe_chain(source_chain),
    )


__all__ = ["_handle_calculate_var"]
