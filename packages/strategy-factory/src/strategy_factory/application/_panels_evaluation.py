"""Panel construction and validation/risk report orchestration."""

from __future__ import annotations

from typing import List

import numpy as np
from aiask_quant_core.backtest.utils import normalize_trade_date

from ..domain.constants import FACTORY_VALIDATION_PANEL_SAMPLE_SIZE
from ..domain.targets import _resolve_strategy_sample_selection, _normalize_target_codes, _is_index_reference_code
from ..infrastructure.mcp_services import (
    get_backtest_engine_class,
    get_normalize_klines,
    get_risk_model_class,
    get_strategy_registry,
    get_validation_runtime,
)
from ._panels_normalizers import (
    _build_validation_focus_annotation,
    _generate_strategy_signal_series,
    _resolve_validation_focus,
    _resolve_validation_focus_layer,
)
from ._panels_policy import _apply_trade_quality_rating_adjustment


def _estimate_beta(
    own_returns: np.ndarray | None,
    market_returns: np.ndarray | None,
) -> float | None:
    """Beta of one holding against the sampled panel's equal-weighted return.

    Returns ``None`` rather than a default of 1.0 when it cannot be estimated:
    a defaulted beta is what let the stress test report the same loss for every
    portfolio regardless of what it held.
    """
    if own_returns is None or market_returns is None:
        return None
    length = min(len(own_returns), len(market_returns))
    if length < 24:
        return None
    own = np.asarray(own_returns[-length:], dtype=np.float64)
    market = np.asarray(market_returns[-length:], dtype=np.float64)
    mask = np.isfinite(own) & np.isfinite(market)
    if int(np.count_nonzero(mask)) < 24:
        return None
    own = own[mask]
    market = market[mask]
    market_variance = float(np.var(market))
    if market_variance <= 1e-18:
        return None
    covariance = float(np.cov(own, market)[0, 1])
    beta = covariance / market_variance
    if not np.isfinite(beta):
        return None
    return float(beta)


def _execution_panel_series(strategy_type: str, params: dict, code: str, rows: list[dict]) -> dict:
    """Use the same fill engine as backtests, retaining the final open book.

    Signal values are order events, never portfolio weights. Mark-to-market
    mode avoids fabricating a liquidation merely because the sample ends.
    """
    execution_params = {
        "initial_capital": 100000.0,
        "commission": 0.0003,
        "market_ruleset": "cn_equity",
        "execution_price_policy": "next_open",
        **dict(params or {}),
        "use_daily_execution_replay": True,
        "force_liquidate_at_end": False,
    }
    result = get_backtest_engine_class().run_backtest(
        code, rows, strategy_type, execution_params, True,
    )
    if not result.get("success"):
        raise ValueError(f"validation_execution_unavailable:{result.get('error')}")
    data = dict(result.get("data") or {})
    equity = np.asarray(data.get("equity_curve") or [], dtype=np.float64)
    if len(equity) != len(rows) or not np.all(np.isfinite(equity)) or np.any(equity <= 0):
        raise ValueError("validation_daily_equity_unavailable")
    quantity = sum(
        int(item.get("remaining_qty") or 0)
        for item in data.get("round_trip_positions") or []
        if item.get("status") == "open_mark_to_market"
    )
    return {"equity": equity, "quantity": quantity, "value": quantity * float(rows[-1]["close"])}


def _build_family_returns(
    strategy_registry,
    strategy_type: str,
    params: dict,
    kline_histories: List[list[dict]],
    close_histories: List[np.ndarray],
    volume_histories: List[np.ndarray],
    *,
    min_len: int,
    aligned_indices: list[np.ndarray] | None = None,
    codes: list[str] | None = None,
) -> np.ndarray | None:
    if min_len < 24 or not kline_histories:
        return None

    def _series_for(candidate_params: dict) -> np.ndarray | None:
        previous, current = [], []
        try:
            for index, rows in enumerate(kline_histories):
                executed = _execution_panel_series(strategy_type, candidate_params, codes[index] if codes else str(index), rows)
                indices = aligned_indices[index] if aligned_indices is not None else np.arange(len(rows) - min_len, len(rows))
                equity = executed["equity"]
                previous.append(equity[indices - 1])
                current.append(equity[indices])
        except (ValueError, TypeError, KeyError):
            return None
        if not current:
            return None
        prev = np.sum(previous, axis=0)
        return np.sum(current, axis=0) / prev - 1.0

    base = _series_for(dict(params or {}))
    if base is None:
        return None
    series_family = [base]
    for key, value in sorted((params or {}).items()):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value == 0:
            continue
        # Execution/cost assumptions stay fixed across the parameter family.
        if key in {"initial_capital", "commission", "slippage", "sell_tax_rate", "min_trade_lot", "max_position_pct", "market_impact_bps"}:
            continue
        for mult in (0.8, 1.2):
            varied = dict(params or {})
            varied[key] = max(1, int(round(value * mult))) if isinstance(value, int) else float(value * mult)
            candidate = _series_for(varied)
            if candidate is not None and not any(np.allclose(candidate, old, atol=1e-9, rtol=1e-6) for old in series_family):
                series_family.append(candidate)
    return np.column_stack(series_family)


async def _build_strategy_panels(strategy_type: str, params: dict, db, sample_size: int = FACTORY_VALIDATION_PANEL_SAMPLE_SIZE) -> dict:
    strategy_registry = get_strategy_registry()
    normalize_klines = get_normalize_klines()
    selection = _resolve_strategy_sample_selection(strategy_type, dict(params or {}), sample_size=sample_size)
    sample_codes = list(selection.get("sample_codes") or [])
    histories, pair_indices = {}, {}
    skipped_codes, failed_codes = [], []
    for code in sample_codes:
        try:
            ordered = normalize_klines(await db.get_klines(code, limit=220))
            if len(ordered) < 90:
                skipped_codes.append({"code": code, "reason": f"insufficient_history:{len(ordered)}"})
                continue
            dates = [normalize_trade_date(row.get("date") or row.get("trade_date") or row.get("time")) for row in ordered]
            if not all(dates) or len(set(dates)) != len(dates) or dates != sorted(dates):
                skipped_codes.append({"code": code, "reason": "invalid_or_duplicate_trade_dates"})
                continue
            if any(not np.isfinite(float(row.get("close") or 0)) or float(row.get("close") or 0) <= 0 for row in ordered):
                skipped_codes.append({"code": code, "reason": "invalid_close"})
                continue
            histories[code] = ordered
            # Both interval endpoints must agree. Matching only the ending day
            # would mix a one-session return with a multi-session suspension gap.
            pair_indices[code] = {(dates[i - 1], dates[i]): i for i in range(1, len(dates))}
        except Exception as exc:
            failed_codes.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
    coverage = {"requested": len(sample_codes), "used": len(histories), "skipped_codes": skipped_codes, "failed_codes": failed_codes}
    missing = {"sample_codes": sample_codes, "sample_selection": selection, "panel_coverage": coverage}
    if len(histories) < 3:
        return {**missing, "reason": "insufficient_panel_symbols"}
    common = sorted(set.intersection(*(set(pairs) for pairs in pair_indices.values())))[-120:]
    if len(common) < 60:
        return {**missing, "reason": "insufficient_common_trade_intervals", "common_interval_count": len(common)}
    factors, raw_returns, prev_equity, curr_equity = [], [], [], []
    retained_histories, closes_history, volumes_history, indices_history, holdings = [], [], [], [], []
    used_codes = []
    for code, full_rows in histories.items():
        try:
            indices = np.asarray([pair_indices[code][pair] for pair in common], dtype=int)
            rows = full_rows[:int(indices[-1]) + 1]
            closes = np.asarray([float(row["close"]) for row in rows])
            volumes = np.asarray([float(row.get("volume") or 0) for row in rows])
            signals = _generate_strategy_signal_series(strategy_registry, strategy_type, params or {}, closes, volumes, klines=rows)
            executed = _execution_panel_series(strategy_type, params, code, rows)
            equity = executed["equity"]
            factors.append(signals[indices - 1])
            raw_returns.append(closes[indices] / closes[indices - 1] - 1.0)
            prev_equity.append(equity[indices - 1]); curr_equity.append(equity[indices])
            retained_histories.append(rows); closes_history.append(closes); volumes_history.append(volumes); indices_history.append(indices)
            used_codes.append(code)
            if executed["quantity"] > 0:
                holdings.append({"code": code, "value": executed["value"], "quantity": executed["quantity"], "as_of_date": common[-1][1]})
        except Exception as exc:
            failed_codes.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
    coverage["used"] = len(used_codes)
    if len(used_codes) < 3:
        return {**missing, "reason": "insufficient_execution_panel_symbols"}
    return_panel = np.column_stack(raw_returns)
    total_equity = float(np.sum(curr_equity, axis=0)[-1])
    for holding in holdings:
        holding["weight"] = holding["value"] / total_equity
        beta = _estimate_beta(raw_returns[used_codes.index(holding["code"])], np.mean(return_panel, axis=1))
        if beta is not None:
            holding["beta"] = beta
    return {
        **missing,
        "factor_panel": np.column_stack(factors),
        "return_panel": return_panel,
        "strategy_returns": np.sum(curr_equity, axis=0) / np.sum(prev_equity, axis=0) - 1.0,
        "family_returns": _build_family_returns(strategy_registry, strategy_type, params, retained_histories, closes_history, volumes_history, min_len=len(common), aligned_indices=indices_history, codes=used_codes),
        "holdings": holdings,
        "portfolio_value": total_equity,
        "cash_value": total_equity - sum(holding["value"] for holding in holdings),
        "dates": [pair[1] for pair in common],
        "return_intervals": common,
        "used_codes": used_codes,
        "strategy_return_source": "backtest_engine_daily_mark_to_market_after_costs",
    }


async def _run_validation_report(strategy_type: str, params: dict, db) -> dict | None:
    validation_runtime = get_validation_runtime()
    panels = await _build_strategy_panels(strategy_type, params, db)
    factor_panel = panels.get("factor_panel")
    return_panel = panels.get("return_panel")
    strategy_returns = panels.get("strategy_returns")
    family_returns = panels.get("family_returns")
    if factor_panel is None or return_panel is None:
        return None
    pipeline = validation_runtime.FactorValidationPipeline(validation_parallel=False)
    report = pipeline.run(
        factor_panel,
        return_panel,
        factor_name=f"strategy:{strategy_type}",
        validation_parallel=False,
        strategy_returns=strategy_returns,
        family_returns=family_returns,
    )
    sample_selection = dict(panels.get("sample_selection") or {})
    adjusted_report = _apply_trade_quality_rating_adjustment(
        dict(report or {}),
        strategy_type=strategy_type,
        params=dict(params or {}),
        strategy_returns=strategy_returns,
        sample_codes=list(panels.get("sample_codes") or []),
        sample_selection=sample_selection,
    )
    validation_focus = _resolve_validation_focus(dict(params or {}))
    validation_focus_layer = str(
        sample_selection.get("validation_focus_layer")
        or _resolve_validation_focus_layer(validation_focus)
        or "broad_market"
    ).strip().lower() or "broad_market"
    sample_selection_mode = str(
        sample_selection.get("sample_selection_mode") or "representative_only"
    ).strip().lower() or "representative_only"
    sample_alignment_reason = str(
        sample_selection.get("sample_alignment_reason") or ""
    ).strip() or None
    sample_codes = list(sample_selection.get("sample_codes") or panels.get("sample_codes") or [])
    validation_focus_annotation = _build_validation_focus_annotation(
        validation_focus,
        validation_focus_layer,
    )
    adjusted_report["validation_profile"] = {
        **dict(adjusted_report.get("validation_profile") or {}),
        "validation_focus": validation_focus or None,
        "validation_focus_layer": validation_focus_layer,
        "validation_focus_annotation": validation_focus_annotation,
    }
    adjusted_report["sample_selection"] = {
        "sample_codes": sample_codes,
        "sample_code_count": int(len(sample_codes)),
        "target_codes": list(sample_selection.get("target_codes") or []),
        "family_peer_codes": list(sample_selection.get("family_peer_codes") or []),
        "validation_focus": validation_focus or None,
        "validation_focus_layer": validation_focus_layer,
        "sample_selection_mode": sample_selection_mode,
        "sample_alignment_reason": sample_alignment_reason,
        "requested_sample_size": int(sample_selection.get("requested_sample_size") or 0),
        "effective_sample_size": int(sample_selection.get("effective_sample_size") or len(sample_codes)),
        "statistical_sample_min": int(sample_selection.get("statistical_sample_min") or 0),
        "statistical_sample_expanded": bool(sample_selection.get("statistical_sample_expanded")),
    }
    adjusted_report["validation_focus"] = validation_focus or None
    adjusted_report["validation_focus_layer"] = validation_focus_layer
    adjusted_report["validation_focus_annotation"] = validation_focus_annotation
    adjusted_report["sample_selection_mode"] = sample_selection_mode
    adjusted_report["sample_alignment_reason"] = sample_alignment_reason
    adjusted_report["sample_codes"] = sample_codes
    return adjusted_report


async def _build_target_risk_panels(strategy_type: str, params: dict, db) -> dict:
    """Replay recorded target sleeves; never fill missing weights with peers."""
    contract = dict(params.get("target_risk_contract") or {})
    targets = _normalize_target_codes(contract.get("target_codes") or [])
    weights = dict(contract.get("allocation_weights") or {})
    execution = dict(contract.get("execution_kwargs") or {})
    result = {
        "available": False, "target_codes": targets, "used_codes": [],
        "allocation_weights": weights, "allocation_source": contract.get("allocation_source"),
        "tested_object_hash": contract.get("tested_object_hash"),
        "current_tested_object_hash": contract.get("current_tested_object_hash"),
        "execution_kwargs": execution, "scope": "actual_target_portfolio",
        "beta_reference": {"code": contract.get("benchmark_code"), "source": "dated_index_returns"},
        "replay_mode": "target_sleeves_mark_to_market_no_terminal_liquidation",
    }
    def unavailable(reason, **details):
        return {**result, "reason": reason, **details}
    if not contract.get("tested_object_hash") or contract.get("tested_object_hash") != contract.get("current_tested_object_hash"):
        return unavailable("target_risk_tested_object_mismatch")
    if not targets or contract.get("primary_layer") != "target":
        return unavailable("target_risk_primary_target_missing")
    if any(_is_index_reference_code(code) for code in targets):
        return unavailable("non_tradable_index_target")
    if contract.get("portfolio_engine_used"):
        return unavailable("target_risk_shared_cash_replay_unavailable")
    if set(weights) != set(targets):
        return unavailable("target_risk_allocation_missing_or_mismatched")
    try:
        weights = {code: float(value) for code, value in weights.items()}
    except (TypeError, ValueError):
        return unavailable("target_risk_allocation_invalid")
    if any(not np.isfinite(w) or w < 0 for w in weights.values()) or not 0 < sum(weights.values()) <= 1.00001:
        return unavailable("target_risk_allocation_invalid")
    required = ("initial_capital", "commission", "slippage", "execution_price_policy", "market_ruleset", "sell_tax_rate", "min_trade_lot", "t_plus_one")
    if contract.get("missing_execution_fields") or any(execution.get(k) is None for k in required):
        return unavailable("target_risk_execution_assumptions_missing")
    try:
        capital = float(execution["initial_capital"])
        if not np.isfinite(capital) or capital <= 0:
            return unavailable("target_risk_initial_capital_invalid")
    except (TypeError, ValueError):
        return unavailable("target_risk_initial_capital_invalid")
    normalize = get_normalize_klines()
    histories, intervals = {}, {}
    for code in targets:
        try:
            rows = normalize(await db.get_klines(code, limit=500))
            dates = [normalize_trade_date(row.get("date") or row.get("time")) for row in rows]
            if len(rows) < 90 or not all(dates) or len(set(dates)) != len(dates) or dates != sorted(dates):
                return unavailable("target_risk_invalid_target_history", failed_code=code)
            if any(not np.isfinite(float(row.get("close") or 0)) or float(row.get("close") or 0) <= 0 for row in rows):
                return unavailable("target_risk_invalid_target_history", failed_code=code)
            histories[code] = rows
            intervals[code] = {(dates[i-1], dates[i]): i for i in range(1, len(rows))}
        except Exception as exc:
            return unavailable("target_risk_target_data_unavailable", failed_code=code, error=f"{type(exc).__name__}: {exc}")
    common = sorted(set.intersection(*(set(value) for value in intervals.values())))[-120:]
    if len(common) < 60:
        return unavailable("target_risk_common_intervals_insufficient")
    # Benchmark supplies sensitivity only, never portfolio equity or holdings.
    benchmark_returns, benchmark_reason = None, None
    try:
        rows = normalize(await db.get_klines(str(contract.get("benchmark_code") or "sh000300"), limit=500))
        dates = [normalize_trade_date(row.get("date") or row.get("time")) for row in rows]
        if len(set(dates)) != len(dates) or not all(dates):
            raise ValueError("invalid_benchmark_dates")
        dated_returns = {(dates[i-1], dates[i]):float(rows[i]["close"]) / float(rows[i-1]["close"]) - 1 for i in range(1,len(rows))}
        benchmark_returns = np.asarray([dated_returns[pair] for pair in common], dtype=float)
    except Exception as exc:
        benchmark_reason = f"target_risk_benchmark_unavailable:{type(exc).__name__}"
    previous = np.full(len(common), max(0.0, 1.0-sum(weights.values())) * capital)
    current = previous.copy()
    holdings = []
    from .backtest_filter import _ensure_strategy_params
    resolved = _ensure_strategy_params(strategy_type, params, research_task=params.get("research_task"))
    for code in targets:
        try:
            indices = np.asarray([intervals[code][pair] for pair in common],dtype=int)
            rows = histories[code][:int(indices[-1])+1]
            executed = _execution_panel_series(strategy_type, {**resolved, **execution}, code, rows)
            equity = executed["equity"]
            previous += equity[indices-1] * weights[code]
            current += equity[indices] * weights[code]
            if executed["quantity"] > 0 and weights[code] > 0:
                closes = np.asarray([float(row["close"]) for row in rows])
                beta = _estimate_beta(closes[indices]/closes[indices-1]-1.0, benchmark_returns)
                holding = {"code":code, "value":executed["value"]*weights[code], "quantity":executed["quantity"]*weights[code], "unscaled_sleeve_quantity":executed["quantity"], "quantity_basis":"weighted_full_capital_sleeve", "allocation_weight":weights[code], "as_of_date":common[-1][1]}
                if beta is not None:
                    holding["beta"] = beta
                holdings.append(holding)
        except Exception as exc:
            return unavailable("target_risk_execution_unavailable", failed_code=code, error=f"{type(exc).__name__}: {exc}")
    nav = float(current[-1])
    for holding in holdings:
        holding["weight"] = holding["value"] / nav
    return {**result, "available":True, "reason":None, "used_codes":list(targets),
        "allocation_weights":weights, "returns":current/previous-1.0, "holdings":holdings,
        "portfolio_value":nav, "cash_value":nav-sum(h["value"] for h in holdings),
        "dates":[pair[1] for pair in common], "benchmark_reason":benchmark_reason}


async def _run_risk_report(strategy_type: str, params: dict, db) -> dict | None:
    risk_model = get_risk_model_class()
    target_payload = await _build_target_risk_panels(strategy_type, params, db)
    try:
        panels = await _build_strategy_panels(strategy_type, params, db)
    except Exception as exc:
        panels = {"reason": "supplemental_peer_risk_unavailable", "error": f"{type(exc).__name__}: {exc}"}
    strategy_returns = panels.get("strategy_returns")
    holdings = panels.get("holdings")
    # The validation panel includes family peers for statistical context. They
    # must never stand in for a candidate's target portfolio in a risk gate.
    # The target builder supplies its equity series and allocation values;
    # an incomplete target set fails closed instead of silently dropping names.
    target_returns = target_payload.get("returns")
    target_holdings = list(target_payload.get("holdings") or [])
    target_value = target_payload.get("portfolio_value")
    target_reason = target_payload.get("reason")
    if target_returns is None or target_value is None or not target_holdings:
        target_reason = target_reason or "no_positions"
        target_var = {"var_percent": None, "cvar_percent": None, "measurable": False, "reason": target_reason or "target_risk_unavailable"}
        target_stress = {"loss_percent": None, "measurable": False, "reason": target_reason or "target_risk_unavailable", "scenario": "market_crash"}
    else:
        target_returns = np.asarray(target_returns, dtype=float)
        target_var = risk_model.calculate_var(target_returns.tolist(), confidence=0.95, portfolio_value=1000000)
        target_stress = risk_model.stress_test(target_holdings, scenario="market_crash", portfolio_value=target_value)
        if target_stress.get("missing_sensitivity"):
            target_stress = {**target_stress, "loss_percent": None, "measurable": False, "reason": "target_risk_incomplete_sensitivity"}
    # Preserve peer-panel calculations strictly as supplemental diagnostics.
    try:
        peer_var = risk_model.calculate_var(strategy_returns.tolist(), confidence=0.95, portfolio_value=1000000) if strategy_returns is not None and len(strategy_returns) else {}
        peer_stress = risk_model.stress_test(holdings, scenario="market_crash", portfolio_value=panels.get("portfolio_value")) if holdings is not None else {}
    except Exception as exc:
        peer_var, peer_stress = {}, {}
        panels["reason"] = f"supplemental_peer_risk_unavailable:{type(exc).__name__}"
    # stress_loss_percent stays None when the scenario could not be measured
    # (no live position, or no estimable beta). The submission gate treats a
    # missing metric as missing evidence and hard-fails at incubation/live
    # level, which is the correct outcome — the previous `or 0.0` turned an
    # unmeasurable scenario into a measured 0% loss.
    raw_stress_loss = target_stress.get("loss_percent")
    stress_loss_percent = (
        round(float(raw_stress_loss), 4) if raw_stress_loss is not None else None
    )
    # Same rule as stress_loss_percent above, and for the same reason: a missing
    # measurement must stay missing. `var_report.get("var_percent", 0.0)` turned
    # an unmeasurable VaR into a measured 0%, which passes
    # RISK_REPORT_THRESHOLDS["var_percent_max"] = 4.5 without anything having been
    # computed. Eight of the stored quality reports read 0.0 this way.
    raw_var_percent = target_var.get("var_percent")
    var_percent = (
        round(float(raw_var_percent), 4) if raw_var_percent is not None else None
    )
    raw_cvar_percent = target_var.get("cvar_percent")
    cvar_percent = (
        round(float(raw_cvar_percent), 4) if raw_cvar_percent is not None else None
    )
    report = {
        "var_percent": var_percent,
        "cvar_percent": cvar_percent,
        "stress_loss_percent": stress_loss_percent,
        "scenario": target_stress.get("scenario"),
        "stress_measurable": bool(target_stress.get("measurable")),
        "var_measurable": bool(target_var.get("measurable")),
        "target_risk_binding": {
            "contract_version": "target_risk.v1",
            "scope": "actual_target_portfolio",
            "status": "measured" if target_payload.get("available") and target_var.get("measurable") and target_stress.get("measurable") else "missing",
            "target_codes": list(target_payload.get("target_codes") or []),
            "used_target_codes": list(target_payload.get("used_codes") or []),
            "allocation_weights": dict(target_payload.get("allocation_weights") or {}),
            "allocation_source": target_payload.get("allocation_source"),
            "beta_reference": target_payload.get("beta_reference"),
            "reason": target_reason or target_stress.get("reason"),
            "tested_object_hash": target_payload.get("tested_object_hash"),
            "current_tested_object_hash": target_payload.get("current_tested_object_hash"),
            "execution_kwargs": target_payload.get("execution_kwargs"),
            "replay_mode": target_payload.get("replay_mode"),
            "benchmark_reason": target_payload.get("benchmark_reason"),
            "holdings": target_holdings,
            "portfolio_value": target_value,
            "cash_value": target_payload.get("cash_value"),
            "failed_code": target_payload.get("failed_code"),
            "error": target_payload.get("error"),
        },
        "supplemental_peer_risk": {
            "binding": "advisory_only",
            "reason": panels.get("reason"),
            "used_codes": list(panels.get("used_codes") or []),
            "var_percent": peer_var.get("var_percent"),
            "cvar_percent": peer_var.get("cvar_percent"),
            "stress_loss_percent": peer_stress.get("loss_percent"),
            "stress_measurable": bool(peer_stress.get("measurable")),
        },
    }
    if target_var.get("reason"):
        report["var_reason"] = target_var.get("reason")
    if target_stress.get("reason"):
        report["stress_reason"] = target_stress.get("reason")
    if target_stress.get("coverage") is not None:
        report["stress_coverage"] = float(target_stress.get("coverage") or 0.0)
    panel_coverage = panels.get("panel_coverage")
    if panel_coverage:
        report["panel_coverage"] = panel_coverage
    return report
