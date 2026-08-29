"""Async data collectors for the strategy-factory quality session script."""

from __future__ import annotations

import json
from collections import Counter
import os as _os
import time
from datetime import datetime
from typing import Any

from _quality_session_common import (
    LOGGER, DEFAULT_EXECUTION_MODE, _iso_now, _now, _safe_float, _safe_int,
)
from akshare_mcp.storage.sqlite import close_db, get_db
from akshare_mcp.tools.managers.strategy_mgr_crud import handle_review_report
from akshare_mcp.tools.managers.strategy_mgr_lifecycle import (
    handle_execution_audit_verification,
    handle_factory_run_detail,
    handle_factory_runs,
    handle_incubation_overview,
)
from _quality_session_common import (
    _LEGACY_BUDGET_MISMATCH_FLAGS, _LEGACY_BUDGET_MISMATCH_NOTE_FRAGMENTS,
    _format_dt, _json_dump, _pct, _write_json,
)
from _quality_session_report import (
    _build_blocker_summary, _compact_run_detail, _extract_issue_flags,
    _format_top_blockers, _merge_strategy_samples, _select_representative_samples,
    _sort_strategy_samples, _quality_strategy_pool, _resolve_candidate_artifact,
)

def _split_run_ids(raw: Any) -> list[str]:
    value = str(raw or "").strip()
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]



async def _resolve_latest_run_id_since(started_at_iso: str | None) -> str | None:
    db = get_db()
    recent = await handle_factory_runs(db, {"limit": 10})
    items = list(((recent or {}).get("data") or {}).get("items") or [])
    if not started_at_iso:
        return str((items[0] or {}).get("run_id") or "").strip() or None
    try:
        started_at = datetime.fromisoformat(started_at_iso)
    except Exception:
        started_at = None
    for item in items:
        run_id = str((item or {}).get("run_id") or "").strip()
        if not run_id:
            continue
        if started_at is None:
            return run_id
        try:
            candidate_started_at = datetime.fromisoformat(str((item or {}).get("started_at") or ""))
        except Exception:
            candidate_started_at = None
        if candidate_started_at is None or candidate_started_at >= started_at:
            return run_id
    return None



async def _run_strategy_factory_once(
    *,
    factory_mod,
    codes: list[str],
    execution_mode: str,
    universe_limit: int = 0,
) -> dict[str, Any]:
    # P2-2: universe_limit>0 即走 dispatch 默认 universe 模式(覆盖全市场前 N 只)。
    # 原逻辑 `universe_limit>0 AND not codes` 因 --codes 默认非空(["601288"])恒 False,
    # 使 --universe-limit 被静默架空、永远只跑单标的。现在传了 universe_limit 即视为
    # 明确要全市场扫描(优先级高于 codes 默认值);不传 universe_limit 时才用显式 codes。
    use_dispatch = int(universe_limit or 0) > 0
    if use_dispatch:
        runner = factory_mod.StrategyFactoryRunner(
            interval_sec=300,
            run_once=True,
            target_codes=[],
            execution_mode=execution_mode,
            dispatch_run_mode=True,
            dispatch_default_universe=True,
            dispatch_default_universe_limit=int(universe_limit),
        )
    else:
        runner = factory_mod.StrategyFactoryRunner(
            interval_sec=300,
            run_once=True,
            target_codes=list(codes or []),
            execution_mode=execution_mode,
            dispatch_run_mode=False,
        )
    started_monotonic = time.monotonic()
    started_at = _iso_now()
    raw_result = await runner._execute_cycle()
    normalized = factory_mod._normalize_cycle_result(
        raw_result,
        elapsed_seconds=time.monotonic() - started_monotonic,
    )
    data = dict(normalized.get("data") or {})
    run_ids = _split_run_ids(data.get("run_id"))
    if not run_ids:
        fallback_run_id = await _resolve_latest_run_id_since(started_at)
        if fallback_run_id:
            run_ids = [fallback_run_id]
            data["run_id"] = fallback_run_id
            normalized["data"] = data
    return {
        "started_at": started_at,
        "completed_at": _iso_now(),
        "result": normalized,
        "run_ids": run_ids,
    }


async def _run_factor_mining_once(
    enabled: bool,
    *,
    round_no: int = 1,
    every_n_rounds: int = 10,
) -> dict[str, Any] | None:
    """因子挖掘按频率运行(默认每 10 轮一次),使因子超市持续有新候选但不拖慢每轮节奏。

    因子挖掘严格验证较重(含 llm_primary 180s 超时),每轮跑会显著拉长全链路单轮耗时。
    频率由 STRATEGY_QUALITY_FACTOR_MINING_EVERY_N_ROUNDS 控制(默认 10);第 1 轮总是跑一次。
    失败不阻断本轮(只记录)。
    """
    if not enabled:
        return None
    import os as _os
    try:
        every_n = int(str(_os.getenv("STRATEGY_QUALITY_FACTOR_MINING_EVERY_N_ROUNDS", every_n_rounds)).strip())
    except Exception:
        every_n = every_n_rounds
    every_n = max(1, every_n)
    # 第 1 轮跑一次(建立基线),之后每 every_n 轮跑一次;其余轮跳过。
    if round_no != 1 and (round_no % every_n) != 0:
        return {"skipped": True, "reason": f"factor_mining_every_{every_n}_rounds", "round": round_no}
    raw_engines = str(_os.getenv("STRATEGY_QUALITY_FACTOR_MINING_ENGINES", "rule_seed") or "").strip()
    engines = [item.strip() for item in raw_engines.replace(";", ",").split(",") if item.strip()]
    try:
        candidate_count = int(str(_os.getenv("STRATEGY_QUALITY_FACTOR_MINING_CANDIDATE_COUNT", "4")).strip())
    except Exception:
        candidate_count = 4
    try:
        evolution_generations = int(str(_os.getenv("STRATEGY_QUALITY_FACTOR_MINING_EVOLUTION_GENERATIONS", "1")).strip())
    except Exception:
        evolution_generations = 1
    candidate_count = max(1, min(candidate_count, 30))
    evolution_generations = max(0, min(evolution_generations, 5))
    started_at = _iso_now()
    def _clear_factor_research_cache() -> bool:
        try:
            from strategy_factory.application.research.factor_research import FactorResearchBuilder

            clear = getattr(FactorResearchBuilder, "_cache_clear", None)
            if callable(clear):
                clear()
                return True
        except Exception as exc:  # noqa: BLE001 - cache invalidation must not fail the round
            LOGGER.debug("Factor research cache clear failed: %s", exc)
        return False

    def _compact_strict_candidate_results(rows: Any, *, limit: int = 5) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for row in list(rows or [])[: max(0, limit)]:
            if not isinstance(row, dict):
                continue
            quick = dict(row.get("quick_evidence") or {})
            decision = dict(row.get("admission_decision") or {})
            trace = dict(row.get("generation_trace") or {})
            compact.append(
                {
                    "name": row.get("name"),
                    "generation_engine": row.get("generation_engine"),
                    "family": row.get("family"),
                    "expression_dsl": row.get("expression_dsl"),
                    "fitness": row.get("fitness"),
                    "quick_quality_score": quick.get("quality_score"),
                    "quick_rank_ic_mean": quick.get("rank_ic_mean"),
                    "strict_priority_rank": trace.get("strict_validation_priority_rank"),
                    "strict_gate_passed": bool(decision.get("strict_gate_passed")),
                    "admitted": bool(decision.get("admitted")),
                    "evidence_reasons": list(decision.get("evidence_reasons") or []),
                    "evidence_summary": dict(decision.get("evidence_summary") or {}),
                    "rating_grade": decision.get("rating_grade"),
                    "blockers": list(decision.get("blockers") or []),
                }
            )
        return compact

    async def _run_post_mining_maintenance(factory) -> dict[str, Any] | None:
        enabled_raw = str(_os.getenv("STRATEGY_QUALITY_FACTOR_MAINTENANCE_AFTER_MINING", "1") or "1").strip().lower()
        if enabled_raw not in {"1", "true", "yes", "on"}:
            return None
        try:
            timeout_sec = float(str(_os.getenv("STRATEGY_QUALITY_FACTOR_MAINTENANCE_TIMEOUT_SEC", "60") or "60").strip())
        except Exception:
            timeout_sec = 60.0
        timeout_sec = max(5.0, min(timeout_sec, 180.0))
        import asyncio as _asyncio

        try:
            maintenance = await _asyncio.wait_for(factory.run_maintenance(), timeout=timeout_sec)
        except _asyncio.TimeoutError:
            return {"success": False, "timeout": True, "timeout_sec": timeout_sec}
        except Exception as exc:  # noqa: BLE001 - maintenance is additive evidence, not a round blocker
            LOGGER.warning("Factor mining maintenance after quality round failed: %s", exc)
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        promotion_report = dict((maintenance or {}).get("promotion_report") or {})
        qc_report = dict((maintenance or {}).get("qc_pipeline_report") or {})
        return {
            "success": True,
            "timeout": False,
            "timeout_sec": timeout_sec,
            "promoted_count": int(
                promotion_report.get("promoted_count")
                or len(list(promotion_report.get("promoted") or []))
                or 0
            ),
            "checked_count": int(promotion_report.get("checked") or 0),
            "qc_enabled": bool(qc_report.get("enabled")),
            "qc_skipped": bool(qc_report.get("skipped")),
            "qc_processed": int(qc_report.get("processed") or 0),
            "pool_size": int((maintenance or {}).get("pool_size") or 0),
        }

    try:
        from strategy_factory.runtime.default_bootstrap import (
            ensure_default_runtime_services,
        )
        from strategy_factory.runtime.factor_mining import get_factor_mining_runtime

        ensure_default_runtime_services()
        runtime = get_factor_mining_runtime()
        result = await runtime.run_once(
            trigger="quality_session",
            engines=engines or None,
            candidate_count=candidate_count,
            evolution_generations=evolution_generations,
        )
        maintenance_result = await _run_post_mining_maintenance(runtime)
        cache_cleared = _clear_factor_research_cache()
        quality_summary = dict((result or {}).get("quality_summary") or {})
        quarantine_reappraisal = dict((result or {}).get("quarantine_reappraisal") or {})
        strict_candidate_results = _compact_strict_candidate_results(
            quality_summary.get("strict_candidate_results")
        )
        return {
            "started_at": started_at,
            "completed_at": _iso_now(),
            "result": {
                "success": bool((result or {}).get("success")),
                "requested_engines": list(engines or []),
                "requested_candidate_count": candidate_count,
                "requested_evolution_generations": evolution_generations,
                "run_id": (result or {}).get("run_id"),
                "raw_candidate_count": int((result or {}).get("raw_candidate_count") or 0),
                "evolved_count": int((result or {}).get("evolved_count") or 0),
                "validated_count": int((result or {}).get("validated_count") or 0),
                "admitted_count": int((result or {}).get("admitted_count") or 0),
                "active_promoted_count": int((result or {}).get("active_promoted_count") or 0),
                "cycle_active_promoted_count": int((result or {}).get("cycle_active_promoted_count") or 0),
                "reappraisal_promoted_count": int((result or {}).get("reappraisal_promoted_count") or 0),
                "quarantine_reappraisal": {
                    "scanned": int(quarantine_reappraisal.get("scanned") or 0),
                    "promoted": int(quarantine_reappraisal.get("promoted") or 0),
                    "kept_quarantine": int(quarantine_reappraisal.get("kept_quarantine") or 0),
                },
                "maintenance": maintenance_result,
                "factor_research_cache_cleared": cache_cleared,
                "quality_reject_reasons": dict(quality_summary.get("reject_reasons") or {}),
                "strict_candidate_results": strict_candidate_results,
                "pool_size": int((result or {}).get("pool_size") or 0),
                "engines_used": list((result or {}).get("engines_used") or []),
                "error": (result or {}).get("error"),
            },
        }
    except Exception as exc:  # noqa: BLE001 - 因子挖掘失败不得拖垮整轮 session
        LOGGER.warning("Factor mining round failed: %s", exc)
        return {
            "started_at": started_at,
            "completed_at": _iso_now(),
            "result": {"success": False, "error": f"{type(exc).__name__}: {exc}"},
        }


async def _collect_factor_shelf_status() -> dict[str, Any]:
    """采集因子超市池状态(active/quarantine/retired + IC 健康度),用于每轮记录与质量追踪。"""
    try:
        from akshare_mcp.services.factor_mining_factory.api import get_factor_pool_gateway

        gateway = get_factor_pool_gateway()
        status = await gateway.get_pool_status()
        health = dict((status or {}).get("pool_health") or {})
        return {
            "pool_size": (status or {}).get("pool_size") or (status or {}).get("size"),
            "active_count": health.get("active_promoted_count"),
            "quarantine_count": health.get("quarantine_count"),
            "retired_count": health.get("retired_count"),
            "recent_60d_icir": health.get("recent_60d_icir"),
            "evidence_insufficient_count": health.get("evidence_insufficient_count"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _run_signal_tracker_once(enabled: bool) -> dict[str, Any] | None:
    """P0-1: 在孵化前跑一轮 SignalTracker,为 observe 样本生成当日信号。

    历史断点:质量 session 只跑工厂+孵化,从不调度 SignalTracker,导致 strategy_signals
    表无新信号、孵化每轮 signals=0、纸面交易空转。此处显式驱动一轮,使信号→下单→成交链路
    在 session 内闭合。失败不阻断本轮(只记录)。
    """
    if not enabled:
        return None
    started_at = _iso_now()
    try:
        from strategy_factory.runtime.default_bootstrap import (
            ensure_default_runtime_services,
        )
        from strategy_factory.runtime.signal_tracker import get_signal_tracker_runtime

        ensure_default_runtime_services()
        tracker = get_signal_tracker_runtime()
        result = await tracker.run_once()
        return {
            "started_at": started_at,
            "completed_at": _iso_now(),
            "result": {
                "signals_generated": int((result or {}).get("signals_generated") or 0),
                "incubation_orders": int((result or {}).get("incubation_orders") or 0),
                "incubation_orders_filled": int((result or {}).get("incubation_orders_filled") or 0),
                "forward_returns_computed": int((result or {}).get("forward_returns_computed") or 0),
                "timeout": bool((result or {}).get("timeout")),
                "phase_timeout_count": int((result or {}).get("phase_timeout_count") or 0),
                "phase_timeouts": list((result or {}).get("phase_timeouts") or []),
                "phase_error_count": int((result or {}).get("phase_error_count") or 0),
                "runtime_universe": dict((result or {}).get("runtime_universe") or {}),
                "phase_results": dict((result or {}).get("phase_results") or {}),
                "errors": list((result or {}).get("errors") or [])[:8],
            },
        }
    except Exception as exc:  # noqa: BLE001 - 信号轮失败不得拖垮整轮 session
        LOGGER.warning("Signal tracker round failed: %s", exc)
        return {
            "started_at": started_at,
            "completed_at": _iso_now(),
            "result": {"signals_generated": 0, "error": f"{type(exc).__name__}: {exc}"},
        }


async def _run_incubation_once(enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.incubation import build_incubation_runtime

    ensure_default_runtime_services()
    runtime = build_incubation_runtime(
        dry_run=False,
        owns_paper_trading=False,
    )
    started_at = _iso_now()
    result = await runtime.run_once()
    return {
        "started_at": started_at,
        "completed_at": _iso_now(),
        "result": result,
    }


async def _call_optional_db_method(db, method_name: str, *args, **kwargs):
    method = getattr(db, method_name, None)
    if not callable(method):
        return None
    try:
        return await method(*args, **kwargs)
    except TypeError:
        try:
            return await method(*args)
        except Exception as exc:
            LOGGER.warning("Failed to call db.%s%s: %s", method_name, args, exc)
            return None
    except Exception as exc:
        LOGGER.warning("Failed to call db.%s%s: %s", method_name, args, exc)
        return None


async def _enrich_strategy_snapshot_with_persistence(
    strategy_snapshot: dict[str, Any],
    *,
    db,
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = dict(strategy_snapshot or {})
    strategy_id = str(snapshot.get("strategy_id") or "").strip()
    if not strategy_id:
        return snapshot

    if cache is not None and strategy_id in cache:
        snapshot.update(dict(cache[strategy_id]))
        return snapshot

    strategy_row = dict(
        await _call_optional_db_method(db, "get_strategy", strategy_id) or {}
    )
    latest_quality_report = dict(
        await _call_optional_db_method(db, "get_latest_strategy_quality_report", strategy_id)
        or {}
    )
    params = dict(strategy_row.get("params") or {})
    storage_audit = dict(params.get("_storage_audit") or {})
    dropped_large_nodes = dict(storage_audit.get("dropped_large_nodes") or {})
    quality_summary = dict(latest_quality_report.get("summary") or {})
    quality_gate = dict(latest_quality_report.get("quality_gate") or {})

    def _normalized_runtime_scalar(value: Any) -> Any:
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return value
        text = str(value or "").strip()
        if not text:
            return ""
        lowered = text.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        return lowered

    runtime_field_names = (
        "runtime_family_data_source",
        "proxy_runtime_used",
        "diagnostic_only",
        "execution_readiness_tier",
        "trade_prediction_contract_status",
        "trade_prediction_contract_observation_gap",
    )
    runtime_mismatch_fields: list[str] = []
    for field_name in runtime_field_names:
        gate_value = _normalized_runtime_scalar(quality_gate.get(field_name))
        summary_value = _normalized_runtime_scalar(quality_summary.get(field_name))
        if gate_value in (None, "") and summary_value in (None, ""):
            continue
        if gate_value != summary_value:
            runtime_mismatch_fields.append(field_name)

    diagnostics = {
        "persisted_params_storage_mode": str(storage_audit.get("storage_mode") or ""),
        "persisted_params_truncated": bool(storage_audit.get("truncated")),
        "persisted_params_original_size_bytes": _safe_int(storage_audit.get("original_size_bytes")),
        "persisted_params_dropped_large_keys": sorted(str(key) for key in dropped_large_nodes.keys()),
        "persisted_params_dropped_incubation_budget": "incubation_budget" in dropped_large_nodes,
        "persisted_observe_first_intake": params.get("observe_first_intake"),
        "persisted_incubation_budget_present": bool(params.get("incubation_budget")),
        "persisted_submission_lane": params.get("submission_lane"),
        "persisted_planned_submission_lane": params.get("planned_submission_lane"),
        "persisted_final_status": params.get("final_status"),
        "quality_report_submission_lane": quality_summary.get("submission_lane"),
        "quality_report_planned_submission_lane": quality_summary.get("planned_submission_lane"),
        "quality_report_incubation_budget_track": quality_summary.get("incubation_budget_track"),
        "quality_report_final_status": quality_summary.get("final_status"),
        "quality_report_formal_track_requested": quality_summary.get("formal_track_requested"),
        "quality_report_formal_track_auto_corrected": quality_summary.get("formal_track_auto_corrected"),
        "quality_report_formal_track_eligible": quality_summary.get("formal_track_eligible"),
        "quality_report_formal_auto_correction_source_track": quality_summary.get(
            "formal_auto_correction_source_track"
        ),
        "quality_report_submission_action_type": quality_summary.get("submission_action_type"),
        "quality_report_submission_action_trigger": quality_summary.get("submission_action_trigger"),
        "quality_report_runtime_bootstrap_reason": quality_summary.get("runtime_bootstrap_reason"),
        "quality_report_admission_decision": quality_summary.get("admission_decision"),
        "quality_report_runtime_family_data_source": quality_summary.get("runtime_family_data_source"),
        "quality_report_proxy_runtime_used": quality_summary.get("proxy_runtime_used"),
        "quality_report_diagnostic_only": quality_summary.get("diagnostic_only"),
        "quality_report_execution_readiness_tier": quality_summary.get("execution_readiness_tier"),
        "quality_report_trade_prediction_contract_status": quality_summary.get("trade_prediction_contract_status"),
        "quality_report_trade_prediction_contract_observation_gap": quality_summary.get(
            "trade_prediction_contract_observation_gap"
        ),
        "quality_gate_runtime_family_data_source": quality_gate.get("runtime_family_data_source"),
        "quality_gate_proxy_runtime_used": quality_gate.get("proxy_runtime_used"),
        "quality_gate_diagnostic_only": quality_gate.get("diagnostic_only"),
        "quality_gate_execution_readiness_tier": quality_gate.get("execution_readiness_tier"),
        "quality_gate_trade_prediction_contract_status": quality_gate.get("trade_prediction_contract_status"),
        "quality_gate_trade_prediction_contract_observation_gap": quality_gate.get(
            "trade_prediction_contract_observation_gap"
        ),
        "quality_runtime_context_consistent": not runtime_mismatch_fields,
        "quality_runtime_context_mismatch_fields": runtime_mismatch_fields,
    }
    snapshot.update(diagnostics)
    if cache is not None:
        cache[strategy_id] = dict(diagnostics)
    return snapshot


async def _collect_strategy_snapshot(strategy_id: str) -> dict[str, Any]:
    db = get_db()
    review_resp = await handle_review_report(db, {"strategy_id": strategy_id, "limit": 3})
    incubation_resp = await handle_incubation_overview(db, {"strategy_id": strategy_id})
    audit_resp = await handle_execution_audit_verification(db, {"strategy_id": strategy_id})
    review_data = dict((review_resp or {}).get("data") or {})
    incubation_data = dict((incubation_resp or {}).get("data") or {})
    audit_data = dict((audit_resp or {}).get("data") or {})
    review_summary = dict(review_data.get("summary") or {})
    signal_quality = dict(incubation_data.get("signal_quality") or {})
    business_admission = dict(review_data.get("business_admission_decision") or {})
    benchmark_comparison = dict(review_data.get("benchmark_comparison") or {})
    cost_sensitivity = dict(review_data.get("cost_sensitivity_summary") or {})
    review_passed = review_data.get("review_passed")
    if review_passed is None:
        review_passed = review_data.get("passed")
    strict_incubation_ready = review_data.get("strict_incubation_ready")
    if strict_incubation_ready is None:
        strict_incubation_ready = review_summary.get("strict_incubation_ready")
    audit_summary = dict(dict(audit_data.get("trade_round_trip") or {}).get("audit_summary") or {})
    snapshot = {
        "strategy_id": strategy_id,
        "review_passed": review_passed,
        "report_type": review_data.get("report_type"),
        "validation_grade": review_data.get("validation_grade") or review_summary.get("validation_grade"),
        "raw_validation_grade": review_data.get("raw_validation_grade") or review_summary.get("raw_validation_grade"),
        "validation_total_score": review_data.get("validation_total_score") or review_summary.get("validation_total_score"),
        "strategy_type": review_summary.get("strategy_type") or incubation_data.get("strategy_type"),
        "status_after_review": review_summary.get("status_after_review") or incubation_data.get("status"),
        "submission_lane": review_data.get("submission_lane") or review_summary.get("submission_lane"),
        "strict_incubation_ready": strict_incubation_ready,
        "live_candidate_ready": review_data.get("live_candidate_ready") or review_summary.get("live_candidate_ready"),
        "incubation_candidate_ready": review_data.get("incubation_candidate_ready")
        or review_summary.get("incubation_candidate_ready"),
        "admission_block_reasons": list(
            review_data.get("admission_block_reasons") or review_summary.get("admission_block_reasons") or []
        ),
        "trade_prediction_contract_status": review_data.get("trade_prediction_contract_status")
        or review_summary.get("trade_prediction_contract_status"),
        "trade_prediction_contract_reject_reasons": list(
            review_data.get("trade_prediction_contract_reject_reasons")
            or review_summary.get("trade_prediction_contract_reject_reasons")
            or []
        ),
        "evidence_gate_status": review_data.get("evidence_gate_status") or review_summary.get("evidence_gate_status"),
        "business_admission_status": business_admission.get("status"),
        "business_admission_decision": business_admission.get("decision"),
        "business_admission_reasons": list(business_admission.get("reasons") or []),
        "benchmark_oos_cagr": benchmark_comparison.get("oos_cagr"),
        "benchmark_oos_max_drawdown": benchmark_comparison.get("oos_max_drawdown"),
        "benchmark_available": benchmark_comparison.get("available"),
        "cost_review_decision": cost_sensitivity.get("review_decision"),
        "cost_post_cost_sharpe": (
            dict((list(cost_sensitivity.get("scenarios") or []) or [{}])[0]).get("post_cost_sharpe")
        ),
        "cost_total_return": (
            dict((list(cost_sensitivity.get("scenarios") or []) or [{}])[0]).get("total_return")
        ),
        "signal_coverage_ratio": signal_quality.get("coverage_ratio"),
        "primary_sample_count": signal_quality.get("primary_sample_count"),
        "primary_hit_rate": signal_quality.get("primary_hit_rate"),
        "primary_skill_lcb": signal_quality.get("primary_skill_lcb"),
        "audit_status": audit_data.get("status"),
        "audit_method": audit_data.get("method"),
        "execution_audit_gate_status": audit_data.get("execution_audit_gate_status") or audit_summary.get("execution_audit_gate_status"),
        "execution_audit_gate_reasons": list(
            audit_data.get("execution_audit_gate_reasons") or audit_summary.get("execution_audit_gate_reasons") or []
        ),
        "audit_recommendation_count": len(list(audit_data.get("recommendations") or [])),
        "audit_candidate_evidence_count": dict(audit_data.get("coverage") or {}).get("strategy_candidate_evidence_count"),
        "audit_signal_evidence_count": dict(audit_data.get("coverage") or {}).get("strategy_signal_evidence_count"),
        "forward_missing_days": list(signal_quality.get("missing_forward_days") or []),
    }
    return await _enrich_strategy_snapshot_with_persistence(snapshot, db=db)


async def _collect_paper_observation_backlog() -> dict[str, Any]:
    db = get_db()
    method = getattr(db, "get_paper_observation_backlog_status", None)
    if not callable(method):
        return {}
    try:
        return dict(await method(limit=500) or {})
    except TypeError:
        try:
            return dict(await method() or {})
        except Exception as exc:
            LOGGER.warning("Failed to collect paper observation backlog status: %s", exc)
            return {"error": str(exc)}
    except Exception as exc:
        LOGGER.warning("Failed to collect paper observation backlog status: %s", exc)
        return {"error": str(exc)}


async def _collect_run_snapshot(run_id: str, strategy_sample_limit: int) -> dict[str, Any]:
    db = get_db()
    detail_resp = await handle_factory_run_detail(db, {"run_id": run_id, "artifact_mode": "summary"})
    detail = dict((detail_resp or {}).get("data") or {})

    strategy_ids: list[str] = []
    for gate_name in ("gate_b", "gate_c"):
        gate_payload = dict(detail.get(gate_name) or {})
        for strategy_id in list(gate_payload.get("artifact_ids") or []):
            sid = str(strategy_id or "").strip()
            if sid and sid not in strategy_ids:
                strategy_ids.append(sid)

    analyzed_strategies: list[dict[str, Any]] = []
    analysis_limit = min(len(strategy_ids), max(10, max(1, strategy_sample_limit)))
    for strategy_id in strategy_ids[:analysis_limit]:
        analyzed_strategies.append(await _collect_strategy_snapshot(strategy_id))

    ranked_strategies = _sort_strategy_samples(analyzed_strategies)
    sampled_strategies = ranked_strategies[: max(1, strategy_sample_limit)]
    representative_samples = _select_representative_samples(ranked_strategies, limit=2)
    blocker_summary = _build_blocker_summary(analyzed_strategies)

    issue_flags, issue_notes = _extract_issue_flags(
        detail,
        _merge_strategy_samples([*representative_samples, *sampled_strategies]),
    )
    top_blockers = list(blocker_summary.get("top_blockers") or [])
    if top_blockers:
        issue_notes.append(
            "formal admission blockers among analyzed strategies: "
            f"{_format_top_blockers(top_blockers)}"
        )
    return {
        "run_id": run_id,
        "detail": _compact_run_detail(detail),
        "strategy_ids": strategy_ids,
        "sampled_strategies": sampled_strategies,
        "representative_samples": representative_samples,
        "blocker_summary": blocker_summary,
        "issue_flags": issue_flags,
        "issue_notes": issue_notes,
    }


async def _refresh_state_strategy_persistence_metadata(state: dict[str, Any]) -> bool:
    db = get_db()
    cache: dict[str, dict[str, Any]] = {}
    changed = False

    for entry in list(state.get("entries") or []):
        quality = dict(entry.get("quality_snapshot") or {})
        sampled_strategies = list(quality.get("sampled_strategies") or [])
        existing_representatives = list(quality.get("representative_samples") or [])
        if not sampled_strategies and not existing_representatives:
            continue

        refreshed_sampled = [
            await _enrich_strategy_snapshot_with_persistence(item, db=db, cache=cache)
            for item in sampled_strategies
        ]
        sampled_by_id = {
            str(item.get("strategy_id") or "").strip(): dict(item)
            for item in refreshed_sampled
            if str(item.get("strategy_id") or "").strip()
        }
        refreshed_existing_representatives: list[dict[str, Any]] = []
        for item in existing_representatives:
            strategy_id = str(item.get("strategy_id") or "").strip()
            if strategy_id and strategy_id in sampled_by_id:
                refreshed_existing_representatives.append(dict(sampled_by_id[strategy_id]))
                continue
            refreshed_existing_representatives.append(
                await _enrich_strategy_snapshot_with_persistence(item, db=db, cache=cache)
            )
        representative_pool = list(refreshed_sampled)
        seen_ids = {
            str(item.get("strategy_id") or "").strip()
            for item in representative_pool
            if str(item.get("strategy_id") or "").strip()
        }
        for item in refreshed_existing_representatives:
            strategy_id = str(item.get("strategy_id") or "").strip()
            if strategy_id and strategy_id in seen_ids:
                continue
            if strategy_id:
                seen_ids.add(strategy_id)
            representative_pool.append(item)
        refreshed_representatives = _select_representative_samples(representative_pool, limit=2)

        detail = dict(quality.get("detail") or {})
        derived_flags, derived_notes = _extract_issue_flags(
            detail,
            _merge_strategy_samples([*refreshed_representatives, *refreshed_sampled]),
        )
        existing_flags = [
            item
            for item in list(quality.get("issue_flags") or [])
            if str(item or "").strip() not in _LEGACY_BUDGET_MISMATCH_FLAGS
        ]
        existing_notes = [
            item
            for item in list(quality.get("issue_notes") or [])
            if not any(fragment in str(item or "") for fragment in _LEGACY_BUDGET_MISMATCH_NOTE_FRAGMENTS)
        ]
        merged_flags = list(dict.fromkeys([*existing_flags, *derived_flags]))
        merged_notes = list(dict.fromkeys([*existing_notes, *derived_notes]))

        if (
            refreshed_sampled != sampled_strategies
            or refreshed_representatives != existing_representatives
            or merged_flags != existing_flags
            or merged_notes != existing_notes
        ):
            quality["sampled_strategies"] = refreshed_sampled
            quality["representative_samples"] = refreshed_representatives
            quality["issue_flags"] = merged_flags
            quality["issue_notes"] = merged_notes
            entry["quality_snapshot"] = quality
            changed = True

    return changed
