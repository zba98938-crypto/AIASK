"""策略工厂分级门禁。

Gate-0: 结构校验 — JSON 合法、strategy_type 合法、DSL 可编译
Gate-1: 快速筛选 — 少量代表性股票快速回测
Gate-2: 完整回测 — 仅对 Gate-1 Top-K 执行（复用 BacktestFilter）
Gate-3: 提交门禁 — 质量报告 + 风险报告 + 去重（委托 submission_gate / submitter 调用）
"""

from __future__ import annotations

from .precompile_contract import validate_precompile_candidate_contract
from .trade_prediction_contract import attach_trade_prediction_context
from strategy_factory.domain.target_count_resolver import resolve_target_symbol_limit  # noqa: F401


# --- quality_gates_parts/normalizers.py ---
import asyncio
import json
import logging
import math
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..domain.constants import (
    BACKTEST_CONCURRENCY,
    BENCHMARK_ASSUMED_ANNUAL_VOLATILITY,
    FACTORY_PRE_GATE_ENABLED,
    GATE1_PASS_RATIO,
    GATE1_REPRESENTATIVE_COUNT,
    GATE1_SHARPE_MIN,
    GATE2_TOPK_FLOOR,
    GATE2_TOPK_USE_CEIL,
    REPRESENTATIVE_STOCKS,
)
from ..domain.strategy_profile import candidate_signature, infer_candidate_strategy_profile
from ..domain.targets import _build_target_alignment_contract
from ..domain.targets import _extract_target_codes_from_payload
from ..domain.targets import _normalize_target_codes
from ..domain.targets import _normalize_research_task_contract
from ..infrastructure.mcp_services import get_strategy_dsl_compiler
from .backtest_filter import build_target_quality_gate_summary
from .candidate_contract import (
    apply_resolved_candidate_envelope,
    build_candidate_contract_hash,
    build_factory_backtest_assumptions,
    build_portfolio_candidate_contract,
    build_tested_object_hash,
    candidate_contract_value,
)
from .runtime import get_strategy_factory_package as _runtime_get_strategy_factory_package

logger = logging.getLogger(__name__)

_PRE_GATE_SIGNAL_DENSITY_MIN = 4.0
_PRE_GATE_SIGNAL_DENSITY_MAX = 100.0
_PRE_GATE_FAMILY_QUOTA_DEFAULT = 8
_PRE_GATE_PER_STOCK_QUOTA_DEFAULT = 5
_GATE_0_BATCH_SIZE = 100
_SNAPSHOT_ALIGNMENT_HARD_BLOCK_COVERAGE = 0.05
_SNAPSHOT_ALIGNMENT_HARD_BLOCK_INTERSECTION = 0.05
_SNAPSHOT_ALIGNMENT_SOFT_COVERAGE = 0.35
_SNAPSHOT_ALIGNMENT_SOFT_INTERSECTION = 0.25
_SNAPSHOT_HIGH_TURNOVER_THRESHOLD = 1.25
_SNAPSHOT_VERY_HIGH_TURNOVER_THRESHOLD = 1.75
_SNAPSHOT_LOW_EDGE_BUFFER = 0.12
_PIPELINE_MA_CROSS_GATE1_REPRESENTATIVE_FLOOR = 5
_PIPELINE_RSI_GATE1_REPRESENTATIVE_FLOOR = 4
_PIPELINE_RSI_ALIGNMENT_HARD_BLOCK_INTERSECTION = 0.5
_PIPELINE_MA_CROSS_HIGH_TURNOVER_THRESHOLD = 1.5
_PIPELINE_MA_CROSS_VERY_HIGH_TURNOVER_THRESHOLD = 2.0
_PIPELINE_MA_CROSS_EDGE_RETURN_FLOOR = 0.01
_RL_BANDIT_ALIGNMENT_HARD_BLOCK_COVERAGE = 0.3
_RL_BANDIT_ALIGNMENT_HARD_BLOCK_INTERSECTION = 0.45
_RL_BANDIT_ALIGNMENT_SOFT_COVERAGE = 0.4
_RL_BANDIT_ALIGNMENT_SOFT_INTERSECTION = 0.5
_RL_BANDIT_VOLATILITY_BREAKOUT_ALIGNMENT_HARD_BLOCK_INTERSECTION = 0.15
_RL_BANDIT_VOLATILITY_BREAKOUT_ALIGNMENT_MIN_TARGET_COUNT = 10
_PRE_GATE_MARKET_CAP_THRESHOLDS = {
    "high": 20_000_000_000.0,
    "medium": 8_000_000_000.0,
    "low": 2_000_000_000.0,
    "all": 0.0,
}
_PRE_GATE_TURNOVER_THRESHOLDS = {
    "high": 1_500_000_000.0,
    "medium": 300_000_000.0,
    "low": 50_000_000.0,
    "all": 0.0,
}

def _compat_setting(name: str, default):
    return default


async def _compat_gate_1_fast_screen(candidate: dict, db, *, kline_cache: Optional[Dict[str, list]] = None):
    return await gate_1_fast_screen(candidate, db, kline_cache=kline_cache)


def get_strategy_factory_package():
    return _runtime_get_strategy_factory_package()


def _normalized_gate_3_counts(submission_result: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    payload = dict(submission_result or {})
    input_count = int(payload.get("gate_3_input", payload.get("submitted", 0)))
    submitted = int(payload.get("submitted", 0))
    passed_count = int(payload.get("gate_3_passed", payload.get("passed_quality_gate", 0)))
    failed_count = int(payload.get("gate_3_failed", max(input_count - passed_count, 0)))
    provisional_passed_count = int(payload.get("gate_3_provisional_passed", 0))
    return {
        "input_count": input_count,
        "submitted": submitted,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "provisional_passed_count": provisional_passed_count,
    }


def build_pending_gate_3_report(pending_count: int) -> Dict[str, Any]:
    return {
        "status": "pending_submission_gate",
        "input_count": int(pending_count),
        "passed_count": 0,
        "failed_count": 0,
        "pending_count": int(pending_count),
        "delegate": "submission_gate.run_submission_quality_gate",
        "reason": "gate_3_executes_during_submission",
    }


def build_completed_gate_3_report(submission_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = dict(submission_result or {})
    counts = _normalized_gate_3_counts(payload)
    return {
        "gate_3": {
            "status": "completed_submission_gate",
            "input_count": counts["input_count"],
            "passed_count": counts["passed_count"],
            "failed_count": counts["failed_count"],
            "pending_count": 0,
            "provisional_passed_count": counts["provisional_passed_count"],
            "recorded_count": int(payload.get("gate_3_recorded") or 0),
            "quality_recorded_count": int(
                payload.get("gate3_quality_recorded")
                or payload.get("gate3_record_quality_qualified_count")
                or 0
            ),
            "diagnostic_recorded_count": int(payload.get("gate3_record_diagnostic_only_count") or 0),
            "failure_reason_topn": list(payload.get("gate_3_failure_reason_topn") or []),
            "gate3_audit_failed_count": int(payload.get("gate3_audit_failed_count") or counts["failed_count"]),
            "observe_admitted_count": int(payload.get("observe_admitted_count") or 0),
            "pre_observe_hard_reject_count": int(payload.get("pre_observe_hard_reject_count") or 0),
            "formal_incubation_count": int(payload.get("formal_incubation_count") or 0),
            "observe_incubation_count": int(payload.get("observe_incubation_count") or 0),
            "diagnostic_observation_count": int(payload.get("diagnostic_observation_count") or 0),
            "live_ready_review_count": int(payload.get("live_ready_review_count") or 0),
            "deferred_submission_count": int(payload.get("deferred_submission_count") or 0),
            "research_only_count": int(payload.get("research_only_count") or 0),
            "strict_incubation_ready_count": int(payload.get("strict_incubation_ready_count") or 0),
        },
        "final_decision": {
            "stage": "gate_3",
            "passed_count": counts["passed_count"],
            "failed_count": counts["failed_count"],
            "provisional_passed_count": counts["provisional_passed_count"],
            "recorded_count": int(payload.get("gate_3_recorded") or 0),
            "quality_recorded_count": int(
                payload.get("gate3_quality_recorded")
                or payload.get("gate3_record_quality_qualified_count")
                or 0
            ),
            "diagnostic_recorded_count": int(payload.get("gate3_record_diagnostic_only_count") or 0),
            "gate3_audit_failed_count": int(payload.get("gate3_audit_failed_count") or counts["failed_count"]),
            "observe_admitted_count": int(payload.get("observe_admitted_count") or 0),
            "pre_observe_hard_reject_count": int(payload.get("pre_observe_hard_reject_count") or 0),
            "formal_incubation_count": int(payload.get("formal_incubation_count") or 0),
            "observe_incubation_count": int(payload.get("observe_incubation_count") or 0),
            "diagnostic_observation_count": int(payload.get("diagnostic_observation_count") or 0),
            "live_ready_review_count": int(payload.get("live_ready_review_count") or 0),
            "deferred_submission_count": int(payload.get("deferred_submission_count") or 0),
            "research_only_count": int(payload.get("research_only_count") or 0),
        },
    }


def _gate_2_group_key(candidate: dict) -> str:
    payload = dict(candidate or {})
    generation_reason = dict(payload.get("generation_reason") or {})
    research_task = dict(payload.get("research_task") or {})
    parent_strategy_id = str(
        payload.get("parent_strategy_id")
        or generation_reason.get("parent_strategy_id")
        or research_task.get("parent_strategy_id")
        or ""
    ).strip()
    if parent_strategy_id:
        return f"parent:{parent_strategy_id}"

    task_key = str(research_task.get("task_key") or research_task.get("task_id") or "").strip()
    if task_key:
        return f"task:{task_key}"

    strategy_type = str(payload.get("strategy_type") or "unknown").strip().lower() or "unknown"
    target_codes = tuple(sorted(_extract_target_codes_from_payload(payload, limit=4)))
    if target_codes:
        return f"universe:{strategy_type}:{','.join(target_codes)}"
    return f"type:{strategy_type}"


def _is_bulk_stock_matrix_candidate(candidate: dict) -> bool:
    payload = dict(candidate or {})
    research_task = dict(payload.get("research_task") or {})
    task_source = str(
        research_task.get("task_source")
        or payload.get("task_source")
        or payload.get("generator_mode")
        or ""
    ).strip().lower()
    return task_source == "bulk_stock_matrix"


def _resolve_gate_1_representative_count(candidate: dict, default_count: int) -> int:
    payload = dict(candidate or {})
    research_task = dict(payload.get("research_task") or {})
    normalized_task = _normalize_research_task_contract(research_task)
    explicit = (
        payload.get("gate_1_representative_count")
        or research_task.get("gate_1_representative_count")
    )
    if explicit is not None:
        try:
            resolved = max(1, int(explicit))
        except Exception:
            resolved = max(1, int(default_count or 1))
    elif _is_bulk_stock_matrix_candidate(payload):
        resolved = 1
    else:
        resolved = max(1, int(default_count or 1))

    target_count = len(_extract_target_codes_from_payload(payload, limit=12))
    validation_focus = str(normalized_task.get("validation_focus") or "").strip().lower()
    target_alignment_contract = _build_target_alignment_contract(normalized_task, candidate=payload)
    min_target_sample_count = int(target_alignment_contract.get("min_target_sample_count") or 0)
    if (
        target_count > 1
        and _is_targeted_snapshot_candidate(
            payload,
            normalized_task,
            target_count=target_count,
            validation_focus=validation_focus,
        )
        and min_target_sample_count > 0
    ):
        resolved = max(resolved, min(target_count, min_target_sample_count))
    if (
        target_count > 1
        and _is_targeted_snapshot_candidate(
            payload,
            normalized_task,
            target_count=target_count,
            validation_focus=validation_focus,
        )
        and _is_pipeline_staged_ma_cross_candidate(payload, normalized_task)
    ):
        # Snapshot basket ma_cross routinely looked strong on a single-stock fast screen
        # and then collapsed once Gate-2 evaluated the whole basket. Widen the Gate-1
        # sample so these fragile candidates are filtered earlier.
        resolved = max(
            resolved,
            min(target_count, _PIPELINE_MA_CROSS_GATE1_REPRESENTATIVE_FLOOR),
        )

    if (
        target_count > 1
        and _is_targeted_snapshot_candidate(
            payload,
            normalized_task,
            target_count=target_count,
            validation_focus=validation_focus,
        )
        and _is_pipeline_staged_rsi_candidate(payload, normalized_task)
    ):
        # Snapshot basket RSI has become the main Gate-2 leak source. We now force a
        # wider target sample so Gate-1 can expose low-overlap / low-stability
        # baskets before they consume full-backtest capacity.
        resolved = max(
            resolved,
            min(target_count, _PIPELINE_RSI_GATE1_REPRESENTATIVE_FLOOR),
        )

    return resolved


def _resolve_gate_1_codes(
    candidate: dict,
) -> tuple[list[str], list[str], str, str, dict[str, Any]]:
    def _preferred_target_order(payload: dict) -> list[str]:
        params = dict(payload.get("params") or {})
        envelope = dict(payload.get("resolved_candidate_envelope") or {})
        preferred = []
        for source in (
            payload.get("requested_target_symbols"),
            params.get("requested_target_symbols"),
            envelope.get("requested_target_symbols"),
        ):
            preferred.extend(_normalize_target_codes(source, limit=12))
        return list(dict.fromkeys(preferred))

    def _apply_preferred_order(codes: list[str], preferred: list[str]) -> list[str]:
        if not codes or not preferred:
            return list(codes)
        preferred_set = set(codes)
        ordered = [code for code in preferred if code in preferred_set]
        ordered.extend(code for code in codes if code not in ordered)
        return ordered

    representative_stocks = list(_compat_setting("REPRESENTATIVE_STOCKS", REPRESENTATIVE_STOCKS))
    representative_count = _resolve_gate_1_representative_count(
        candidate,
        int(_compat_setting("GATE1_REPRESENTATIVE_COUNT", GATE1_REPRESENTATIVE_COUNT) or GATE1_REPRESENTATIVE_COUNT),
    )
    research_task = _normalize_research_task_contract(candidate.get("research_task"))
    validation_focus = str(research_task.get("validation_focus") or "target_plus_representative").strip().lower()
    target_codes = _extract_target_codes_from_payload(candidate, limit=max(representative_count, 12))
    target_codes = _apply_preferred_order(target_codes, _preferred_target_order(dict(candidate or {})))
    prioritized_target_codes = list(target_codes[:representative_count])
    padded_representatives = [code for code in representative_stocks if code not in prioritized_target_codes]
    if validation_focus == "event_target_only" and prioritized_target_codes:
        codes = list(prioritized_target_codes)
        code_source = "event_target_only"
    else:
        codes = list(dict.fromkeys([*prioritized_target_codes, *padded_representatives]))[
            : max(representative_count, len(prioritized_target_codes))
        ]
        code_source = "candidate_target_symbols" if prioritized_target_codes else "representative_only"
    return codes, prioritized_target_codes, code_source, validation_focus, research_task


def _collect_gate_1_preload_codes(candidates: list[dict]) -> list[str]:
    ordered_codes: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        codes, _target_codes, _code_source, _validation_focus, _research_task = _resolve_gate_1_codes(candidate)
        for code in codes:
            if code in seen:
                continue
            seen.add(code)
            ordered_codes.append(code)
    return ordered_codes


def _candidate_constraint_check(candidate: dict) -> dict[str, Any]:
    payload = dict(candidate or {})
    for source in (
        payload,
        dict(payload.get("params") or {}),
        _normalize_research_task_contract(payload.get("research_task") or {}),
    ):
        constraint_check = source.get("constraint_check")
        if isinstance(constraint_check, dict) and constraint_check:
            return dict(constraint_check)
    return {}


def _candidate_tags(candidate: dict) -> set[str]:
    payload = dict(candidate or {})
    return {
        str(tag).strip().lower()
        for tag in list(payload.get("tags") or [])
        if str(tag).strip()
    }


def _candidate_generation_mode(candidate: dict, research_task: Optional[dict] = None) -> str:
    payload = dict(candidate or {})
    normalized_task = _normalize_research_task_contract(research_task or payload.get("research_task") or {})
    dsl_metadata = dict((dict((payload.get("params") or {}).get("dsl") or {}).get("metadata") or {}))
    strategy_profile = dict(dsl_metadata.get("strategy_profile") or {})
    candidate_provenance = dict(payload.get("candidate_provenance") or {})
    generation_reason = dict(payload.get("generation_reason") or {})
    for source in (
        payload,
        dict(payload.get("params") or {}),
        normalized_task,
        strategy_profile,
        candidate_provenance,
        generation_reason,
    ):
        if not isinstance(source, dict):
            continue
        for key in ("generator_type", "generator_mode"):
            value = str(source.get(key) or "").strip().lower()
            if value:
                return value
    return ""


def _is_targeted_snapshot_candidate(
    candidate: dict,
    research_task: Optional[dict] = None,
    *,
    tags: Optional[set[str]] = None,
    target_count: Optional[int] = None,
    validation_focus: Optional[str] = None,
) -> bool:
    payload = dict(candidate or {})
    normalized_task = _normalize_research_task_contract(research_task or payload.get("research_task") or {})
    resolved_tags = tags if tags is not None else _candidate_tags(payload)
    candidate_target_count = int(target_count if target_count is not None else len(_extract_target_codes_from_payload(payload, limit=12)))
    task_target_count = len(list(normalized_task.get("target_symbols") or []))
    resolved_target_count = max(candidate_target_count, task_target_count)
    resolved_validation_focus = str(
        validation_focus if validation_focus is not None else normalized_task.get("validation_focus") or ""
    ).strip().lower()
    task_source = str(
        normalized_task.get("task_source")
        or payload.get("task_source")
        or payload.get("generator_mode")
        or ""
    ).strip().lower()
    return (
        task_source == "snapshot"
        and resolved_target_count > 0
        and (
            "targeted_universe" in resolved_tags
            or resolved_validation_focus in {"candidate_target_only", "event_target_only", "target_plus_representative"}
        )
    )


def _is_pipeline_staged_ma_cross_candidate(candidate: dict, research_task: Optional[dict] = None) -> bool:
    payload = dict(candidate or {})
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    generation_mode = _candidate_generation_mode(payload, research_task)
    tags = _candidate_tags(payload)
    return strategy_type == "ma_cross" and (
        generation_mode == "pipeline_staged"
        or "pipeline_staged" in tags
        or "generator_pipeline_staged" in tags
    )


def _is_pipeline_staged_rsi_candidate(candidate: dict, research_task: Optional[dict] = None) -> bool:
    payload = dict(candidate or {})
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    generation_mode = _candidate_generation_mode(payload, research_task)
    tags = _candidate_tags(payload)
    return strategy_type == "rsi" and (
        generation_mode == "pipeline_staged"
        or "pipeline_staged" in tags
        or "generator_pipeline_staged" in tags
    )

# --- quality_gates_parts/policy.py ---
def _is_rl_bandit_momentum_candidate(candidate: dict, research_task: Optional[dict] = None) -> bool:
    payload = dict(candidate or {})
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    generation_mode = _candidate_generation_mode(payload, research_task)
    tags = _candidate_tags(payload)
    return strategy_type == "momentum" and (
        generation_mode == "rl_bandit"
        or "generator_rl_bandit" in tags
        or "rl_bandit" in tags
        or "rl_evolved" in tags
    )


def _is_rl_bandit_volatility_breakout_candidate(candidate: dict, research_task: Optional[dict] = None) -> bool:
    payload = dict(candidate or {})
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    generation_mode = _candidate_generation_mode(payload, research_task)
    tags = _candidate_tags(payload)
    return strategy_type == "volatility_breakout" and (
        generation_mode == "rl_bandit"
        or "generator_rl_bandit" in tags
        or "rl_bandit" in tags
        or "rl_evolved" in tags
    )


def _gate_2_selection_signature(candidate: dict) -> str:
    try:
        return candidate_signature(candidate)
    except Exception:
        return json.dumps(
            {
                "strategy_type": str((candidate or {}).get("strategy_type") or "").strip().lower(),
                "target_symbols": list(_extract_target_codes_from_payload(candidate or {}, limit=12)),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )


def _gate_2_priority_adjustments(candidate: dict, research_task: dict, gate_1_score: float) -> dict[str, Any]:
    payload = dict(candidate or {})
    tags = _candidate_tags(payload)
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    target_codes = _extract_target_codes_from_payload(payload, limit=12)
    target_count = len(target_codes)
    validation_focus = str(research_task.get("validation_focus") or "").strip().lower()
    gate_1_metrics = dict((payload.get("gate_1_result") or {}).get("metrics") or {})
    constraint_check = _candidate_constraint_check(payload)
    coverage_ratio_raw = constraint_check.get("coverage_ratio")
    intersection_ratio_raw = constraint_check.get("intersection_ratio")
    coverage_ratio = None if coverage_ratio_raw is None else _safe_float(coverage_ratio_raw, 0.0)
    intersection_ratio = None if intersection_ratio_raw is None else _safe_float(intersection_ratio_raw, 0.0)
    avg_turnover_proxy = _safe_float(gate_1_metrics.get("avg_turnover_proxy"), 0.0)
    avg_total_return = _safe_float(gate_1_metrics.get("avg_total_return"), 0.0)
    target_quality_summary = build_target_quality_gate_summary(payload, gate_1_metrics=gate_1_metrics)
    sampled_target_count = int(target_quality_summary.get("sampled_target_count") or 0)
    min_target_sample_count = int(target_quality_summary.get("min_target_sample_count") or 0)
    target_layer_stability = (
        None
        if target_quality_summary.get("target_layer_stability") is None
        else _safe_float(target_quality_summary.get("target_layer_stability"), 0.0)
    )
    min_target_layer_stability = _safe_float(target_quality_summary.get("min_target_layer_stability"), 0.0)
    gate_1_threshold = float(_compat_setting("GATE1_SHARPE_MIN", GATE1_SHARPE_MIN) or GATE1_SHARPE_MIN)
    low_edge_cutoff = gate_1_threshold + _SNAPSHOT_LOW_EDGE_BUFFER
    targeted_snapshot = _is_targeted_snapshot_candidate(
        payload,
        research_task,
        tags=tags,
        target_count=target_count,
        validation_focus=validation_focus,
    )
    pipeline_staged_ma_cross = _is_pipeline_staged_ma_cross_candidate(payload, research_task)
    rl_bandit_momentum = _is_rl_bandit_momentum_candidate(payload, research_task)

    adjustments: dict[str, float] = {}
    if targeted_snapshot:
        if coverage_ratio is not None:
            if coverage_ratio <= 0.0:
                adjustments["coverage_zero_penalty"] = -12.0
            elif coverage_ratio < _SNAPSHOT_ALIGNMENT_SOFT_COVERAGE:
                adjustments["coverage_penalty"] = -8.0
            elif coverage_ratio < 0.6:
                adjustments["coverage_penalty"] = -4.0
            elif coverage_ratio >= 0.85:
                adjustments["coverage_bonus"] = 2.0
        if intersection_ratio is not None:
            if intersection_ratio <= 0.0:
                adjustments["intersection_zero_penalty"] = adjustments.get("intersection_zero_penalty", 0.0) - 8.0
            elif intersection_ratio < _SNAPSHOT_ALIGNMENT_SOFT_INTERSECTION:
                adjustments["intersection_penalty"] = adjustments.get("intersection_penalty", 0.0) - 4.0
            elif intersection_ratio < 0.5:
                adjustments["intersection_penalty"] = adjustments.get("intersection_penalty", 0.0) - 2.0
            else:
                adjustments["intersection_bonus"] = adjustments.get("intersection_bonus", 0.0) + 1.5
        if avg_turnover_proxy >= _SNAPSHOT_VERY_HIGH_TURNOVER_THRESHOLD and gate_1_score < (low_edge_cutoff + 0.06):
            adjustments["turnover_penalty"] = adjustments.get("turnover_penalty", 0.0) - 10.0
        elif avg_turnover_proxy >= _SNAPSHOT_HIGH_TURNOVER_THRESHOLD and gate_1_score < low_edge_cutoff:
            adjustments["turnover_penalty"] = adjustments.get("turnover_penalty", 0.0) - 6.0
        if avg_total_return <= 0.0 and gate_1_score < (low_edge_cutoff + 0.03):
            adjustments["low_edge_penalty"] = adjustments.get("low_edge_penalty", 0.0) - 4.0
        if avg_turnover_proxy > 0.0 and avg_turnover_proxy <= 0.6 and avg_total_return > 0.0 and gate_1_score >= (gate_1_threshold + 0.15):
            adjustments["efficient_turnover_bonus"] = adjustments.get("efficient_turnover_bonus", 0.0) + 3.0
        if min_target_sample_count > 0 and sampled_target_count < min_target_sample_count:
            adjustments["target_sample_penalty"] = adjustments.get("target_sample_penalty", 0.0) - 8.0
        if (
            target_layer_stability is not None
            and min_target_layer_stability > 0.0
            and target_layer_stability < min_target_layer_stability
        ):
            adjustments["target_layer_stability_penalty"] = adjustments.get("target_layer_stability_penalty", 0.0) - 6.0
        if pipeline_staged_ma_cross:
            if avg_turnover_proxy >= _PIPELINE_MA_CROSS_VERY_HIGH_TURNOVER_THRESHOLD:
                adjustments["pipeline_ma_cross_turnover_penalty"] = adjustments.get("pipeline_ma_cross_turnover_penalty", 0.0) - 18.0
            elif avg_turnover_proxy >= _PIPELINE_MA_CROSS_HIGH_TURNOVER_THRESHOLD:
                adjustments["pipeline_ma_cross_turnover_penalty"] = adjustments.get("pipeline_ma_cross_turnover_penalty", 0.0) - 12.0
            elif avg_turnover_proxy >= _SNAPSHOT_HIGH_TURNOVER_THRESHOLD:
                adjustments["pipeline_ma_cross_turnover_penalty"] = adjustments.get("pipeline_ma_cross_turnover_penalty", 0.0) - 6.0
            if avg_total_return <= 0.0:
                adjustments["pipeline_ma_cross_edge_penalty"] = adjustments.get("pipeline_ma_cross_edge_penalty", 0.0) - 5.0
            elif avg_total_return < _PIPELINE_MA_CROSS_EDGE_RETURN_FLOOR:
                adjustments["pipeline_ma_cross_edge_penalty"] = adjustments.get("pipeline_ma_cross_edge_penalty", 0.0) - 3.0
            if avg_turnover_proxy >= _PIPELINE_MA_CROSS_HIGH_TURNOVER_THRESHOLD and gate_1_score < (gate_1_threshold + 0.45):
                adjustments["pipeline_ma_cross_fragility_penalty"] = adjustments.get("pipeline_ma_cross_fragility_penalty", 0.0) - 4.0
        if rl_bandit_momentum:
            if coverage_ratio is not None:
                if coverage_ratio < _RL_BANDIT_ALIGNMENT_SOFT_COVERAGE:
                    adjustments["rl_bandit_coverage_penalty"] = adjustments.get("rl_bandit_coverage_penalty", 0.0) - 6.0
                if coverage_ratio < _RL_BANDIT_ALIGNMENT_HARD_BLOCK_COVERAGE:
                    adjustments["rl_bandit_coverage_penalty"] = adjustments.get("rl_bandit_coverage_penalty", 0.0) - 4.0
            if intersection_ratio is not None:
                if intersection_ratio < _RL_BANDIT_ALIGNMENT_SOFT_INTERSECTION:
                    adjustments["rl_bandit_intersection_penalty"] = adjustments.get("rl_bandit_intersection_penalty", 0.0) - 5.0
                if intersection_ratio < _RL_BANDIT_ALIGNMENT_HARD_BLOCK_INTERSECTION:
                    adjustments["rl_bandit_intersection_penalty"] = adjustments.get("rl_bandit_intersection_penalty", 0.0) - 4.0
            if target_count > 10:
                adjustments["rl_bandit_basket_penalty"] = adjustments.get("rl_bandit_basket_penalty", 0.0) - 2.5

    return {
        "score_delta": round(sum(adjustments.values()), 4),
        "adjustments": {key: round(value, 4) for key, value in adjustments.items()},
        "coverage_ratio": None if coverage_ratio is None else round(coverage_ratio, 4),
        "intersection_ratio": None if intersection_ratio is None else round(intersection_ratio, 4),
        "avg_turnover_proxy": round(avg_turnover_proxy, 4),
        "avg_total_return": round(avg_total_return, 6),
        "target_quality_summary": dict(target_quality_summary),
        "targeted_snapshot": targeted_snapshot,
    }


def _gate_2_disallow_same_group_fill(candidate: dict) -> bool:
    payload = dict(candidate or {})
    research_task = _normalize_research_task_contract(payload.get("research_task") or {})
    if not _is_targeted_snapshot_candidate(payload, research_task):
        return False
    return _is_rl_bandit_momentum_candidate(payload, research_task)


def _post_gate_1_target_quality_block_reason(candidate: dict, gate_1_score: float) -> Optional[str]:
    payload = dict(candidate or {})
    research_task = _normalize_research_task_contract(payload.get("research_task") or {})
    target_codes = _extract_target_codes_from_payload(payload, limit=12)
    target_count = len(target_codes)
    if target_count <= 1:
        return None

    tags = _candidate_tags(payload)
    validation_focus = str(research_task.get("validation_focus") or "").strip().lower()
    if not _is_targeted_snapshot_candidate(
        payload,
        research_task,
        tags=tags,
        target_count=target_count,
        validation_focus=validation_focus,
    ):
        return None

    gate_1_metrics = dict((payload.get("gate_1_result") or {}).get("metrics") or {})
    constraint_check = _candidate_constraint_check(payload)
    coverage_ratio_raw = constraint_check.get("coverage_ratio")
    intersection_ratio_raw = constraint_check.get("intersection_ratio")
    coverage_ratio = None if coverage_ratio_raw is None else _safe_float(coverage_ratio_raw, 0.0)
    intersection_ratio = None if intersection_ratio_raw is None else _safe_float(intersection_ratio_raw, 0.0)
    avg_turnover_proxy = _safe_float(gate_1_metrics.get("avg_turnover_proxy"), 0.0)
    avg_total_return = _safe_float(gate_1_metrics.get("avg_total_return"), 0.0)
    gate_1_threshold = float(_compat_setting("GATE1_SHARPE_MIN", GATE1_SHARPE_MIN) or GATE1_SHARPE_MIN)
    target_quality_summary = build_target_quality_gate_summary(payload, gate_1_metrics=gate_1_metrics)
    target_quality_reasons = list(target_quality_summary.get("reasons") or [])

    for reason in target_quality_reasons:
        if reason in {
            "target_universe_alignment_too_low",
            "target_sample_sufficiency_too_low",
            "target_layer_stability_too_low",
        }:
            return reason

    if _is_pipeline_staged_ma_cross_candidate(payload, research_task):
        if (
            avg_turnover_proxy >= _PIPELINE_MA_CROSS_VERY_HIGH_TURNOVER_THRESHOLD
            and gate_1_score < (gate_1_threshold + 0.75)
        ):
            return "snapshot_turnover_fragility_too_high"
        if (
            avg_turnover_proxy >= _PIPELINE_MA_CROSS_HIGH_TURNOVER_THRESHOLD
            and target_count >= 4
            and (
                gate_1_score < (gate_1_threshold + 0.55)
                or avg_total_return <= _PIPELINE_MA_CROSS_EDGE_RETURN_FLOOR
            )
        ):
            return "snapshot_turnover_fragility_too_high"

    if (
        _is_pipeline_staged_rsi_candidate(payload, research_task)
        and target_count >= 6
        and intersection_ratio is not None
        and intersection_ratio < _PIPELINE_RSI_ALIGNMENT_HARD_BLOCK_INTERSECTION
    ):
        return "target_universe_alignment_too_low"

    if _is_rl_bandit_momentum_candidate(payload, research_task):
        if (
            coverage_ratio is not None
            and coverage_ratio < _RL_BANDIT_ALIGNMENT_HARD_BLOCK_COVERAGE
            and intersection_ratio is not None
            and intersection_ratio < _RL_BANDIT_ALIGNMENT_HARD_BLOCK_INTERSECTION
        ):
            return "target_universe_alignment_too_low"
        if (
            target_count > 8
            and intersection_ratio is not None
            and intersection_ratio < _RL_BANDIT_ALIGNMENT_SOFT_INTERSECTION
        ):
            return "target_universe_alignment_too_low"

    return None


def _gate_2_priority_score(candidate: dict, gate_1_score: float, *, return_meta: bool = False):
    payload = dict(candidate or {})
    research_task = _normalize_research_task_contract(payload.get("research_task") or {})
    target_codes = _extract_target_codes_from_payload(payload, limit=12)
    target_count = len(target_codes)
    task_source = str(
        research_task.get("task_source")
        or payload.get("task_source")
        or payload.get("generator_mode")
        or ""
    ).strip().lower()
    validation_focus = str(research_task.get("validation_focus") or "").strip().lower()
    tags = {
        str(tag).strip().lower()
        for tag in list(payload.get("tags") or [])
        if str(tag).strip()
    }
    priority = _safe_float(payload.get("priority") or research_task.get("priority"))
    matrix_priority = _safe_float(
        payload.get("matrix_priority_score")
        or research_task.get("matrix_priority_score")
    )
    stock_family_priority = _safe_float(
        payload.get("stock_family_priority")
        or research_task.get("stock_family_priority")
    )
    base_score = _safe_float(gate_1_score) * 100.0
    priority_bonus = priority * 0.35
    matrix_bonus = matrix_priority * 0.25
    family_bonus = stock_family_priority * 25.0
    target_bonus = min(target_count, 8) * 0.9
    score = base_score + priority_bonus + matrix_bonus + family_bonus + target_bonus
    if target_count > 0:
        score += 4.0
    if "targeted_universe" in tags:
        score += 3.0
    if validation_focus in {"candidate_target_only", "event_target_only"}:
        score += 4.0
    if task_source in {"snapshot", "event_driven", "bulk_stock_matrix"} and target_count > 0:
        score += 2.0
    if not target_count and not research_task:
        score -= 12.0
    if _is_bulk_stock_matrix_candidate(payload):
        score += 8.0
    if str(research_task.get("candidate_family") or payload.get("candidate_family") or "").strip():
        score += 2.0
    quality_meta = _gate_2_priority_adjustments(payload, research_task, _safe_float(gate_1_score))
    score += _safe_float(quality_meta.get("score_delta"), 0.0)
    final_score = round(score, 4)
    if return_meta:
        return final_score, {
            "base_score": round(base_score, 4),
            "priority_bonus": round(priority_bonus, 4),
            "matrix_bonus": round(matrix_bonus, 4),
            "family_bonus": round(family_bonus, 4),
            "target_count_bonus": round(target_bonus, 4),
            **quality_meta,
        }
    return final_score


def _select_gate_2_candidates(
    gate_1_scored: list[tuple[dict, float]],
    top_k: int,
    *,
    per_group_cap: int = 2,
) -> list[dict]:
    if top_k <= 0 or not gate_1_scored:
        return []

    selected: list[dict] = []
    selected_groups: dict[str, int] = {}
    selected_ids: set[int] = set()
    selected_signatures: set[str] = set()

    def try_select(candidate: dict, *, require_new_group: bool, enforce_cap: bool) -> bool:
        group_key = _gate_2_group_key(candidate)
        current = int(selected_groups.get(group_key) or 0)
        if require_new_group and current > 0:
            return False
        if current > 0 and _gate_2_disallow_same_group_fill(candidate):
            return False
        if enforce_cap and current >= max(1, per_group_cap):
            return False
        marker = id(candidate)
        if marker in selected_ids:
            return False
        selection_signature = _gate_2_selection_signature(candidate)
        if selection_signature in selected_signatures:
            return False
        selected.append(candidate)
        selected_ids.add(marker)
        selected_signatures.add(selection_signature)
        selected_groups[group_key] = current + 1
        return True

    for candidate, _score in gate_1_scored:
        if len(selected) >= top_k:
            break
        try_select(candidate, require_new_group=True, enforce_cap=True)

    for candidate, _score in gate_1_scored:
        if len(selected) >= top_k:
            break
        try_select(candidate, require_new_group=False, enforce_cap=True)

    for candidate, _score in gate_1_scored:
        if len(selected) >= top_k:
            break
        try_select(candidate, require_new_group=False, enforce_cap=False)

    return selected[:top_k]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """单个门禁的结果。"""
    passed: bool
    gate: str
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _per_stock_quota_increment(candidate: dict, research_task: dict, target_codes: list[str]) -> float:
    count = len(list(target_codes or []))
    if count <= 1:
        return 1.0

    task_source = str(research_task.get("task_source") or "").strip().lower()
    validation_focus = str(research_task.get("validation_focus") or "").strip().lower()
    if task_source == "bulk_stock_matrix":
        return 1.0
    if validation_focus in {"candidate_target_only", "event_target_only"}:
        return 0.5 if count >= 4 else 0.75
    if count >= 4:
        return 0.5
    if count >= 3:
        return 0.65
    return 0.8


def _resolve_gate_2_top_k(total_passed: int, pass_ratio: float) -> int:
    if total_passed <= 0:
        return 0
    scaled = float(total_passed) * max(0.0, float(pass_ratio or 0.0))
    # 默认 floor=1 + use_ceil=False ⇒ 与 max(1, round(...)) 逐位等价；toggle 打开后抬高小样本下限
    floor = int(_compat_setting("GATE2_TOPK_FLOOR", GATE2_TOPK_FLOOR) or GATE2_TOPK_FLOOR)
    use_ceil = bool(_compat_setting("GATE2_TOPK_USE_CEIL", GATE2_TOPK_USE_CEIL))
    base = int(math.ceil(scaled)) if use_ceil else int(round(scaled))
    # floor 不得超过候选池大小（否则 top_k 会大于 total_passed，违反"取前 k 个"语义）
    effective_floor = min(floor, int(total_passed))
    return max(effective_floor, min(int(total_passed), base))


def _collect_symbol_summaries(candidate: dict, research_task: dict) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for source in (candidate, research_task, dict(research_task.get("stock_pool") or {})):
        if not isinstance(source, dict):
            continue
        for key in (
            "source_symbol_summary",
            "target_symbol_summary",
            "source_symbol_summaries",
            "target_symbol_summaries",
            "symbol_summaries",
        ):
            payload = source.get(key)
            if isinstance(payload, dict):
                summaries.append(dict(payload))
            elif isinstance(payload, list):
                summaries.extend(dict(item) for item in payload if isinstance(item, dict))
    return summaries


def _resolve_liquidity_requirement(candidate: dict, research_task: dict, target_codes: list[str]) -> str:
    explicit = str(
        candidate.get("liquidity_requirement")
        or research_task.get("liquidity_requirement")
        or candidate.get("market_liquidity_requirement")
        or ""
    ).strip().lower()
    if explicit in {"high", "medium", "low", "all"}:
        return explicit
    return "medium" if len(target_codes) <= 1 else "low"


def _estimate_liquidity_proxy(candidate: dict, research_task: dict, target_codes: list[str]) -> dict[str, Any]:
    summaries = _collect_symbol_summaries(candidate, research_task)
    if not summaries:
        return {"available": False, "proxy_kind": None, "proxy_value": None}

    target_set = {str(code).strip() for code in list(target_codes or []) if str(code).strip()}
    matched = [
        summary
        for summary in summaries
        if not target_set
        or not str(summary.get("code") or summary.get("symbol") or summary.get("stock_code") or "").strip()
        or str(summary.get("code") or summary.get("symbol") or summary.get("stock_code") or "").strip() in target_set
    ]
    if not matched:
        matched = summaries

    turnover_values = []
    for summary in matched:
        for field in ("avg_daily_turnover", "daily_turnover", "avg_turnover", "turnover", "amount"):
            value = _safe_float(summary.get(field), 0.0)
            if value > 0:
                turnover_values.append(value)
                break
    if turnover_values:
        return {
            "available": True,
            "proxy_kind": "avg_daily_turnover",
            "proxy_value": min(turnover_values),
        }

    market_caps = [_safe_float(summary.get("market_cap"), 0.0) for summary in matched]
    market_caps = [value for value in market_caps if value > 0]
    if market_caps:
        return {
            "available": True,
            "proxy_kind": "market_cap",
            "proxy_value": min(market_caps),
        }

    return {"available": False, "proxy_kind": None, "proxy_value": None}

# --- quality_gates_parts/evaluation.py ---
def _liquidity_threshold(requirement: str, proxy_kind: Optional[str]) -> float:
    requirement = str(requirement or "all").strip().lower() or "all"
    if requirement == "all":
        return 0.0
    if proxy_kind == "avg_daily_turnover":
        return float(_PRE_GATE_TURNOVER_THRESHOLDS.get(requirement, 0.0) or 0.0)
    return float(_PRE_GATE_MARKET_CAP_THRESHOLDS.get(requirement, 0.0) or 0.0)


def _estimate_signal_density(strategy_type: str, params: dict[str, Any]) -> float:
    strategy_type = str(strategy_type or "").strip().lower()
    lookback = max(2, int(params.get("lookback", params.get("period", 20)) or 20))
    threshold = max(0.005, _safe_float(params.get("threshold", 0.02), 0.02))
    if strategy_type == "ma_cross":
        short_period = max(2, int(params.get("short_period", 5) or 5))
        long_period = max(short_period + 1, int(params.get("long_period", 20) or 20))
        return round(252.0 / max((short_period + long_period) / 2.0, 4.0), 4)
    if strategy_type in {"momentum", "volatility_breakout", "event_structure_breakout", "north_capital_track"}:
        density = 252.0 / max(float(lookback), 3.0)
        density *= max(0.35, min(1.8, 0.02 / threshold))
        if strategy_type == "event_structure_breakout":
            density *= 0.72
        return round(density, 4)
    if strategy_type in {"rsi", "gap_fill", "mean_reversion_short"}:
        period = max(2, int(params.get("rsi_period", 14) or 14))
        band_width = max(5.0, _safe_float(params.get("overbought", 70), 70.0) - _safe_float(params.get("oversold", 30), 30.0))
        density = 252.0 / max(period * 0.75, 2.0)
        density *= max(0.5, min(1.5, 40.0 / band_width))
        return round(density, 4)
    if strategy_type in {"value_factor", "quality_factor", "growth_factor", "multi_factor", "sector_rotation"}:
        return round(252.0 / max(lookback * 0.45, 8.0), 4)
    if strategy_type in {"macro_timing", "margin_divergence"}:
        return round(252.0 / max(lookback * 1.1, 6.0), 4)
    return 12.0


# ---------------------------------------------------------------------------
# Gate-0: 结构校验
# ---------------------------------------------------------------------------

_VALID_STRATEGY_TYPES = frozenset({
    "momentum", "ma_cross", "rsi",
    "value_factor", "quality_factor", "growth_factor",
    "multi_factor", "macro_timing", "dsl_rule",
    "volatility_breakout", "event_structure_breakout", "gap_fill", "mean_reversion_short",
    "sector_rotation", "north_capital_track", "margin_divergence",
})
_REQUIRED_TRADE_FIELDS = frozenset({
    "holding_horizon",
    "trade_plan",
    "risk_rules",
    "rebalance_rule",
    "portfolio_spec",
    "execution_assumptions",
    "validation_profile",
})
_LEGACY_TRADE_ENRICHMENT_REQUIRED_FIELDS = frozenset(_REQUIRED_TRADE_FIELDS - {"validation_profile"})


def _should_enrich_legacy_gate_0_candidate(candidate: dict) -> bool:
    payload = dict(candidate or {})
    if not payload:
        return False
    missing_trade_fields = [
        key for key in sorted(_REQUIRED_TRADE_FIELDS)
        if candidate_contract_value(payload, key) in (None, "", [], {})
    ]
    return bool(missing_trade_fields)


def _enrich_legacy_gate_0_candidate(candidate: dict) -> dict:
    if not _should_enrich_legacy_gate_0_candidate(candidate):
        return candidate
    payload = dict(candidate or {})
    contract_snapshot = build_portfolio_candidate_contract(payload)
    assumptions = build_factory_backtest_assumptions(payload)
    holding_horizon = dict(contract_snapshot.get("holding_horizon") or {})
    max_days = max(1, int(holding_horizon.get("max_days") or 10))
    defaults = {
        "holding_horizon": {"max_days": max_days},
        "trade_plan": {"entry_bias": "signal_confirmed", "exit_bias": "signal_or_time_stop"},
        "risk_rules": {
            "stop_loss_pct": 0.08,
            "take_profit_pct": 0.18,
            "max_holding_days": max_days,
        },
        "rebalance_rule": {"mode": "signal_rebalance"},
        "portfolio_spec": {
            "position_assumption": assumptions.position_assumption,
            "target_weight_scheme": assumptions.target_weight_scheme,
        },
        "execution_assumptions": {
            "slippage_bps": assumptions.slippage_bps,
            "commission_rate": assumptions.commission_rate,
            "tradability_filter": assumptions.tradability_filter,
            "slippage_model": assumptions.slippage_model or "fixed",
        },
        "validation_profile": dict(contract_snapshot.get("validation_profile") or {}),
    }
    enriched = dict(payload)
    params = dict(enriched.get("params") or {})
    for key in sorted(_REQUIRED_TRADE_FIELDS):
        default_value = deepcopy(defaults.get(key) or contract_snapshot.get(key))
        if candidate_contract_value(enriched, key) in (None, "", [], {}):
            enriched[key] = deepcopy(default_value)
        params.setdefault(key, deepcopy(default_value))
    enriched["params"] = params
    return enriched


def gate_0_structural(candidate: dict) -> GateResult:
    """纯同步结构校验。"""
    reasons: list[str] = []
    strategy_type = str(candidate.get("strategy_type") or "").strip()
    if not strategy_type:
        reasons.append("missing_strategy_type")
    elif strategy_type not in _VALID_STRATEGY_TYPES:
        reasons.append(f"invalid_strategy_type:{strategy_type}")

    params = candidate.get("params")
    if params is None:
        reasons.append("missing_params")
    elif not isinstance(params, dict):
        reasons.append("params_not_dict")
    elif params:
        # 检查 params 是否只包含任务元数据（无实际策略参数）
        _META_ONLY_KEYS = {
            "task_attempt_count", "task_stage_attempt_count", "candidate_local_attempt_count",
            "task_local_attempt_count", "candidate_local_selected_count", "task_local_selected_count",
            "task_network_request_count", "task_real_request_count", "task_compatibility_skip_count",
            "task_cooldown_skip_count", "task_compatibility_failure_count",
            "task_effective_response_count", "task_empty_200_response_count",
            "task_compatibility_failure_ratio", "task_effective_response_ratio",
            "task_selected_count",
        }
        actual_strategy_keys = {k for k in params.keys() if k not in _META_ONLY_KEYS}
        if not actual_strategy_keys:
            # 只有元数据，无策略参数 → 用 Spawner 补充（不阻断，只补充）
            try:
                from strategy_factory.domain.spawner import StrategySpawner
                from strategy_factory.domain.parameter_distribution_registry import (
                    ParameterDistributionRegistry,
                )
                spawner = StrategySpawner.__new__(StrategySpawner)
                default_params = spawner._legacy_varied_defaults(strategy_type, 0, snapshot=None)
                # PR-S21：优先取 task 上的 stock_profile 参数 band（更专属）
                research_task = dict(candidate.get("research_task") or {})
                band = (
                    research_task.get("param_search_space")
                    or research_task.get("profile_param_band")
                )
                band_sample = ParameterDistributionRegistry.sample_from_param_band(band, idx=0)
                merged_defaults = dict(default_params or {})
                if band_sample:
                    merged_defaults.update(dict(band_sample.get("params") or {}))
                if merged_defaults:
                    candidate["params"] = {**merged_defaults, **params}
            except Exception:
                # Not fatal: the candidate still runs, on a narrower parameter set
                # than its research task asked for. Staying silent about that made
                # "sampled the profile band" and "did not" look identical in the
                # gate output.
                logger.warning(
                    "Gate-1 parameter backfill failed for candidate %s; running with the "
                    "parameters it arrived with",
                    candidate.get("strategy_id") or candidate.get("id") or "unknown",
                    exc_info=True,
                )

    missing_trade_fields = [
        key for key in sorted(_REQUIRED_TRADE_FIELDS)
        if candidate_contract_value(candidate, key) in (None, "", [], {})
    ]
    if missing_trade_fields:
        reasons.append(f"missing_trade_fields:{','.join(missing_trade_fields)}")

    # DSL 编译检查（可选）。
    # 注意：此分支仅在 strategy_type == "dsl_rule" 时触发。自治 spawner（spawner_parts/factories.py
    # 的 _make / _preferred_fill_types）只产出具体策略族（value_factor / momentum / ma_cross / rsi /
    # quality_factor / event_structure_breakout / north_capital_track / sector_rotation / gap_fill /
    # macro_timing），从不产出 dsl_rule，故本 gate 不参与自治零产出链路。
    # 若 compile 抛 ModuleNotFoundError，属外部 DSL 提交路径或 PYTHONPATH 环境问题，非自治漏斗堵点。
    if strategy_type == "dsl_rule":
        dsl = (params or {}).get("dsl") if isinstance(params, dict) else None
        if not dsl or not isinstance(dsl, dict):
            reasons.append("dsl_rule_missing_dsl_payload")
        else:
            try:
                compile_strategy_blueprint = get_strategy_dsl_compiler()
                compile_strategy_blueprint(candidate, tune_for_factory=True)
            except Exception as exc:
                reasons.append(f"dsl_compile_failed:{type(exc).__name__}")

    return GateResult(passed=len(reasons) == 0, gate="gate_0", reasons=reasons)


def pre_gate_screen(
    candidate: dict,
    *,
    seen_signatures: Optional[set[str]] = None,
    family_counts: Optional[Dict[str, int]] = None,
    stock_counts: Optional[Dict[str, int]] = None,
    family_quota_limit: int = _PRE_GATE_FAMILY_QUOTA_DEFAULT,
    per_stock_quota_limit: int = _PRE_GATE_PER_STOCK_QUOTA_DEFAULT,
) -> GateResult:
    """廉价预筛：约束任务类型一致性、单股矩阵目标完整性与重复候选。"""
    payload = dict(candidate or {})
    reasons: list[str] = []
    raw_research_task = dict(payload.get("research_task") or {})
    # PR-E (Phase 3, 2026-05-24): capture the operator-supplied
    # ``target_alignment_contract`` *before* normalize. Normalize will
    # always synthesize a default contract from ``target_symbols`` /
    # ``strategy_type``, which would mask the operator's explicit cap.
    # Gate priority #1 must look at the *raw* contract, not the
    # synthesized one.
    raw_user_contract = (
        raw_research_task.get("target_alignment_contract")
        or payload.get("target_alignment_contract")
        or {}
    )
    research_task = _normalize_research_task_contract(raw_research_task)
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    # PR-E: ``target_codes`` keeps the legacy 12-cap so quota/snapshot
    # bookkeeping stays untouched. The *real* count for the dynamic
    # limit check is derived separately below from the unbounded payload.
    target_codes = _extract_target_codes_from_payload(payload, limit=12)
    raw_target_codes = _extract_target_codes_from_payload(payload, limit=64)
    tags = _candidate_tags(payload)
    allowed_strategy_types = {
        str(item).strip().lower()
        for item in list(research_task.get("allowed_strategy_types") or [])
        if str(item).strip()
    }
    signature = candidate_signature(payload)
    task_source = str(research_task.get("task_source") or "").strip().lower()
    strategy_profile = infer_candidate_strategy_profile(payload, research_task=research_task)
    candidate_family = str(
        payload.get("candidate_family")
        or strategy_profile.get("strategy_family")
        or strategy_type
        or "unknown"
    ).strip().lower() or "unknown"
    family_used_before = int((family_counts or {}).get(candidate_family) or 0)
    per_stock_quota_increment = _per_stock_quota_increment(payload, research_task, target_codes)
    stock_quota_snapshot = {
        code: round(_safe_float((stock_counts or {}).get(code), 0.0), 4)
        for code in target_codes
    }
    constraint_check = _candidate_constraint_check(payload)
    coverage_ratio_raw = constraint_check.get("coverage_ratio")
    intersection_ratio_raw = constraint_check.get("intersection_ratio")
    coverage_ratio = None if coverage_ratio_raw is None else round(_safe_float(coverage_ratio_raw, 0.0), 4)
    intersection_ratio = None if intersection_ratio_raw is None else round(_safe_float(intersection_ratio_raw, 0.0), 4)
    validation_focus = str(research_task.get("validation_focus") or "").strip().lower()
    targeted_snapshot = _is_targeted_snapshot_candidate(
        payload,
        research_task,
        tags=tags,
        target_count=len(target_codes),
        validation_focus=validation_focus,
    )
    target_quality_summary = build_target_quality_gate_summary(payload)
    explicit_target_sample_count = None
    if targeted_snapshot:
        explicit_raw = (
            payload.get("gate_1_representative_count")
            or research_task.get("gate_1_representative_count")
        )
        if explicit_raw is not None:
            try:
                explicit_target_sample_count = max(1, int(explicit_raw))
            except Exception:
                explicit_target_sample_count = None
    resolved_target_sample_count = len(_resolve_gate_1_codes(payload)[1]) if targeted_snapshot else None
    planned_target_sample_count = (
        explicit_target_sample_count
        if explicit_target_sample_count is not None
        else resolved_target_sample_count
    )
    min_target_sample_count = int(target_quality_summary.get("min_target_sample_count") or 0)

    if allowed_strategy_types and strategy_type and strategy_type not in allowed_strategy_types:
        reasons.append("outside_allowed_strategy_types")
    if task_source == "bulk_stock_matrix":
        if not target_codes:
            reasons.append("bulk_stock_matrix_missing_target_symbols")
        elif len(target_codes) != 1:
            reasons.append("bulk_stock_matrix_requires_single_target")
    elif task_source == "event_driven" and not target_codes:
        reasons.append("event_task_missing_target_symbols")

    # PR-E (Phase 3, 2026-05-24): replace the hardcoded ``> 12`` check
    # with the canonical ``resolve_target_symbol_limit`` so:
    #   - ``snapshot`` and other legacy task_sources still cap at 12;
    #   - ``event_driven`` honors the dynamic feature flag (defaults to
    #     12 in 5a, allows up to 30 in 5b);
    #   - operators see the *actual* limit in the reject reason.
    #
    # Resolution priority (matches plan §6 Phase 3):
    #   1. operator-supplied target_alignment_contract.max_candidate_target_symbols
    #      (raw, pre-normalize — see ``raw_user_contract`` above)
    #   2. research_task.target_symbol_limit (rare; legacy override)
    #   3. resolve_target_symbol_limit(task_source=...)
    #
    # Why we use ``raw_user_contract`` instead of the normalized one:
    # ``_normalize_research_task_contract`` always synthesizes a default
    # ``target_alignment_contract`` from ``target_symbols`` /
    # ``strategy_type`` (typically capping at 8 for event_targeted,
    # 4 for snapshot pipelines). Reading the normalized contract would
    # mean the gate's "5b allow 30" path can never be reached because
    # the synthesized cap of 8 always wins priority #1.
    contract_cap = 0
    try:
        contract_cap = int((raw_user_contract or {}).get("max_candidate_target_symbols") or 0)
    except (TypeError, ValueError):
        contract_cap = 0

    explicit_limit = 0
    explicit_raw = research_task.get("target_symbol_limit") or payload.get("target_symbol_limit")
    if explicit_raw is not None:
        try:
            explicit_limit = int(explicit_raw)
        except (TypeError, ValueError):
            explicit_limit = 0

    fallback_limit = resolve_target_symbol_limit(
        task_source=task_source,
        validation_focus=validation_focus,
    )

    target_symbol_limit = max(
        1,
        contract_cap if contract_cap > 0 else (explicit_limit if explicit_limit > 0 else fallback_limit),
    )

    if len(target_codes) > target_symbol_limit:
        reasons.append(
            f"target_symbol_count_exceeds_limit:{target_symbol_limit}"
        )
    elif len(raw_target_codes) > target_symbol_limit:
        # ``target_codes`` is already capped at 12 by the legacy extractor;
        # if the raw payload would have exceeded the dynamic limit, we
        # still surface the violation so 5b operators (limit > 12) see
        # the real reject.
        reasons.append(
            f"target_symbol_count_exceeds_limit:{target_symbol_limit}"
        )
    if (
        targeted_snapshot
        and "target_universe_alignment_too_low" in list(target_quality_summary.get("reasons") or [])
    ):
        reasons.append("target_universe_alignment_too_low")
    if (
        targeted_snapshot
        and planned_target_sample_count is not None
        and min_target_sample_count > 0
        and planned_target_sample_count < min_target_sample_count
    ):
        reasons.append("target_sample_sufficiency_too_low")
    if (
        targeted_snapshot
        and _is_rl_bandit_momentum_candidate(payload, research_task)
        and coverage_ratio is not None
        and coverage_ratio <= _RL_BANDIT_ALIGNMENT_HARD_BLOCK_COVERAGE
        and (
            intersection_ratio is None
            or intersection_ratio <= _RL_BANDIT_ALIGNMENT_HARD_BLOCK_INTERSECTION
        )
        and "target_universe_alignment_too_low" not in reasons
    ):
        reasons.append("target_universe_alignment_too_low")
    if (
        targeted_snapshot
        and len(target_codes) >= 6
        and _is_pipeline_staged_rsi_candidate(payload, research_task)
        and intersection_ratio is not None
        and intersection_ratio <= _PIPELINE_RSI_ALIGNMENT_HARD_BLOCK_INTERSECTION
        and "target_universe_alignment_too_low" not in reasons
    ):
        reasons.append("target_universe_alignment_too_low")
    if (
        targeted_snapshot
        and len(target_codes) >= _RL_BANDIT_VOLATILITY_BREAKOUT_ALIGNMENT_MIN_TARGET_COUNT
        and _is_rl_bandit_volatility_breakout_candidate(payload, research_task)
        and intersection_ratio is not None
        and intersection_ratio <= _RL_BANDIT_VOLATILITY_BREAKOUT_ALIGNMENT_HARD_BLOCK_INTERSECTION
        and "target_universe_alignment_too_low" not in reasons
    ):
        reasons.append("target_universe_alignment_too_low")
    if seen_signatures is not None:
        if signature in seen_signatures:
            reasons.append("duplicate_candidate_signature")
        else:
            seen_signatures.add(signature)

    liquidity_requirement = _resolve_liquidity_requirement(payload, research_task, target_codes)
    liquidity = _estimate_liquidity_proxy(payload, research_task, target_codes)
    liquidity_threshold = _liquidity_threshold(liquidity_requirement, liquidity.get("proxy_kind"))
    liquidity_value = _safe_float(liquidity.get("proxy_value"), 0.0)
    if liquidity.get("available") and liquidity_threshold > 0 and 0 < liquidity_value < liquidity_threshold:
        reasons.append("liquidity_below_requirement")

    signal_density = _estimate_signal_density(strategy_type, dict(payload.get("params") or {}))
    if signal_density < _PRE_GATE_SIGNAL_DENSITY_MIN:
        reasons.append("signal_density_too_sparse")
    elif signal_density > _PRE_GATE_SIGNAL_DENSITY_MAX:
        reasons.append("signal_density_too_dense")

    if family_counts is not None and family_used_before >= max(1, int(family_quota_limit or 1)):
        reasons.append("family_quota_exceeded")

    per_stock_quota_hit = [
        code
        for code in target_codes
        if _safe_float((stock_counts or {}).get(code), 0.0) >= max(1, int(per_stock_quota_limit or 1))
    ]
    if per_stock_quota_hit:
        reasons.append("per_stock_quota_exceeded")

    if not reasons:
        if family_counts is not None:
            family_counts[candidate_family] = family_used_before + 1
        if stock_counts is not None:
            for code in target_codes:
                stock_counts[code] = round(
                    _safe_float(stock_counts.get(code), 0.0) + per_stock_quota_increment,
                    4,
                )

    return GateResult(
        passed=len(reasons) == 0,
        gate="pre_gate",
        reasons=reasons,
        metrics={
            "task_source": task_source or None,
            "target_symbol_count": len(target_codes),
            "target_symbols": target_codes,
            "allowed_strategy_types": sorted(allowed_strategy_types),
            "candidate_signature": signature,
            "candidate_family": candidate_family,
            "coverage_ratio": coverage_ratio,
            "intersection_ratio": intersection_ratio,
            "target_quality_summary": dict(target_quality_summary),
            "planned_target_sample_count": planned_target_sample_count,
            "resolved_target_sample_count": resolved_target_sample_count,
            "min_target_sample_count": min_target_sample_count,
            "family_quota_limit": max(1, int(family_quota_limit or 1)),
            "family_quota_used_before": family_used_before,
            "per_stock_quota_limit": max(1, int(per_stock_quota_limit or 1)),
            "per_stock_quota_used_before": stock_quota_snapshot,
            "per_stock_quota_increment": per_stock_quota_increment,
            "per_stock_quota_hit_symbols": per_stock_quota_hit,
            "liquidity_requirement": liquidity_requirement,
            "liquidity_proxy_available": bool(liquidity.get("available")),
            "liquidity_proxy_kind": liquidity.get("proxy_kind"),
            "liquidity_proxy_value": liquidity.get("proxy_value"),
            "liquidity_threshold": liquidity_threshold,
            "signal_density_estimate": signal_density,
            "signal_density_min": _PRE_GATE_SIGNAL_DENSITY_MIN,
            "signal_density_max": _PRE_GATE_SIGNAL_DENSITY_MAX,
        },
    )

# --- quality_gates_parts/reporting.py ---
# ---------------------------------------------------------------------------
# Gate-1: 快速筛选
# ---------------------------------------------------------------------------

from .compact_contracts import (
    COMPACT_PREVIEW_LIMIT,
    compact_backtest_report as _compact_backtest_report_for_gate,
    compact_candidate_brief as _compact_gate_candidate_brief,
    compact_quality_gate_report as _compact_quality_gate_report,
)


async def gate_1_fast_screen(
    candidate: dict,
    db,
    *,
    kline_cache: Optional[Dict[str, list]] = None,
) -> GateResult:
    """用少量代表性股票做快速回测，Sharpe ≥ GATE1_SHARPE_MIN 即通过。"""
    candidate = apply_resolved_candidate_envelope(candidate)
    factory_pkg = get_strategy_factory_package()
    contract_snapshot = build_portfolio_candidate_contract(candidate)
    contract_hash = build_candidate_contract_hash(contract=contract_snapshot)
    tested_object_hash = str(candidate.get("tested_object_hash") or "").strip() or build_tested_object_hash(candidate)
    strategy_type = str(contract_snapshot.get("strategy_type") or candidate.get("strategy_type") or "momentum")
    assumptions = build_factory_backtest_assumptions(candidate)
    params = {
        **dict(candidate.get("params") or {}),
        **assumptions.to_backtest_kwargs(),
    }

    # ── 参数补充：LLM 生成的候选可能缺少回测所需的策略参数 ──────────
    # 检查 params 中是否有实际的策略参数（排除任务元数据字段）
    _TASK_META_KEYS = {
        "task_attempt_count", "task_stage_attempt_count", "candidate_local_attempt_count",
        "task_local_attempt_count", "candidate_local_selected_count", "task_local_selected_count",
        "task_network_request_count", "task_real_request_count", "task_compatibility_skip_count",
        "task_cooldown_skip_count", "task_compatibility_failure_count",
        "task_effective_response_count", "task_empty_200_response_count",
        "task_compatibility_failure_ratio", "task_effective_response_ratio",
        "task_selected_count",
    }
    strategy_params = {k: v for k, v in params.items() if k not in _TASK_META_KEYS}
    if not strategy_params:
        # 缺少策略参数 → 用 Spawner 的默认参数补充
        try:
            from strategy_factory.domain.spawner import StrategySpawner
            from strategy_factory.domain.parameter_distribution_registry import (
                ParameterDistributionRegistry,
            )
            spawner = StrategySpawner.__new__(StrategySpawner)
            default_params = spawner._legacy_varied_defaults(strategy_type, 0, snapshot=None)
            # PR-S21：优先用 task 上的 stock_profile 参数 band
            research_task = dict(candidate.get("research_task") or {})
            band = (
                research_task.get("param_search_space")
                or research_task.get("profile_param_band")
            )
            band_sample = ParameterDistributionRegistry.sample_from_param_band(band, idx=0)
            merged_defaults = dict(default_params or {})
            if band_sample:
                merged_defaults.update(dict(band_sample.get("params") or {}))
            if merged_defaults:
                params = {**merged_defaults, **params}
                # 写回 candidate 以便下游 Gate 使用
                candidate["params"] = params
        except Exception:
            # Same disclosure as the Gate-1 backfill above: the candidate proceeds
            # with the parameters it arrived with, and that fact is recorded.
            logger.warning(
                "Parameter backfill failed for candidate %s; running with the parameters "
                "it arrived with",
                candidate.get("strategy_id") or candidate.get("id") or "unknown",
                exc_info=True,
            )
    # ── End 参数补充 ──────────────────────────────────────────────────

    sharpe_min = float(_compat_setting("GATE1_SHARPE_MIN", GATE1_SHARPE_MIN) or GATE1_SHARPE_MIN)
    codes, prioritized_target_codes, code_source, validation_focus, research_task = _resolve_gate_1_codes(candidate)
    precompile_validation = validate_precompile_candidate_contract(
        candidate,
        research_task=research_task,
        source="gate_1_fast_screen",
    )
    precompile_summary = precompile_validation.to_dict()
    tested_codes = (
        list(prioritized_target_codes)
        if validation_focus != "broad_generalization" and prioritized_target_codes
        else list(codes)
    )
    sharpe_values: list[float] = []
    total_return_values: list[float] = []
    turnover_values: list[float] = []
    trade_count_values: list[float] = []
    max_drawdown_values: list[float] = []
    errors: list[str] = []
    market_data: dict[str, list] = {}

    if not precompile_validation.contract_completeness_ok:
        contract_reasons = list(precompile_validation.contract_reject_reasons or ["contract_incomplete"])
        return GateResult(
            passed=False,
            gate="gate_1",
            reasons=contract_reasons,
            metrics={
                "tested_codes": tested_codes,
                "target_codes": prioritized_target_codes,
                "code_source": code_source,
                "validation_focus": validation_focus,
                "primary_validation_layer": (
                    "combined"
                    if validation_focus == "broad_generalization"
                    else "target"
                ),
                "validation_contract_mode": "contract_fast_screen",
                "validation_scope": "fast_screen",
                "stage_role": "fast_screen",
                "contract_gate_status": "failed",
                "contract_gate": precompile_summary,
                "candidate_contract_hash": contract_hash,
                "tested_object_hash": tested_object_hash,
                "candidate_contract_snapshot": contract_snapshot,
                "backtest_assumptions": assumptions.to_audit_dict(),
                "tested_code_count": len(tested_codes),
                "eligible_code_count": 0,
                "sharpe_values": [],
            },
        )

    for code in tested_codes:
        try:
            if kline_cache and code in kline_cache:
                klines = kline_cache[code]
            else:
                klines = await db.get_klines(code, limit=250)
            if not klines or len(klines) < 30:
                errors.append(f"{code}:insufficient_klines")
                continue
            market_data[code] = list(klines)
        except Exception as exc:
            errors.append(f"{code}:{type(exc).__name__}")

    if len(market_data) > 1:
        portfolio_runner = getattr(factory_pkg.BacktestEngine, "run_portfolio_backtest", None)
        if not callable(portfolio_runner):
            errors.append("portfolio_contract_runner_unavailable")
        else:
            try:
                result = await asyncio.to_thread(
                    portfolio_runner,
                    market_data,
                    strategy_type,
                    params,
                )
                payload = dict(result.get("data") or {}) if isinstance(result, dict) else {}
                if not isinstance(result, dict) or not result.get("success"):
                    raise ValueError((result or {}).get("error") or "portfolio_backtest_failed")
                sharpe = float(payload.get("sharpe_ratio") or payload.get("sharpe") or 0.0)
                sharpe_values.append(sharpe)
                if payload.get("total_return") is not None:
                    total_return_values.append(_safe_float(payload.get("total_return"), 0.0))
                if payload.get("turnover_proxy") is not None:
                    turnover_values.append(_safe_float(payload.get("turnover_proxy"), 0.0))
                if payload.get("trades_count") is not None:
                    trade_count_values.append(_safe_float(payload.get("trades_count"), 0.0))
                if payload.get("max_drawdown") is not None:
                    max_drawdown_values.append(abs(_safe_float(payload.get("max_drawdown"), 0.0)))
            except Exception as exc:
                errors.append(f"portfolio_contract:{type(exc).__name__}")
    else:
        for code, klines in market_data.items():
            try:
                BacktestEngine = factory_pkg.BacktestEngine
                result = await asyncio.to_thread(
                    BacktestEngine.run_backtest,
                    code,
                    klines,
                    strategy_type,
                    params,
                )
                payload = dict(result.get("data") or {}) if isinstance(result, dict) else {}
                if not isinstance(result, dict) or not result.get("success"):
                    raise ValueError((result or {}).get("error") or "backtest_failed")
                sharpe = float(payload.get("sharpe_ratio") or payload.get("sharpe") or 0.0)
                sharpe_values.append(sharpe)
                if payload.get("total_return") is not None:
                    total_return_values.append(_safe_float(payload.get("total_return"), 0.0))
                if payload.get("turnover_proxy") is not None:
                    turnover_values.append(_safe_float(payload.get("turnover_proxy"), 0.0))
                if payload.get("trades_count") is not None:
                    trade_count_values.append(_safe_float(payload.get("trades_count"), 0.0))
                if payload.get("max_drawdown") is not None:
                    max_drawdown_values.append(abs(_safe_float(payload.get("max_drawdown"), 0.0)))
            except Exception as exc:
                errors.append(f"{code}:{type(exc).__name__}")

    if not sharpe_values:
        return GateResult(
            passed=False,
            gate="gate_1",
            reasons=["no_backtest_results", *errors],
            metrics={
                "tested_codes": tested_codes,
                "target_codes": prioritized_target_codes,
                "code_source": code_source,
                "validation_focus": validation_focus,
                "primary_validation_layer": (
                    "combined"
                    if validation_focus == "broad_generalization"
                    else "target"
                ),
                "validation_contract_mode": (
                    "portfolio_contract_fast_screen"
                    if len(tested_codes) > 1
                    else "single_code_fast_screen"
                ),
                "validation_scope": "fast_screen",
                "stage_role": "fast_screen",
                "contract_gate_status": "passed" if precompile_validation.contract_completeness_ok else "failed",
                "contract_gate": precompile_summary,
                "event_window_config": {
                    "event_window": dict(research_task.get("event_window") or {}),
                    "estimation_window": dict(research_task.get("estimation_window") or {}),
                    "holding_window": dict(research_task.get("holding_window") or {}),
                },
                "contamination_summary": {
                    "validation_focus": validation_focus,
                    "representative_included": bool([code for code in tested_codes if code not in prioritized_target_codes]),
                    "representative_code_count": len([code for code in tested_codes if code not in prioritized_target_codes]),
                },
                "candidate_contract_hash": contract_hash,
                "tested_object_hash": tested_object_hash,
                "candidate_contract_snapshot": contract_snapshot,
                "backtest_assumptions": assumptions.to_audit_dict(),
                "tested_code_count": len(tested_codes),
                "eligible_code_count": len(market_data),
                "sharpe_values": [],
            },
        )

    avg_sharpe = sum(sharpe_values) / len(sharpe_values)
    target_sample_count = len(prioritized_target_codes)
    research_target_count = len(_extract_target_codes_from_payload(candidate, limit=12))
    target_sample_ratio = round(target_sample_count / max(1, research_target_count), 4) if research_target_count > 0 else None

    # ══════════════════════════════════════════════════════════════════════════
    # 自适应门控评估（阶段 2-4 实现）
    # ══════════════════════════════════════════════════════════════════════════

    # ── 阶段 2: Benchmark-Relative Sharpe ──────────────────────────────────
    # 计算同期 Buy-and-Hold 基准收益
    benchmark_returns = []
    for code, klines in market_data.items():
        if klines and len(klines) >= 2:
            first_close = float(klines[0].get("close") or klines[0].get("Close") or 0)
            last_close = float(klines[-1].get("close") or klines[-1].get("Close") or 0)
            if first_close > 0:
                benchmark_returns.append(last_close / first_close - 1.0)
    avg_benchmark_return = sum(benchmark_returns) / len(benchmark_returns) if benchmark_returns else 0.0
    # 简化基准 Sharpe 估算：年化收益 / 假设波动率。
    #
    # 波动率是假设值而非观测量，故提取为命名常量。原式的 `else 0.0` 分支已删除：
    # 分子为 0 时商本来就是 0.0，该分支与主表达式等价——是冗余，不是兜底。把它
    # 当作"缺失时兜底"会误判严重度，所以这里按实际性质处理。
    trading_days = len(list(market_data.values())[0]) if market_data else 250
    annualization = 252.0 / max(trading_days, 1)
    benchmark_sharpe = (
        avg_benchmark_return * annualization
    ) / BENCHMARK_ASSUMED_ANNUAL_VOLATILITY
    excess_sharpe = avg_sharpe - benchmark_sharpe
    excess_sharpe_min = 0.10  # 策略需超越基准 0.10

    # ── 阶段 3: Regime-Aware 动态阈值 ─────────────────────────────────────
    if avg_benchmark_return > 0.10:
        regime = "bull"
        regime_sharpe_min = 0.30
    elif avg_benchmark_return < -0.05:
        regime = "bear"
        regime_sharpe_min = -0.50  # 熊市中亏得少就行
    else:
        regime = "neutral"
        regime_sharpe_min = 0.10

    # ── 阶段 4: 多维度综合评分 ─────────────────────────────────────────────
    multi_score = 0.0

    # 维度 1: 超额收益（30 分）
    avg_total_return = sum(total_return_values) / len(total_return_values) if total_return_values else 0.0
    excess_return = avg_total_return - avg_benchmark_return
    if excess_return > 0.05:
        multi_score += 30
    elif excess_return > 0.0:
        multi_score += 20
    elif excess_return > -0.03:
        multi_score += 10

    # 维度 2: 交易质量（25 分）— 有交易且 Sharpe 不是极端负值
    avg_trades = sum(trade_count_values) / len(trade_count_values) if trade_count_values else 0.0
    if avg_trades >= 3 and avg_sharpe > -1.0:
        multi_score += 25
    elif avg_trades >= 1:
        multi_score += 10

    # 维度 3: 风险控制（20 分）
    avg_mdd = sum(max_drawdown_values) / len(max_drawdown_values) if max_drawdown_values else 0.0
    if avg_mdd < 0.10:
        multi_score += 20
    elif avg_mdd < 0.20:
        multi_score += 12
    elif avg_mdd < 0.30:
        multi_score += 5

    # 维度 4: 相对表现（15 分）
    if excess_sharpe >= 0.20:
        multi_score += 15
    elif excess_sharpe >= 0.05:
        multi_score += 10
    elif excess_sharpe >= 0.0:
        multi_score += 5

    # 维度 5: 信号稳定性（10 分）
    if 3 <= avg_trades <= 50:
        multi_score += 10
    elif avg_trades > 0:
        multi_score += 5

    # ── 综合判定：三种方式任一通过即可 ─────────────────────────────────────
    passed_absolute = avg_sharpe >= sharpe_min                    # 原始绝对阈值
    passed_relative = excess_sharpe >= excess_sharpe_min          # 相对基准
    passed_regime = avg_sharpe >= regime_sharpe_min               # 市场状态自适应
    passed_multi = multi_score >= 45                              # 多维度综合

    passed = passed_absolute or passed_relative or passed_regime or passed_multi

    # 构建失败原因
    gate_1_reasons = []
    if not passed:
        gate_1_reasons.append(f"avg_sharpe_{avg_sharpe:.4f}_below_{sharpe_min}")
        gate_1_reasons.append(f"excess_sharpe_{excess_sharpe:.4f}_below_{excess_sharpe_min}")
        gate_1_reasons.append(f"regime_{regime}_sharpe_below_{regime_sharpe_min}")
        gate_1_reasons.append(f"multi_score_{multi_score:.0f}_below_45")

    return GateResult(
        passed=passed,
        gate="gate_1",
        reasons=gate_1_reasons,
        metrics={
            "tested_codes": tested_codes,
            "target_codes": prioritized_target_codes,
            "target_sample_count": target_sample_count,
            "research_target_count": research_target_count,
            "target_sample_ratio": target_sample_ratio,
            "code_source": code_source,
            "validation_focus": validation_focus,
            "primary_validation_layer": (
                "combined"
                if validation_focus == "broad_generalization"
                else "target"
            ),
            "validation_contract_mode": (
                "portfolio_contract_fast_screen"
                if len(tested_codes) > 1
                else "single_code_fast_screen"
            ),
            "validation_scope": "fast_screen",
            "stage_role": "fast_screen",
            "contract_gate_status": "passed" if precompile_validation.contract_completeness_ok else "failed",
            "contract_gate": precompile_summary,
            "tested_code_count": len(tested_codes),
            "eligible_code_count": len(market_data),
            "sharpe_values": [round(v, 4) for v in sharpe_values],
            "avg_sharpe": round(avg_sharpe, 4),
            "avg_total_return": round(avg_total_return, 6),
            "avg_turnover_proxy": round(sum(turnover_values) / len(turnover_values), 4) if turnover_values else 0.0,
            "avg_trades_count": round(avg_trades, 4),
            "avg_max_drawdown": round(avg_mdd, 6),
            "threshold": sharpe_min,
            # 自适应门控指标
            "adaptive_gate": {
                "benchmark_sharpe": round(benchmark_sharpe, 4),
                "excess_sharpe": round(excess_sharpe, 4),
                "excess_sharpe_min": excess_sharpe_min,
                "regime": regime,
                "regime_sharpe_min": regime_sharpe_min,
                "multi_score": round(multi_score, 1),
                "multi_score_threshold": 45,
                "passed_absolute": passed_absolute,
                "passed_relative": passed_relative,
                "passed_regime": passed_regime,
                "passed_multi": passed_multi,
                "avg_benchmark_return": round(avg_benchmark_return, 6),
            },
            "error_count": len(errors),
            "errors": errors,
            "candidate_contract_hash": contract_hash,
            "tested_object_hash": tested_object_hash,
            "candidate_contract_snapshot": contract_snapshot,
            "backtest_assumptions": assumptions.to_audit_dict(),
            "event_window_config": {
                "event_window": dict(research_task.get("event_window") or {}),
                "estimation_window": dict(research_task.get("estimation_window") or {}),
                "holding_window": dict(research_task.get("holding_window") or {}),
            },
            "contamination_summary": {
                "validation_focus": validation_focus,
                "representative_included": bool([code for code in tested_codes if code not in prioritized_target_codes]),
                "representative_code_count": len([code for code in tested_codes if code not in prioritized_target_codes]),
            },
        },
    )


# ---------------------------------------------------------------------------
# Pipeline: Gate-0 → Gate-1 → select top-K → Gate-2 (full backtest)
# ---------------------------------------------------------------------------

async def run_gated_filter(
    candidates: List[dict],
    db,
    backtest_filter,
    *,
    kline_cache: Optional[Dict[str, list]] = None,
) -> Dict[str, Any]:
    """执行 Gate-0 → Gate-1 → Gate-2 全流程。

    Returns dict with:
        passed: Gate-2 通过的候选列表
        gate_0_results: Gate-0 结果
        gate_1_results: Gate-1 结果
        gate_2_results: Gate-2 结果（BacktestFilter 报告）
    """
    # --- Gate-0 ---
    gate_0_passed: list[dict] = []
    gate_0_failed: list[dict] = []
    gate_0_batch_count = 0
    for start in range(0, len(candidates), _GATE_0_BATCH_SIZE):
        gate_0_batch_count += 1
        for candidate in candidates[start : start + _GATE_0_BATCH_SIZE]:
            prepared_candidate = apply_resolved_candidate_envelope(
                _enrich_legacy_gate_0_candidate(candidate)
            )
            candidate.clear()
            candidate.update(prepared_candidate)
            result = gate_0_structural(candidate)
            candidate["gate_0_result"] = {"passed": result.passed, "reasons": result.reasons}
            if result.passed:
                gate_0_passed.append(candidate)
            else:
                gate_0_failed.append(candidate)

    logger.info("Gate-0: %d/%d passed structural check", len(gate_0_passed), len(candidates))

    # --- Pre-Gate ---
    pre_gate_passed: list[dict] = []
    pre_gate_failed: list[dict] = []
    family_quota_limit = max(2, min(_PRE_GATE_FAMILY_QUOTA_DEFAULT, math.ceil(len(gate_0_passed) * 0.4))) if gate_0_passed else _PRE_GATE_FAMILY_QUOTA_DEFAULT
    per_stock_quota_limit = _PRE_GATE_PER_STOCK_QUOTA_DEFAULT
    if FACTORY_PRE_GATE_ENABLED:
        seen_signatures: set[str] = set()
        family_counts: dict[str, int] = {}
        stock_counts: dict[str, int] = {}
        for candidate in gate_0_passed:
            result = pre_gate_screen(
                candidate,
                seen_signatures=seen_signatures,
                family_counts=family_counts,
                stock_counts=stock_counts,
                family_quota_limit=family_quota_limit,
                per_stock_quota_limit=per_stock_quota_limit,
            )
            candidate["pre_gate_result"] = {
                "passed": result.passed,
                "reasons": result.reasons,
                "metrics": result.metrics,
            }
            if result.passed:
                pre_gate_passed.append(candidate)
            else:
                pre_gate_failed.append(candidate)
    else:
        pre_gate_passed = list(gate_0_passed)

    logger.info(
        "Pre-Gate: %d/%d passed cheap filter",
        len(pre_gate_passed),
        len(gate_0_passed),
    )

    # --- Gate-1 ---
    backtest_concurrency = int(_compat_setting("BACKTEST_CONCURRENCY", BACKTEST_CONCURRENCY) or BACKTEST_CONCURRENCY)
    sem = asyncio.Semaphore(backtest_concurrency)
    gate_1_preload_codes: list[str] = []
    gate_1_preload_status = "skipped"
    gate_1_kline_cache_ready = bool(kline_cache)
    if pre_gate_passed and hasattr(backtest_filter, "preload_klines"):
        gate_1_preload_codes = _collect_gate_1_preload_codes(pre_gate_passed)
        if gate_1_preload_codes:
            try:
                await backtest_filter.preload_klines(db, gate_1_preload_codes)
                refreshed_cache = getattr(backtest_filter, "_kline_cache", None)
                if refreshed_cache is not None:
                    kline_cache = refreshed_cache
                gate_1_kline_cache_ready = bool(kline_cache)
                gate_1_preload_status = "ready"
            except Exception as exc:
                # Retry with reduced scope (representative stocks only)
                try:
                    from ...domain.constants import REPRESENTATIVE_STOCKS as _REPR_STOCKS
                    await backtest_filter.preload_klines(db, list(_REPR_STOCKS))
                    refreshed_cache = getattr(backtest_filter, "_kline_cache", None)
                    if refreshed_cache is not None:
                        kline_cache = refreshed_cache
                    gate_1_kline_cache_ready = bool(kline_cache)
                    gate_1_preload_status = "partial_retry"
                except Exception:
                    gate_1_preload_status = f"failed:{type(exc).__name__}"
                logger.warning("Gate-1 preload failed for %d codes: %s", len(gate_1_preload_codes), exc)
        else:
            gate_1_preload_status = "no_codes"
    elif pre_gate_passed:
        gate_1_preload_status = "unsupported"
    elif FACTORY_PRE_GATE_ENABLED:
        gate_1_preload_status = "no_candidates"

    async def _screen_one(c: dict) -> tuple[dict, GateResult]:
        async with sem:
            try:
                return c, await _compat_gate_1_fast_screen(c, db, kline_cache=kline_cache)
            except Exception as exc:
                logger.warning("Gate-1 exception for %s: %s", c.get("strategy_type"), exc)
                return c, GateResult(
                    passed=False,
                    gate="gate_1",
                    reasons=[f"gate_1_exception:{type(exc).__name__}"],
                    metrics={"exception": str(exc)},
                )

    gate_1_tasks = [_screen_one(c) for c in pre_gate_passed]
    gate_1_raw = await asyncio.gather(*gate_1_tasks, return_exceptions=False)

    gate_1_scored: list[tuple[dict, float]] = []
    gate_1_failed: list[dict] = []
    for item in gate_1_raw:
        candidate, result = item
        candidate["gate_1_result"] = {
            "passed": result.passed,
            "reasons": result.reasons,
            "metrics": result.metrics,
        }
        if result.passed:
            avg_sharpe = float(result.metrics.get("avg_sharpe") or 0.0)
            candidate["gate_1_result"]["metrics"]["target_quality_summary"] = build_target_quality_gate_summary(
                candidate,
                gate_1_metrics=result.metrics,
            )
            block_reason = _post_gate_1_target_quality_block_reason(candidate, avg_sharpe)
            if block_reason:
                candidate["gate_1_result"]["passed"] = False
                candidate["gate_1_result"]["reasons"] = list(
                    dict.fromkeys([*(result.reasons or []), block_reason])
                )
                candidate["gate_1_result"]["metrics"]["post_gate_1_target_quality_block_reason"] = block_reason
                gate_1_failed.append(candidate)
                continue
            priority_score, priority_meta = _gate_2_priority_score(candidate, avg_sharpe, return_meta=True)
            candidate["gate_1_result"]["metrics"]["gate_2_priority_score"] = priority_score
            candidate["gate_1_result"]["metrics"]["gate_2_priority_meta"] = priority_meta
            gate_1_scored.append((candidate, priority_score))
        else:
            gate_1_failed.append(candidate)

    # 按综合优先级排序，进入 Gate-2 优先队列。
    gate_1_scored.sort(key=lambda x: x[1], reverse=True)
    gate1_pass_ratio = float(_compat_setting("GATE1_PASS_RATIO", GATE1_PASS_RATIO) or GATE1_PASS_RATIO)
    top_k = _resolve_gate_2_top_k(len(gate_1_scored), gate1_pass_ratio)
    gate_2_candidates = _select_gate_2_candidates(gate_1_scored, top_k)

    logger.info(
        "Gate-1: %d/%d passed fast screen, top-%d enter Gate-2 priority queue",
        len(gate_1_scored), len(pre_gate_passed), len(gate_2_candidates),
    )

    # --- Gate-2 (full backtest via BacktestFilter) ---
    if gate_2_candidates:
        gate_2_passed = await backtest_filter.filter(gate_2_candidates, db)
    else:
        gate_2_passed = []

    logger.info("Gate-2: %d/%d passed full backtest", len(gate_2_passed), len(gate_2_candidates))

    summary = {
        "input_count": len(candidates),
        "gate_0_passed": len(gate_0_passed),
        "gate_0_failed": len(gate_0_failed),
        "gate_0_batch_size": _GATE_0_BATCH_SIZE,
        "gate_0_batch_count": gate_0_batch_count,
        "pre_gate_passed": len(pre_gate_passed),
        "pre_gate_failed": len(pre_gate_failed),
        "gate_1_passed": len(gate_1_scored),
        "gate_1_failed": len(gate_1_failed),
        "gate_1_preload_code_count": len(gate_1_preload_codes),
        "gate_1_kline_cache_ready": gate_1_kline_cache_ready,
        "gate_2_input": len(gate_2_candidates),
        "gate_2_passed": len(gate_2_passed),
        "gate_3_pending": len(gate_2_passed),
    }
    gate_0_failed_details = [
        {"strategy_type": c.get("strategy_type"), "reasons": (c.get("gate_0_result") or {}).get("reasons")}
        for c in gate_0_failed[:COMPACT_PREVIEW_LIMIT]
    ]
    gate_1_failed_details = [
        {
            "strategy_type": c.get("strategy_type"),
            "reasons": (c.get("gate_1_result") or {}).get("reasons"),
            "metrics": (c.get("gate_1_result") or {}).get("metrics") or {},
        }
        for c in gate_1_failed[:COMPACT_PREVIEW_LIMIT]
    ]
    pre_gate_failed_details = [
        {
            "strategy_type": c.get("strategy_type"),
            "reasons": (c.get("pre_gate_result") or {}).get("reasons"),
            "metrics": (c.get("pre_gate_result") or {}).get("metrics") or {},
        }
        for c in pre_gate_failed[:COMPACT_PREVIEW_LIMIT]
    ]
    gate_2_report = _compact_backtest_report_for_gate(
        backtest_filter.get_last_report() if hasattr(backtest_filter, "get_last_report") else {}
    )
    gate_report = {
        "gate_0": {
            "passed_count": len(gate_0_passed),
            "failed_count": len(gate_0_failed),
            "batch_size": _GATE_0_BATCH_SIZE,
            "batch_count": gate_0_batch_count,
            "failed": gate_0_failed_details,
            "failed_preview_count": len(gate_0_failed_details),
        },
        "pre_gate": {
            "status": "completed" if FACTORY_PRE_GATE_ENABLED else "disabled",
            "passed_count": len(pre_gate_passed),
            "failed_count": len(pre_gate_failed),
            "failed": pre_gate_failed_details,
            "failed_preview_count": len(pre_gate_failed_details),
            "limits": {
                "family_quota_limit": family_quota_limit if FACTORY_PRE_GATE_ENABLED else None,
                "per_stock_quota_limit": per_stock_quota_limit if FACTORY_PRE_GATE_ENABLED else None,
                "signal_density_min": _PRE_GATE_SIGNAL_DENSITY_MIN if FACTORY_PRE_GATE_ENABLED else None,
                "signal_density_max": _PRE_GATE_SIGNAL_DENSITY_MAX if FACTORY_PRE_GATE_ENABLED else None,
            },
        },
        "gate_1": {
            "passed_count": len(gate_1_scored),
            "failed_count": len(gate_1_failed),
            "selection_mode": "priority_queue",
            "preload_status": gate_1_preload_status,
            "preload_code_count": len(gate_1_preload_codes),
            "kline_cache_ready": gate_1_kline_cache_ready,
            "failed": gate_1_failed_details,
            "failed_preview_count": len(gate_1_failed_details),
            "passed_candidates": [
                _compact_gate_candidate_brief(candidate, score=round(score, 4))
                for candidate, score in gate_1_scored[:COMPACT_PREVIEW_LIMIT]
            ],
            "passed_candidates_count": len(gate_1_scored),
            "passed_candidates_is_brief": True,
        },
        "gate_2": {
            "input_count": len(gate_2_candidates),
            "passed_count": len(gate_2_passed),
            "selection_mode": "priority_queue",
            "passed_candidates": [
                _compact_gate_candidate_brief(candidate)
                for candidate in gate_2_passed[:COMPACT_PREVIEW_LIMIT]
            ],
            "passed_candidates_count": len(gate_2_passed),
            "passed_candidates_is_brief": True,
            "report": gate_2_report,
        },
        "gate_3": build_pending_gate_3_report(len(gate_2_passed)),
        "final_decision": {
            "stage": "gate_2",
            "passed_count": len(gate_2_passed),
            "pending_submission_gate_count": len(gate_2_passed),
        },
    }

    gate_report = _compact_quality_gate_report(gate_report)

    return {
        "passed": gate_2_passed,
        "summary": summary,
        "gate_0_failed": gate_0_failed_details,
        "pre_gate_failed": pre_gate_failed_details,
        "quality_gate": gate_report,
        "gate_report": gate_report,
    }

# --- quality_gates_parts/models.py ---
async def run_gated_submission_pipeline(
    candidates: List[dict],
    snapshot: dict,
    db,
    *,
    backtest_filter=None,
    deduplicator=None,
    submitter=None,
    gated_runner=None,
    kline_cache: Optional[Dict[str, list]] = None,
) -> Dict[str, Any]:
    """执行 Gate-0/1/2 → 去重 → Gate-3 的统一工厂门禁编排。"""
    factory_pkg = get_strategy_factory_package()
    backtest_filter = backtest_filter or factory_pkg.BacktestFilter()
    deduplicator = deduplicator or factory_pkg.Deduplicator()
    submitter = submitter or factory_pkg.StrategySubmitter()
    candidates = [
        attach_trade_prediction_context(candidate, snapshot=snapshot)
        for candidate in list(candidates or [])
    ]

    gate_runner = gated_runner or getattr(factory_pkg, "run_gated_filter", run_gated_filter)

    gate_run = await gate_runner(
        candidates,
        db,
        backtest_filter,
        kline_cache=kline_cache,
    )
    gate_report = _compact_quality_gate_report(
        gate_run.get("gate_report") or gate_run.get("quality_gate") or {}
    )
    passed = list(gate_run.get("passed") or [])
    unique = await deduplicator.deduplicate(passed, db)
    submit_result = await submitter.submit(unique, snapshot, db)
    final_gate_report = _compact_quality_gate_report(
        finalize_gate_report(gate_report, submit_result)
    )
    raw_backtest_report = (gate_report.get("gate_2") or {}).get("report")
    if not raw_backtest_report and hasattr(backtest_filter, "get_last_report"):
        raw_backtest_report = backtest_filter.get_last_report()
    return {
        "passed": passed,
        "unique": unique,
        "submitted": list(submit_result.get("strategies") or []),
        "gate_run": {
            "summary": dict(gate_run.get("summary") or {}),
            "gate_report": gate_report,
            "quality_gate": gate_report,
        },
        "submit_result": submit_result,
        "gate_report": final_gate_report,
        "quality_gate": final_gate_report,
        "dedup_report": (
            deduplicator.get_last_report()
            if hasattr(deduplicator, "get_last_report")
            else {}
        ),
        "backtest_report": _compact_backtest_report_for_gate(raw_backtest_report or {}),
    }


def build_legacy_gate_report(
    candidates: List[dict],
    passed: List[dict],
    backtest_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """为尚未切到统一 GateRunner 的调用方构造兼容 gate_report。"""
    backtest_report = _compact_backtest_report_for_gate(backtest_report or {})
    backtest_summary = dict(backtest_report.get("summary") or {})
    gate_2_passed = int(backtest_summary.get("passed_count", len(passed)))
    gate_2_input = int(backtest_summary.get("input_count", len(candidates)))
    gate_2_failed = int(backtest_summary.get("failed_count", max(gate_2_input - gate_2_passed, 0)))
    return {
        "gate_0": {
            "status": "legacy_backtest_only",
            "passed_count": None,
            "failed_count": None,
            "reason": "gate_0_not_recorded_in_legacy_path",
        },
        "pre_gate": {
            "status": "legacy_backtest_only",
            "passed_count": None,
            "failed_count": None,
            "reason": "pre_gate_not_recorded_in_legacy_path",
        },
        "gate_1": {
            "status": "legacy_backtest_only",
            "passed_count": None,
            "failed_count": None,
            "reason": "gate_1_not_recorded_in_legacy_path",
        },
        "gate_2": {
            "status": "legacy_backtest_only",
            "input_count": gate_2_input,
            "passed_count": gate_2_passed,
            "failed_count": gate_2_failed,
            "report": backtest_report,
        },
        "gate_3": build_pending_gate_3_report(gate_2_passed),
        "final_decision": {
            "stage": "gate_2",
            "passed_count": gate_2_passed,
            "pending_submission_gate_count": gate_2_passed,
        },
    }


def finalize_gate_report(
    base_gate_report: Optional[Dict[str, Any]],
    submission_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """将 Gate-3 提交结果合并进 Gate-0/1/2 报告，形成最终门禁闭环。"""
    merged = deepcopy(base_gate_report or {})
    submission_result = dict(submission_result or {})
    submit_gate_report = dict(submission_result.get("gate_report") or {})
    completed_gate_report = build_completed_gate_3_report(submission_result)
    merged["gate_3"] = dict(submit_gate_report.get("gate_3") or completed_gate_report["gate_3"])
    merged["final_decision"] = dict(
        submit_gate_report.get("final_decision") or completed_gate_report["final_decision"]
    )
    return merged

