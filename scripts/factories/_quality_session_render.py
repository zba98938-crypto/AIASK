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
from _quality_session_report import (
    _build_blocker_summary,
    _compact_run_detail,
    _extract_issue_flags,
    _format_top_blockers,
    _merge_strategy_samples,
    _normalize_blocker_reason,
    _quality_strategy_pool,
    _resolve_candidate_artifact,
    _select_representative_samples,
    _sort_strategy_samples,
)


def _entry_mode_id(entry: dict[str, Any]) -> str:
    mode_config = dict(entry.get("mode_config") or {})
    return str(
        entry.get("quality_mode")
        or entry.get("mode")
        or mode_config.get("mode_id")
        or "default"
    ).strip() or "default"


def _entry_mode_label(entry: dict[str, Any]) -> str:
    mode_config = dict(entry.get("mode_config") or {})
    mode_id = _entry_mode_id(entry)
    return str(
        entry.get("quality_mode_label")
        or mode_config.get("label")
        or mode_id.replace("_", "-")
    ).strip() or mode_id


def _session_mode_rows(session: dict[str, Any], entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in list(session.get("quality_modes") or []):
        payload = dict(item or {})
        mode_id = str(payload.get("mode_id") or "").strip()
        if mode_id:
            rows.append(payload)
    seen = {str(item.get("mode_id") or "").strip() for item in rows}
    for entry in entries:
        mode_id = _entry_mode_id(entry)
        if mode_id in seen:
            continue
        mode_config = dict(entry.get("mode_config") or {})
        rows.append(
            {
                "mode_id": mode_id,
                "label": _entry_mode_label(entry),
                "execution_mode": mode_config.get("execution_mode")
                or dict(entry.get("quality_snapshot") or {}).get("execution_mode"),
                "observe_first_enabled": mode_config.get("observe_first_enabled"),
                "wide_intake_observe_enabled": mode_config.get("wide_intake_observe_enabled"),
            }
        )
        seen.add(mode_id)
    return rows


def _sample_strategy_table(strategies: list[dict[str, Any]]) -> list[str]:
    if not strategies:
        return ["无关联策略快照。"]
    lines = [
        "| strategy_id | family | grade | score | review | signal_coverage | audit | status |",
        "| --- | --- | --- | ---: | --- | ---: | --- | --- |",
    ]
    for item in strategies:
        audit_gate = str(item.get("execution_audit_gate_status") or "").strip()
        audit_status = str(item.get("audit_status") or "").strip()
        audit_display = audit_gate or audit_status or "-"
        if audit_gate and audit_status and audit_status not in {"ok", "passed"} and audit_status != audit_gate:
            audit_display = f"{audit_gate}/{audit_status}"
        lines.append(
            "| {strategy_id} | {family} | {grade} | {score} | {review} | {coverage} | {audit} | {status} |".format(
                strategy_id=str(item.get("strategy_id") or "-"),
                family=str(item.get("strategy_type") or "-"),
                grade=str(item.get("validation_grade") or "-"),
                score=(
                    "-"
                    if item.get("validation_total_score") is None
                    else f"{_safe_float(item.get('validation_total_score')):.2f}"
                ),
                review=(
                    "-"
                    if item.get("review_passed") is None
                    else ("pass" if bool(item.get("review_passed")) else "fail")
                ),
                coverage=(
                    "-"
                    if item.get("signal_coverage_ratio") is None
                    else f"{_safe_float(item.get('signal_coverage_ratio')):.2f}"
                ),
                audit=audit_display,
                status=str(item.get("status_after_review") or "-"),
            )
        )
    return lines


def _render_representative_samples(strategies: list[dict[str, Any]]) -> list[str]:
    if not strategies:
        return []
    lines = ["", "代表样本诊断"]
    for item in strategies:
        blockers = " | ".join(list(item.get("admission_block_reasons") or [])[:4]) or "-"
        lines.append(
            "- `{strategy_id}` grade={grade} score={score:.2f} lane={lane} strict_ready={strict_ready} "
            "forward_coverage={coverage} audit={audit} exec_gate={exec_gate} post_cost_sharpe={post_cost_sharpe} "
            "oos_cagr={oos_cagr} evidence_gate={evidence_gate}".format(
                strategy_id=str(item.get("strategy_id") or "-"),
                grade=str(item.get("validation_grade") or "-"),
                score=_safe_float(item.get("validation_total_score")),
                lane=str(item.get("submission_lane") or "-"),
                strict_ready=str(bool(item.get("strict_incubation_ready"))).lower(),
                coverage=(
                    "-"
                    if item.get("signal_coverage_ratio") is None
                    else f"{_safe_float(item.get('signal_coverage_ratio')):.2f}"
                ),
                audit=str(item.get("audit_status") or "-"),
                exec_gate=str(item.get("execution_audit_gate_status") or "-"),
                post_cost_sharpe=(
                    "-"
                    if item.get("cost_post_cost_sharpe") is None
                    else f"{_safe_float(item.get('cost_post_cost_sharpe')):.3f}"
                ),
                oos_cagr=(
                    "-"
                    if item.get("benchmark_oos_cagr") is None
                    else _pct(item.get("benchmark_oos_cagr"))
                ),
                evidence_gate=str(item.get("evidence_gate_status") or "-"),
            )
        )
        lines.append(f"- 核心阻塞: {blockers}")
        if bool(item.get("persisted_params_truncated")) or str(item.get("quality_report_submission_lane") or "").strip():
            lines.append(
                "- 持久化痕迹: params_storage={storage_mode} dropped_budget={dropped_budget} "
                "persisted_lane={persisted_lane} quality_lane={quality_lane} planned_lane={planned_lane} "
                "budget_track={budget_track} formal_requested={formal_requested} strict_ready={strict_ready}".format(
                    storage_mode=str(item.get("persisted_params_storage_mode") or "-"),
                    dropped_budget=str(bool(item.get("persisted_params_dropped_incubation_budget"))).lower(),
                    persisted_lane=str(item.get("persisted_submission_lane") or "-"),
                    quality_lane=str(item.get("quality_report_submission_lane") or "-"),
                    planned_lane=str(item.get("quality_report_planned_submission_lane") or "-"),
                    budget_track=str(item.get("quality_report_incubation_budget_track") or "-"),
                    formal_requested=str(item.get("quality_report_formal_track_requested")).lower(),
                    strict_ready=str(bool(item.get("strict_incubation_ready"))).lower(),
                )
            )
        if item.get("quality_runtime_context_consistent") is False:
            lines.append(
                "- runtime 一致性: gate_vs_summary_mismatch="
                + ", ".join(str(name) for name in list(item.get("quality_runtime_context_mismatch_fields") or []))
            )
            lines.append(
                "- runtime 对照: "
                "gate_family={gate_family} summary_family={summary_family} "
                "gate_proxy={gate_proxy} summary_proxy={summary_proxy} "
                "gate_diag={gate_diag} summary_diag={summary_diag} "
                "gate_tier={gate_tier} summary_tier={summary_tier}".format(
                    gate_family=str(item.get("quality_gate_runtime_family_data_source") or "-"),
                    summary_family=str(item.get("quality_report_runtime_family_data_source") or "-"),
                    gate_proxy=str(item.get("quality_gate_proxy_runtime_used")),
                    summary_proxy=str(item.get("quality_report_proxy_runtime_used")),
                    gate_diag=str(item.get("quality_gate_diagnostic_only")),
                    summary_diag=str(item.get("quality_report_diagnostic_only")),
                    gate_tier=str(item.get("quality_gate_execution_readiness_tier") or "-"),
                    summary_tier=str(item.get("quality_report_execution_readiness_tier") or "-"),
                )
            )
    lines.append("")
    return lines


def _strict_ready_example_payload(
    entry: dict[str, Any],
    detail: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    return {
        "round": _safe_int(entry.get("round")),
        "run_id": str(detail.get("run_id") or entry.get("run_id") or ""),
        "strategy_id": str(item.get("strategy_id") or ""),
        "validation_grade": item.get("validation_grade"),
        "validation_total_score": item.get("validation_total_score"),
        "submission_lane": item.get("submission_lane"),
        "quality_report_submission_lane": item.get("quality_report_submission_lane"),
        "quality_report_planned_submission_lane": item.get("quality_report_planned_submission_lane"),
        "quality_report_incubation_budget_track": item.get("quality_report_incubation_budget_track"),
        "quality_report_formal_track_requested": item.get("quality_report_formal_track_requested"),
        "quality_report_formal_track_auto_corrected": item.get("quality_report_formal_track_auto_corrected"),
        "quality_report_formal_track_eligible": item.get("quality_report_formal_track_eligible"),
        "quality_report_formal_auto_correction_source_track": item.get(
            "quality_report_formal_auto_correction_source_track"
        ),
        "quality_report_submission_action_trigger": item.get("quality_report_submission_action_trigger"),
        "quality_report_runtime_bootstrap_reason": item.get("quality_report_runtime_bootstrap_reason"),
        "persisted_observe_first_intake": item.get("persisted_observe_first_intake"),
        "persisted_submission_lane": item.get("persisted_submission_lane"),
        "persisted_params_storage_mode": item.get("persisted_params_storage_mode"),
        "persisted_params_dropped_incubation_budget": item.get("persisted_params_dropped_incubation_budget"),
        "strict_incubation_ready": item.get("strict_incubation_ready"),
        "admission_block_reasons": list(item.get("admission_block_reasons") or []),
    }


def _bool_text(value: Any) -> str:
    if value is None:
        return "unknown"
    return str(bool(value)).lower()


def _format_strict_ready_example_evidence(example: dict[str, Any]) -> str:
    if not example:
        return ""
    parts: list[str] = []
    round_no = _safe_int(example.get("round"))
    if round_no > 0:
        parts.append(f"example_round={round_no}")
    strategy_id = str(example.get("strategy_id") or "").strip()
    if strategy_id:
        parts.append(f"example_strategy_id={strategy_id}")
    grade = str(example.get("validation_grade") or "").strip()
    if grade:
        parts.append(f"example_grade={grade}")
    score = example.get("validation_total_score")
    if score is not None:
        parts.append(f"example_score={_safe_float(score):.2f}")
    lane = str(
        example.get("quality_report_submission_lane")
        or example.get("submission_lane")
        or ""
    ).strip()
    if lane:
        parts.append(f"example_lane={lane}")
    parts.append(
        "example_formal_requested="
        f"{_bool_text(example.get('quality_report_formal_track_requested'))}"
    )
    parts.append(
        "example_observe_first="
        f"{_bool_text(example.get('persisted_observe_first_intake'))}"
    )
    trigger = str(example.get("quality_report_submission_action_trigger") or "").strip()
    if trigger:
        parts.append(f"example_trigger={trigger}")
    runtime_reason = str(example.get("quality_report_runtime_bootstrap_reason") or "").strip()
    if runtime_reason:
        parts.append(f"example_runtime_reason={runtime_reason}")
    return ", ".join(parts)


def _render_strict_ready_example(example: dict[str, Any]) -> list[str]:
    if not example:
        return []
    blockers = " | ".join(list(example.get("admission_block_reasons") or [])[:4]) or "-"
    lane = str(
        example.get("quality_report_submission_lane")
        or example.get("submission_lane")
        or "-"
    )
    lines = [
        f"- round={_safe_int(example.get('round'))} strategy=`{str(example.get('strategy_id') or '-')}` "
        f"grade={str(example.get('validation_grade') or '-')} "
        f"score={_safe_float(example.get('validation_total_score')):.2f} "
        f"lane={lane} strict_ready={_bool_text(example.get('strict_incubation_ready'))} "
        f"formal_requested={_bool_text(example.get('quality_report_formal_track_requested'))} "
        f"observe_first={_bool_text(example.get('persisted_observe_first_intake'))}"
    ]
    trace_parts: list[str] = []
    trigger = str(example.get("quality_report_submission_action_trigger") or "").strip()
    if trigger:
        trace_parts.append(f"trigger={trigger}")
    runtime_reason = str(example.get("quality_report_runtime_bootstrap_reason") or "").strip()
    if runtime_reason:
        trace_parts.append(f"runtime_reason={runtime_reason}")
    budget_track = str(example.get("quality_report_incubation_budget_track") or "").strip()
    if budget_track:
        trace_parts.append(f"budget_track={budget_track}")
    planned_lane = str(example.get("quality_report_planned_submission_lane") or "").strip()
    if planned_lane:
        trace_parts.append(f"planned_lane={planned_lane}")
    persisted_lane = str(example.get("persisted_submission_lane") or "").strip()
    if persisted_lane:
        trace_parts.append(f"persisted_lane={persisted_lane}")
    storage_mode = str(example.get("persisted_params_storage_mode") or "").strip()
    if storage_mode:
        trace_parts.append(f"params_storage={storage_mode}")
    if trace_parts:
        lines.append("- 运行轨迹: " + ", ".join(trace_parts))
    lines.append(f"- 核心阻塞: {blockers}")
    return lines


def _render_entry(entry: dict[str, Any], *, fallback_execution_mode: str | None = None) -> list[str]:
    factory_result = dict(entry.get("factory_result") or {})
    factory_data = dict(factory_result.get("data") or {})
    quality = dict(entry.get("quality_snapshot") or {})
    detail = dict(quality.get("detail") or {})
    summary = dict(detail.get("summary") or {})
    dedup = dict(detail.get("dedup_artifact") or {})
    candidate_artifact = dict(detail.get("candidate_artifact") or {})
    submission_artifact = dict(detail.get("submission_artifact") or {})
    incubation_budget_summary = dict(submission_artifact.get("incubation_budget_summary") or {})
    incubation_result = dict((entry.get("incubation_result") or {}).get("result") or {})
    incubation_intake = dict(incubation_result.get("intake") or {})
    paper_intake = dict(incubation_intake.get("paper_observation_intake") or {})
    incubation_verification = dict(incubation_result.get("verification") or {})
    incubation_pipeline = dict(incubation_result.get("pipeline") or {})
    incubation_acceptance = dict(incubation_result.get("execution_audit_acceptance") or {})
    incubation_report = dict(incubation_result.get("report") or {})
    paper_backlog = dict(incubation_result.get("paper_observation_backlog") or {})
    sampled_strategies = list(quality.get("sampled_strategies") or [])
    representative_samples = list(
        quality.get("representative_samples") or _select_representative_samples(sampled_strategies, limit=2)
    )
    blocker_summary = dict(quality.get("blocker_summary") or {})
    resolved_execution_mode = str(detail.get("execution_mode") or fallback_execution_mode or "-")

    lines = [
        f"### 第 {entry.get('round')} 轮",
        f"- 工厂开始: {_format_dt(entry.get('factory_started_at'))}",
        f"- 工厂结束: {_format_dt(entry.get('factory_completed_at'))}",
        f"- 工厂状态: `{str(factory_data.get('status') or factory_result.get('status') or 'unknown')}`",
        f"- run_id: `{detail.get('run_id') or factory_data.get('run_id') or '-'}`",
        f"- execution_mode: `{resolved_execution_mode}`",
        f"- quality_mode: `{_entry_mode_label(entry)}` (`{_entry_mode_id(entry)}`)",
        (
            "- 工厂核心漏斗: "
            f"spawned={_safe_int(detail.get('candidates_spawned'))}, "
            f"dedup_kept={_safe_int(dedup.get('kept_count'))}/{_safe_int(dedup.get('input_count'))}, "
            f"submitted={_safe_int(detail.get('submitted'))}, "
            f"G3={_safe_int(summary.get('gate_3_passed'))}/{_safe_int(summary.get('gate_3_input'))}"
        ),
        (
            "- 质量概览: "
            f"readiness={_safe_float(detail.get('readiness_score')):.2f}, "
            f"raw A/B/C/D={_pct(detail.get('raw_validation_a_rate'))}/"
            f"{_pct(detail.get('raw_validation_b_rate'))}/"
            f"{_pct(detail.get('raw_validation_c_rate'))}/"
            f"{_pct(detail.get('raw_validation_d_rate'))}"
        ),
        (
            "- 提交通道: "
            f"{json.dumps(summary.get('submission_lane_counts') or {}, ensure_ascii=False)}; "
            f"pipeline_fallback={json.dumps(summary.get('pipeline_fallback_counts') or {}, ensure_ascii=False)}"
        ),
        (
            "- Dedup: "
            f"existing={_safe_int(dedup.get('existing_count'))}, "
            f"kept={_safe_int(dedup.get('kept_count'))}, "
            f"dropped={_safe_int(dedup.get('dropped_count'))}, "
            f"duplicate_levels={json.dumps(dedup.get('duplicate_level_counts') or {}, ensure_ascii=False)}"
        ),
        (
            "- 候选来源: "
            f"families={json.dumps(candidate_artifact.get('family_counts') or {}, ensure_ascii=False)}, "
            f"origins={json.dumps(candidate_artifact.get('candidate_origin_counts') or {}, ensure_ascii=False)}"
        ),
        (
            "- 预算轨道摘要: "
            f"track_counts={json.dumps(incubation_budget_summary.get('track_counts') or {}, ensure_ascii=False)}, "
            f"formal_slots={_safe_int(incubation_budget_summary.get('formal_slots'))}, "
            f"observe_slots={_safe_int(incubation_budget_summary.get('observe_slots'))}, "
            f"dominant_families={json.dumps(incubation_budget_summary.get('dominant_families') or [], ensure_ascii=False)}"
        ),
    ]

    issue_flags = list(quality.get("issue_flags") or [])
    if issue_flags:
        lines.append("- 问题标记: " + ", ".join(f"`{item}`" for item in issue_flags))
    for note in list(quality.get("issue_notes") or []):
        lines.append(f"- 观察到的问题: {note}")
    top_blockers = list(blocker_summary.get("top_blockers") or [])
    if top_blockers:
        lines.append(
            "- formal 准入阻塞: "
            f"analyzed={_safe_int(blocker_summary.get('analyzed_strategy_count'))}, "
            f"strict_not_ready={_safe_int(blocker_summary.get('strict_not_ready_count'))}, "
            f"top={_format_top_blockers(top_blockers)}"
        )

    gate_fail_topn = list(summary.get("gate_3_failure_topn") or summary.get("gate_3_failure_reason_topn") or [])
    if gate_fail_topn:
        reason_text = "; ".join(
            f"{item.get('reason_code') or item.get('reason') or 'unknown'} x{_safe_int(item.get('count'))}"
            for item in gate_fail_topn[:5]
        )
        lines.append(f"- Gate 3 失败Top: {reason_text}")

    if incubation_result:
        lines.extend(
            [
                f"- 孵化状态: `{incubation_result.get('status', 'unknown')}`",
                (
                    "- 孵化摘要: "
                    f"accepted={_safe_int(dict(incubation_result.get('intake') or {}).get('accepted'))}, "
                    f"verified={_safe_int(dict(incubation_result.get('verification') or {}).get('verified'))}, "
                    f"paper_count={_safe_int(incubation_pipeline.get('paper_count'))}, "
                    f"stage_counts={json.dumps(incubation_pipeline.get('stage_counts') or {}, ensure_ascii=False)}"
                ),
                (
                    "- 孵化质量: "
                    f"overall_hit_rate={_pct(incubation_report.get('overall_hit_rate'))}, "
                    f"overall_skill_lcb={incubation_report.get('overall_skill_lcb', '-')}"
                ),
                (
                    "- execution audit acceptance: "
                    f"status={incubation_acceptance.get('status') or '-'}, "
                    f"evaluated={_safe_int(incubation_acceptance.get('evaluated'))}, "
                    f"saved_signal_evidence={_safe_int(incubation_acceptance.get('saved_signal_evidence_count'))}, "
                    f"hard_gate_passed={_safe_int(incubation_acceptance.get('hard_gate_passed_count'))}, "
                    f"gate_status_counts={json.dumps(incubation_acceptance.get('gate_status_counts') or {}, ensure_ascii=False)}"
                ),
            ]
        )

    if incubation_result:
        lines.extend(
            [
                (
                    "- observe intake evidence: "
                    f"paper_scanned={_safe_int(paper_intake.get('scanned'))}, "
                    f"paper_recognized={_safe_int(paper_intake.get('recognized'))}"
                ),
                (
                    "- observe active pool: "
                    f"verified={_safe_int(incubation_verification.get('verified'))}/"
                    f"{_safe_int(incubation_verification.get('total'))}, "
                    f"incubating={_safe_int(incubation_verification.get('incubating_count'))}, "
                    f"paper_active={_safe_int(incubation_verification.get('paper_count'))}, "
                    f"diagnostic={_safe_int(incubation_verification.get('diagnostic_count'))}, "
                    f"errors={_safe_int(incubation_verification.get('errors'))}"
                ),
                (
                    "- paper backlog evidence: "
                    f"status=`{paper_backlog.get('paper_observation_backlog_status') or '-'}`, "
                    f"active={_safe_int(paper_backlog.get('paper_observation_active_count'))}, "
                    f"stage_paper_only={_safe_int(paper_backlog.get('paper_observation_backlog_count'))}, "
                    f"last_recognized_at={_format_dt(paper_backlog.get('paper_observation_last_recognized_at'))}"
                ),
                (
                    "- promotion evidence: "
                    f"auto_promoted={_safe_int(incubation_pipeline.get('auto_promoted'))}, "
                    f"stage_counts={json.dumps(incubation_pipeline.get('stage_counts') or {}, ensure_ascii=False)}"
                ),
            ]
        )

    lines.append("")
    lines.append("关联策略抽样")
    lines.extend(_sample_strategy_table(sampled_strategies))
    lines.extend(_render_representative_samples(representative_samples))
    lines.append("")
    return lines


def _build_aggregate_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    issue_counter: Counter[str] = Counter()
    gate_reason_counter: Counter[str] = Counter()
    blocker_reason_counter: Counter[str] = Counter()
    lane_counter: Counter[str] = Counter()
    execution_mode_counter: Counter[str] = Counter()
    quality_mode_counter: Counter[str] = Counter()
    submitted_total = 0
    gate_input_total = 0
    gate_pass_total = 0
    spawned_total = 0
    observe_only_rounds = 0
    gate_pass_but_observe_only_rounds = 0
    paper_intake_rounds = 0
    paper_recognized_total = 0
    last_gate_pass_but_observe_only_example: dict[str, Any] = {}
    last_strict_ready_observe_example: dict[str, Any] = {}
    last_strict_ready_formal_missing_example: dict[str, Any] = {}
    last_strict_ready_observe_override_example: dict[str, Any] = {}

    for entry in entries:
        quality = dict(entry.get("quality_snapshot") or {})
        detail = dict(quality.get("detail") or {})
        summary = dict(detail.get("summary") or {})
        blocker_summary = dict(quality.get("blocker_summary") or {})
        incubation_result = dict((entry.get("incubation_result") or {}).get("result") or {})
        paper_intake = dict(dict(incubation_result.get("intake") or {}).get("paper_observation_intake") or {})
        quality_mode_counter[_entry_mode_id(entry)] += 1
        execution_mode = str(detail.get("execution_mode") or dict(entry.get("mode_config") or {}).get("execution_mode") or "").strip()
        if execution_mode:
            execution_mode_counter[execution_mode] += 1
        issue_counter.update(list(quality.get("issue_flags") or []))
        spawned_total += _safe_int(detail.get("candidates_spawned"))
        submitted_total += _safe_int(detail.get("submitted"))
        gate_input_total += _safe_int(summary.get("gate_3_input"))
        gate_pass_total += _safe_int(summary.get("gate_3_passed"))
        lane_counts = dict(summary.get("submission_lane_counts") or {})
        lane_counter.update({str(key): _safe_int(value) for key, value in lane_counts.items()})
        if _safe_int(detail.get("submitted")) > 0 and _safe_int(lane_counts.get("observe_incubation")) >= _safe_int(detail.get("submitted")):
            observe_only_rounds += 1
            if _safe_int(summary.get("gate_3_passed")) > 0:
                gate_pass_but_observe_only_rounds += 1
                submission_artifact = dict(detail.get("submission_artifact") or {})
                budget_summary = dict(submission_artifact.get("incubation_budget_summary") or {})
                last_gate_pass_but_observe_only_example = {
                    "round": _safe_int(entry.get("round")),
                    "run_id": str(detail.get("run_id") or entry.get("run_id") or ""),
                    "execution_mode": str(detail.get("execution_mode") or ""),
                    "gate_3_passed": _safe_int(summary.get("gate_3_passed")),
                    "submitted": _safe_int(detail.get("submitted") or summary.get("submitted")),
                    "submission_lane_counts": dict(lane_counts),
                    "budget_track_counts": dict(budget_summary.get("track_counts") or {}),
                    "strategy_status_counts": dict(submission_artifact.get("strategy_status_counts") or {}),
                }
        paper_recognized = _safe_int(paper_intake.get("recognized"))
        paper_recognized_total += paper_recognized
        if paper_recognized > 0:
            paper_intake_rounds += 1
        for item in _quality_strategy_pool(quality):
            lane = str(
                item.get("quality_report_submission_lane")
                or item.get("submission_lane")
                or ""
            ).strip().lower()
            if bool(item.get("strict_incubation_ready")) and lane == "observe_incubation":
                example = _strict_ready_example_payload(entry, detail, item)
                last_strict_ready_observe_example = example
                if item.get("quality_report_formal_track_requested") is False:
                    last_strict_ready_formal_missing_example = example
                if (
                    item.get("quality_report_formal_track_requested") is False
                    and bool(item.get("persisted_observe_first_intake"))
                ):
                    last_strict_ready_observe_override_example = example
        for item in list(summary.get("gate_3_failure_topn") or summary.get("gate_3_failure_reason_topn") or []):
            key = str(item.get("reason_code") or item.get("reason") or "").strip()
            if key:
                gate_reason_counter[key] += _safe_int(item.get("count"), 1)
        for item in list(blocker_summary.get("top_blockers") or []):
            key = str(item.get("reason") or "").strip()
            if key:
                blocker_reason_counter[key] += _safe_int(item.get("count"), 1)

    return {
        "issue_counts": issue_counter,
        "gate_reason_counts": gate_reason_counter,
        "blocker_reason_counts": blocker_reason_counter,
        "submission_lane_counts": lane_counter,
        "execution_mode_counts": execution_mode_counter,
        "quality_mode_counts": quality_mode_counter,
        "spawned_total": spawned_total,
        "submitted_total": submitted_total,
        "gate_input_total": gate_input_total,
        "gate_pass_total": gate_pass_total,
        "observe_only_rounds": observe_only_rounds,
        "gate_pass_but_observe_only_rounds": gate_pass_but_observe_only_rounds,
        "paper_intake_rounds": paper_intake_rounds,
        "paper_recognized_total": paper_recognized_total,
        "last_gate_pass_but_observe_only_example": last_gate_pass_but_observe_only_example,
        "last_strict_ready_observe_example": last_strict_ready_observe_example,
        "last_strict_ready_formal_missing_example": last_strict_ready_formal_missing_example,
        "last_strict_ready_observe_override_example": last_strict_ready_observe_override_example,
    }


def _json_inline(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    token = str(value or "").strip().lower()
    if token in {"1", "true", "yes", "on", "enabled"}:
        return True
    if token in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(value)


def _top_counter_text(counter: Counter[str], *, limit: int = 3) -> str:
    if not counter:
        return "-"
    return ", ".join(f"{name} x{count}" for name, count in counter.most_common(limit))


def _mode_round_cell(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "-"
    quality = dict(entry.get("quality_snapshot") or {})
    detail = dict(quality.get("detail") or {})
    summary = dict(detail.get("summary") or {})
    factory_result = dict(entry.get("factory_result") or {})
    factory_data = dict(factory_result.get("data") or {})
    status = str(factory_data.get("status") or detail.get("status") or factory_result.get("status") or "-")
    run_id = str(detail.get("run_id") or factory_data.get("run_id") or "-")
    submitted = _safe_int(detail.get("submitted") or summary.get("submitted"))
    gate_passed = _safe_int(summary.get("gate_3_passed"))
    gate_input = _safe_int(summary.get("gate_3_input"))
    lanes = _json_inline(summary.get("submission_lane_counts") or {})
    return f"`{run_id}`<br>{status}<br>submitted={submitted}; G3={gate_passed}/{gate_input}<br>{lanes}"


def _render_mode_comparison(entries: list[dict[str, Any]], session: dict[str, Any]) -> list[str]:
    mode_rows = _session_mode_rows(session, entries)
    if not mode_rows:
        return []

    lines = [
        "",
        "## Mode comparison",
        "",
        "| mode | rounds | execution_mode | observe_first | wide_intake | spawned | submitted | Gate 3 | formal_lane | observe_lane | observe_only_rounds | top_flags |",
        "| --- | ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    entries_by_mode = {
        str(row.get("mode_id") or "").strip(): [
            entry for entry in entries if _entry_mode_id(entry) == str(row.get("mode_id") or "").strip()
        ]
        for row in mode_rows
    }
    for row in mode_rows:
        mode_id = str(row.get("mode_id") or "").strip()
        mode_entries = entries_by_mode.get(mode_id, [])
        aggregate = _build_aggregate_summary(mode_entries)
        lane_counts: Counter[str] = aggregate["submission_lane_counts"]
        execution_modes: Counter[str] = aggregate["execution_mode_counts"]
        issue_counts: Counter[str] = aggregate["issue_counts"]
        execution_mode = (
            str(row.get("execution_mode") or "").strip()
            or _top_counter_text(execution_modes, limit=2)
        )
        lines.append(
            "| {label} | {rounds} | `{execution_mode}` | {observe_first} | {wide_intake} | {spawned} | {submitted} | {gate_pass}/{gate_input} | {formal} | {observe} | {observe_only} | {flags} |".format(
                label=f"`{str(row.get('label') or mode_id)}`",
                rounds=len(mode_entries),
                execution_mode=execution_mode or "-",
                observe_first=str(_boolish(row.get("observe_first_enabled"))).lower(),
                wide_intake=str(_boolish(row.get("wide_intake_observe_enabled"))).lower(),
                spawned=_safe_int(aggregate.get("spawned_total")),
                submitted=_safe_int(aggregate.get("submitted_total")),
                gate_pass=_safe_int(aggregate.get("gate_pass_total")),
                gate_input=_safe_int(aggregate.get("gate_input_total")),
                formal=_safe_int(lane_counts.get("formal_incubation")),
                observe=_safe_int(lane_counts.get("observe_incubation")),
                observe_only=_safe_int(aggregate.get("observe_only_rounds")),
                flags=_top_counter_text(issue_counts),
            )
        )

    if len(mode_rows) > 1 and entries:
        lines.extend(
            [
                "",
                "### Round matrix",
                "",
                "| round | " + " | ".join(str(row.get("label") or row.get("mode_id") or "-") for row in mode_rows) + " |",
                "| ---: | " + " | ".join("---" for _ in mode_rows) + " |",
            ]
        )
        round_numbers = sorted({_safe_int(entry.get("round")) for entry in entries if _safe_int(entry.get("round")) > 0})
        for round_no in round_numbers:
            row_entries = [
                next(
                    (
                        entry
                        for entry in entries
                        if _safe_int(entry.get("round")) == round_no
                        and _entry_mode_id(entry) == str(row.get("mode_id") or "").strip()
                    ),
                    None,
                )
                for row in mode_rows
            ]
            lines.append(
                f"| {round_no} | " + " | ".join(_mode_round_cell(entry) for entry in row_entries) + " |"
            )
    return lines


def _build_priority_findings(
    entries: list[dict[str, Any]],
    aggregate: dict[str, Any],
    session: dict[str, Any],
    last_detail: dict[str, Any],
    last_incubation_result: dict[str, Any],
    last_paper_backlog: dict[str, Any],
) -> list[dict[str, str]]:
    issue_counts: Counter[str] = aggregate["issue_counts"]
    blocker_reason_counts: Counter[str] = aggregate["blocker_reason_counts"]
    last_summary = dict(last_detail.get("summary") or {})
    last_verification = dict(last_incubation_result.get("verification") or {})
    last_pipeline = dict(last_incubation_result.get("pipeline") or {})

    findings: list[dict[str, str]] = []
    submitted_total = _safe_int(aggregate.get("submitted_total"))
    gate_input_total = _safe_int(aggregate.get("gate_input_total"))
    gate_pass_total = _safe_int(aggregate.get("gate_pass_total"))
    paper_intake_rounds = _safe_int(aggregate.get("paper_intake_rounds"))
    paper_recognized_total = _safe_int(aggregate.get("paper_recognized_total"))
    latest_submitted = _safe_int(last_detail.get("submitted") or last_summary.get("submitted"))
    latest_raw_b_or_above = _safe_int(last_detail.get("raw_b_or_above_count"))
    latest_strict_ready = _safe_int(last_detail.get("strict_incubation_ready_count"))
    latest_paper_active = _safe_int(last_verification.get("paper_count"))
    latest_incubating = _safe_int(last_verification.get("incubating_count"))
    latest_active_pool = _safe_int(last_paper_backlog.get("paper_observation_active_count"))
    latest_auto_promoted = _safe_int(last_pipeline.get("auto_promoted"))
    gate_pass_but_observe_only_rounds = _safe_int(aggregate.get("gate_pass_but_observe_only_rounds"))
    gate_pass_observe_example = dict(aggregate.get("last_gate_pass_but_observe_only_example") or {})
    strict_ready_observe_example = dict(aggregate.get("last_strict_ready_observe_example") or {})
    strict_ready_formal_missing_example = dict(
        aggregate.get("last_strict_ready_formal_missing_example") or {}
    )
    strict_ready_observe_override_example = dict(
        aggregate.get("last_strict_ready_observe_override_example") or {}
    )

    if submitted_total > 0 and paper_intake_rounds > 0:
        findings.append(
            {
                "priority": "P0",
                "status": "已解决",
                "title": "旧 G3 全拦 / record-only 卡死",
                "summary": "当前实跑已经证明策略能进入 observe 提交，并被 Incubation Factory 识别消费。",
                "evidence": (
                    f"累计 submitted={submitted_total}, Gate3={gate_pass_total}/{gate_input_total}, "
                    f"observe intake 识别轮数={paper_intake_rounds}, recognized 合计={paper_recognized_total}"
                ),
            }
        )
    else:
        findings.append(
            {
                "priority": "P0",
                "status": "未解决",
                "title": "旧 G3 全拦 / record-only 卡死",
                "summary": "当前记录里还缺少足够的提交和 observe intake 证据，不能证明旧式全拦已经解除。",
                "evidence": (
                    f"累计 submitted={submitted_total}, Gate3={gate_pass_total}/{gate_input_total}, "
                    f"observe intake 识别轮数={paper_intake_rounds}"
                ),
            }
        )

    if (
        issue_counts.get("strict_ready_zero_despite_raw_b", 0) > 0
        or issue_counts.get("no_forward_signal_coverage_yet", 0) > 0
        or issue_counts.get("execution_audit_needs_attention", 0) > 0
        or issue_counts.get("execution_audit_bootstrap_pending", 0) > 0
    ):
        findings.append(
            {
                "priority": "P0",
                "status": "未解决",
                "title": "高质量策略产出仍未打通",
                "summary": "虽然已不再全拦，但高质量策略还没有形成 formal readiness、前向覆盖和执行审计正反馈。",
                "evidence": (
                    f"strict_ready_zero={issue_counts.get('strict_ready_zero_despite_raw_b', 0)} 轮, "
                    f"zero_forward_coverage={issue_counts.get('no_forward_signal_coverage_yet', 0)} 轮, "
                    f"audit_needs_attention={issue_counts.get('execution_audit_needs_attention', 0)} 轮, "
                    f"audit_bootstrap_pending={issue_counts.get('execution_audit_bootstrap_pending', 0)} 轮; "
                    f"最新轮 raw_b_or_above={latest_raw_b_or_above}, strict_ready={latest_strict_ready}, submitted={latest_submitted}"
                ),
            }
        )

    if (
        issue_counts.get("factory_runtime_degraded", 0) > 0
        or issue_counts.get("llm_timeout_cooldown_active", 0) > 0
    ):
        findings.append(
            {
                "priority": "P1",
                "status": "未解决",
                "title": "运行时退化仍在影响候选生成质量",
                "summary": "当前不只是候选质量本身偏弱，LLM 超时冷却和 partial_llm 退化也在把生成链路推回本地 fallback，压低可执行规格和策略上限。",
                "evidence": (
                    f"factory_runtime_degraded={issue_counts.get('factory_runtime_degraded', 0)} 轮, "
                    f"llm_timeout_cooldown_active={issue_counts.get('llm_timeout_cooldown_active', 0)} 轮, "
                    f"latest_factory_status={str(last_detail.get('status') or 'unknown')}"
                ),
            }
        )

    if gate_pass_but_observe_only_rounds > 0:
        findings.append(
            {
                "priority": "P0",
                "status": "未解决",
                "title": "G3 通过样本仍未进入 formal 通道",
                "summary": "当前执行模式下，G3 通过并不等于 formal_incubation；实跑已经出现“有 G3 通过样本，但整轮仍全部落在 observe”的现象。",
                "evidence": (
                    f"gate_pass_but_observe_only_rounds={gate_pass_but_observe_only_rounds}, "
                    f"latest_gate3_passed={_safe_int(last_summary.get('gate_3_passed'))}, "
                    f"latest_submitted={latest_submitted}, "
                    f"latest_lane_counts={json.dumps(last_summary.get('submission_lane_counts') or {}, ensure_ascii=False)}"
                ),
            }
        )

    observe_mode = str(
        gate_pass_observe_example.get("execution_mode")
        or session.get("execution_mode")
        or last_detail.get("execution_mode")
        or ""
    ).strip().lower()
    if observe_mode == "stock_first_observe_primary" and gate_pass_but_observe_only_rounds > 0:
        example_round = _safe_int(gate_pass_observe_example.get("round"))
        example_gate3_passed = _safe_int(gate_pass_observe_example.get("gate_3_passed"))
        example_submitted = _safe_int(gate_pass_observe_example.get("submitted"))
        example_lane_counts = dict(gate_pass_observe_example.get("submission_lane_counts") or {})
        example_budget_track_counts = dict(gate_pass_observe_example.get("budget_track_counts") or {})
        example_strategy_status_counts = dict(gate_pass_observe_example.get("strategy_status_counts") or {})
        evidence_parts = [
            f"execution_mode={observe_mode}",
            f"gate_pass_but_observe_only_rounds={gate_pass_but_observe_only_rounds}",
        ]
        if example_round > 0:
            evidence_parts.append(f"example_round={example_round}")
        if example_gate3_passed > 0 or example_submitted > 0:
            evidence_parts.append(f"example_gate3_passed={example_gate3_passed}")
            evidence_parts.append(f"example_submitted={example_submitted}")
        if example_lane_counts:
            evidence_parts.append(
                f"example_lane_counts={json.dumps(example_lane_counts, ensure_ascii=False)}"
            )
        if example_budget_track_counts:
            evidence_parts.append(
                f"example_budget_track_counts={json.dumps(example_budget_track_counts, ensure_ascii=False)}"
            )
        if example_strategy_status_counts:
            evidence_parts.append(
                f"example_strategy_status_counts={json.dumps(example_strategy_status_counts, ensure_ascii=False)}"
            )
        findings.append(
            {
                "priority": "P0",
                "status": "未解决",
                "title": "stock_first_observe_primary 模式疑似在提交前预路由到 observe 轨道",
                "summary": (
                    "当前模式级证据表明，候选在提交前就被 observe-first 路径优先送往 observe 轨道，"
                    "导致 G3 通过与 formal_incubation 进一步脱钩。"
                ),
                "evidence": ", ".join(evidence_parts),
            }
        )

    if issue_counts.get("strict_ready_but_formal_not_requested", 0) > 0:
        evidence_parts = [
            f"strict_ready_but_formal_not_requested={issue_counts.get('strict_ready_but_formal_not_requested', 0)} 轮",
        ]
        example_evidence = _format_strict_ready_example_evidence(strict_ready_formal_missing_example)
        if example_evidence:
            evidence_parts.append(example_evidence)
        findings.append(
            {
                "priority": "P0",
                "status": "未解决",
                "title": "strict-ready 样本仍未发起 formal 申请",
                "summary": (
                    "这已经不是单纯“formal 被质量门拦住”，而是有样本达到 strict incubation readiness 后，"
                    "持久化审查结果仍显示 `formal_track_requested=false`，说明 observe-first 预路由仍在压制 formal 申请。"
                ),
                "evidence": ", ".join(evidence_parts),
            }
        )

    if issue_counts.get("strict_ready_observe_first_override", 0) > 0:
        evidence_parts = [
            f"strict_ready_observe_first_override={issue_counts.get('strict_ready_observe_first_override', 0)} 轮",
        ]
        example_evidence = _format_strict_ready_example_evidence(strict_ready_observe_override_example)
        if example_evidence:
            evidence_parts.append(example_evidence)
        findings.append(
            {
                "priority": "P0",
                "status": "未解决",
                "title": "strict-ready 样本仍被 observe-first 标记压回 observe",
                "summary": (
                    "这说明问题已经不只是 formal 质量门或 runtime bootstrap 本身，"
                    "而是 strict-ready 样本在进入最终准入前仍带着 `observe_first_intake` 标记，"
                    "导致 formal 申请轨道被 observe-first 预路由覆盖。"
                ),
                "evidence": ", ".join(evidence_parts),
            }
        )

    if (
        issue_counts.get("strategy_params_budget_metadata_compacted_away", 0) > 0
        or issue_counts.get("quality_report_plan_metadata_missing", 0) > 0
        or issue_counts.get("strategy_row_submission_metadata_missing", 0) > 0
    ):
        findings.append(
            {
                "priority": "P1",
                "status": "未解决",
                "title": "预算/提交通道元数据的持久化可观测性不足",
                "summary": (
                    "planner 的 formal/observe 计划没有稳定保留在策略行与质量报告摘要里，"
                    "导致 plan-vs-final 路由链路难以直接从持久化结果回放。"
                ),
                "evidence": (
                    f"params_budget_metadata_compacted_away={issue_counts.get('strategy_params_budget_metadata_compacted_away', 0)} 轮, "
                    f"strategy_row_submission_metadata_missing={issue_counts.get('strategy_row_submission_metadata_missing', 0)} 轮, "
                    f"quality_report_plan_metadata_missing={issue_counts.get('quality_report_plan_metadata_missing', 0)} 轮"
                ),
            }
        )

    if issue_counts.get("quality_report_runtime_context_mismatch", 0) > 0:
        findings.append(
            {
                "priority": "P1",
                "status": "未解决",
                "title": "持久化质量报告仍存在 runtime 上下文不一致样本",
                "summary": (
                    "这说明部分样本的 `quality_gate` 与最终 `summary` 仍在使用不同 runtime 语义，"
                    "相关 formal blocker 和路由归因不能被直接当作可信证据。"
                ),
                "evidence": (
                    f"quality_report_runtime_context_mismatch={issue_counts.get('quality_report_runtime_context_mismatch', 0)} 轮"
                ),
            }
        )

    if latest_paper_active > 0 or latest_active_pool > 0:
        findings.append(
            {
                "priority": "P1",
                "status": "未解决",
                "title": "observe 池在消费，但没有转成 formal / promotion",
                "summary": "当前问题更像观察池持续堆积和 warmup 停留，而不是完全无人消费。",
                "evidence": (
                    f"最新轮 incubating={latest_incubating}, paper_active={latest_paper_active}, "
                    f"active_pool={latest_active_pool}, auto_promoted={latest_auto_promoted}"
                ),
            }
        )

    if issue_counts.get("pipeline_stage_fallback", 0) > 0 or issue_counts.get("pipeline_no_executable_specs", 0) > 0:
        findings.append(
            {
                "priority": "P1",
                "status": "未解决",
                "title": "生成管线仍有空规格 / fallback 产能损耗",
                "summary": "staged pipeline 仍会退回本地规则生成，限制可执行规格产出和候选质量上限。",
                "evidence": (
                    f"pipeline_stage_fallback={issue_counts.get('pipeline_stage_fallback', 0)} 次, "
                    f"pipeline_no_executable_specs={issue_counts.get('pipeline_no_executable_specs', 0)} 次"
                ),
            }
        )

    if blocker_reason_counts:
        findings.append(
            {
                "priority": "P1",
                "status": "未解决",
                "title": "formal 准入阻塞仍集中在交易质量指标",
                "summary": "当前主要不是流程断路，而是 post-cost sharpe、profit factor、win rate 等质量门没有被穿透。",
                "evidence": ", ".join(
                    f"{name} x{count}" for name, count in blocker_reason_counts.most_common(5)
                ),
            }
        )

    if entries and gate_input_total > 0 and gate_pass_total == 0:
        findings.append(
            {
                "priority": "P2",
                "status": "关注",
                "title": "当前阶段 Gate 3 通过仍偏低",
                "summary": "这不是旧式全拦，但说明当前轮次里真正能穿过 formal 质量门的候选仍然稀少。",
                "evidence": f"累计 Gate3={gate_pass_total}/{gate_input_total}",
            }
        )

    return findings


def _render_report(state: dict[str, Any]) -> str:
    entries = list(state.get("entries") or [])
    aggregate = _build_aggregate_summary(entries)
    issue_counts: Counter[str] = aggregate["issue_counts"]
    gate_reason_counts: Counter[str] = aggregate["gate_reason_counts"]
    blocker_reason_counts: Counter[str] = aggregate["blocker_reason_counts"]
    session = dict(state.get("session") or {})
    mode_rows = _session_mode_rows(session, entries)
    runtime_controls = dict(session.get("runtime_controls") or {})
    last_entry = entries[-1] if entries else {}
    last_quality = dict((last_entry or {}).get("quality_snapshot") or {})
    last_detail = dict(last_quality.get("detail") or {})
    last_incubation_result = dict(((last_entry or {}).get("incubation_result") or {}).get("result") or {})
    last_paper_backlog = dict(last_incubation_result.get("paper_observation_backlog") or {})
    priority_findings = _build_priority_findings(
        entries,
        aggregate,
        session,
        last_detail,
        last_incubation_result,
        last_paper_backlog,
    )

    lines = [
        f"# 策略工厂24小时运行与质量追踪",
        "",
        f"- session_id: `{session.get('session_id')}`",
        f"- started_at: `{session.get('started_at')}`",
        f"- updated_at: `{state.get('updated_at')}`",
        f"- duration_hours: `{session.get('hours')}`",
        f"- pause_sec_between_rounds: `{session.get('pause_sec')}`",
        f"- quality_modes: `{', '.join(str(row.get('label') or row.get('mode_id') or '-') for row in mode_rows) or session.get('quality_session_mode') or '-'}`",
        f"- execution_mode: `{session.get('execution_mode')}`",
        f"- runtime_controls: `{_json_inline(runtime_controls)}`",
        f"- **compensation_mode**: `{'🚨 ENABLED (补偿模式)' if session.get('compensation_enabled') else '✅ DISABLED (纯验证模式)'}`",  # P0 FIX
        f"- target_codes: `{', '.join(session.get('codes') or []) or 'default_universe'}`",
        f"- python: `{session.get('python_executable')}`",
        f"- sqlite: `{session.get('sqlite_path')}`",
        f"- report_path: `{session.get('report_path')}`",
        f"- data_source: `real Strategy Factory runtime + MCP-equivalent manager handlers`",
        "",
        "## 累计概览",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 记录轮数 | {len(entries)} |",
        f"| spawned 总数 | {aggregate['spawned_total']} |",
        f"| submitted 总数 | {aggregate['submitted_total']} |",
        f"| Gate 3 通过率 | {aggregate['gate_pass_total']}/{aggregate['gate_input_total']} |",
        f"| 全部 observe 提交轮数 | {aggregate['observe_only_rounds']} |",
        f"| observe 被 intake 识别轮数 | {aggregate['paper_intake_rounds']} |",
        f"| paper observation recognized 合计 | {aggregate['paper_recognized_total']} |",
        "",
    ]
    lines.extend(_render_mode_comparison(entries, session))
    lines.extend(["", "## 优先级判断", ""])

    if priority_findings:
        for item in priority_findings:
            lines.append(
                f"- `{item.get('priority')} {item.get('status')}` {item.get('title')}："
                f"{item.get('summary')} 证据：{item.get('evidence')}"
            )
    else:
        lines.append("- 暂无优先级结论。")

    lines.extend(["", "## 当前主要问题", ""])

    strict_ready_observe_example = dict(aggregate.get("last_strict_ready_observe_example") or {})
    if strict_ready_observe_example:
        lines.extend(["", "## 最近一次关键反例", ""])
        lines.extend(_render_strict_ready_example(strict_ready_observe_example))

    if issue_counts:
        for name, count in issue_counts.most_common(10):
            lines.append(f"- `{name}`: {count} 次")
    else:
        lines.append("- 暂无累计问题标记。")

    lines.extend(["", "## Gate 3 失败原因累计", ""])
    if gate_reason_counts:
        for name, count in gate_reason_counts.most_common(10):
            lines.append(f"- `{name}`: {count}")
    else:
        lines.append("- 暂无 Gate 3 失败原因。")

    lines.extend(["", "## Formal 准入阻塞累计", ""])
    if blocker_reason_counts:
        for name, count in blocker_reason_counts.most_common(10):
            lines.append(f"- `{name}`: {count}")
    else:
        lines.append("- 暂无 formal 准入阻塞统计。")

    lines.extend(["", "## 最新轮观察", ""])
    if last_entry:
        lines.extend(_render_entry(last_entry, fallback_execution_mode=str(session.get("execution_mode") or "")))
    else:
        lines.append("暂无运行记录。")

    lines.extend(["", "## 全部运行记录", ""])
    if entries:
        for entry in entries:
            lines.extend(_render_entry(entry, fallback_execution_mode=str(session.get("execution_mode") or "")))
    else:
        lines.append("暂无运行记录。")

    lines.extend(["", "## 当前判断", ""])
    if last_detail:
        if aggregate.get("paper_intake_rounds", 0) > 0:
            lines.append("- observe 通道已经被 incubation_factory 实际识别/消费，但目前还没有转化成正向前瞻信号覆盖或晋级证据。")
        if issue_counts.get("pipeline_no_executable_specs", 0) > 0:
            lines.append("- staged pipeline 仍然存在 `no_executable_specs` 型空规格回退，这是真实产能问题。")
        if issue_counts.get("dedup_zero_keep", 0) > 0:
            lines.append("- 去重存在批次性全清空现象，说明产出稳定性仍不足。")
        if issue_counts.get("observe_only_submission", 0) > 0:
            lines.append("- 当前多数提交仍落在 observe lane，说明正式孵化就绪率偏低。")
        if issue_counts.get("strict_ready_zero_despite_raw_b", 0) > 0:
            lines.append("- 出现了原始质量不差但 strict incubation readiness 仍为 0 的轮次，需要继续查 formal 准入约束。")
        if (
            issue_counts.get("strategy_params_budget_metadata_compacted_away", 0) > 0
            or issue_counts.get("quality_report_plan_metadata_missing", 0) > 0
        ):
            lines.append(
                "- 持久化可观测性存在缺口：`strategies.params` 会把 `incubation_budget` 作为大节点压缩掉，"
                "`strategy_quality_reports.summary` 也没有稳定保留 `planned_submission_lane` / `incubation_budget_track`。"
            )
        if blocker_reason_counts:
            lines.append(
                "- 当前 formal 准入阻塞集中在: "
                f"{', '.join(f'`{name}`' for name, _ in blocker_reason_counts.most_common(5))}。"
            )
        if _safe_int(last_paper_backlog.get("paper_observation_active_count")) > 0:
            lines.append(
                "- 当前 observe 池仍有 "
                f"{_safe_int(last_paper_backlog.get('paper_observation_active_count'))} "
                "条 active paper/warmup 策略，问题更像“观察池堆积、质量不转正”，而不是“无人消费”。"
            )
        if issue_counts.get("no_forward_signal_coverage_yet", 0) > 0:
            lines.append("- 新提交策略的前向观测覆盖还很低，短期内不应夸大其真实交易质量。")
    else:
        lines.append("- 尚未获得足够数据。")

    return "\n".join(lines).rstrip() + "\n"
