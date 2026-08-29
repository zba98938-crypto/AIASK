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

        for holding in holdings:
            code = _extract_holding_code(holding)
            shares = _extract_holding_shares(holding)
            klines, one_chain = await _get_klines_with_fallback(db, code, lookback_days)
            source_chain.extend(one_chain)
            if len(klines) < 2:
                continue

            prices = [float(item["close"]) for item in klines]
            if prices[-1] <= 0:
                continue

            returns = [
                (prices[index] - prices[index - 1]) / prices[index - 1]
                for index in range(1, len(prices))
                if prices[index - 1] > 0
            ]
            if not returns:
                continue

            current_value = shares * prices[-1]
            total_value += current_value
            returns_data.append(
                {
                    "code": code,
                    "returns": returns,
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

        for code, weight in zip(codes, weights):
            klines, one_chain = await _get_klines_with_fallback(db, code, lookback_days)
            source_chain.extend(one_chain)
            if len(klines) < 2:
                continue

            prices = [float(item["close"]) for item in klines]
            returns = [
                (prices[index] - prices[index - 1]) / prices[index - 1]
                for index in range(1, len(prices))
                if prices[index - 1] > 0
            ]
            if not returns:
                continue

            returns_data.append(
                {
                    "code": code,
                    "returns": returns,
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

    if not returns_data:
        return fail("no available kline data for VaR calculation", source_chain=_dedupe_chain(source_chain))

    min_length = min(len(item["returns"]) for item in returns_data)
    portfolio_returns = np.array(
        [sum(item["returns"][index] * item["weight"] for item in returns_data) for index in range(min_length)]
    )

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
        simulations = np.random.normal(mean, std, 10000)
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
        },
        source_chain=_dedupe_chain(source_chain),
    )


__all__ = ["_handle_calculate_var"]
