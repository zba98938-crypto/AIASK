"""Extracted quant-oriented skill workflows."""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np

from ..utils import normalize_code
from . import skills_support as skill_support


def _skill_support():
    return skill_support


async def exec_factor_mining(
    params: Dict[str, Any],
    *,
    runtime_quant_manager,
) -> Dict[str, Any]:
    skill_support = _skill_support()

    task = str(params.get("task") or "candidate_pipeline").strip().lower()
    supported_tasks = [
        "candidate_pipeline",
        "candidate_generation",
        "candidate_registry",
        "scheduler_check",
        "smoke_test",
    ]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    codes = skill_support._normalize_codes_input(
        params.get("stock_codes") or params.get("codes") or params.get("code"),
        ["600519", "000001", "000858"],
    )
    candidate_count = max(1, min(skill_support._safe_int(params.get("candidate_count"), 6), 16))
    lookback_bars = max(120, min(skill_support._safe_int(params.get("lookback_bars"), 220), 500))
    horizon_days = max(3, min(skill_support._safe_int(params.get("horizon_days"), 10), 30))
    max_dates = max(20, min(skill_support._safe_int(params.get("max_dates"), 60), 120))
    limit = max(1, min(skill_support._safe_int(params.get("limit"), 20), 500))
    candidate_index = max(0, skill_support._safe_int(params.get("candidate_index"), 0))
    run_scheduler_now = bool(skill_support._parse_bool_flag(params.get("run_scheduler_now")))

    generation_kwargs = {
        "codes": codes,
        "artifact_id": params.get("artifact_id"),
        "candidate_count": candidate_count,
        "lookback_bars": lookback_bars,
        "alternative_lookback_days": max(
            7,
            min(skill_support._safe_int(params.get("alternative_lookback_days"), 30), 90),
        ),
        "allow_fallback": True
        if params.get("allow_fallback") is None
        else bool(params.get("allow_fallback")),
        "persist_artifact": True
        if params.get("persist_artifact") is None
        else bool(params.get("persist_artifact")),
        "dedup_mode": str(params.get("dedup_mode") or "penalty"),
        "dedup_high_similarity_threshold": skill_support._safe_float(
            params.get("dedup_high_similarity_threshold"),
            0.98,
        ),
        "dedup_failure_similarity_threshold": skill_support._safe_float(
            params.get("dedup_failure_similarity_threshold"),
            0.93,
        ),
        "startup_warmup": bool(skill_support._parse_bool_flag(params.get("startup_warmup")))
        if params.get("startup_warmup") is not None
        else None,
        "startup_warmup_force": bool(
            skill_support._parse_bool_flag(params.get("startup_warmup_force"))
        )
        if params.get("startup_warmup_force") is not None
        else None,
        "startup_warmup_limit": max(
            1,
            min(skill_support._safe_int(params.get("startup_warmup_limit"), 4), 20),
        ),
        "startup_warmup_task_type": str(
            params.get("startup_warmup_task_type") or "core_market,factor_context"
        ),
    }
    validation_kwargs = {
        "artifact_id": params.get("artifact_id"),
        "candidate_index": candidate_index,
        "candidate": params.get("candidate"),
        "codes": codes,
        "lookback_bars": lookback_bars,
        "horizon_days": horizon_days,
        "max_dates": max_dates,
        "persist_artifact": True
        if params.get("persist_artifact") is None
        else bool(params.get("persist_artifact")),
        "write_memory": True
        if params.get("write_memory") is None
        else bool(params.get("write_memory")),
        "output_artifact_id": params.get("output_artifact_id"),
    }
    registry_op = str(params.get("op") or "active_pool").strip().lower() or "active_pool"
    memory_op = str(params.get("memory_op") or "stats").strip().lower() or "stats"
    registry_kwargs = {
        "op": registry_op,
        "artifact_id": params.get("artifact_id"),
        "codes": codes,
        "family": params.get("family"),
        "grade": params.get("grade"),
        "recommendation": params.get("recommendation"),
        "min_score": params.get("min_score"),
        "only_active": True
        if params.get("only_active") is None and registry_op == "active_pool"
        else bool(params.get("only_active", False)),
        "limit": limit,
    }
    memory_kwargs = {
        "op": memory_op,
        "artifact_id": params.get("artifact_id"),
        "candidate": params.get("candidate"),
        "query_text": params.get("query_text"),
        "codes": codes,
        "status": params.get("status"),
        "family": params.get("family"),
        "limit": limit,
    }

    steps: List[Dict[str, Any]] = []

    if task == "candidate_generation":
        generation_resp = await runtime_quant_manager(
            action="llm_factor_mining",
            kwargs=generation_kwargs,
        )
        steps.append(
            skill_support._step_result(
                "quant_manager.llm_factor_mining",
                output=generation_resp,
            )
        )
        result = skill_support._finalize_skill_result(task, steps)
        result["summary"]["codes"] = codes
        result["summary"]["candidate_count"] = candidate_count
        result["summary"]["artifact_id"] = skill_support._response_data_dict(
            generation_resp
        ).get("artifact_id")
        return result

    if task == "candidate_registry":
        registry_resp = await runtime_quant_manager(
            action="factor_candidate_registry",
            kwargs=registry_kwargs,
        )
        steps.append(
            skill_support._step_result(
                "quant_manager.factor_candidate_registry",
                output=registry_resp,
            )
        )
        memory_resp = await runtime_quant_manager(
            action="factor_research_memory",
            kwargs=memory_kwargs,
        )
        steps.append(
            skill_support._step_result(
                "quant_manager.factor_research_memory",
                output=memory_resp,
            )
        )
        result = skill_support._finalize_skill_result(task, steps)
        result["summary"]["codes"] = codes
        result["summary"]["registry_op"] = registry_op
        result["summary"]["memory_op"] = memory_op
        return result

    if task == "scheduler_check":
        scheduler_status = await runtime_quant_manager(action="scheduler_status", kwargs={})
        steps.append(
            skill_support._step_result(
                "quant_manager.scheduler_status",
                output=scheduler_status,
            )
        )
        if run_scheduler_now:
            scheduler_run = await runtime_quant_manager(action="scheduler_run_now", kwargs={})
            steps.append(
                skill_support._step_result(
                    "quant_manager.scheduler_run_now",
                    output=scheduler_run,
                )
            )
        result = skill_support._finalize_skill_result(task, steps)
        result["summary"]["run_scheduler_now"] = run_scheduler_now
        return result

    generation_resp = await runtime_quant_manager(action="llm_factor_mining", kwargs=generation_kwargs)
    steps.append(
        skill_support._step_result(
            "quant_manager.llm_factor_mining",
            output=generation_resp,
        )
    )
    generation_data = skill_support._response_data_dict(generation_resp)
    resolved_artifact_id = str(
        params.get("artifact_id") or generation_data.get("artifact_id") or ""
    ).strip()

    if resolved_artifact_id or isinstance(params.get("candidate"), dict):
        validation_kwargs["artifact_id"] = resolved_artifact_id or validation_kwargs.get("artifact_id")
        validation_resp = await runtime_quant_manager(
            action="validate_factor_candidate",
            kwargs=validation_kwargs,
        )
        steps.append(
            skill_support._step_result(
                "quant_manager.validate_factor_candidate",
                output=validation_resp,
            )
        )
    else:
        steps.append(
            skill_support._static_step(
                "quant_manager.validate_factor_candidate.skipped",
                {"reason": "artifact_id_or_inline_candidate_required"},
            )
        )

    registry_resp = await runtime_quant_manager(
        action="factor_candidate_registry",
        kwargs=registry_kwargs,
    )
    steps.append(
        skill_support._step_result(
            "quant_manager.factor_candidate_registry",
            output=registry_resp,
        )
    )
    memory_resp = await runtime_quant_manager(
        action="factor_research_memory",
        kwargs=memory_kwargs,
    )
    steps.append(
        skill_support._step_result(
            "quant_manager.factor_research_memory",
            output=memory_resp,
        )
    )
    scheduler_status = await runtime_quant_manager(action="scheduler_status", kwargs={})
    steps.append(
        skill_support._step_result(
            "quant_manager.scheduler_status",
            output=scheduler_status,
        )
    )
    if task == "candidate_pipeline" and run_scheduler_now:
        scheduler_run = await runtime_quant_manager(action="scheduler_run_now", kwargs={})
        steps.append(
            skill_support._step_result(
                "quant_manager.scheduler_run_now",
                output=scheduler_run,
            )
        )

    result = skill_support._finalize_skill_result(task, steps)
    result["summary"].update(
        {
            "codes": codes,
            "candidate_count": candidate_count,
            "artifact_id": resolved_artifact_id or None,
            "registry_op": registry_op,
            "memory_op": memory_op,
            "run_scheduler_now": run_scheduler_now,
        }
    )
    return result


async def exec_quant_data_engineering(params: Dict[str, Any]) -> Dict[str, Any]:
    from .market.kline import get_kline_data
    from .market.quote import get_realtime_quote

    skill_support = _skill_support()

    task = str(params.get("task") or "quality_check").strip().lower()
    supported_tasks = ["quality_check", "warmup_blueprint", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    code = normalize_code(str(params.get("code") or "600519"))
    start_date, end_date = skill_support._default_notice_window(params)
    limit = max(20, skill_support._safe_int(params.get("limit"), 120))
    steps: List[Dict[str, Any]] = []
    if task in {"quality_check", "smoke_test"}:
        steps.append(skill_support._run_step("get_realtime_quote", get_realtime_quote, stock_code=code))
        steps.append(
            await skill_support._run_step_async(
                "get_kline_data",
                get_kline_data,
                code=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                limit=limit,
                adjust=str(params.get("adjust") or "qfq"),
            )
        )
    steps.append(
        skill_support._static_step(
            "define_warmup_blueprint",
            {
                "code": code,
                "window": {"start_date": start_date, "end_date": end_date},
                "fallback_chain": [
                    "data_warmup",
                    "sync_kline_data",
                    "batch_sync_klines",
                    "data_sync_manager(action=sync)",
                ],
                "quality_contract": [
                    "missing_values",
                    "duplicate_rows",
                    "price_jump_outliers",
                    "cache_staleness",
                ],
            },
        )
    )
    return skill_support._finalize_skill_result(task, steps)


def exec_quant(params: Dict[str, Any]) -> Dict[str, Any]:
    from .quant import _factor_library_payload

    skill_support = _skill_support()

    task = str(params.get("task") or "factor_inventory").strip().lower()
    supported_tasks = ["factor_inventory", "signal_research", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    factor_payload = _factor_library_payload(str(params.get("category") or "all"))
    selected_factor = str(params.get("factor") or "momentum").strip().lower()
    code = normalize_code(str(params.get("code") or "600519"))
    steps = [
        skill_support._static_step(
            "load_factor_library",
            {
                "factor_count": factor_payload.get("count"),
                "categories": factor_payload.get("categories"),
                "sample_factors": [item.get("name") for item in (factor_payload.get("factors") or [])[:5]],
            },
        )
    ]
    if task in {"signal_research", "smoke_test"}:
        steps.append(
            skill_support._static_step(
                "define_signal_research_card",
                {
                    "code": code,
                    "factor": selected_factor,
                    "window_days": max(10, skill_support._safe_int(params.get("window_days"), 60)),
                    "research_checks": [
                        "Verify data sufficiency and tradability",
                        "Measure factor direction and stability",
                        "Compare against alternative signal families",
                    ],
                },
            )
        )
    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["factor_count"] = factor_payload.get("count")
    return result


def exec_quant_methods_foundation(params: Dict[str, Any]) -> Dict[str, Any]:
    skill_support = _skill_support()

    task = str(params.get("task") or "risk_metrics").strip().lower()
    supported_tasks = ["risk_metrics", "correlation_frame", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    raw_series = params.get("series") or {
        "alpha": [0.01, -0.004, 0.006, 0.008, -0.002, 0.004],
        "beta": [0.008, -0.003, 0.004, 0.007, -0.001, 0.003],
    }
    series_map = {
        str(name): np.asarray(values or [], dtype=float)
        for name, values in dict(raw_series).items()
        if isinstance(values, list) and values
    }
    annualization_factor = max(
        1.0,
        skill_support._safe_float(params.get("annualization_factor"), 252.0),
    )
    if not series_map:
        return skill_support._unsupported_task_result(task, supported_tasks)

    metrics = {
        name: {
            "mean": round(float(np.mean(values)), 6),
            "volatility": round(float(np.std(values)), 6),
            "annualized_volatility": round(
                float(np.std(values) * np.sqrt(annualization_factor)),
                6,
            ),
            "max_drawdown_proxy": round(float(np.min(np.cumsum(values))), 6),
        }
        for name, values in series_map.items()
    }
    ordered_names = list(series_map.keys())
    matrix = np.vstack([series_map[name] for name in ordered_names])
    covariance = (
        np.cov(matrix).round(6).tolist()
        if len(ordered_names) > 1
        else [[float(np.var(matrix[0]))]]
    )
    correlation = (
        np.corrcoef(matrix).round(6).tolist()
        if len(ordered_names) > 1
        else [[1.0]]
    )
    steps = [
        skill_support._static_step("compute_risk_metrics", {"metrics": metrics}),
        skill_support._static_step(
            "compute_dependency_matrix",
            {"names": ordered_names, "covariance": covariance, "correlation": correlation},
        ),
    ]
    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["series_count"] = len(series_map)
    return result


def exec_quant_ml_signals(params: Dict[str, Any]) -> Dict[str, Any]:
    skill_support = _skill_support()

    task = str(params.get("task") or "signal_guardrails").strip().lower()
    supported_tasks = ["signal_guardrails", "research_card", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    guardrails = {
        "code": normalize_code(str(params.get("code") or "600519")),
        "factor": str(params.get("factor") or "momentum").strip().lower(),
        "train_window": max(60, skill_support._safe_int(params.get("train_window"), 252)),
        "test_window": max(20, skill_support._safe_int(params.get("test_window"), 63)),
        "requirements": [
            "Separate in-sample and out-of-sample windows",
            "Track feature drift and prediction drift",
            "Keep a plain-language explanation for every promoted signal",
            "Backtest after cost assumptions, not before",
        ],
    }
    steps = [
        skill_support._static_step("define_ml_signal_guardrails", guardrails),
        skill_support._static_step(
            "build_research_card",
            {
                "validation_stack": [
                    "factor_ic",
                    "group_backtest",
                    "oos_validation",
                    "stress_test",
                ],
                "failure_conditions": [
                    "feature_instability",
                    "oos_decay",
                    "excess_turnover",
                ],
            },
        ),
    ]
    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["research_ready"] = True
    return result


async def exec_quant_research_process(params: Dict[str, Any]) -> Dict[str, Any]:
    from .backtest import run_simple_backtest

    skill_support = _skill_support()

    task = str(params.get("task") or "stage_gate").strip().lower()
    supported_tasks = ["stage_gate", "backtest_gate", "smoke_test"]
    if task not in supported_tasks:
        return skill_support._unsupported_task_result(task, supported_tasks)

    code = normalize_code(str(params.get("code") or "600519"))
    factor = str(params.get("factor") or "momentum").strip().lower()
    hypothesis = str(
        params.get("hypothesis") or f"{factor} signal should improve risk-adjusted return"
    ).strip()
    stage_report = [
        {"stage": "definition", "passed": True, "note": hypothesis},
        {
            "stage": "data_gate",
            "passed": True,
            "note": "Use normalized kline inputs and explicit cost assumptions",
        },
        {"stage": "signal_gate", "passed": True, "note": f"Selected factor family: {factor}"},
        {
            "stage": "portfolio_gate",
            "passed": True,
            "note": "Position limits and risk budget must be written down",
        },
        {"stage": "review_gate", "passed": True, "note": "Persist results and limitations"},
    ]
    steps: List[Dict[str, Any]] = [
        skill_support._static_step("build_stage_gate_report", {"stages": stage_report})
    ]
    if task in {"backtest_gate", "smoke_test"}:
        steps.append(
            await skill_support._run_step_async(
                "run_simple_backtest",
                run_simple_backtest,
                code=code,
                strategy=str(params.get("strategy") or "ma_cross"),
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                initial_capital=skill_support._safe_float(
                    params.get("initial_capital"),
                    100_000.0,
                ),
                commission=skill_support._safe_float(params.get("commission"), 0.0003),
                short_period=skill_support._safe_int(params.get("short_period"), 5),
                long_period=skill_support._safe_int(params.get("long_period"), 20),
                benchmark=str(params.get("benchmark") or "000300"),
                slippage=skill_support._safe_float(params.get("slippage"), 0.0),
            )
        )
    result = skill_support._finalize_skill_result(task, steps)
    result["summary"]["all_stage_passed"] = all(stage["passed"] for stage in stage_report)
    return result
