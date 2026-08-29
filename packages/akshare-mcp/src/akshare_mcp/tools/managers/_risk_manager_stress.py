"""Stress test action handler for risk_manager."""

from __future__ import annotations

from typing import Any, Callable

from ._risk_manager_support import (
    _dedupe_chain,
    _extract_holding_code,
    _extract_holding_shares,
    _get_klines_with_fallback,
    _get_stock_info_with_fallback,
    _load_portfolio_holdings,
)
from .risk_mgr_helpers import (
    _empty_stress_payload,
    _parse_codes_weights,
    _parse_dict_param,
    _parse_list_param,
    _safe_float,
    _safe_portfolio_id,
)


async def _handle_stress_test(
    *,
    db: Any,
    kwargs: dict[str, Any],
    ok: Callable[..., dict],
    fail: Callable[..., dict],
) -> dict:
    source_chain = ["risk_manager"]
    portfolio_id = _safe_portfolio_id(kwargs.get("portfolio_id"))
    scenario = kwargs.get("scenario")
    scenarios_input = _parse_list_param(kwargs.get("scenarios")) or [scenario or "market_crash"]

    scenario_defs = {
        "market_crash": {
            "market": -0.20,
            "volatility": 2.0,
            "liquidity_penalty_pct": 0.0,
            "description": "market down 20%",
        },
        "black_swan": {
            "market": -0.30,
            "volatility": 3.0,
            "liquidity_penalty_pct": 0.003,
            "description": "black swan event",
        },
        "interest_rate_hike": {
            "market": -0.10,
            "volatility": 1.5,
            "liquidity_penalty_pct": 0.0,
            "description": "sharp rate hike",
        },
        "sector_rotation": {
            "market": -0.05,
            "volatility": 1.2,
            "liquidity_penalty_pct": 0.0,
            "description": "sector rotation",
        },
        "liquidity_crisis": {
            "market": -0.15,
            "volatility": 2.5,
            "liquidity_penalty_pct": 0.003,
            "description": "liquidity crunch",
        },
    }
    custom_scenarios = _parse_list_param(kwargs.get("custom_scenarios"))
    for item in custom_scenarios:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("name") or "").strip().lower()
        if not raw_name:
            continue
        custom_market = _safe_float(item.get("market"), None)
        custom_volatility = _safe_float(item.get("volatility"), None)
        custom_liquidity = _safe_float(item.get("liquidity_penalty_pct"), None)
        scenario_defs[raw_name] = {
            "market": float(custom_market if custom_market is not None else -0.20),
            "volatility": float(custom_volatility if custom_volatility is not None else 1.0),
            "liquidity_penalty_pct": float(custom_liquidity if custom_liquidity is not None else 0.0),
            "description": str(item.get("description") or f"custom scenario: {raw_name}"),
        }

    scenario_overrides = _parse_dict_param(kwargs.get("scenario_overrides"))
    global_override = {
        "market": _safe_float(scenario_overrides.get("market"), None),
        "volatility": _safe_float(scenario_overrides.get("volatility"), None),
        "liquidity_penalty_pct": _safe_float(scenario_overrides.get("liquidity_penalty_pct"), None),
    }

    input_mode = "portfolio_id"
    holdings_values: list[dict[str, Any]] = []

    if portfolio_id is not None:
        async with db.acquire() as conn:
            holdings = await _load_portfolio_holdings(conn, portfolio_id)
        source_chain.append("db.holdings")

        if not holdings:
            first_name = str(scenarios_input[0]) if scenarios_input else "market_crash"
            if first_name not in scenario_defs:
                first_name = "market_crash"
            return ok(
                _empty_stress_payload(portfolio_id, input_mode, first_name, scenario_defs[first_name]["description"]),
                source_chain=_dedupe_chain(source_chain),
            )

        for holding in holdings:
            code = _extract_holding_code(holding)
            shares = _extract_holding_shares(holding)
            klines, one_kline_chain = await _get_klines_with_fallback(db, code, 1)
            source_chain.extend(one_kline_chain)
            if not klines:
                continue
            stock_info, one_info_chain = await _get_stock_info_with_fallback(db, code)
            source_chain.extend(one_info_chain)
            sector = stock_info.get("industry", "unknown") if stock_info else "unknown"
            current_price = float(klines[-1]["close"])
            holdings_values.append({"code": code, "value": float(shares * current_price), "sector": sector})
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
            sector = stock_info.get("industry", "unknown") if stock_info else "unknown"
            holdings_values.append({"code": code, "value": float(weight * portfolio_value), "sector": sector})

    if not holdings_values:
        return fail("no positions or quotes available for stress test", source_chain=_dedupe_chain(source_chain))

    total_value = float(sum(item["value"] for item in holdings_values))

    def _run_one_scenario(raw_scenario: Any) -> dict[str, Any]:
        local_override: dict[str, Any] = {}
        if isinstance(raw_scenario, dict):
            local_override = raw_scenario
            scenario_name = str(raw_scenario.get("name") or "market_crash").strip().lower()
        else:
            scenario_name = str(raw_scenario or "market_crash").strip().lower()

        if scenario_name not in scenario_defs:
            scenario_name = "market_crash"
        params = dict(scenario_defs.get(scenario_name, scenario_defs["market_crash"]))

        if global_override.get("market") is not None:
            params["market"] = float(global_override["market"])
        if global_override.get("volatility") is not None:
            params["volatility"] = float(global_override["volatility"])
        if global_override.get("liquidity_penalty_pct") is not None:
            params["liquidity_penalty_pct"] = float(global_override["liquidity_penalty_pct"])

        override_from_map = scenario_overrides.get(scenario_name)
        if isinstance(override_from_map, dict):
            if _safe_float(override_from_map.get("market"), None) is not None:
                params["market"] = float(_safe_float(override_from_map.get("market"), params["market"]))
            if _safe_float(override_from_map.get("volatility"), None) is not None:
                params["volatility"] = float(_safe_float(override_from_map.get("volatility"), params["volatility"]))
            if _safe_float(override_from_map.get("liquidity_penalty_pct"), None) is not None:
                params["liquidity_penalty_pct"] = float(
                    _safe_float(override_from_map.get("liquidity_penalty_pct"), params["liquidity_penalty_pct"])
                )
            if override_from_map.get("description"):
                params["description"] = str(override_from_map.get("description"))

        if local_override:
            if _safe_float(local_override.get("market"), None) is not None:
                params["market"] = float(_safe_float(local_override.get("market"), params["market"]))
            if _safe_float(local_override.get("volatility"), None) is not None:
                params["volatility"] = float(_safe_float(local_override.get("volatility"), params["volatility"]))
            if _safe_float(local_override.get("liquidity_penalty_pct"), None) is not None:
                params["liquidity_penalty_pct"] = float(
                    _safe_float(local_override.get("liquidity_penalty_pct"), params["liquidity_penalty_pct"])
                )
            if local_override.get("description"):
                params["description"] = str(local_override.get("description"))

        market_shock = max(-1.0, min(1.0, float(params.get("market", -0.2))))
        volatility_multiplier = max(0.0, float(params.get("volatility", 1.0)))
        liquidity_penalty_pct = max(0.0, float(params.get("liquidity_penalty_pct", 0.0)))

        stressed_value = float(sum(item["value"] * (1 + market_shock) for item in holdings_values))
        loss = float(total_value - stressed_value)
        loss_pct = (loss / total_value) if total_value > 0 else 0.0
        volatility_penalty_pct = max(0.0, (volatility_multiplier - 1.0) * 0.01)
        volatility_penalty = total_value * volatility_penalty_pct
        liquidity_penalty = total_value * liquidity_penalty_pct
        adjusted_loss = loss + volatility_penalty + liquidity_penalty
        adjusted_loss_pct = (adjusted_loss / total_value) if total_value > 0 else 0.0

        return {
            "scenario": scenario_name,
            "description": str(params.get("description", scenario_name)),
            "current_value": float(total_value),
            "stressed_value": stressed_value,
            "loss": float(adjusted_loss),
            "loss_percentage": f"{adjusted_loss_pct * 100:.2f}%",
            "severity": "high" if adjusted_loss_pct > 0.15 else ("medium" if adjusted_loss_pct > 0.08 else "low"),
            "recommendation": "consider hedging" if adjusted_loss_pct > 0.15 else "risk acceptable",
            "assumptions": {
                "market_shock": market_shock,
                "volatility_multiplier": volatility_multiplier,
                "liquidity_penalty_pct": liquidity_penalty_pct,
            },
            "layer_losses": {
                "market_loss": float(loss),
                "volatility_penalty": float(volatility_penalty),
                "liquidity_penalty": float(liquidity_penalty),
                "total_loss": float(adjusted_loss),
            },
            "layer_loss_pct": {
                "market_loss_pct": f"{loss_pct * 100:.2f}%",
                "volatility_penalty_pct": f"{volatility_penalty_pct * 100:.2f}%",
                "liquidity_penalty_pct": f"{liquidity_penalty_pct * 100:.2f}%",
                "total_loss_pct": f"{adjusted_loss_pct * 100:.2f}%",
            },
        }

    scenario_results = [_run_one_scenario(item) for item in scenarios_input]
    if not scenario_results:
        scenario_results = [_run_one_scenario("market_crash")]

    worst_case = max(scenario_results, key=lambda item: float(item.get("loss", 0.0)))
    summary = {
        "count": len(scenario_results),
        "worst_scenario": worst_case.get("scenario"),
        "worst_loss": float(worst_case.get("loss", 0.0)),
        "worst_loss_pct": str(worst_case.get("loss_percentage", "0.00%")),
    }

    if len(scenario_results) == 1:
        result = dict(scenario_results[0])
        result["portfolio_id"] = portfolio_id
        result["input_mode"] = input_mode
        result["scenario_results"] = scenario_results
        result["summary"] = summary
        return ok(result, source_chain=_dedupe_chain(source_chain))

    batch = {item["scenario"]: item for item in scenario_results}
    return ok(
        {
            "portfolio_id": portfolio_id,
            "input_mode": input_mode,
            "scenarios": batch,
            "scenario_results": scenario_results,
            "summary": summary,
            "current_value": total_value,
            "count": len(scenario_results),
        },
        source_chain=_dedupe_chain(source_chain),
    )


__all__ = ["_handle_stress_test"]
