"""Incubation-stage planning helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from .common import _safe_float, _safe_int, _string
from .confidence import evaluate_execution_audit_gate

# P-1: warmup 长期滞留治理阈值。停留超过此天数且前向有效样本仍低于最低毕业门,
# 视为信号频率/质量过低,冻结待修以释放 observe 池容量。阈值偏保守,需后续运营校准。
_WARMUP_STALL_DAYS = 45
_WARMUP_STALL_MIN_EFFECTIVE_N = 12

def resolve_incubation_pipeline_stage(
    signal_quality: Optional[dict],
    *,
    open_risk_count: int = 0,
    audit_summary: Optional[dict[str, Any]] = None,
    execution_audit_gate_status: Optional[str] = None,
    holding_bucket: Optional[str] = None,
) -> str:
    quality = dict(signal_quality or {})
    primary_effective_n = _safe_int(quality.get("primary_effective_n"))
    secondary_effective_n = _safe_int(quality.get("secondary_effective_n"))
    primary_skill_lcb = _safe_float(
        quality.get("primary_skill_lcb") if quality.get("primary_skill_lcb") is not None else quality.get("primary_signal_skill_lcb")
    )
    secondary_skill_lcb = _safe_float(
        quality.get("secondary_skill_lcb") if quality.get("secondary_skill_lcb") is not None else quality.get("secondary_signal_skill_lcb")
    )
    recent_primary_skill_lcb = _safe_float(quality.get("recent_primary_skill_lcb"))
    coverage_ratio = _safe_float(quality.get("coverage_ratio")) or 0.0
    stability_gap = _safe_float(quality.get("stability_gap"))

    # P1: warmup / graduation 样本门按持有周期自适应。低频策略(中长线)信号稀疏,
    # 在合理时间内积累不到短线所需样本量,会被不公平地长期卡在 warmup。
    # 这里仅缩放"样本量"门槛,skill_lcb>0 / coverage / stability 等统计显著性条件保持不变——
    # 不放水,只是承认低频策略每个样本承载更长持有期信息。skill_lcb 本身已含小样本惩罚。
    bucket = str(holding_bucket or "").strip().lower()
    if bucket in {"long", "long_term"}:
        warmup_min_n, grad_primary_n, grad_secondary_n = 12, 30, 15
    elif bucket in {"medium", "mid", "medium_term"}:
        warmup_min_n, grad_primary_n, grad_secondary_n = 15, 45, 22
    else:
        warmup_min_n, grad_primary_n, grad_secondary_n = 20, 60, 30

    if primary_effective_n < warmup_min_n or coverage_ratio < 0.25:
        signal_stage_without_execution_gate = "warmup"
    elif (recent_primary_skill_lcb is not None and recent_primary_skill_lcb < -0.03) or (
        stability_gap is not None and stability_gap > 0.10
    ) or open_risk_count >= 3:
        signal_stage_without_execution_gate = "failed"
    elif (
        primary_effective_n >= grad_primary_n
        and secondary_effective_n >= grad_secondary_n
        and (primary_skill_lcb or 0.0) > 0.0
        and (secondary_skill_lcb or 0.0) > 0.0
        and (recent_primary_skill_lcb or 0.0) > 0.0
        and coverage_ratio >= 0.75
        and (stability_gap is None or stability_gap <= 0.05)
        and open_risk_count == 0
    ):
        signal_stage_without_execution_gate = "graduation_ready"
    elif (
        primary_skill_lcb is None
        or primary_skill_lcb <= 0.0
        or coverage_ratio < 0.5
        or (stability_gap is not None and stability_gap > 0.08)
        or open_risk_count > 1
    ):
        signal_stage_without_execution_gate = "observe"
    else:
        signal_stage_without_execution_gate = "candidate"

    if signal_stage_without_execution_gate == "warmup":
        return "warmup"

    gate_status = execution_audit_gate_status
    if not gate_status:
        gate_status, _reasons, _passes, _metrics = evaluate_execution_audit_gate(audit_summary)
    if gate_status == "failed_metrics":
        return "failed"
    if signal_stage_without_execution_gate == "failed":
        return "failed"
    if signal_stage_without_execution_gate == "graduation_ready" and gate_status == "passed":
        return "graduation_ready"
    if signal_stage_without_execution_gate == "candidate" and gate_status == "passed":
        return "candidate"
    return "observe"


def _parse_time(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.fromisoformat(f"{text}T00:00:00+00:00")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _coerce_date(value: Any) -> Optional[date]:
    dt = _parse_time(value)
    return dt.date() if dt is not None else None


def _estimate_stage_clock_days(
    strategy: Optional[dict[str, Any]],
    quality_report: Optional[dict[str, Any]],
    incubation_metrics: list[dict[str, Any]],
) -> int:
    strategy_payload = dict(strategy or {})
    report_payload = dict(quality_report or {})
    metric_times = [
        _parse_time(dict(item or {}).get("metric_date"))
        for item in list(incubation_metrics or [])
    ]
    metric_times = [item for item in metric_times if item is not None]
    if metric_times:
        return max(0, (max(metric_times).date() - min(metric_times).date()).days)
    start = (
        _parse_time(report_payload.get("created_at"))
        or _parse_time(report_payload.get("updated_at"))
        or _parse_time(strategy_payload.get("created_at"))
        or _parse_time(strategy_payload.get("updated_at"))
    )
    if start is None:
        return 0
    return max(0, (datetime.now(timezone.utc).date() - start.date()).days)


def _metric_signal_value(item: dict[str, Any], key: str) -> Optional[float]:
    payload = dict(item or {})
    value = payload.get(key)
    if value is None:
        value = dict(payload.get("signal_quality") or {}).get(key)
    return _safe_float(value)


def _recent_negative_skill_streak(incubation_metrics: list[dict[str, Any]]) -> int:
    streak = 0
    for metric in list(incubation_metrics or []):
        recent_skill_lcb = _metric_signal_value(metric, "recent_primary_skill_lcb")
        if recent_skill_lcb is None or recent_skill_lcb >= 0:
            break
        streak += 1
    return streak


async def resolve_incubation_action_plan(
    db,
    strategy: dict[str, Any],
    *,
    pipeline_stage: str,
    signal_quality: Optional[dict[str, Any]] = None,
    execution_quality: Optional[dict[str, Any]] = None,
    total_signals: int = 0,
    validation_grade: Optional[str] = None,
    quality_report: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    signal_payload = dict(signal_quality or {})
    execution_payload = dict(execution_quality or {})
    strategy_id = str(dict(strategy or {}).get("id") or "").strip()
    incubation_metrics: list[dict[str, Any]] = []
    list_metrics = getattr(db, "list_strategy_incubation_metrics", None)
    if callable(list_metrics) and strategy_id:
        try:
            incubation_metrics = list(await list_metrics(strategy_id, limit=10) or [])
        except Exception:
            incubation_metrics = []

    stage_clock_days = _estimate_stage_clock_days(strategy, quality_report, incubation_metrics)
    signal_vacuum_days = stage_clock_days if pipeline_stage == "warmup" and int(total_signals or 0) <= 0 else 0
    recent_primary_skill_lcb = _safe_float(signal_payload.get("recent_primary_skill_lcb"))
    stability_gap = _safe_float(signal_payload.get("stability_gap"))
    prediction_quality_label = _string(execution_payload.get("prediction_quality_label")) or "insufficient_evidence"
    execution_quality_label = _string(execution_payload.get("execution_quality_label")) or "insufficient_evidence"
    negative_skill_streak = _recent_negative_skill_streak(incubation_metrics)

    remediation_action = "continue_observe"
    remediation_reason = "stage_progression_normal"
    budget_action = "keep_bootstrap"
    runtime_control_mode = "observe_only"
    revision_required = False
    cleanup_recommended = False

    normalized_grade = _string(validation_grade).upper() or None
    if normalized_grade == "D":
        remediation_action = "cleanup_low_confidence_candidate"
        remediation_reason = "validation_grade_d_not_allowed_for_runtime"
        budget_action = "stop_runtime"
        runtime_control_mode = "research_only"
        cleanup_recommended = True
    elif (
        recent_primary_skill_lcb is not None and recent_primary_skill_lcb < -0.03
    ) or (
        stability_gap is not None and stability_gap > 0.10
    ) or pipeline_stage == "failed":
        remediation_action = "freeze_and_revise"
        remediation_reason = (
            "prediction_skill_negative"
            if recent_primary_skill_lcb is not None and recent_primary_skill_lcb < -0.03
            else "stability_break"
            if stability_gap is not None and stability_gap > 0.10
            else "pipeline_failed"
        )
        budget_action = "freeze_new_budget"
        runtime_control_mode = "exit_only"
        revision_required = True
    elif pipeline_stage == "warmup" and int(total_signals or 0) <= 0:
        remediation_reason = "signal_vacuum"
        if signal_vacuum_days >= 30:
            remediation_action = "return_to_research"
            budget_action = "stop_runtime"
            runtime_control_mode = "exit_only"
            revision_required = True
        elif signal_vacuum_days >= 20:
            remediation_action = "freeze_and_revise"
            budget_action = "freeze_new_budget"
            runtime_control_mode = "exit_only"
            revision_required = True
        elif signal_vacuum_days >= 5:
            remediation_action = "signal_vacuum_warning"
        else:
            remediation_action = "continue_observe"
    elif (
        pipeline_stage == "warmup"
        and int(total_signals or 0) > 0
        and stage_clock_days >= _WARMUP_STALL_DAYS
        and _safe_int(signal_payload.get("primary_effective_n")) < _WARMUP_STALL_MIN_EFFECTIVE_N
    ):
        # P-1: warmup 长期滞留治理。样本有信号(区别于 signal_vacuum),但停留
        # >=45 天后前向有效样本仍 < 12(最低自适应毕业门),说明信号频率/质量过低,
        # 在合理窗口内无法积累出可晋升证据 → 冻结待修(可逆,非删除),给 observe 池一个出口,
        # 避免长期滞留样本无限沉淀。阈值偏保守,仅抓真正证据停滞的样本。
        remediation_action = "freeze_and_revise"
        remediation_reason = "warmup_stall_low_evidence"
        budget_action = "freeze_new_budget"
        runtime_control_mode = "exit_only"
        revision_required = True
    elif negative_skill_streak >= 5:
        remediation_action = "freeze_and_revise_signal_logic"
        remediation_reason = "prediction_skill_negative"
        budget_action = "freeze_new_budget"
        runtime_control_mode = "freeze_new_entries"
        revision_required = True
    elif prediction_quality_label in {"strong", "mixed"} and execution_quality_label == "weak":
        remediation_action = "execution_template_adjustment"
        remediation_reason = "execution_conversion_failure"
        budget_action = "budget_cut_50"
        runtime_control_mode = "marketable_limit_keep_observe"
        revision_required = True
    elif prediction_quality_label == "weak" and execution_quality_label != "weak":
        remediation_action = "freeze_and_revise_signal_logic"
        remediation_reason = "prediction_skill_negative"
        budget_action = "freeze_new_budget"
        runtime_control_mode = "freeze_new_entries"
        revision_required = True
    elif pipeline_stage == "candidate":
        remediation_action = "candidate_keep_observe"
        remediation_reason = "candidate_waiting_for_more_signal_and_execution_evidence"
    elif pipeline_stage == "graduation_ready":
        remediation_action = "ready_for_promotion_review"
        remediation_reason = "signal_and_execution_quality_ready"
        budget_action = "promote_budget"
        runtime_control_mode = "monitor"

    return {
        "stage_clock_days": stage_clock_days,
        "signal_vacuum_days": signal_vacuum_days,
        "remediation_action": remediation_action,
        "remediation_reason": remediation_reason,
        "budget_action": budget_action,
        "runtime_control_mode": runtime_control_mode,
        "revision_required": revision_required,
        "cleanup_recommended": cleanup_recommended,
        "negative_skill_metric_streak": negative_skill_streak,
    }


async def list_quality_reports(db, strategy_id: str, limit: int = 10) -> list[dict]:
    if hasattr(db, "list_strategy_quality_reports"):
        return await db.list_strategy_quality_reports(strategy_id, limit=limit)
    latest = None
    if hasattr(db, "get_latest_strategy_quality_report"):
        latest = await db.get_latest_strategy_quality_report(strategy_id)
    elif hasattr(db, "get_strategy_quality_report"):
        latest = await db.get_strategy_quality_report(strategy_id)
    return [latest] if latest else []


async def get_latest_quality_report(db, strategy_id: str) -> Optional[dict]:
    rows = await list_quality_reports(db, strategy_id, limit=1)
    return rows[0] if rows else None

