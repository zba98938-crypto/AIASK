"""Measured trade holdout replay for an explicitly frozen candidate.

A historical backtest is never an OOS observation by default. The caller must
supply the candidate-bound, pre-period seal and its information cutoff; absent
or invalid provenance returns an unavailable metric, not a synthetic zero.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any

import numpy as np
from aiask_quant_core.backtest.utils import normalize_trade_date
from aiask_quant_core.backtest.strategy_registry import StrategyRegistry
from aiask_quant_core.backtest.builtin_strategies import BuyAndHoldStrategy, MaCrossStrategy, MomentumStrategy
from aiask_quant_core.backtest.dsl_strategy import DslRuleStrategy

from .candidate_contract import build_tested_object_hash, build_candidate_contract_hash, build_portfolio_candidate_contract


def _day(value: Any) -> str:
    day = normalize_trade_date(value)
    try:
        datetime.strptime(day, "%Y%m%d")
    except (ValueError, TypeError):
        return ""
    return day


def holdout_identity(candidate: dict, execution_params: dict) -> dict:
    """Persist these identities when sealing the candidate, before holdout data exists."""
    # Envelope application refreshes these cached summaries. Their underlying
    # logic and portfolio fields are bound independently below; hashing the
    # summaries themselves makes an unchanged, normalized candidate fail.
    execution_payload = {
        key: value for key, value in execution_params.items()
        if key not in {
            "resolved_candidate_envelope", "candidate_contract_snapshot",
            "candidate_contract_hash", "execution_contract_hash",
            "candidate_identity_signature", "tested_object_hash",
        }
    }
    if isinstance(execution_payload.get("research_task"), dict):
        execution_payload["research_task"] = {
            key: value for key, value in execution_payload["research_task"].items()
            if key not in {"universe_expansion_policy_explicit", "target_symbol_policy_explicit"}
        }
    return {
        "tested_object_hash": build_tested_object_hash(candidate),
        "candidate_contract_hash": build_candidate_contract_hash(contract=build_portfolio_candidate_contract(candidate)),
        "execution_params_hash": hashlib.sha256(json.dumps(execution_payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest(),
    }


async def replay_frozen_holdout(candidate: dict, histories: dict, engine: Any, execution_params: dict, *, codes: list[str], timeout: float) -> dict:
    contract = dict(candidate.get("oos_validation_contract") or {})
    unavailable = {"status": "unavailable", "total_return": None, "source": "frozen_candidate_holdout_replay"}
    def fail(reason: str, **details: Any) -> dict:
        return {**unavailable, "reason": reason, **details}
    if not contract:
        return fail("missing_oos_validation_contract")
    if contract.get("sealed") is not True or contract.get("excluded_from_selection") is not True or contract.get("opened_for_candidate_selection") is True:
        return fail("holdout_not_proven_excluded_from_selection")
    identities = holdout_identity(candidate, execution_params)
    if any(contract.get(key) != value for key, value in identities.items()):
        return fail("holdout_candidate_hash_mismatch")
    start, end = _day(contract.get("start")), _day(contract.get("end"))
    frozen, cutoff = _day(contract.get("frozen_at")), _day(contract.get("selection_data_end"))
    if not all((start, end, frozen, cutoff)) or not cutoff <= frozen < start <= end:
        return fail("invalid_holdout_information_cutoff")
    if not codes:
        return fail("holdout_target_symbols_missing")
    runtime, semantic_mode = StrategyRegistry.create_runtime_strategy(candidate["strategy_type"], execution_params)
    if execution_params.get("dsl") and semantic_mode != "compiled_dsl":
        return fail("holdout_executable_contract_unavailable")
    # Each supported implementation either has stateless entry conditions or
    # explicitly resets its position state. Other builtins can maintain their
    # own warmup position/cooldown; skipping their early fills is not a reset.
    if type(runtime) not in {BuyAndHoldStrategy, MaCrossStrategy, MomentumStrategy, DslRuleStrategy}:
        return fail("holdout_strategy_state_reset_unsupported")
    measured = []
    intervals = None
    for code in codes:
        raw = histories.get(code) or []
        by_date = {}
        for row in raw:
            day = _day(row.get("date") or row.get("trade_date") or row.get("time"))
            if not day or day in by_date:
                return fail("holdout_invalid_trade_dates", code=code)
            if day <= end:
                by_date[day] = row
        dates = sorted(by_date)
        test_dates = [day for day in dates if start <= day <= end]
        if len(test_dates) < 20 or test_dates[-1] != end:
            return fail("holdout_not_mature_or_insufficient_sessions", code=code)
        if intervals is None:
            intervals = test_dates
        elif intervals != test_dates:
            return fail("holdout_calendar_mismatch", code=code)
        first = dates.index(test_dates[0])
        if first < 60:
            return fail("holdout_insufficient_warmup", code=code)
        replay_rows = [by_date[day] for day in dates]
        replay_start_index = first - 1
        if type(runtime) is BuyAndHoldStrategy:
            # Its only signal is at index 0, and it has no warmup indicators.
            # Include the preceding signal bar so entry executes at the first
            # holdout open, with the normal costs and execution constraints.
            replay_rows = replay_rows[first - 1:]
            replay_start_index = 0
        params = {
            **execution_params,
            "trade_start_index": replay_start_index,
            "use_daily_execution_replay": True,
            "force_liquidate_at_end": True,
        }
        try:
            result = await asyncio.wait_for(asyncio.to_thread(engine.run_backtest, code, replay_rows, candidate["strategy_type"], params, True), timeout=timeout)
        except Exception as exc:
            return fail("holdout_execution_failed", code=code, error=f"{type(exc).__name__}: {exc}")
        data = dict(result.get("data") or {})
        value = data.get("total_return")
        if not result.get("success") or value is None or not np.isfinite(float(value)):
            return fail("holdout_return_unavailable", code=code)
        measured.append({"code": code, "total_return": float(value), "trades_count": data.get("trades_count"), "trade_start_index": replay_start_index})
    weights = execution_params.get("target_weight_map") or {}
    raw_weights = np.asarray([max(0.0, float(weights.get(row["code"], 1.0))) for row in measured])
    if raw_weights.sum() <= 0:
        return fail("holdout_invalid_target_weights")
    weights_array = raw_weights / raw_weights.sum()
    return {
        "status": "measured", "reason": None, "source": "frozen_candidate_holdout_replay",
        "total_return": float(np.dot(weights_array, [row["total_return"] for row in measured])),
        **identities, "start": start, "end": end,
        "frozen_at": contract["frozen_at"], "selection_data_end": cutoff,
        "excluded_from_selection": True, "sealed": True,
        "sessions": len(intervals or []), "sample_count": len(measured), "per_code": measured,
        "aggregation": "fixed_initial_capital_sleeves",
        "initial_state": "cash_at_holdout_start",
    }
