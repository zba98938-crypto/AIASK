"""Report rendering + blocker/sample analysis for the quality session script."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from _quality_session_common import (
    DEFAULT_EXECUTION_MODE,
    LOGGER,
    MARKET_TZ,
    _format_dt,
    _iso_now,
    _json_dump,
    _now,
    _pct,
    _process_alive,
    _safe_float,
    _safe_int,
    _write_json,
)
def _normalize_blocker_reason(reason: Any) -> str:
    text = str(reason or "").strip()
    if not text:
        return ""
    return text.split(" ", 1)[0]


def _build_blocker_summary(strategies: list[dict[str, Any]]) -> dict[str, Any]:
    blocker_counts: Counter[str] = Counter()
    blocker_examples: dict[str, str] = {}
    strict_not_ready_count = 0
    strategies_with_blockers = 0

    for item in strategies:
        blockers = list(item.get("admission_block_reasons") or [])
        if item.get("strict_incubation_ready") is False:
            strict_not_ready_count += 1
        if blockers:
            strategies_with_blockers += 1
        for blocker in blockers:
            key = _normalize_blocker_reason(blocker)
            if not key:
                continue
            blocker_counts[key] += 1
            blocker_examples.setdefault(key, str(blocker))

    top_blockers = [
        {
            "reason": reason,
            "count": count,
            "example": blocker_examples.get(reason),
        }
        for reason, count in blocker_counts.most_common(10)
    ]
    return {
        "analyzed_strategy_count": len(strategies),
        "strict_not_ready_count": strict_not_ready_count,
        "strategies_with_blockers": strategies_with_blockers,
        "top_blockers": top_blockers,
    }


def _format_top_blockers(items: list[dict[str, Any]], limit: int = 5) -> str:
    if not items:
        return ""
    return "; ".join(
        f"{str(item.get('reason') or 'unknown')} x{_safe_int(item.get('count'))}"
        for item in items[: max(1, limit)]
    )


_FORMAL_CONTRACT_BLOCKER_FRAGMENTS = (
    "missing_executable_contract",
    "default_profile_not_allowed_for_single_name_runtime",
    "measured_profile_incomplete",
    "observe_diagnostic_only",
    "diagnostic_only",
    "proxy_runtime",
    "semantic_runtime_mismatch",
    "runtime_family_semantic_mismatch",
    "execution_semantic_gap",
    "execution_readiness_tier:",
    "trade_prediction_contract_not_ready",
    "trade_prediction_contract_observation_gap",
)

_TRUE_QUALITY_BLOCKER_FRAGMENTS = (
    "profit_factor",
    "walk_forward_ic_ir",
    "wf_ic_ir",
    "purged_kfold_ic",
    "pkf_ic",
    "bootstrap_ci_lower",
    "win_rate",
    "expectancy",
    "trade_count",
    "period_robustness",
    "param_sensitivity",
    "out_of_sample_profit_factor",
    "sharpe",
    "max_drawdown",
)


def _sample_reason_tokens(item: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for key in (
        "admission_block_reasons",
        "business_admission_reasons",
        "execution_audit_gate_reasons",
        "trade_prediction_contract_reject_reasons",
    ):
        for value in list(item.get(key) or []):
            text = str(value or "").strip().lower()
            if text:
                tokens.append(text)
    for key in (
        "quality_report_runtime_bootstrap_reason",
        "quality_report_execution_readiness_tier",
        "trade_prediction_contract_status",
    ):
        text = str(item.get(key) or "").strip().lower()
        if text:
            tokens.append(text)
    return list(dict.fromkeys(tokens))


def _sort_strategy_samples(strategies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        list(strategies or []),
        key=lambda item: (
            item.get("validation_total_score") is not None,
            _safe_float(item.get("validation_total_score")),
            -len(list(item.get("admission_block_reasons") or [])),
        ),
        reverse=True,
    )


def _select_representative_samples(strategies: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    ranked = _sort_strategy_samples(strategies)
    representatives: list[dict[str, Any]] = []

    for item in ranked:
        if (
            str(item.get("submission_lane") or "").strip().lower() == "observe_incubation"
            and bool(item.get("strict_incubation_ready"))
            and item.get("quality_report_formal_track_requested") is False
        ):
            representatives.append(item)

    for item in ranked:
        if item in representatives:
            continue
        if (
            str(item.get("submission_lane") or "").strip().lower() == "observe_incubation"
            and item.get("strict_incubation_ready") is False
            and _safe_float(item.get("validation_total_score")) >= 60.0
        ):
            representatives.append(item)

    unique_representatives = []
    seen_strategy_ids: set[str] = set()
    for item in representatives:
        strategy_id = str(item.get("strategy_id") or "").strip()
        if strategy_id and strategy_id in seen_strategy_ids:
            continue
        if strategy_id:
            seen_strategy_ids.add(strategy_id)
        unique_representatives.append(item)

    return unique_representatives[: max(1, limit)]


def _merge_strategy_samples(strategies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged_by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    anonymous: list[dict[str, Any]] = []

    for raw_item in list(strategies or []):
        item = dict(raw_item or {})
        strategy_id = str(item.get("strategy_id") or "").strip()
        if not strategy_id:
            anonymous.append(item)
            continue
        if strategy_id not in merged_by_id:
            merged_by_id[strategy_id] = item
            ordered_ids.append(strategy_id)
            continue
        merged = dict(merged_by_id[strategy_id])
        for key, value in item.items():
            if merged.get(key) in (None, "", [], {}):
                merged[key] = value
        merged_by_id[strategy_id] = merged

    return [*anonymous, *(merged_by_id[strategy_id] for strategy_id in ordered_ids)]


def _quality_strategy_pool(quality: dict[str, Any]) -> list[dict[str, Any]]:
    return _merge_strategy_samples(
        [
            *list(quality.get("representative_samples") or []),
            *list(quality.get("sampled_strategies") or []),
        ]
    )


def _resolve_candidate_artifact(detail: dict[str, Any]) -> dict[str, Any]:
    primary = dict(detail.get("candidate_artifact") or {})
    if bool(primary.get("available")):
        return primary
    research_plane = dict(detail.get("research_plane") or {})
    plane_candidate = dict(research_plane.get("candidate_artifact") or {})
    if bool(plane_candidate.get("available")):
        return plane_candidate
    return primary


def _submission_breakdown(
    summary: dict[str, Any],
    submission_artifact: dict[str, Any],
) -> dict[str, int]:
    lane_counts = dict(
        summary.get("submission_lane_counts")
        or submission_artifact.get("submission_lane_counts")
        or {}
    )
    strategy_status_counts = dict(submission_artifact.get("strategy_status_counts") or {})
    return {
        "live_submitted": _safe_int(lane_counts.get("live_ready_review")),
        "formal_incubation_created": _safe_int(lane_counts.get("formal_incubation")),
        "observe_paper_created": (
            _safe_int(lane_counts.get("observe_incubation"))
            + _safe_int(lane_counts.get("paper_observation"))
        ),
        "audit_only_created": (
            _safe_int(lane_counts.get("diagnostic_observation"))
            + _safe_int(strategy_status_counts.get("diagnostic"))
        ),
    }


def _compact_run_detail(detail: dict[str, Any]) -> dict[str, Any]:
    summary = dict(detail.get("summary") or {})
    stages = dict(detail.get("stages") or {})
    readiness_stage = dict(stages.get("readiness") or {})
    factor_stage = dict(stages.get("factor_research") or {})
    factor_summary = dict(factor_stage.get("summary") or {})
    dedup = dict(detail.get("dedup_artifact") or {})
    candidate_artifact = _resolve_candidate_artifact(detail)
    submission_artifact = dict(detail.get("submission_artifact") or {})
    incubation_budget_summary = dict(submission_artifact.get("incubation_budget_summary") or {})
    submission_breakdown = _submission_breakdown(summary, submission_artifact)
    return {
        "run_id": detail.get("run_id"),
        "status": detail.get("status"),
        "execution_mode": detail.get("execution_mode"),
        "candidates_spawned": _safe_int(detail.get("candidates_spawned")),
        "submitted": _safe_int(detail.get("submitted")),
        **submission_breakdown,
        "readiness_score": _safe_float(detail.get("readiness_score")),
        "readiness": {
            "stage_status": readiness_stage.get("status"),
            "can_proceed": bool(
                summary.get("factory_readiness_can_proceed")
                if summary.get("factory_readiness_can_proceed") is not None
                else readiness_stage.get("can_proceed")
            ),
            "hard_gate": bool(summary.get("factory_readiness_hard_gate") or readiness_stage.get("hard_gate")),
            "blocker_count": _safe_int(
                summary.get("factory_readiness_blocker_count")
                if summary.get("factory_readiness_blocker_count") is not None
                else readiness_stage.get("blocker_count")
            ),
            "warning_count": _safe_int(
                summary.get("factory_readiness_warning_count")
                if summary.get("factory_readiness_warning_count") is not None
                else readiness_stage.get("warning_count")
            ),
            "blockers": list(readiness_stage.get("blockers") or []),
            "warnings": list(readiness_stage.get("warnings") or []),
        },
        "factor_research": {
            "stage_status": factor_stage.get("status"),
            "factor_source_mode": (
                factor_stage.get("factor_source_mode")
                or factor_summary.get("factor_source_mode")
            ),
            "governed_candidate_pool_mode": (
                factor_stage.get("governed_candidate_pool_mode")
                or factor_summary.get("governed_candidate_pool_mode")
            ),
            "active_candidate_count": _safe_int(
                factor_stage.get("active_candidate_count")
                or factor_summary.get("active_candidate_count")
            ),
            "active_factor_count": _safe_int(
                factor_stage.get("active_factor_count")
                or factor_summary.get("active_factor_count")
            ),
            "refresh_attempted": bool(factor_stage.get("refresh_attempted")),
            "refresh_status": factor_stage.get("refresh_status"),
            "scheduler_recent_success": bool(
                factor_stage.get("scheduler_recent_success")
                or factor_summary.get("scheduler_recent_success")
            ),
        },
        "raw_validation_a_rate": _safe_float(detail.get("raw_validation_a_rate")),
        "raw_validation_b_rate": _safe_float(detail.get("raw_validation_b_rate")),
        "raw_validation_c_rate": _safe_float(detail.get("raw_validation_c_rate")),
        "raw_validation_d_rate": _safe_float(detail.get("raw_validation_d_rate")),
        "strict_incubation_ready_count": _safe_int(detail.get("strict_incubation_ready_count")),
        "raw_b_or_above_count": _safe_int(detail.get("raw_b_or_above_count")),
        "summary": {
            "gate_3_input": _safe_int(summary.get("gate_3_input")),
            "gate_3_passed": _safe_int(summary.get("gate_3_passed")),
            "gate_3_failed": _safe_int(summary.get("gate_3_failed")),
            "submitted": _safe_int(summary.get("submitted")),
            **submission_breakdown,
            "submission_lane_counts": dict(summary.get("submission_lane_counts") or {}),
            "pipeline_fallback_counts": dict(summary.get("pipeline_fallback_counts") or {}),
            "gate_3_failure_topn": list(
                summary.get("gate_3_failure_topn") or summary.get("gate_3_failure_reason_topn") or []
            ),
        },
        "dedup_artifact": {
            "input_count": _safe_int(dedup.get("input_count")),
            "existing_count": _safe_int(dedup.get("existing_count")),
            "kept_count": _safe_int(dedup.get("kept_count")),
            "dropped_count": _safe_int(dedup.get("dropped_count")),
            "duplicate_level_counts": dict(dedup.get("duplicate_level_counts") or {}),
        },
        "candidate_artifact": {
            "family_counts": dict(candidate_artifact.get("family_counts") or {}),
            "candidate_origin_counts": dict(candidate_artifact.get("candidate_origin_counts") or {}),
        },
        "submission_artifact": {
            "incubation_budget_summary": {
                "formal_slots": _safe_int(incubation_budget_summary.get("formal_slots")),
                "observe_slots": _safe_int(incubation_budget_summary.get("observe_slots")),
                "formal_family_cap": _safe_int(incubation_budget_summary.get("formal_family_cap")),
                "observe_family_cap": _safe_int(incubation_budget_summary.get("observe_family_cap")),
                "exploration_reserved_slots": _safe_int(incubation_budget_summary.get("exploration_reserved_slots")),
                "feedback_available": bool(incubation_budget_summary.get("feedback_available")),
                "dominant_families": list(incubation_budget_summary.get("dominant_families") or []),
                "track_counts": dict(incubation_budget_summary.get("track_counts") or {}),
                "planned_track_counts": dict(incubation_budget_summary.get("planned_track_counts") or {}),
                "effective_track_counts": dict(incubation_budget_summary.get("effective_track_counts") or {}),
                "final_lane_counts": dict(incubation_budget_summary.get("final_lane_counts") or {}),
                "track_counts_reconciled": bool(incubation_budget_summary.get("track_counts_reconciled")),
                "auto_promoted_formal_count": _safe_int(incubation_budget_summary.get("auto_promoted_formal_count")),
                "auto_promoted_observe_count": _safe_int(incubation_budget_summary.get("auto_promoted_observe_count")),
            },
            "strategy_status_counts": dict(submission_artifact.get("strategy_status_counts") or {}),
        },
    }


def _extract_issue_flags(
    detail: dict[str, Any],
    sampled_strategies: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    summary = dict(detail.get("summary") or {})
    stages = dict(detail.get("stages") or {})
    readiness_stage = dict(stages.get("readiness") or {})
    factor_stage = dict(stages.get("factor_research") or {})
    factor_summary = dict(factor_stage.get("summary") or {})
    submission_artifact = dict(detail.get("submission_artifact") or {})
    budget_summary = dict(submission_artifact.get("incubation_budget_summary") or {})
    dedup_artifact = dict(detail.get("dedup_artifact") or {})

    flags: list[str] = []
    notes: list[str] = []

    fallback_counts = dict(summary.get("pipeline_fallback_counts") or {})
    if fallback_counts:
        flags.append("pipeline_stage_fallback")
        flags.append("infra_degraded")
        notes.append(f"pipeline staged fallback observed: {fallback_counts}")
    if any("no_executable_specs" in str(key) for key in fallback_counts):
        flags.append("pipeline_no_executable_specs")
        flags.append("infra_degraded")
        notes.append(f"staged pipeline empty-spec fallback: {fallback_counts}")
    runtime_status = str(detail.get("status") or "").strip().lower()
    if runtime_status and runtime_status != "success":
        flags.append("factory_runtime_degraded")
        notes.append(f"factory runtime completed with degraded status `{runtime_status}`")
        readiness_can_proceed = (
            summary.get("factory_readiness_can_proceed")
            if summary.get("factory_readiness_can_proceed") is not None
            else readiness_stage.get("can_proceed")
        )
        readiness_blockers = _safe_int(
            summary.get("factory_readiness_blocker_count")
            if summary.get("factory_readiness_blocker_count") is not None
            else readiness_stage.get("blocker_count")
        )
        readiness_warnings = _safe_int(
            summary.get("factory_readiness_warning_count")
            if summary.get("factory_readiness_warning_count") is not None
            else readiness_stage.get("warning_count")
        )
        if (
            runtime_status == "partial_infra"
            and bool(readiness_can_proceed)
            and (readiness_blockers > 0 or readiness_warnings > 0)
            and not fallback_counts
        ):
            flags.append("readiness_evidence_debt")
            notes.append(
                "readiness allowed the run to proceed, but evidence debt remains "
                f"(blockers={readiness_blockers}, warnings={readiness_warnings})"
            )
        elif runtime_status == "partial_llm":
            flags.append("llm_runtime_degraded")
        else:
            flags.append("infra_degraded")
    if _safe_int(fallback_counts.get("cooldown_skip")) > 0:
        flags.append("llm_timeout_cooldown_active")
        flags.append("infra_degraded")
        notes.append(
            "staged pipeline entered timeout cooldown and skipped some LLM phases "
            f"(cooldown_skip={_safe_int(fallback_counts.get('cooldown_skip'))})"
        )

    factor_source_mode = str(
        factor_stage.get("factor_source_mode")
        or factor_summary.get("factor_source_mode")
        or ""
    ).strip().lower()
    governed_candidate_pool_mode = str(
        factor_stage.get("governed_candidate_pool_mode")
        or factor_summary.get("governed_candidate_pool_mode")
        or ""
    ).strip().lower()
    if factor_source_mode == "active_factor_pool_fallback" or governed_candidate_pool_mode == "active_factor_pool_fallback":
        flags.append("factor_research_active_pool_fallback")
        notes.append(
            "factor research consumed the existing active factor pool fallback rather than a fresh governed candidate pool"
        )
    if str(factor_stage.get("refresh_status") or "").strip().lower() == "disabled":
        flags.append("factor_research_refresh_disabled")
        notes.append("factor research refresh was disabled during the run")

    sampled_reason_tokens = [
        token
        for item in sampled_strategies
        for token in _sample_reason_tokens(dict(item or {}))
    ]
    if any(
        any(fragment in token for fragment in _FORMAL_CONTRACT_BLOCKER_FRAGMENTS)
        for token in sampled_reason_tokens
    ) or any(
        str(item.get("quality_report_execution_readiness_tier") or "").strip().lower()
        not in {"", "formal_runtime_ready"}
        for item in sampled_strategies
    ):
        flags.append("formal_contract_blocked")
        notes.append(
            "sampled strategies still show formal contract blockers "
            "(runtime contract, measured profile, diagnostic/proxy runtime, or trade prediction readiness)"
        )
    if any(
        any(fragment in token for fragment in _TRUE_QUALITY_BLOCKER_FRAGMENTS)
        for token in sampled_reason_tokens
    ):
        flags.append("true_quality_blocked")
        notes.append(
            "sampled strategies still show true quality blockers such as PF, WF/PKF IC, bootstrap, win-rate, "
            "trade-count, or drawdown thresholds"
        )

    if _safe_int(dedup_artifact.get("input_count")) > 0 and _safe_int(dedup_artifact.get("kept_count")) == 0:
        flags.append("dedup_zero_keep")
        notes.append(
            "all post-backtest candidates were removed by dedup "
            f"(input={_safe_int(dedup_artifact.get('input_count'))}, existing={_safe_int(dedup_artifact.get('existing_count'))})"
        )

    submission_lane_counts = dict(summary.get("submission_lane_counts") or submission_artifact.get("submission_lane_counts") or {})
    concrete_lane_total = sum(
        _safe_int(submission_lane_counts.get(name))
        for name in (
            "formal_incubation",
            "observe_incubation",
            "diagnostic_observation",
            "live_ready_review",
            "deferred_submission",
        )
    )

    if (
        _safe_int(detail.get("candidates_spawned")) > 0
        and _safe_int(detail.get("submitted")) == 0
        and concrete_lane_total == 0
    ):
        flags.append("no_submission_after_generation")
        notes.append(
            "factory generated candidates but produced no submissions "
            f"(spawned={_safe_int(detail.get('candidates_spawned'))}, submitted=0)"
        )

    submitted = _safe_int(detail.get("submitted") or summary.get("submitted"))
    observe_only_count = _safe_int(submission_lane_counts.get("observe_incubation"))
    if submitted > 0 and observe_only_count >= submitted:
        flags.append("observe_only_submission")
        notes.append(f"all submitted strategies were routed to observe_incubation ({observe_only_count}/{submitted})")
        if _safe_int(summary.get("gate_3_passed")) > 0:
            flags.append("gate_pass_but_observe_only")
            notes.append(
                "gate_3 reported passed candidates, but the completed round still routed all submissions to observe_incubation "
                f"(gate_3_passed={_safe_int(summary.get('gate_3_passed'))}, submitted={submitted})"
            )

    budget_track_counts = dict(
        budget_summary.get("effective_track_counts")
        or budget_summary.get("track_counts")
        or {}
    )
    if _safe_int(budget_track_counts.get("deferred_budget_queue")) > 0 and (
        _safe_int(budget_track_counts.get("formal_incubation")) == 0
        and _safe_int(budget_track_counts.get("observe_incubation")) == 0
    ):
        final_lane_total = sum(
            _safe_int(submission_lane_counts.get(name))
            for name in (
                "formal_incubation",
                "observe_incubation",
                "diagnostic_observation",
                "live_ready_review",
                "deferred_submission",
            )
        )
        if final_lane_total > 0:
            flags.append("budget_summary_final_lane_mismatch")
            notes.append(
                "incubation budget summary stayed in deferred_budget_queue, but final admission still produced concrete submission lanes; "
                "this points to a plan-vs-final routing contract mismatch rather than a pure no-track condition "
                f"(budget_track_counts={budget_track_counts}, final_lane_counts={submission_lane_counts})"
            )
        else:
            flags.append("budget_queue_without_track_assignment")
            notes.append(
                "incubation budget summary shows candidates staying in deferred_budget_queue with no formal/observe budget assignment "
                f"({budget_track_counts})"
            )

    if _safe_int(detail.get("strict_incubation_ready_count")) == 0 and _safe_int(detail.get("raw_b_or_above_count")) > 0:
        flags.append("strict_ready_zero_despite_raw_b")
        notes.append(
            "there are B-or-above strategies, but none reached strict incubation readiness "
            f"(raw_b_or_above={_safe_int(detail.get('raw_b_or_above_count'))})"
        )

    if _safe_float(detail.get("raw_validation_d_rate")) >= 0.5:
        flags.append("quality_d_heavy")
        notes.append(f"D-grade share is high ({_pct(detail.get('raw_validation_d_rate'))})")

    if any(bool(item.get("persisted_params_truncated")) for item in sampled_strategies):
        flags.append("strategy_params_storage_truncated")
        notes.append(
            "sampled strategy rows were stored in compact_json mode, so row-level params were truncated in SQLite "
            "and cannot be treated as complete persistence evidence"
        )

    if any(
        bool(item.get("persisted_params_dropped_incubation_budget"))
        and not bool(item.get("persisted_incubation_budget_present"))
        for item in sampled_strategies
    ):
        flags.append("strategy_params_budget_metadata_compacted_away")
        notes.append(
            "sampled strategy rows dropped `incubation_budget` from `strategies.params` during JSON compaction, "
            "so planner budget selections are not directly visible on persisted strategy rows"
        )

    if any(
        str(item.get("quality_report_submission_lane") or "").strip()
        and not str(item.get("quality_report_planned_submission_lane") or "").strip()
        and not str(item.get("quality_report_incubation_budget_track") or "").strip()
        for item in sampled_strategies
    ):
        flags.append("quality_report_plan_metadata_missing")
        notes.append(
            "sampled submission quality reports preserve the final `submission_lane`, but omit "
            "`planned_submission_lane` and `incubation_budget_track`, leaving a plan-vs-final metadata gap"
        )

    if any(
        str(item.get("quality_report_submission_lane") or "").strip()
        and not str(item.get("persisted_submission_lane") or "").strip()
        for item in sampled_strategies
    ):
        flags.append("strategy_row_submission_metadata_missing")
        notes.append(
            "sampled strategy rows do not retain `submission_lane` / `planned_submission_lane` in `strategies.params` "
            "even though submission quality reports record a final submission lane"
        )

    if any(item.get("quality_runtime_context_consistent") is False for item in sampled_strategies):
        flags.append("quality_report_runtime_context_mismatch")
        first_mismatch = next(
            (item for item in sampled_strategies if item.get("quality_runtime_context_consistent") is False),
            None,
        )
        mismatch_fields = list((first_mismatch or {}).get("quality_runtime_context_mismatch_fields") or [])
        notes.append(
            "sampled strategy quality reports still show runtime context mismatch between `quality_gate` and "
            "`summary`; blocker attribution cannot be trusted for those rows until the persisted runtime fields align "
            f"(fields={mismatch_fields})"
        )

    if any(
        bool(item.get("strict_incubation_ready"))
        and str(item.get("quality_report_submission_lane") or "").strip().lower() == "observe_incubation"
        and item.get("quality_report_formal_track_requested") is False
        for item in sampled_strategies
    ):
        flags.append("strict_ready_but_formal_not_requested")
        notes.append(
            "at least one sampled strategy reached `strict_incubation_ready=true`, but the persisted submission review still "
            "shows `formal_track_requested=false` and final lane `observe_incubation`; this points to observe-first pre-routing "
            "winning before formal admission is even requested"
        )
        first_match = next(
            (
                item
                for item in sampled_strategies
                if bool(item.get("strict_incubation_ready"))
                and str(item.get("quality_report_submission_lane") or "").strip().lower() == "observe_incubation"
                and item.get("quality_report_formal_track_requested") is False
            ),
            None,
        )
        if first_match:
            notes.append(
                "strict-ready observe sample: "
                f"{str(first_match.get('strategy_id') or '-')} "
                f"(trigger={str(first_match.get('quality_report_submission_action_trigger') or '-')}, "
                f"runtime_reason={str(first_match.get('quality_report_runtime_bootstrap_reason') or '-')})"
            )

    if any(
        bool(item.get("strict_incubation_ready"))
        and bool(item.get("persisted_observe_first_intake"))
        and str(item.get("quality_report_submission_lane") or "").strip().lower() == "observe_incubation"
        and item.get("quality_report_formal_track_requested") is False
        for item in sampled_strategies
    ):
        flags.append("strict_ready_observe_first_override")
        notes.append(
            "the strict-ready observe sample still carries `observe_first_intake=true`, which strengthens the case that "
            "observe-first pre-routing is overriding formal-track request before final admission"
        )

    if any(_safe_float(item.get("signal_coverage_ratio")) == 0.0 for item in sampled_strategies):
        flags.append("no_forward_signal_coverage_yet")
        notes.append("sampled submitted strategies still have zero forward-observation coverage")

    bootstrap_pending_audit = [
        item
        for item in sampled_strategies
        if str(item.get("execution_audit_gate_status") or "").strip().lower() == "bootstrap_pending"
    ]
    audit_needs_attention = [
        item
        for item in sampled_strategies
        if str(item.get("audit_status") or "").strip().lower() == "needs_attention"
        and str(item.get("execution_audit_gate_status") or "").strip().lower()
        not in {"bootstrap_pending", "passed"}
    ]
    audit_missing_evidence = [
        item
        for item in sampled_strategies
        if str(item.get("audit_status") or "").strip().lower() == "needs_attention"
        and str(item.get("execution_audit_gate_status") or "").strip().lower() == "bootstrap_pending"
        and _safe_int(item.get("audit_signal_evidence_count")) <= 0
    ]
    if bootstrap_pending_audit:
        flags.append("execution_audit_bootstrap_pending")
        notes.append(
            "execution audit has native evidence but is still bootstrap_pending because realized trade samples are insufficient"
        )
    if audit_needs_attention or audit_missing_evidence:
        flags.append("execution_audit_needs_attention")
        notes.append("execution audit verification still reports missing or needs_attention on sampled strategies")

    return list(dict.fromkeys(flags)), notes
