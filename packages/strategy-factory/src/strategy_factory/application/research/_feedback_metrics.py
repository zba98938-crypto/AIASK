"""Feedback evidence, aggregation, and scoring helpers for factor research."""

from __future__ import annotations

import logging
import math
from typing import Any, List

from .._budget_feedback import normalize_text, resolve_feedback_metrics
from ..runtime import _call_optional_async

logger = logging.getLogger(__name__)


def feedback_ab_quality_score(builder_cls, feedback_metrics: dict[str, Any]) -> float:
    raw_validation_a_rate = builder_cls._safe_float(feedback_metrics.get("raw_validation_a_rate"))
    raw_validation_b_rate = builder_cls._safe_float(feedback_metrics.get("raw_validation_b_rate"))
    raw_validation_d_rate = builder_cls._safe_float(feedback_metrics.get("raw_validation_d_rate"))
    raw_validation_total_score_mean = max(
        0.0,
        min(builder_cls._safe_float(feedback_metrics.get("raw_validation_total_score_mean")), 100.0),
    )
    strict_incubation_ready_rate = builder_cls._safe_float(
        feedback_metrics.get("strict_incubation_ready_rate")
    )
    score = (
        raw_validation_a_rate * 1.3
        + raw_validation_b_rate * 0.95
        + strict_incubation_ready_rate * 0.7
        + max(raw_validation_total_score_mean - 50.0, 0.0) / 50.0 * 0.45
        - raw_validation_d_rate * 0.85
    )
    return round(score, 4)


def resolve_search_route_action(
    builder_cls,
    plan: dict[str, Any],
    feedback_metrics: dict[str, Any],
) -> str:
    control_mode = normalize_text(plan.get("feedback_control_mode")) or "normal"
    zero_signal_ratio = builder_cls._safe_float(feedback_metrics.get("zero_signal_ratio"))
    evidence_debt_ratio = builder_cls._safe_float(feedback_metrics.get("evidence_debt_ratio"))
    gate_failure_rate = builder_cls._safe_float(feedback_metrics.get("gate_failure_rate"))
    trace_completeness_ratio = builder_cls._safe_float(
        feedback_metrics.get("trace_completeness_ratio"),
        1.0,
    )
    admission_quality_objective = builder_cls._safe_float(
        feedback_metrics.get("admission_quality_objective")
    )
    promotion_ready_ratio = builder_cls._safe_float(
        feedback_metrics.get("promotion_ready_ratio"),
        1.0,
    )
    raw_validation_a_rate = builder_cls._safe_float(feedback_metrics.get("raw_validation_a_rate"))
    raw_validation_b_rate = builder_cls._safe_float(feedback_metrics.get("raw_validation_b_rate"))
    raw_validation_d_rate = builder_cls._safe_float(feedback_metrics.get("raw_validation_d_rate"))
    raw_validation_total_score_mean = builder_cls._safe_float(
        feedback_metrics.get("raw_validation_total_score_mean")
    )
    strict_incubation_ready_rate = builder_cls._safe_float(
        feedback_metrics.get("strict_incubation_ready_rate")
    )
    budget_multiplier = builder_cls._safe_float(plan.get("feedback_budget_multiplier"), 1.0)
    priority_adjustment = builder_cls._safe_float(plan.get("feedback_priority_adjustment"))
    quality_score = feedback_ab_quality_score(builder_cls, feedback_metrics)
    if bool(plan.get("feedback_family_freeze_active")) or control_mode == "freeze":
        return "family_freeze"
    if control_mode == "suppress":
        if zero_signal_ratio >= 0.65 or evidence_debt_ratio >= 0.65:
            return "family_retire"
        return "family_cooldown"
    if control_mode == "cooldown":
        return "family_cooldown"
    if gate_failure_rate >= 0.65 and admission_quality_objective <= 0.20:
        return "family_cooldown"
    if (
        raw_validation_d_rate >= 0.75
        and raw_validation_a_rate <= 0.0
        and raw_validation_b_rate <= 0.12
        and raw_validation_total_score_mean <= 42.0
    ):
        return "family_cooldown"
    if (
        budget_multiplier > 1.0
        or priority_adjustment > 0.0
        or promotion_ready_ratio >= 0.35
        or strict_incubation_ready_rate >= 0.2
        or raw_validation_a_rate >= 0.1
        or raw_validation_b_rate >= 0.3
        or raw_validation_total_score_mean >= 58.0
        or quality_score >= 0.35
        or admission_quality_objective >= 0.35
        or trace_completeness_ratio >= 0.55
    ):
        return "family_explore"
    if zero_signal_ratio >= 0.3 or evidence_debt_ratio >= 0.35:
        return "family_cooldown"
    return "family_explore"


def scope_route_action(
    builder_cls,
    *,
    scope_name: str,
    scope_metrics: dict[str, Any],
    preferred_shift_target: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    if not scope_metrics:
        return None, {}
    control_mode = normalize_text(scope_metrics.get("control_mode")) or "normal"
    budget_multiplier = builder_cls._safe_float(scope_metrics.get("budget_multiplier"), 1.0)
    priority_adjustment = builder_cls._safe_float(scope_metrics.get("priority_adjustment"))
    promotion_ready_ratio = builder_cls._safe_float(scope_metrics.get("promotion_ready_ratio"), 1.0)
    forward_window_coverage_ratio = builder_cls._safe_float(
        scope_metrics.get("forward_window_coverage_ratio"),
        1.0,
    )
    zero_signal_ratio = builder_cls._safe_float(scope_metrics.get("zero_signal_ratio"))
    evidence_debt_ratio = builder_cls._safe_float(scope_metrics.get("evidence_debt_ratio"))
    payload = {
        "control_mode": control_mode,
        "budget_multiplier": round(budget_multiplier, 4),
        "priority_adjustment": round(priority_adjustment, 4),
        "reasons": list(scope_metrics.get("control_reasons") or []),
        "legacy_control_mode": normalize_text(scope_metrics.get("legacy_control_mode")) or control_mode,
        "skill_control_mode": normalize_text(scope_metrics.get("skill_control_mode")) or "normal",
        "legacy_budget_multiplier": round(
            builder_cls._safe_float(scope_metrics.get("legacy_budget_multiplier"), budget_multiplier),
            4,
        ),
        "skill_budget_multiplier": round(
            builder_cls._safe_float(scope_metrics.get("skill_budget_multiplier"), budget_multiplier),
            4,
        ),
        "legacy_priority_adjustment": round(
            builder_cls._safe_float(scope_metrics.get("legacy_priority_adjustment"), priority_adjustment),
            4,
        ),
        "skill_priority_adjustment": round(
            builder_cls._safe_float(scope_metrics.get("skill_priority_adjustment"), priority_adjustment),
            4,
        ),
        "paper_skill_lcb": round(
            builder_cls._safe_float(scope_metrics.get("paper_skill_lcb")),
            4,
        ),
        "paper_recent_skill_lcb": round(
            builder_cls._safe_float(scope_metrics.get("paper_recent_skill_lcb")),
            4,
        ),
        "paper_stability_gap": round(
            builder_cls._safe_float(scope_metrics.get("paper_stability_gap")),
            4,
        ),
        "paper_coverage_ratio": round(
            builder_cls._safe_float(scope_metrics.get("paper_coverage_ratio"), 1.0),
            4,
        ),
        "effective_feedback_signal": scope_metrics.get("effective_feedback_signal")
        or "legacy_paper_hit_ratio",
    }
    if scope_name == "target_pool":
        if control_mode in {"freeze", "suppress"} or evidence_debt_ratio >= 0.55:
            return "universe_shrink", payload
        if (
            budget_multiplier > 1.0
            or priority_adjustment > 0.0
            or promotion_ready_ratio >= 0.35
            or forward_window_coverage_ratio >= 0.55
        ):
            return "universe_expand", payload
        if control_mode == "cooldown":
            return "universe_shrink", payload
        return None, payload
    if scope_name == "holding_bucket":
        if control_mode in {"freeze", "suppress"} or zero_signal_ratio >= 0.55:
            return "holding_demote", payload
        if (
            budget_multiplier > 1.0
            or priority_adjustment > 0.0
            or promotion_ready_ratio >= 0.35
            or forward_window_coverage_ratio >= 0.55
        ):
            return "holding_promote", payload
        if control_mode == "cooldown":
            return "holding_demote", payload
        return None, payload
    if scope_name == "generator_mode":
        if control_mode in {"freeze", "suppress", "cooldown"} or preferred_shift_target:
            if preferred_shift_target:
                payload["recommended_generator_mode"] = preferred_shift_target
            return "generator_mode_shift", payload
        if budget_multiplier > 1.0 or priority_adjustment > 0.0:
            payload["recommended_generator_mode"] = preferred_shift_target
            return "generator_mode_shift", payload
    return None, payload


def preferred_generator_shift_target(
    builder_cls,
    family_bucket: dict[str, Any],
    *,
    current_mode: str | None,
) -> str | None:
    generator_scope = dict(family_bucket.get("generator_mode_feedback") or {})
    ranked_modes: list[tuple[tuple[int, float, float, str], str]] = []
    for mode_name, _mode_bucket in generator_scope.items():
        mode = normalize_text(mode_name)
        if not mode:
            continue
        metrics = resolve_feedback_metrics(
            {"family_gate_feedback": {"candidate_family": family_bucket}},
            family="candidate_family",
            generator_mode=mode,
        )
        control_mode = normalize_text(metrics.get("generator_mode_control_mode")) or "normal"
        ranked_modes.append(
            (
                (
                    -{
                        "normal": 0,
                        "cooldown": 1,
                        "suppress": 2,
                        "freeze": 3,
                    }.get(control_mode, 0),
                    builder_cls._safe_float(metrics.get("budget_multiplier"), 1.0),
                    builder_cls._safe_float(metrics.get("priority_adjustment")),
                    mode,
                ),
                mode,
            )
        )
    if not ranked_modes:
        fallback = "rule" if normalize_text(current_mode) != "rule" else "external_llm"
        return fallback or None
    ranked_modes.sort(reverse=True)
    for _score, mode in ranked_modes:
        if mode != normalize_text(current_mode):
            return mode
    return ranked_modes[0][1]


def feedback_family_key(payload: dict[str, Any]) -> str:
    item = dict(payload or {})
    params = dict(item.get("params") or {})
    provenance = dict(params.get("candidate_provenance") or {})
    research_task = dict(item.get("research_task") or {})
    contract_snapshot = dict(item.get("candidate_contract_snapshot") or {})
    targeting = dict(contract_snapshot.get("targeting") or {})
    for source in (item, provenance, params, research_task, targeting, contract_snapshot):
        for key in ("candidate_family_id", "candidate_family", "family", "strategy_type"):
            token = normalize_text(source.get(key))
            if token:
                return token
    return "unknown"


def feedback_runtime_alert_pressure(
    builder_cls,
    latest_metric: dict[str, Any],
    risk_events: list[dict[str, Any]],
    runtime_alerts: list[dict[str, Any]],
) -> float:
    severity_weights = {
        "critical": 0.55,
        "high": 0.35,
        "medium": 0.18,
        "low": 0.08,
    }
    open_alerts = [
        dict(item or {})
        for item in list(runtime_alerts or [])
        if normalize_text((item or {}).get("status") or "open") not in {"resolved", "closed"}
    ]
    open_events = [
        dict(item or {})
        for item in list(risk_events or [])
        if normalize_text((item or {}).get("status") or "open") not in {"resolved", "closed"}
    ]
    pressure = 0.0
    for row in [*open_alerts, *open_events]:
        pressure += severity_weights.get(normalize_text(row.get("severity")) or "medium", 0.18)
    total_open = len(open_alerts) + len(open_events)
    if total_open > 1:
        pressure += min((total_open - 1) * 0.06, 0.3)
    decision = normalize_text(latest_metric.get("decision"))
    if decision == "halt":
        pressure = max(pressure, 0.85)
    elif decision in {"review", "defer"}:
        pressure = max(pressure, 0.45)
    return round(min(max(pressure, 0.0), 1.0), 4)


def feedback_capacity_crowding(
    builder_cls,
    latest_metric: dict[str, Any],
    risk_events: list[dict[str, Any]],
    runtime_alerts: list[dict[str, Any]],
) -> float:
    turnover_rate = max(0.0, builder_cls._safe_float(latest_metric.get("turnover_rate")))
    exposure_rate = max(0.0, builder_cls._safe_float(latest_metric.get("exposure_rate")))
    crowding = max(turnover_rate, exposure_rate)
    risk_tokens = " ".join(
        normalize_text(
            (item or {}).get("reason")
            or (item or {}).get("message")
            or (item or {}).get("alert_key")
        )
        for item in [*list(risk_events or []), *list(runtime_alerts or [])]
    )
    if any(token in risk_tokens for token in ("crowd", "capacity", "turnover", "exposure")):
        crowding = max(crowding, 0.75)
    return round(min(max(crowding, 0.0), 2.0), 4)


async def list_feedback_source_strategies(
    builder_cls,
    db,
    *,
    limit: int = 180,
) -> List[dict[str, Any]]:
    if not hasattr(db, "list_strategies"):
        return []
    active_submitted_tracks = {
        "formal_incubation",
        "observe_incubation",
        "live_ready_review",
    }
    status_limits = {
        "incubating": max(10, int(math.ceil(limit * 0.75))),
        "listed": max(10, int(math.ceil(limit * 0.10))),
        "submitted": max(10, int(math.ceil(limit * 0.15))),
    }
    seen: set[str] = set()
    items: List[dict[str, Any]] = []
    for status, status_limit in status_limits.items():
        try:
            rows = await _call_optional_async(
                db,
                "list_strategies",
                status,
                None,
                status_limit,
                0,
                default=[],
            )
        except TypeError:
            rows = await _call_optional_async(
                db,
                "list_strategies",
                status,
                status_limit,
                default=[],
            )
        for row in list(rows or []):
            payload = dict(row or {})
            strategy_id = str(payload.get("id") or "").strip()
            if not strategy_id or strategy_id in seen:
                continue
            normalized_status = normalize_text(payload.get("status") or status)
            if normalized_status == "submitted":
                params = dict(payload.get("params") or {})
                incubation_budget = dict(params.get("incubation_budget") or {})
                track = normalize_text(
                    incubation_budget.get("track")
                    or payload.get("incubation_budget_track")
                    or payload.get("submission_lane")
                )
                if track not in active_submitted_tracks:
                    continue
            seen.add(strategy_id)
            items.append(payload)
            if len(items) >= limit:
                return items
    return items


def resolve_promotion_review_outcome(
    status_counts: dict[str, Any] | None,
    recommendation_counts: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    normalized_status_counts = {
        normalize_text(key): int(value or 0)
        for key, value in dict(status_counts or {}).items()
        if normalize_text(key)
    }
    normalized_recommendation_counts = {
        normalize_text(key): int(value or 0)
        for key, value in dict(recommendation_counts or {}).items()
        if normalize_text(key)
    }
    status = next(
        (
            item
            for item in ("rejected", "watch", "approved")
            if int(normalized_status_counts.get(item) or 0) > 0
        ),
        None,
    )
    if status is None:
        status = next(
            (
                key
                for key, value in normalized_status_counts.items()
                if int(value or 0) > 0
            ),
            None,
        )
    recommendation = next(
        (
            item
            for item in ("deprecate", "observe", "promote")
            if int(normalized_recommendation_counts.get(item) or 0) > 0
        ),
        None,
    )
    if recommendation is None:
        recommendation = next(
            (
                key
                for key, value in normalized_recommendation_counts.items()
                if int(value or 0) > 0
            ),
            None,
        )
    return status, recommendation


def fallback_feedback_evidence_overview(
    builder_cls,
    signal_stats: dict[str, Any] | None = None,
    *,
    strategy_id: str | None = None,
    quality_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(signal_stats or {})
    observed_forward_days: list[int] = []
    for days in builder_cls.EVIDENCE_FORWARD_WINDOWS:
        day_key = str(days)
        has_signal = any(
            dict(payload.get(metric_name) or {}).get(day_key) is not None
            or dict(payload.get(metric_name) or {}).get(days) is not None
            for metric_name in ("hit_rate", "forward_ic", "forward_sharpe")
        )
        if has_signal:
            observed_forward_days.append(days)
    total_signals = builder_cls._safe_int(payload.get("total_signals"))
    minimum_signal_count = 10
    missing_forward_days = [
        days for days in builder_cls.EVIDENCE_FORWARD_WINDOWS if days not in observed_forward_days
    ]
    promotion_ready = total_signals >= minimum_signal_count and not missing_forward_days
    primary_horizon = 5 if 5 in builder_cls.EVIDENCE_FORWARD_WINDOWS else builder_cls.EVIDENCE_FORWARD_WINDOWS[0]

    def _resolve_metric(metric_name: str, *, fallback: float | None = None) -> float | None:
        bucket = dict(payload.get(metric_name) or {})
        for key in (str(primary_horizon), primary_horizon):
            if bucket.get(key) is not None:
                return builder_cls._safe_float(bucket.get(key))
        for days in builder_cls.EVIDENCE_FORWARD_WINDOWS:
            for key in (str(days), days):
                if bucket.get(key) is not None:
                    return builder_cls._safe_float(bucket.get(key))
        return fallback

    coverage_ratio = (
        round(len(observed_forward_days) / len(builder_cls.EVIDENCE_FORWARD_WINDOWS), 4)
        if builder_cls.EVIDENCE_FORWARD_WINDOWS
        else 0.0
    )
    signal_quality = {
        "primary_horizon": primary_horizon,
        "coverage_ratio": coverage_ratio,
        "primary_skill_lcb": _resolve_metric("skill_lcb", fallback=0.0),
        "recent_primary_skill_lcb": _resolve_metric(
            "recent_skill_lcb",
            fallback=_resolve_metric("skill_lcb", fallback=0.0),
        ),
        "stability_gap": _resolve_metric("stability_gap", fallback=0.0),
    }
    result = {
        "strategy_id": strategy_id,
        "total_signals": total_signals,
        "minimum_signal_count": minimum_signal_count,
        "observed_forward_days": observed_forward_days,
        "missing_forward_days": missing_forward_days,
        "promotion_ready": promotion_ready,
        "skill_lcb": signal_quality.get("primary_skill_lcb"),
        "recent_skill_lcb": signal_quality.get("recent_primary_skill_lcb"),
        "stability_gap": signal_quality.get("stability_gap"),
        "coverage_ratio": coverage_ratio,
        "signal_quality": signal_quality,
        "blockers": [],
        "risk_flags": [],
    }
    _apply_quality_report_to_feedback_overview(result, quality_report)
    return result


async def load_feedback_quality_report(db, strategy_id: str) -> dict[str, Any]:
    latest = await _call_optional_async(
        db,
        "get_latest_strategy_quality_report",
        strategy_id,
        default=None,
    )
    if latest:
        return dict(latest or {})
    rows = await _call_optional_async(
        db,
        "list_strategy_quality_reports",
        strategy_id,
        limit=1,
        default=[],
    )
    first = list(rows or [])[:1]
    return dict(first[0] or {}) if first else {}


def _quality_payload_value(quality_report: dict[str, Any] | None, key: str) -> Any:
    report = dict(quality_report or {})
    summary = dict(report.get("summary") or {})
    quality_gate = dict(summary.get("quality_gate") or report.get("quality_gate") or {})
    for payload in (summary, quality_gate, report):
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return None


def _apply_quality_report_to_feedback_overview(
    overview: dict[str, Any],
    quality_report: dict[str, Any] | None,
) -> None:
    report = dict(quality_report or {})
    if not report:
        return
    for key in (
        "validation_grade",
        "raw_validation_grade",
        "effective_validation_grade",
        "validation_total_score",
        "raw_validation_total_score",
        "strict_incubation_ready",
        "strict_incubation_blocked",
        "live_candidate_ready",
        "admission_stage",
        "incubation_pass_mode",
    ):
        value = _quality_payload_value(report, key)
        if value is not None:
            overview[key] = value
    if overview.get("raw_validation_grade") is None and overview.get("validation_grade") is not None:
        overview["raw_validation_grade"] = overview.get("validation_grade")
    if overview.get("raw_validation_total_score") is None and overview.get("validation_total_score") is not None:
        overview["raw_validation_total_score"] = overview.get("validation_total_score")
    if "passed" in report:
        overview["quality_report_passed"] = bool(report.get("passed"))


async def load_feedback_evidence_overview(
    builder_cls,
    db,
    strategy: dict[str, Any],
    *,
    lifecycle_runtime_provider,
) -> dict[str, Any]:
    strategy_id = str((strategy or {}).get("id") or "").strip()
    if not strategy_id:
        return fallback_feedback_evidence_overview(builder_cls, {})

    lifecycle_runtime = lifecycle_runtime_provider()
    build_overview = getattr(lifecycle_runtime, "build_incubation_overview", None)
    if callable(build_overview):
        try:
            overview = await build_overview(db, strategy)
            if isinstance(overview, dict) and overview:
                return overview
        except Exception as exc:
            logger.debug("_feedback_metrics: build_overview failed for %s: %s", strategy_id, exc)

    signal_stats = await _call_optional_async(
        db,
        "get_signal_stats",
        strategy_id,
        default={},
    )
    quality_report = await load_feedback_quality_report(db, strategy_id)
    return fallback_feedback_evidence_overview(
        builder_cls,
        signal_stats,
        strategy_id=strategy_id,
        quality_report=quality_report,
    )


def accumulate_feedback_bucket(
    builder_cls,
    accumulator: dict[str, Any],
    *,
    strategy_id: str,
    metrics: dict[str, Any],
    runtime_alert_count: int,
    runtime_risk_event_count: int,
    evidence_overview: dict[str, Any] | None = None,
    promotion_review: dict[str, Any] | None = None,
) -> None:
    accumulator["strategy_count"] = int(accumulator.get("strategy_count") or 0) + 1
    if strategy_id:
        strategy_ids = list(accumulator.get("strategy_ids") or [])
        if strategy_id not in strategy_ids:
            strategy_ids.append(strategy_id)
        accumulator["strategy_ids"] = strategy_ids[:20]
    accumulator["runtime_alert_count"] = int(accumulator.get("runtime_alert_count") or 0) + int(runtime_alert_count or 0)
    accumulator["runtime_risk_event_count"] = int(accumulator.get("runtime_risk_event_count") or 0) + int(runtime_risk_event_count or 0)
    accumulator["paper_hit_ratio_total"] = builder_cls._safe_float(accumulator.get("paper_hit_ratio_total")) + builder_cls._safe_float(
        metrics.get("paper_hit_ratio")
    )
    accumulator["paper_skill_lcb_total"] = builder_cls._safe_float(
        accumulator.get("paper_skill_lcb_total")
    ) + builder_cls._safe_float(metrics.get("paper_skill_lcb"))
    accumulator["paper_recent_skill_lcb_total"] = builder_cls._safe_float(
        accumulator.get("paper_recent_skill_lcb_total")
    ) + builder_cls._safe_float(metrics.get("paper_recent_skill_lcb"))
    accumulator["paper_stability_gap_total"] = builder_cls._safe_float(
        accumulator.get("paper_stability_gap_total")
    ) + builder_cls._safe_float(metrics.get("paper_stability_gap"))
    accumulator["paper_coverage_ratio_total"] = builder_cls._safe_float(
        accumulator.get("paper_coverage_ratio_total")
    ) + builder_cls._safe_float(metrics.get("paper_coverage_ratio"), 1.0)
    accumulator["runtime_alert_pressure_total"] = builder_cls._safe_float(
        accumulator.get("runtime_alert_pressure_total")
    ) + builder_cls._safe_float(metrics.get("runtime_alert_pressure"))
    accumulator["realized_turnover_total"] = builder_cls._safe_float(
        accumulator.get("realized_turnover_total")
    ) + builder_cls._safe_float(metrics.get("realized_turnover"))
    accumulator["capacity_crowding_total"] = builder_cls._safe_float(
        accumulator.get("capacity_crowding_total")
    ) + builder_cls._safe_float(metrics.get("capacity_crowding"))
    overview = dict(evidence_overview or {})
    total_signals = max(0, builder_cls._safe_int(overview.get("total_signals")))
    minimum_signal_count = max(1, builder_cls._safe_int(overview.get("minimum_signal_count") or 10))
    observed_forward_days = [
        int(day)
        for day in list(overview.get("observed_forward_days") or [])
        if int(day) in builder_cls.EVIDENCE_FORWARD_WINDOWS
    ]
    missing_forward_days = [
        int(day)
        for day in list(overview.get("missing_forward_days") or [])
        if int(day) in builder_cls.EVIDENCE_FORWARD_WINDOWS
    ]
    promotion_ready = bool(overview.get("promotion_ready"))
    accumulator["signal_count_total"] = int(accumulator.get("signal_count_total") or 0) + total_signals
    accumulator["expected_forward_window_count"] = int(
        accumulator.get("expected_forward_window_count") or 0
    ) + len(builder_cls.EVIDENCE_FORWARD_WINDOWS)
    accumulator["observed_forward_window_count"] = int(
        accumulator.get("observed_forward_window_count") or 0
    ) + len(observed_forward_days)
    accumulator["missing_forward_window_count"] = int(
        accumulator.get("missing_forward_window_count") or 0
    ) + len(missing_forward_days)
    if total_signals <= 0:
        accumulator["zero_signal_strategy_count"] = int(
            accumulator.get("zero_signal_strategy_count") or 0
        ) + 1
    if total_signals < minimum_signal_count:
        accumulator["low_signal_strategy_count"] = int(
            accumulator.get("low_signal_strategy_count") or 0
        ) + 1
    if promotion_ready:
        accumulator["promotion_ready_count"] = int(
            accumulator.get("promotion_ready_count") or 0
        ) + 1
    if total_signals < minimum_signal_count or missing_forward_days or not promotion_ready:
        accumulator["evidence_debt_strategy_count"] = int(
            accumulator.get("evidence_debt_strategy_count") or 0
        ) + 1
    raw_validation_grade = str(
        overview.get("raw_validation_grade")
        or overview.get("validation_grade")
        or ""
    ).strip().upper()
    if raw_validation_grade:
        raw_validation_grade_distribution = dict(
            accumulator.get("raw_validation_grade_distribution") or {}
        )
        raw_validation_grade_distribution[raw_validation_grade] = int(
            raw_validation_grade_distribution.get(raw_validation_grade) or 0
        ) + 1
        accumulator["raw_validation_grade_distribution"] = raw_validation_grade_distribution
    raw_validation_total_score = overview.get("raw_validation_total_score")
    if raw_validation_total_score is None:
        raw_validation_total_score = overview.get("validation_total_score")
    if raw_validation_total_score is not None:
        accumulator["raw_validation_total_score_total"] = builder_cls._safe_float(
            accumulator.get("raw_validation_total_score_total")
        ) + builder_cls._safe_float(raw_validation_total_score)
        accumulator["raw_validation_total_score_count"] = int(
            accumulator.get("raw_validation_total_score_count") or 0
        ) + 1
    if bool(overview.get("strict_incubation_ready")):
        accumulator["strict_incubation_ready_count"] = int(
            accumulator.get("strict_incubation_ready_count") or 0
        ) + 1
    if bool(overview.get("live_candidate_ready")):
        accumulator["live_candidate_ready_count"] = int(
            accumulator.get("live_candidate_ready_count") or 0
        ) + 1
    review = dict(promotion_review or {})
    if review:
        accumulator["promotion_review_count"] = int(
            accumulator.get("promotion_review_count") or 0
        ) + 1
        review_status = normalize_text(review.get("status"))
        if review_status:
            status_counts = dict(accumulator.get("promotion_review_status_counts") or {})
            status_counts[review_status] = int(status_counts.get(review_status) or 0) + 1
            accumulator["promotion_review_status_counts"] = status_counts
        review_recommendation = normalize_text(review.get("recommendation"))
        if review_recommendation:
            recommendation_counts = dict(
                accumulator.get("promotion_review_recommendation_counts") or {}
            )
            recommendation_counts[review_recommendation] = int(
                recommendation_counts.get(review_recommendation) or 0
            ) + 1
            accumulator["promotion_review_recommendation_counts"] = recommendation_counts
        if review.get("score") is not None:
            accumulator["promotion_review_score_total"] = builder_cls._safe_float(
                accumulator.get("promotion_review_score_total")
            ) + min(max(builder_cls._safe_float(review.get("score")), 0.0), 1.0)


def finalize_feedback_bucket(builder_cls, accumulator: dict[str, Any]) -> dict[str, Any]:
    payload = dict(accumulator or {})
    strategy_count = max(0, int(payload.get("strategy_count") or 0))
    promotion_review_count = max(0, int(payload.get("promotion_review_count") or 0))
    raw_validation_grade_distribution = {
        str(key or "").strip().upper(): int(value or 0)
        for key, value in dict(payload.get("raw_validation_grade_distribution") or {}).items()
        if str(key or "").strip()
    }
    raw_validation_total_score_total = builder_cls._safe_float(
        payload.get("raw_validation_total_score_total")
    )
    raw_validation_total_score_count = max(
        0,
        int(payload.get("raw_validation_total_score_count") or 0),
    )
    strict_incubation_ready_count = max(
        0,
        int(payload.get("strict_incubation_ready_count") or 0),
    )
    live_candidate_ready_count = max(
        0,
        int(payload.get("live_candidate_ready_count") or 0),
    )
    signal_count_total = max(0, int(payload.get("signal_count_total") or 0))
    expected_forward_window_count = max(
        0,
        int(payload.get("expected_forward_window_count") or 0),
    )
    observed_forward_window_count = max(
        0,
        int(payload.get("observed_forward_window_count") or 0),
    )
    missing_forward_window_count = max(
        0,
        int(payload.get("missing_forward_window_count") or 0),
    )
    zero_signal_strategy_count = max(
        0,
        int(payload.get("zero_signal_strategy_count") or 0),
    )
    low_signal_strategy_count = max(
        0,
        int(payload.get("low_signal_strategy_count") or 0),
    )
    promotion_ready_count = max(
        0,
        int(payload.get("promotion_ready_count") or 0),
    )
    evidence_debt_strategy_count = max(
        0,
        int(payload.get("evidence_debt_strategy_count") or 0),
    )
    promotion_review_status_counts = {
        normalize_text(key): int(value or 0)
        for key, value in dict(payload.get("promotion_review_status_counts") or {}).items()
        if normalize_text(key)
    }
    promotion_review_recommendation_counts = {
        normalize_text(key): int(value or 0)
        for key, value in dict(payload.get("promotion_review_recommendation_counts") or {}).items()
        if normalize_text(key)
    }
    promotion_review_status, promotion_review_recommendation = resolve_promotion_review_outcome(
        promotion_review_status_counts,
        promotion_review_recommendation_counts,
    )
    target_pool_feedback = {
        str(key): finalize_feedback_bucket(builder_cls, value)
        for key, value in dict(payload.get("target_pool_feedback") or {}).items()
        if isinstance(value, dict)
    }
    holding_bucket_feedback = {
        normalize_text(key): finalize_feedback_bucket(builder_cls, value)
        for key, value in dict(payload.get("holding_bucket_feedback") or {}).items()
        if normalize_text(key) and isinstance(value, dict)
    }
    generator_mode_feedback = {
        str(key): finalize_feedback_bucket(builder_cls, value)
        for key, value in dict(payload.get("generator_mode_feedback") or {}).items()
        if isinstance(value, dict)
    }
    zero_signal_ratio = round(zero_signal_strategy_count / strategy_count, 4) if strategy_count else 0.0
    low_signal_ratio = round(low_signal_strategy_count / strategy_count, 4) if strategy_count else 0.0
    promotion_ready_ratio = round(promotion_ready_count / strategy_count, 4) if strategy_count else 1.0
    promotion_review_coverage_ratio = (
        round(promotion_review_count / strategy_count, 4) if strategy_count else 1.0
    )
    forward_window_coverage_ratio = (
        round(observed_forward_window_count / expected_forward_window_count, 4)
        if expected_forward_window_count
        else 1.0
    )
    evidence_debt_ratio = round(
        min(
            max(
                zero_signal_ratio * 0.45
                + (1.0 - forward_window_coverage_ratio) * 0.25
                + (1.0 - promotion_ready_ratio) * 0.15
                + (1.0 - promotion_review_coverage_ratio) * 0.15,
                0.0,
            ),
            1.0,
        ),
        4,
    )
    result = {
        "strategy_count": strategy_count,
        "strategy_ids": list(payload.get("strategy_ids") or [])[:20],
        "runtime_alert_count": int(payload.get("runtime_alert_count") or 0),
        "runtime_risk_event_count": int(payload.get("runtime_risk_event_count") or 0),
        "signal_count_total": signal_count_total,
        "avg_signal_count": round(signal_count_total / strategy_count, 4) if strategy_count else 0.0,
        "zero_signal_strategy_count": zero_signal_strategy_count,
        "zero_signal_ratio": zero_signal_ratio,
        "low_signal_strategy_count": low_signal_strategy_count,
        "low_signal_ratio": low_signal_ratio,
        "observed_forward_window_count": observed_forward_window_count,
        "missing_forward_window_count": missing_forward_window_count,
        "expected_forward_window_count": expected_forward_window_count,
        "forward_window_coverage_ratio": forward_window_coverage_ratio,
        "promotion_ready_count": promotion_ready_count,
        "promotion_ready_ratio": promotion_ready_ratio,
        "promotion_review_coverage_ratio": promotion_review_coverage_ratio,
        "evidence_debt_strategy_count": evidence_debt_strategy_count,
        "evidence_debt_ratio": evidence_debt_ratio,
        "raw_validation_a_rate": round(
            int(raw_validation_grade_distribution.get("A") or 0) / strategy_count,
            4,
        ) if strategy_count else 0.0,
        "raw_validation_b_rate": round(
            int(raw_validation_grade_distribution.get("B") or 0) / strategy_count,
            4,
        ) if strategy_count else 0.0,
        "raw_validation_c_rate": round(
            int(raw_validation_grade_distribution.get("C") or 0) / strategy_count,
            4,
        ) if strategy_count else 0.0,
        "raw_validation_d_rate": round(
            int(raw_validation_grade_distribution.get("D") or 0) / strategy_count,
            4,
        ) if strategy_count else 1.0,
        "raw_validation_total_score_mean": round(
            raw_validation_total_score_total / raw_validation_total_score_count,
            4,
        ) if raw_validation_total_score_count else 0.0,
        "strict_incubation_ready_count": strict_incubation_ready_count,
        "strict_incubation_ready_rate": round(
            strict_incubation_ready_count / strategy_count,
            4,
        ) if strategy_count else 0.0,
        "live_candidate_ready_count": live_candidate_ready_count,
        "live_candidate_ready_rate": round(
            live_candidate_ready_count / strategy_count,
            4,
        ) if strategy_count else 0.0,
        "paper_hit_ratio": round(
            builder_cls._safe_float(payload.get("paper_hit_ratio_total")) / strategy_count,
            4,
        ) if strategy_count else 0.5,
        "paper_skill_lcb": round(
            builder_cls._safe_float(payload.get("paper_skill_lcb_total")) / strategy_count,
            4,
        ) if strategy_count else 0.0,
        "paper_recent_skill_lcb": round(
            builder_cls._safe_float(payload.get("paper_recent_skill_lcb_total")) / strategy_count,
            4,
        ) if strategy_count else 0.0,
        "paper_stability_gap": round(
            builder_cls._safe_float(payload.get("paper_stability_gap_total")) / strategy_count,
            4,
        ) if strategy_count else 0.0,
        "paper_coverage_ratio": round(
            builder_cls._safe_float(payload.get("paper_coverage_ratio_total")) / strategy_count,
            4,
        ) if strategy_count else 1.0,
        "runtime_alert_pressure": round(
            builder_cls._safe_float(payload.get("runtime_alert_pressure_total")) / strategy_count,
            4,
        ) if strategy_count else 0.0,
        "realized_turnover": round(
            builder_cls._safe_float(payload.get("realized_turnover_total")) / strategy_count,
            4,
        ) if strategy_count else 0.0,
        "capacity_crowding": round(
            builder_cls._safe_float(payload.get("capacity_crowding_total")) / strategy_count,
            4,
        ) if strategy_count else 0.0,
    }
    if raw_validation_grade_distribution:
        result["raw_validation_grade_distribution"] = raw_validation_grade_distribution
    if payload.get("ema_submit_count") is not None:
        result["ema_submit_count"] = round(builder_cls._safe_float(payload.get("ema_submit_count")), 4)
    if promotion_review_count > 0:
        result["promotion_review_count"] = promotion_review_count
        if promotion_review_status_counts:
            result["promotion_review_status_counts"] = promotion_review_status_counts
        if promotion_review_recommendation_counts:
            result["promotion_review_recommendation_counts"] = (
                promotion_review_recommendation_counts
            )
        if payload.get("promotion_review_score_total") is not None:
            result["promotion_review_score"] = round(
                builder_cls._safe_float(payload.get("promotion_review_score_total"))
                / max(promotion_review_count, 1),
                4,
            )
        if promotion_review_status:
            result["promotion_review_status"] = promotion_review_status
        if promotion_review_recommendation:
            result["promotion_review_recommendation"] = promotion_review_recommendation
    if target_pool_feedback:
        result["target_pool_feedback"] = target_pool_feedback
    if holding_bucket_feedback:
        result["holding_bucket_feedback"] = holding_bucket_feedback
    if generator_mode_feedback:
        result["generator_mode_feedback"] = generator_mode_feedback
    return result


def merge_feedback_bucket(
    builder_cls,
    base: Any,
    fresh: Any,
) -> dict[str, Any]:
    base_payload = dict(base or {})
    fresh_payload = dict(fresh or {})
    merged = dict(base_payload)
    merged.update(fresh_payload)
    if merged.get("ema_submit_count") is None and base_payload.get("ema_submit_count") is not None:
        merged["ema_submit_count"] = base_payload.get("ema_submit_count")
    for scope_name in (
        "target_pool_feedback",
        "holding_bucket_feedback",
        "generator_mode_feedback",
    ):
        base_scope = dict(base_payload.get(scope_name) or {})
        fresh_scope = dict(fresh_payload.get(scope_name) or {})
        if not base_scope and not fresh_scope:
            continue
        merged_scope: dict[str, Any] = {}
        for scope_key in set(base_scope) | set(fresh_scope):
            merged_scope[str(scope_key)] = merge_feedback_bucket(
                builder_cls,
                base_scope.get(scope_key),
                fresh_scope.get(scope_key),
            )
        merged[scope_name] = merged_scope
    base_review_count = int(base_payload.get("promotion_review_count") or 0)
    fresh_review_count = int(fresh_payload.get("promotion_review_count") or 0)
    merged_review_count = base_review_count + fresh_review_count
    if merged_review_count > 0:
        merged["promotion_review_count"] = merged_review_count
        merged_status_counts: dict[str, int] = {}
        for mapping in (
            base_payload.get("promotion_review_status_counts"),
            fresh_payload.get("promotion_review_status_counts"),
        ):
            for key, value in dict(mapping or {}).items():
                token = normalize_text(key)
                if not token:
                    continue
                merged_status_counts[token] = int(merged_status_counts.get(token) or 0) + int(
                    value or 0
                )
        if merged_status_counts:
            merged["promotion_review_status_counts"] = merged_status_counts
        merged_recommendation_counts: dict[str, int] = {}
        for mapping in (
            base_payload.get("promotion_review_recommendation_counts"),
            fresh_payload.get("promotion_review_recommendation_counts"),
        ):
            for key, value in dict(mapping or {}).items():
                token = normalize_text(key)
                if not token:
                    continue
                merged_recommendation_counts[token] = int(
                    merged_recommendation_counts.get(token) or 0
                ) + int(value or 0)
        if merged_recommendation_counts:
            merged["promotion_review_recommendation_counts"] = merged_recommendation_counts
        weighted_score_total = 0.0
        weighted_score_count = 0
        for payload_item, review_count in (
            (base_payload, base_review_count),
            (fresh_payload, fresh_review_count),
        ):
            if review_count <= 0 or payload_item.get("promotion_review_score") is None:
                continue
            weighted_score_total += builder_cls._safe_float(payload_item.get("promotion_review_score")) * review_count
            weighted_score_count += review_count
        if weighted_score_count > 0:
            merged["promotion_review_score"] = round(
                weighted_score_total / weighted_score_count,
                4,
            )
        review_status, review_recommendation = resolve_promotion_review_outcome(
            merged_status_counts,
            merged_recommendation_counts,
        )
        if review_status:
            merged["promotion_review_status"] = review_status
        if review_recommendation:
            merged["promotion_review_recommendation"] = review_recommendation
    return merged


def family_allocation_entropy(family_counts: dict[str, int]) -> float:
    total = sum(int(value or 0) for value in family_counts.values())
    if total <= 0:
        return 0.0
    entropy = 0.0
    for count in family_counts.values():
        ratio = float(count or 0) / float(total)
        if ratio > 0.0:
            entropy -= ratio * math.log(ratio)
    return round(entropy, 4)
