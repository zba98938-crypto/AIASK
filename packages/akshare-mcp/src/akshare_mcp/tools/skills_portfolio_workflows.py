"""Extracted portfolio-oriented skill workflows."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

import numpy as np

from ..utils import normalize_code
from . import skills_support as skill_support


def _skill_support():
    return skill_support


async def exec_fund_manager_pro(params: Dict[str, Any]) -> Dict[str, Any]:
    from .market.kline import get_kline
    from .market.quote import get_realtime_quote
    from .news import get_stock_notices, get_stock_research
    from ..services import backtest_engine
    from ..services.portfolio_optimization import (
        simple_portfolio_optimizer as portfolio_optimizer,
    )
    from ..services.risk_model import risk_model

    skill_support = _skill_support()

    task = str(params.get("task") or "full_cycle").strip().lower()

    raw_codes = params.get("codes")
    if isinstance(raw_codes, str):
        raw_codes = [x.strip() for x in raw_codes.split(",") if x and str(x).strip()]
    if not isinstance(raw_codes, list) or not raw_codes:
        raw_codes = [params.get("code") or "600519", "000001", "000858"]

    dedup_codes: List[str] = []
    seen: set[str] = set()
    for raw in raw_codes:
        code = normalize_code(str(raw or ""))
        if not code or code in seen:
            continue
        seen.add(code)
        dedup_codes.append(code)
    if not dedup_codes:
        dedup_codes = ["600519", "000001", "000858"]

    lead_code = dedup_codes[0]
    lookback_days = max(30, int(params.get("lookback_days", 120) or 120))
    total_capital = float(params.get("total_capital", 1_000_000) or 1_000_000)
    optimization_method = str(params.get("method") or "equal_weight").strip().lower()
    confidence = float(params.get("confidence", 0.95) or 0.95)

    context: Dict[str, Any] = {
        "codes": dedup_codes,
        "lead_code": lead_code,
        "lookback_days": lookback_days,
        "total_capital": total_capital,
        "optimization_method": optimization_method,
        "confidence": confidence,
    }
    steps: List[Dict[str, Any]] = []

    def _research_step() -> Dict[str, Any]:
        end_date = str(params.get("end_date") or datetime.now().strftime("%Y-%m-%d"))
        start_date = str(
            params.get("start_date")
            or (datetime.now() - timedelta(days=int(params.get("event_window_days", 30) or 30))).strftime("%Y-%m-%d")
        )

        research_res = get_stock_research(
            stock_code=lead_code,
            limit=int(params.get("research_limit", 5) or 5),
        )
        notices_res = get_stock_notices(start_date=start_date, end_date=end_date, stock_code=lead_code)

        context["research"] = {"reports": research_res, "events": notices_res}

        reports_count = 0
        if isinstance(research_res, dict) and research_res.get("success"):
            reports_count = int((research_res.get("data") or {}).get("total") or 0)

        events_count = 0
        if isinstance(notices_res, dict) and notices_res.get("success"):
            events_count = len(((notices_res.get("data") or {}).get("events") or []))

        fallback_reasons = [
            reason
            for reason in [
                "research_reports_empty" if reports_count == 0 else None,
                "events_empty" if events_count == 0 else None,
            ]
            if reason
        ]

        return skill_support.ok(
            {
                "lead_code": lead_code,
                "window": {"start_date": start_date, "end_date": end_date},
                "reports_count": reports_count,
                "events_count": events_count,
                "fallback_used": bool(fallback_reasons),
                "fallback_reason": fallback_reasons or None,
            }
        )

    async def _portfolio_step() -> Dict[str, Any]:
        market_data: Dict[str, Dict[str, Any]] = {}
        dropped: List[str] = []
        returns_list: List[np.ndarray] = []
        valid_codes: List[str] = []

        for code in dedup_codes:
            kline_res = await get_kline(stock_code=code, period="daily", limit=max(lookback_days + 10, 80))
            if not (isinstance(kline_res, dict) and kline_res.get("success")):
                dropped.append(code)
                continue

            rows = kline_res.get("data") or []
            cleaned_rows = [r for r in rows if isinstance(r, dict) and r.get("close") is not None]
            if len(cleaned_rows) < 30:
                dropped.append(code)
                continue

            closes = []
            for row in cleaned_rows:
                try:
                    closes.append(float(row.get("close")))
                except Exception:
                    pass
            if len(closes) < 30:
                dropped.append(code)
                continue

            returns = np.diff(np.array(closes, dtype=float)) / np.array(closes[:-1], dtype=float)
            if len(returns) < 20:
                dropped.append(code)
                continue

            valid_codes.append(code)
            returns_list.append(returns)
            market_data[code] = {
                "rows": cleaned_rows,
                "latest_close": float(closes[-1]),
                "series_len": len(closes),
            }

        if not valid_codes:
            return skill_support.fail("No valid codes with sufficient kline data for portfolio construction")

        min_len = min(len(x) for x in returns_list)
        returns_matrix = np.array([x[-min_len:] for x in returns_list], dtype=float)

        weights_map: Dict[str, float]
        method_used = optimization_method
        try:
            if optimization_method == "risk_parity" and len(valid_codes) >= 2:
                weights_map = portfolio_optimizer.optimize_risk_parity(valid_codes, returns_matrix)
            elif optimization_method == "mean_variance" and len(valid_codes) >= 2:
                expected_returns = np.mean(returns_matrix, axis=1)
                weights_map = portfolio_optimizer.optimize_mean_variance(
                    valid_codes,
                    returns_matrix,
                    expected_returns,
                    risk_aversion=float(params.get("risk_aversion", 1.0) or 1.0),
                )
            elif optimization_method == "max_sharpe" and len(valid_codes) >= 2:
                expected_returns = np.mean(returns_matrix, axis=1)
                weights_map = portfolio_optimizer.optimize_max_sharpe(
                    valid_codes,
                    returns_matrix,
                    expected_returns,
                    risk_free_rate=float(params.get("risk_free_rate", 0.03) or 0.03),
                ).get("weights", {})
            else:
                method_used = "equal_weight"
                weights_map = portfolio_optimizer.optimize_equal_weight(valid_codes)
        except Exception:
            method_used = "equal_weight_fallback"
            weights_map = portfolio_optimizer.optimize_equal_weight(valid_codes)

        weight_sum = sum(float(v) for v in weights_map.values()) or 1.0
        normalized_weights = {code: float(weights_map.get(code, 0.0)) / weight_sum for code in valid_codes}

        holdings: List[Dict[str, Any]] = []
        for code in valid_codes:
            w = float(normalized_weights.get(code, 0.0))
            latest = float(market_data[code]["latest_close"])
            value = total_capital * w
            holdings.append({"code": code, "weight": w, "value": value, "latest_close": latest})

        context["portfolio"] = {
            "valid_codes": valid_codes,
            "dropped_codes": dropped,
            "weights": normalized_weights,
            "returns_matrix": returns_matrix,
            "holdings": holdings,
            "market_data": market_data,
            "method_used": method_used,
        }

        return skill_support.ok(
            {
                "method_used": method_used,
                "valid_codes": valid_codes,
                "dropped_codes": dropped,
                "weights": normalized_weights,
                "lookback_days": lookback_days,
            }
        )

    def _risk_step() -> Dict[str, Any]:
        portfolio_ctx = context.get("portfolio") or {}
        holdings = portfolio_ctx.get("holdings") or []
        returns_matrix = portfolio_ctx.get("returns_matrix")
        if not holdings or returns_matrix is None:
            return skill_support.fail("Portfolio context missing; cannot run risk stage")

        weights = np.array([float(h["weight"]) for h in holdings], dtype=float)
        if len(weights) == 1:
            portfolio_returns = returns_matrix[0]
            var_result = risk_model.calculate_var(
                portfolio_returns.tolist(),
                confidence=confidence,
                portfolio_value=total_capital,
            )
            risk_result = {
                "volatility": float(np.std(portfolio_returns)),
                "annual_volatility": float(np.std(portfolio_returns) * np.sqrt(252)),
                "variance": float(np.var(portfolio_returns)),
            }
        else:
            portfolio_returns = np.dot(weights, returns_matrix)
            var_result = risk_model.calculate_var(
                portfolio_returns.tolist(),
                confidence=confidence,
                portfolio_value=total_capital,
            )
            risk_result = risk_model.calculate_portfolio_risk(
                [{"code": h["code"], "weight": float(h["weight"])} for h in holdings],
                returns_matrix,
            )

        scenario_list = params.get("scenarios") or ["market_crash", "sector_rotation"]
        if isinstance(scenario_list, str):
            scenario_list = [x.strip() for x in scenario_list.split(",") if x.strip()]
        stress_results = []
        for scenario in scenario_list:
            stress_results.append(risk_model.stress_test(holdings, scenario=scenario))

        context["risk"] = {
            "var": var_result,
            "risk": risk_result,
            "stress_tests": stress_results,
            "portfolio_returns": portfolio_returns,
        }

        return skill_support.ok(
            {
                "confidence": confidence,
                "var": var_result,
                "risk": risk_result,
                "stress_tests": stress_results,
            }
        )

    def _compliance_step() -> Dict[str, Any]:
        portfolio_ctx = context.get("portfolio") or {}
        holdings = portfolio_ctx.get("holdings") or []
        if not holdings:
            return skill_support.fail("Portfolio context missing; cannot run compliance stage")

        first = holdings[0]
        order_code = str(params.get("order_code") or first["code"])
        direction = str(params.get("direction") or "buy").strip().lower()

        price = None
        for holding in holdings:
            if holding["code"] == order_code:
                price = holding.get("latest_close")
                break

        try:
            quote_res = get_realtime_quote(stock_code=order_code)
            if isinstance(quote_res, dict):
                if quote_res.get("success"):
                    quote_price = (quote_res.get("data") or {}).get("price")
                    if quote_price is not None:
                        price = quote_price
                elif quote_res.get("price") is not None:
                    price = quote_res.get("price")
        except Exception:
            quote_res = None
        if price is None:
            return skill_support.fail(f"Cannot resolve price for compliance check: {order_code}")

        target_value = total_capital * float(first["weight"])
        quantity = int(params.get("quantity") or (target_value / float(price) // 100) * 100)
        order_value = float(quantity) * float(price)
        max_single_order_pct = float(params.get("max_single_order_pct", 0.40) or 0.40)
        max_position_pct = float(params.get("max_position_pct", 0.40) or 0.40)

        checks = {
            "lot_size_100": quantity > 0 and quantity % 100 == 0,
            "price_positive": float(price) > 0,
            "single_order_limit": order_value <= total_capital * max_single_order_pct,
            "position_limit": float(first["weight"]) <= max_position_pct,
            "direction_valid": direction in {"buy", "sell"},
        }
        issues = [name for name, passed in checks.items() if not passed]
        passed = len(issues) == 0

        context["compliance"] = {
            "passed": passed,
            "issues": issues,
            "order": {
                "code": order_code,
                "direction": direction,
                "quantity": quantity,
                "price": float(price),
                "order_value": order_value,
            },
            "checks": checks,
        }

        return skill_support.ok(
            {
                "mode": "equivalent_check",
                "passed": passed,
                "issues": issues,
                "checks": checks,
                "order": context["compliance"]["order"],
            }
        )

    def _execution_step() -> Dict[str, Any]:
        compliance_ctx = context.get("compliance") or {}
        if not compliance_ctx:
            return skill_support.fail("Compliance context missing; cannot build execution plan")

        if not compliance_ctx.get("passed"):
            return skill_support.fail(f"Compliance gate not passed: {compliance_ctx.get('issues')}")

        order = compliance_ctx.get("order") or {}
        quantity = int(order.get("quantity") or 0)
        duration_minutes = max(5, int(params.get("duration_minutes", 60) or 60))
        slices = max(1, int(params.get("slices", 6) or 6))
        interval = max(1, duration_minutes // slices)

        base = quantity // slices
        remainder = quantity % slices
        plan: List[Dict[str, Any]] = []
        for idx in range(slices):
            plan.append(
                {
                    "slice": idx + 1,
                    "offset_min": idx * interval,
                    "quantity": base + (1 if idx < remainder else 0),
                    "algo": "twap",
                }
            )

        context["execution"] = {
            "task": "twap_plan_only",
            "duration_minutes": duration_minutes,
            "slices": slices,
            "plan": plan,
            "order": order,
        }
        return skill_support.ok(context["execution"])

    async def _review_step() -> Dict[str, Any]:
        portfolio_ctx = context.get("portfolio") or {}
        market_data = portfolio_ctx.get("market_data") or {}
        lead_rows = (market_data.get(lead_code) or {}).get("rows") or []
        if len(lead_rows) < 30:
            return skill_support.fail(f"Insufficient kline rows for review: {lead_code}")

        benchmark = normalize_code(str(params.get("benchmark") or "000300"))
        benchmark_rows: List[Dict[str, Any]] = []
        benchmark_res = await get_kline(stock_code=benchmark, period="daily", limit=len(lead_rows))
        if isinstance(benchmark_res, dict) and benchmark_res.get("success"):
            benchmark_rows = benchmark_res.get("data") or []

        backtest_params = {
            "initial_capital": float(total_capital * float(portfolio_ctx.get("weights", {}).get(lead_code, 1.0))),
            "commission": float(params.get("commission", 0.0003) or 0.0003),
            "slippage": float(params.get("slippage", 0.0001) or 0.0001),
            "short_period": int(params.get("short_period", 5) or 5),
            "long_period": int(params.get("long_period", 20) or 20),
            "benchmark": benchmark,
            "benchmark_klines": benchmark_rows,
        }
        backtest_result = backtest_engine.run_backtest(
            lead_code,
            lead_rows,
            strategy=str(params.get("strategy") or "ma_cross"),
            params=backtest_params,
        )
        if not backtest_result.get("success"):
            return skill_support.fail(backtest_result.get("error") or "Backtest failed")

        risk_ctx = context.get("risk") or {}
        review_data = {
            "lead_code": lead_code,
            "backtest": backtest_result.get("data"),
            "risk_snapshot": {
                "var": risk_ctx.get("var"),
                "volatility": (risk_ctx.get("risk") or {}).get("annual_volatility"),
            },
        }
        context["review"] = review_data
        return skill_support.ok(review_data)

    if task in {"full_cycle", "daily_brief", "smoke_test"}:
        steps.append(skill_support._run_step("research", _research_step))
        steps.append(await skill_support._run_step_async("portfolio_construction", _portfolio_step))
        steps.append(skill_support._run_step("risk_assessment", _risk_step))
        steps.append(skill_support._run_step("compliance_check", _compliance_step))
        steps.append(skill_support._run_step("execution_plan", _execution_step))
        steps.append(await skill_support._run_step_async("performance_review", _review_step))

        result = skill_support._finalize_skill_result(task, steps)
        ring_names = [
            "research",
            "portfolio_construction",
            "risk_assessment",
            "compliance_check",
            "execution_plan",
            "performance_review",
        ]
        ring_status = {
            ring: next((bool(step.get("success")) for step in steps if step.get("step") == ring), False)
            for ring in ring_names
        }
        result["summary"]["ring_status"] = ring_status
        result["summary"]["ring_count"] = len(ring_names)
        result["summary"]["ring_passed"] = sum(1 for passed in ring_status.values() if passed)
        result["summary"]["closed_loop_gate"] = all(ring_status.values())
        result["summary"]["note"] = (
            "Equivalent compliance and execution planning are used inside run_skill orchestrator; "
            "no live order is sent."
        )
        return result

    return {
        "task": task,
        "status": "unsupported_task",
        "steps": [],
        "summary": {
            "total_steps": 0,
            "failed_steps": [],
            "supported_tasks": ["full_cycle", "daily_brief", "smoke_test"],
        },
    }


def exec_asset_allocation(params: Dict[str, Any]) -> Dict[str, Any]:
    skill_support = _skill_support()

    task = str(params.get("task") or "allocation_plan").strip().lower()
    supported_tasks = ["allocation_plan", "rebalance_plan", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    risk_profile = str(params.get("risk_profile") or "balanced").strip().lower()
    horizon_years = max(1.0, skill_support._safe_float(params.get("horizon_years"), 5.0))
    total_capital = max(10_000.0, skill_support._safe_float(params.get("total_capital"), 1_000_000.0))
    liquidity_buffer = max(0.03, min(skill_support._safe_float(params.get("liquidity_buffer"), 0.10), 0.40))
    max_drawdown = max(0.05, min(skill_support._safe_float(params.get("max_drawdown"), 0.18), 0.45))
    rebalance_frequency = str(params.get("rebalance_frequency") or "monthly").strip().lower()
    rebalance_threshold = skill_support._normalize_rebalance_threshold(
        params.get("rebalance_threshold"),
        0.08,
    )

    model_map = {
        "conservative": {"equity_etf": 0.30, "bond_etf": 0.45, "gold_etf": 0.10, "cash": 0.15},
        "balanced": {"equity_etf": 0.55, "bond_etf": 0.25, "gold_etf": 0.10, "cash": 0.10},
        "growth": {"equity_etf": 0.72, "bond_etf": 0.12, "gold_etf": 0.08, "cash": 0.08},
        "aggressive": {"equity_etf": 0.82, "bond_etf": 0.06, "gold_etf": 0.06, "cash": 0.06},
    }
    target = dict(model_map.get(risk_profile, model_map["balanced"]))
    if horizon_years <= 3:
        shift = min(0.12, target.get("equity_etf", 0.0) * 0.2)
        target["equity_etf"] = max(0.0, target.get("equity_etf", 0.0) - shift)
        target["cash"] = target.get("cash", 0.0) + shift / 2
        target["bond_etf"] = target.get("bond_etf", 0.0) + shift / 2
    if liquidity_buffer > target.get("cash", 0.0):
        transfer = liquidity_buffer - target.get("cash", 0.0)
        target["cash"] = liquidity_buffer
        target["equity_etf"] = max(0.0, target.get("equity_etf", 0.0) - transfer)

    total_weight = sum(target.values()) or 1.0
    allocation = [
        {
            "asset_class": asset_class,
            "weight": round(weight / total_weight, 4),
            "target_value": round(total_capital * weight / total_weight, 2),
        }
        for asset_class, weight in target.items()
    ]
    rebalance_policy = {
        "frequency": rebalance_frequency,
        "threshold": rebalance_threshold,
        "cash_buffer": liquidity_buffer,
        "max_drawdown_guardrail": max_drawdown,
        "action_rule": "Only rebalance when drift exceeds threshold or liquidity/risk constraints change.",
    }
    steps = [
        skill_support._static_step(
            "collect_constraints",
            {
                "risk_profile": risk_profile,
                "horizon_years": horizon_years,
                "total_capital": total_capital,
                "liquidity_buffer": liquidity_buffer,
                "max_drawdown": max_drawdown,
            },
        ),
        skill_support._static_step(
            "construct_target_allocation",
            {"allocation": allocation, "model": risk_profile},
        ),
        skill_support._static_step("define_rebalance_policy", rebalance_policy),
    ]
    result = skill_support._finalize_skill_result(task, steps)
    result["summary"].update(
        {
            "risk_profile": risk_profile,
            "allocation_count": len(allocation),
            "target_allocation": allocation,
            "rebalance_policy": rebalance_policy,
        }
    )
    return result


async def exec_fee_costs(params: Dict[str, Any]) -> Dict[str, Any]:
    from .backtest import run_batch_backtest, run_simple_backtest

    skill_support = _skill_support()

    task = str(params.get("task") or "cost_sensitivity").strip().lower()
    supported_tasks = ["cost_sensitivity", "single_backtest", "batch_backtest", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    if task in {"cost_sensitivity", "smoke_test"}:
        initial_capital = max(10_000.0, skill_support._safe_float(params.get("initial_capital"), 1_000_000.0))
        annual_return = skill_support._safe_float(params.get("annual_return_assumption"), 0.10)
        turnover = max(0.1, skill_support._safe_float(params.get("turnover_per_year"), 3.0))
        years = max(1, skill_support._safe_int(params.get("years"), 5))
        scenarios = [
            {
                "label": "low_cost",
                "commission": skill_support._safe_float(params.get("low_commission"), 0.0002),
                "slippage": skill_support._safe_float(params.get("low_slippage"), 0.0001),
                "stamp_tax": skill_support._safe_float(params.get("low_stamp_tax"), 0.0005),
            },
            {
                "label": "base_case",
                "commission": skill_support._safe_float(params.get("commission"), 0.0003),
                "slippage": skill_support._safe_float(params.get("slippage"), 0.0002),
                "stamp_tax": skill_support._safe_float(params.get("stamp_tax"), 0.0005),
            },
            {
                "label": "high_cost",
                "commission": skill_support._safe_float(params.get("high_commission"), 0.0008),
                "slippage": skill_support._safe_float(params.get("high_slippage"), 0.0006),
                "stamp_tax": skill_support._safe_float(params.get("high_stamp_tax"), 0.0010),
            },
        ]
        comparisons = []
        for item in scenarios:
            total_cost_rate = turnover * (item["commission"] + item["slippage"] + item["stamp_tax"])
            net_return = annual_return - total_cost_rate
            terminal_value = initial_capital * ((1.0 + net_return) ** years)
            comparisons.append(
                {
                    "label": item["label"],
                    "annual_cost_rate": round(total_cost_rate, 4),
                    "net_return_assumption": round(net_return, 4),
                    "terminal_value": round(terminal_value, 2),
                    "capital_erosion": round(
                        initial_capital * ((1.0 + annual_return) ** years) - terminal_value,
                        2,
                    ),
                }
            )
        steps = [
            skill_support._static_step(
                "collect_cost_assumptions",
                {
                    "initial_capital": initial_capital,
                    "annual_return_assumption": annual_return,
                    "turnover_per_year": turnover,
                    "years": years,
                },
            ),
            skill_support._static_step("run_cost_sensitivity", {"scenarios": comparisons}),
            skill_support._static_step(
                "output_cost_guidance",
                {
                    "guidance": [
                        "Compare broker commission tiers before increasing turnover.",
                        "Treat slippage as a variable with market regime sensitivity.",
                        "Persist cost assumptions together with any backtest snapshot.",
                    ]
                },
            ),
        ]
        result = skill_support._finalize_skill_result(task, steps)
        result["summary"]["best_scenario"] = min(comparisons, key=lambda item: item["annual_cost_rate"])
        result["summary"]["worst_scenario"] = max(comparisons, key=lambda item: item["annual_cost_rate"])
        return result

    if task == "single_backtest":
        code = normalize_code(str(params.get("code") or "600519"))
        steps = [
            await skill_support._run_step_async(
                "run_simple_backtest",
                run_simple_backtest,
                code=code,
                strategy=str(params.get("strategy") or "ma_cross"),
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                initial_capital=skill_support._safe_float(params.get("initial_capital"), 100_000.0),
                commission=skill_support._safe_float(params.get("commission"), 0.0003),
                short_period=skill_support._safe_int(params.get("short_period"), 5),
                long_period=skill_support._safe_int(params.get("long_period"), 20),
                benchmark=str(params.get("benchmark") or "000300"),
                slippage=skill_support._safe_float(params.get("slippage"), 0.0),
            )
        ]
        return skill_support._finalize_skill_result(task, steps)

    codes = skill_support._normalize_codes_input(params.get("codes"), ["600519", "000001", "000858"])
    steps = [
        await skill_support._run_step_async(
            "run_batch_backtest",
            run_batch_backtest,
            codes=codes,
            strategy=str(params.get("strategy") or "ma_cross"),
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
            initial_capital=skill_support._safe_float(params.get("initial_capital"), 100_000.0),
            commission=skill_support._safe_float(params.get("commission"), 0.0003),
            short_period=skill_support._safe_int(params.get("short_period"), 5),
            long_period=skill_support._safe_int(params.get("long_period"), 20),
            use_parallel=bool(params.get("use_parallel", True)),
            fetch_concurrency=skill_support._safe_int(params.get("fetch_concurrency"), 8),
        )
    ]
    return skill_support._finalize_skill_result(task, steps)


def exec_performance_attribution(params: Dict[str, Any]) -> Dict[str, Any]:
    skill_support = _skill_support()

    task = str(params.get("task") or "attribution_report").strip().lower()
    supported_tasks = ["attribution_report", "benchmark_frame", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    holdings = skill_support._normalize_holdings_input(
        params,
        default_codes=["600519", "000001", "510300"],
    )
    portfolio_return = skill_support._safe_float(params.get("portfolio_return"), 0.08)
    benchmark_return = skill_support._safe_float(params.get("benchmark_return"), 0.05)
    contributions = []
    for item in holdings:
        security_return = skill_support._safe_float(item.get("return_pct"), portfolio_return)
        contributions.append(
            {
                "code": item["code"],
                "weight": item["weight"],
                "return_pct": security_return,
                "contribution_pct": round(item["weight"] * security_return, 4),
            }
        )
    contributions.sort(key=lambda item: item["contribution_pct"], reverse=True)
    steps = [
        skill_support._static_step(
            "collect_holdings_and_returns",
            {"holdings": holdings, "portfolio_return": portfolio_return},
        ),
        skill_support._static_step("compute_contribution_split", {"contributions": contributions}),
        skill_support._static_step(
            "compare_vs_benchmark",
            {
                "benchmark_return": benchmark_return,
                "active_return": round(portfolio_return - benchmark_return, 4),
                "risk_sources": list(
                    params.get("risk_sources") or ["allocation", "security_selection", "timing"]
                ),
            },
        ),
    ]
    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["top_contributor"] = contributions[0] if contributions else None
    result["summary"]["active_return"] = round(portfolio_return - benchmark_return, 4)
    return result


async def exec_portfolio(params: Dict[str, Any]) -> Dict[str, Any]:
    from .backtest import run_batch_backtest, run_simple_backtest

    skill_support = _skill_support()

    task = str(params.get("task") or "allocation_snapshot").strip().lower()
    supported_tasks = ["portfolio_backtest", "batch_backtest", "allocation_snapshot", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    if task == "portfolio_backtest":
        code = normalize_code(str(params.get("code") or "600519"))
        steps = [
            await skill_support._run_step_async(
                "run_simple_backtest",
                run_simple_backtest,
                code=code,
                strategy=str(params.get("strategy") or "ma_cross"),
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                initial_capital=skill_support._safe_float(params.get("initial_capital"), 100_000.0),
                commission=skill_support._safe_float(params.get("commission"), 0.0003),
                short_period=skill_support._safe_int(params.get("short_period"), 5),
                long_period=skill_support._safe_int(params.get("long_period"), 20),
                benchmark=str(params.get("benchmark") or "000300"),
                slippage=skill_support._safe_float(params.get("slippage"), 0.0),
            )
        ]
        return skill_support._finalize_skill_result(task, steps)

    if task == "batch_backtest":
        codes = skill_support._normalize_codes_input(params.get("codes"), ["600519", "000001", "000858"])
        steps = [
            await skill_support._run_step_async(
                "run_batch_backtest",
                run_batch_backtest,
                codes=codes,
                strategy=str(params.get("strategy") or "ma_cross"),
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                initial_capital=skill_support._safe_float(params.get("initial_capital"), 100_000.0),
                commission=skill_support._safe_float(params.get("commission"), 0.0003),
                short_period=skill_support._safe_int(params.get("short_period"), 5),
                long_period=skill_support._safe_int(params.get("long_period"), 20),
                use_parallel=bool(params.get("use_parallel", True)),
                fetch_concurrency=skill_support._safe_int(params.get("fetch_concurrency"), 8),
            )
        ]
        return skill_support._finalize_skill_result(task, steps)

    holdings = skill_support._normalize_holdings_input(
        params,
        default_codes=["600519", "000001", "510300"],
    )
    steps = [
        skill_support._static_step(
            "build_allocation_snapshot",
            {
                "method": str(params.get("method") or "equal_weight"),
                "holdings": holdings,
                "estimated_capital": round(sum(float(item.get("value") or 0.0) for item in holdings), 2),
            },
        ),
        skill_support._static_step(
            "outline_risk_checks",
            {
                "checks": [
                    "Validate concentration and sector exposure",
                    "Run historical drawdown and stress scenarios",
                    "Persist the approved snapshot with assumptions",
                ]
            },
        ),
    ]
    return skill_support._finalize_skill_result(task, steps)


def exec_portfolio_manager_core(params: Dict[str, Any]) -> Dict[str, Any]:
    skill_support = _skill_support()

    task = str(params.get("task") or "closed_loop_plan").strip().lower()
    supported_tasks = ["closed_loop_plan", "execution_gate", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    codes = skill_support._normalize_codes_input(params.get("codes"), ["600519", "000001", "510300"])
    max_position_pct = max(0.05, min(skill_support._safe_float(params.get("max_position_pct"), 0.30), 0.60))
    max_drawdown = max(0.05, min(skill_support._safe_float(params.get("max_drawdown"), 0.18), 0.50))
    stages = [
        {"stage": "profile", "gate": "risk_profile_resolved", "passed": bool(params.get("risk_profile") or True)},
        {"stage": "research", "gate": "candidate_codes_ready", "passed": bool(codes)},
        {"stage": "portfolio", "gate": "position_limit_set", "passed": max_position_pct <= 0.50},
        {"stage": "risk", "gate": "drawdown_guardrail_set", "passed": max_drawdown <= 0.35},
        {"stage": "execution", "gate": "execution_plan_documented", "passed": True},
        {"stage": "review", "gate": "post_trade_review_rule_defined", "passed": True},
    ]
    steps = [
        skill_support._static_step(
            "define_closed_loop_goal",
            {
                "goal": str(params.get("goal") or "Maintain a repeatable portfolio decision loop"),
                "risk_profile": str(params.get("risk_profile") or "balanced"),
                "codes": codes,
            },
        ),
        skill_support._static_step("evaluate_stage_gates", {"stages": stages}),
    ]
    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["closed_loop_gate"] = all(stage["passed"] for stage in stages)
    return result
