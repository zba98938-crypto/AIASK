"""策略工厂调度器实现。"""


from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import suppress
import inspect
import logging
import os
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..domain.constants import (
    AUTONOMY_MAX_RESEARCH_TASKS,
    AUTONOMY_MAX_BULK_RESEARCH_TASKS,
    AUTONOMY_RESERVED_BULK_RESEARCH_TASKS,
    AUTONOMY_CANDIDATES_PER_TASK,
    AUTONOMY_TASK_HARD_CAP,
    AUTONOMY_STARTUP_DELAY_SEC,
    FACTORY_DAILY_RUN_TIME,
    FACTORY_ERROR_BACKOFF_SEC,
    FACTORY_EVENT_RUNTIME_MODE,
    FACTORY_FACTOR_AUTO_REFRESH,
    FACTORY_FACTOR_REFRESH_TIMEOUT_SEC,
    FACTORY_MARKET_HOURS_INTERVAL_SEC,
    FACTORY_MAX_DAILY_RUNS,
    FACTORY_OFF_HOURS_INTERVAL_SEC,
    FACTORY_PRE_GATE_ENABLED,
    FACTORY_READINESS_HARD_BLOCK,
    FACTORY_READINESS_MIN_COMPLETION_RATIO,
    FACTORY_READINESS_MIN_SCORE,
    FACTORY_RUNTIME_ENABLED,
    FACTORY_SCHEDULE_MODE,
    FACTORY_STARTUP_WARMUP_ENABLED,
    FACTORY_STARTUP_WARMUP_FORCE,
    FACTORY_STARTUP_WARMUP_LIMIT,
    FACTORY_STARTUP_WARMUP_TASK_TYPE,
    RESEARCH_TASK_CONCURRENCY,
    STOCK_STRATEGY_MATRIX_BATCH_SIZE,
    STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY,
    STOCK_STRATEGY_MATRIX_ENABLED,
    STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK,
    STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK,
    STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN,
    STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN,
    STOCK_STRATEGY_MATRIX_RUN_WINDOW,
    STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD,
    STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
    is_factory_factor_auto_refresh_enabled,
    is_factory_readiness_hard_block_enabled,
    is_factory_runtime_enabled,
    resolve_event_runtime_mode,
)
from .cycle_runner import FactoryCycleRunner, FactoryRunContext
from .factory_execution import (
    FACTORY_ENGINE_VERSION,
    FactoryExecutionMode,
    build_artifact_refs,
    build_run_artifacts,
    build_shadow_parity_result,
    resolve_factory_engine_version,
    resolve_factory_execution_mode,
    resolve_runtime_mode_flags,
)
from .factory_market_views import build_portfolio_candidate_from_topn
from .runtime import (
    _call_optional_async as _runtime_call_optional_async,
    get_strategy_factory_package as _runtime_get_strategy_factory_package,
)
from .run_models import (
    FactoryRunStatus,
    StageStatus,
    build_stage_result,
    is_product_success_status,
    resolve_run_status,
    summarize_stage_results,
)
from ._budget_feedback import (
    CONTROL_MODE_SEVERITY,
    collect_generator_mode_feedback_controls,
    extract_feedback_root,
    normalize_text as _normalize_feedback_text,
    summarize_task_feedback_controls,
)
from ._autonomy_task_selection import (
    apply_scheduler_planning_controls as _apply_scheduler_planning_controls_payload,
    build_scan_only_task_budget_meta as _build_scan_only_task_budget_meta_payload,
    merge_autonomy_tasks_with_budget as _merge_autonomy_tasks_with_budget_payload,
)
from ._autonomy_task_executor import (
    AutonomyTaskExecutionContext as _AutonomyTaskExecutionContext,
    execute_autonomy_task as _execute_autonomy_task_payload,
)
from ._runtime_toggles import (
    strategy_factory_gate3_record_only_enabled as _gate3_record_only_enabled,
)
from ._autonomy_stage_artifacts import (
    attach_autonomy_stage_artifacts as _attach_autonomy_stage_artifacts_payload,
)
from ._autonomy_stage_summary import (
    build_autonomy_stage_summary as _build_autonomy_stage_summary_payload,
)
from ._bulk_planner_summary import (
    build_bulk_planner_error_report as _build_bulk_planner_error_report_payload,
    build_default_bulk_report as _build_default_bulk_report_payload,
    normalize_bulk_report_summary as _normalize_bulk_report_summary_payload,
)
from ._combined_scan_report import build_combined_scan_report as _build_combined_scan_report_payload
from ._bulk_cursor import (
    extract_bulk_stock_cursor as _extract_bulk_stock_cursor_payload,
    resolve_bulk_stock_matrix_cursor as _resolve_bulk_stock_matrix_cursor_payload,
)
from .utils import _extract_event_context as _local_extract_event_context
from .research.matrix import StockStrategyMatrixPlanner
from .research_plane_contract import (
    build_candidate_artifact,
    build_research_evidence_artifact,
    build_task_artifact,
)
from ..domain.targets import (
    _extract_target_codes_from_payload,
    _normalize_research_task_contract,
    _normalize_target_codes,
)
from ..infrastructure.mcp_services import get_autonomy_lifecycle_runtime, get_runtime_warmup_runner

if TYPE_CHECKING:
    from ..api.contracts import (
        AutonomyGateway,
        FactorResearchGateway,
        IncubationGateway,
        RiskGateway,
        ValidationGateway,
        VectorSearchGateway,
    )
    from ..infrastructure.runtime_adapters import RuntimeAdapters

logger = logging.getLogger(__name__)

_MARKET_TIMEZONE_NAME = str(os.getenv("STRATEGY_MARKET_TIMEZONE") or "Asia/Shanghai").strip() or "Asia/Shanghai"
try:
    _MARKET_TIMEZONE = ZoneInfo(_MARKET_TIMEZONE_NAME)
except Exception:
    _MARKET_TIMEZONE = timezone(timedelta(hours=8))

_FALLBACK_AUTONOMY_PHASE_ORDER = (
    "prepared",
    "generating",
    "reviewing",
    "recording",
    "submitting",
    "completed",
)


def _summarize_autonomy_lifecycle_fallback(lifecycle: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(lifecycle or {})
    return {
        "state": payload.get("state"),
        "current_phase": payload.get("current_phase"),
        "failed_phase": payload.get("failed_phase"),
        "terminal_phase": payload.get("terminal_phase"),
        "phase_status_counts": dict(payload.get("phase_status_counts") or {}),
        "completed_phase_count": int(payload.get("completed_phase_count") or 0),
        "event_count": int(payload.get("event_count") or len(payload.get("events") or [])),
        "phase_order": list(payload.get("phase_order") or _FALLBACK_AUTONOMY_PHASE_ORDER),
    }


def _load_autonomy_lifecycle_runtime():
    try:
        return get_autonomy_lifecycle_runtime()
    except Exception:
        return SimpleNamespace(
            AUTONOMY_PHASE_ORDER=_FALLBACK_AUTONOMY_PHASE_ORDER,
            summarize_autonomy_lifecycle=_summarize_autonomy_lifecycle_fallback,
        )


_AUTONOMY_LIFECYCLE_RUNTIME = _load_autonomy_lifecycle_runtime()
AUTONOMY_PHASE_ORDER = _AUTONOMY_LIFECYCLE_RUNTIME.AUTONOMY_PHASE_ORDER
summarize_autonomy_lifecycle = _AUTONOMY_LIFECYCLE_RUNTIME.summarize_autonomy_lifecycle

def _extract_event_context(*args, **kwargs):
    return _local_extract_event_context(*args, **kwargs)


def get_strategy_factory_package():
    return _runtime_get_strategy_factory_package()


async def _call_optional_async(target: Any, method_name: str, *args, default=None, **kwargs):
    return await _runtime_call_optional_async(target, method_name, *args, default=default, **kwargs)


# P3 (R7.3): timeout-source classification used by the LLM research task
# scheduler. Maps to one of three buckets:
#   - external_llm_timeout: external LLM gateway (network) failed to respond
#   - pipeline_stage_timeout: gateway responded, but a pipeline stage took
#     longer than its per-stage budget
#   - bulk_research_timeout: bulk_stock_matrix task hit the bulk timeout cap
class ResearchTaskTimeoutKind:
    EXTERNAL_LLM = "external_llm_timeout"
    PIPELINE_STAGE = "pipeline_stage_timeout"
    BULK_RESEARCH = "bulk_research_timeout"


_GATE3_WEAK_STAT_REASON_MARKERS = (
    "weak_wf_ic_ir",
    "weak_pkf_ic",
    "weak_bootstrap_ci_lower",
    "weak_param_sensitivity",
    "missing_statistical_metrics",
    "missing_wf_ic_ir",
    "missing_pkf_ic",
    "missing_bootstrap_ci_lower",
    "win_rate_",
    "purged_kfold_ic_",
    "walk_forward_ic_ir_",
    "bootstrap_ci_lower_",
)


def _scheduler_feedback_safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return int(default)


def _scheduler_feedback_safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _scheduler_feedback_family(value: Any) -> str:
    return str(value or "").strip().lower()


def _scheduler_feedback_is_weak_stat_reason(reason: Any) -> bool:
    token = str(reason or "").strip().lower()
    if not token:
        return False
    return any(marker in token for marker in _GATE3_WEAK_STAT_REASON_MARKERS)


def _scheduler_feedback_reason_topn(*reason_sources: Any, limit: int = 5) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for source in reason_sources:
        if isinstance(source, dict):
            reason = str(
                source.get("reason_code")
                or source.get("reason")
                or source.get("code")
                or ""
            ).strip()
            count = _scheduler_feedback_safe_int(source.get("count"), 1)
            if reason:
                counter[reason] += max(1, count)
            continue
        if isinstance(source, (list, tuple, set)):
            for item in source:
                if isinstance(item, dict):
                    reason = str(
                        item.get("reason_code")
                        or item.get("reason")
                        or item.get("code")
                        or ""
                    ).strip()
                    count = _scheduler_feedback_safe_int(item.get("count"), 1)
                else:
                    reason = str(item or "").strip()
                    count = 1
                if reason:
                    counter[reason] += max(1, count)
            continue
        reason = str(source or "").strip()
        if reason:
            counter[reason] += 1
    return [
        {"reason_code": reason, "count": int(count)}
        for reason, count in counter.most_common(max(1, int(limit or 5)))
    ]


def _scheduler_feedback_submission_payload(results: dict[str, Any]) -> dict[str, Any]:
    stages = dict((results or {}).get("stages") or {})
    return dict((results or {}).get("submit_result") or stages.get("submit") or {})


def _scheduler_feedback_submission_family(item: dict[str, Any]) -> str:
    payload = dict(item or {})
    family_summary = dict(payload.get("family_outcome_summary") or {})
    provenance = dict(payload.get("candidate_provenance") or {})
    params = dict(payload.get("params") or {})
    nested_provenance = dict(params.get("candidate_provenance") or {})
    return _scheduler_feedback_family(
        family_summary.get("candidate_family")
        or payload.get("candidate_family")
        or provenance.get("candidate_family")
        or nested_provenance.get("candidate_family")
        or payload.get("strategy_type")
        or family_summary.get("strategy_type")
    )


def _scheduler_feedback_submission_reasons(item: dict[str, Any]) -> list[str]:
    payload = dict(item or {})
    gate = dict(payload.get("gate_3") or {})
    reasons: list[str] = []
    for source in (
        payload.get("reason_codes"),
        payload.get("reasons"),
        payload.get("admission_block_reasons"),
        gate.get("reason_codes"),
        gate.get("reasons"),
        gate.get("admission_block_reasons"),
    ):
        if isinstance(source, (list, tuple, set)):
            for reason in source:
                token = str(reason or "").strip()
                if token:
                    reasons.append(token)
        else:
            token = str(source or "").strip()
            if token:
                reasons.append(token)
    return reasons


def _scheduler_feedback_family_observations(
    results: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = dict(results or {})
    submit = _scheduler_feedback_submission_payload(payload)
    summary = dict(payload.get("summary") or {})
    family_counts = dict(
        (submit.get("incubation_budget_summary") or {}).get("family_counts")
        or summary.get("incubation_budget_family_counts")
        or {}
    )
    topn = _scheduler_feedback_reason_topn(
        submit.get("gate_3_failure_reason_topn"),
        submit.get("gate_3_failure_topn"),
        summary.get("gate_3_failure_reason_topn"),
        summary.get("gate_3_failure_topn"),
    )
    total_input = _scheduler_feedback_safe_int(
        submit.get("gate_3_input"),
        _scheduler_feedback_safe_int(summary.get("gate_3_input")),
    )
    total_passed = _scheduler_feedback_safe_int(
        submit.get("gate_3_passed"),
        _scheduler_feedback_safe_int(summary.get("gate_3_passed")),
    )
    total_submitted = _scheduler_feedback_safe_int(
        submit.get("submitted"),
        _scheduler_feedback_safe_int(summary.get("submitted")),
    )
    total_failed = _scheduler_feedback_safe_int(
        submit.get("gate_3_failed"),
        max(total_input - total_passed, 0),
    )
    total_audit_only = _scheduler_feedback_safe_int(
        submit.get("created_audit_only"),
        _scheduler_feedback_safe_int(summary.get("created_audit_only")),
    )
    total_recorded = _scheduler_feedback_safe_int(
        submit.get("gate_3_recorded"),
        _scheduler_feedback_safe_int(summary.get("gate_3_recorded")),
    )
    observations: dict[str, dict[str, Any]] = {}

    for raw_item in list(submit.get("strategies") or []):
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item or {})
        family = _scheduler_feedback_submission_family(item)
        if not family:
            continue
        gate = dict(item.get("gate_3") or {})
        status = str(item.get("status") or "").strip().lower()
        passed = bool(item.get("passed") or gate.get("passed"))
        submitted = bool(item.get("submitted") or status in {"submitted", "incubating", "listed"})
        audit_only = bool(
            item.get("created_audit_only")
            or item.get("diagnostic_only")
            or status in {"diagnostic", "rejected"}
        )
        recorded = bool(item.get("gate_3_recorded") or item.get("gate3_record_only") or item.get("record_only"))
        reasons = _scheduler_feedback_submission_reasons(item)
        bucket = observations.setdefault(
            family,
            {
                "gate_3_input_count": 0,
                "gate_3_passed_count": 0,
                "submitted_count": 0,
                "gate_3_recorded_count": 0,
                "gate_3_failed_count": 0,
                "created_audit_only_count": 0,
                "weak_stat_failure_count": 0,
                "reasons": [],
            },
        )
        bucket["gate_3_input_count"] += 1
        if passed:
            bucket["gate_3_passed_count"] += 1
        else:
            bucket["gate_3_failed_count"] += 1
        if submitted:
            bucket["submitted_count"] += 1
        if recorded:
            bucket["gate_3_recorded_count"] += 1
        if audit_only:
            bucket["created_audit_only_count"] += 1
        if not passed and any(_scheduler_feedback_is_weak_stat_reason(reason) for reason in reasons):
            bucket["weak_stat_failure_count"] += 1
        bucket["reasons"].extend(reasons)

    if not observations and family_counts:
        family_total = sum(max(0, _scheduler_feedback_safe_int(value)) for value in family_counts.values())
        pass_ratio = (float(total_passed) / float(total_input)) if total_input > 0 else 0.0
        submitted_ratio = (float(total_submitted) / float(total_input)) if total_input > 0 else 0.0
        recorded_ratio = (float(total_recorded) / float(total_input)) if total_input > 0 else 0.0
        audit_ratio = (float(total_audit_only) / float(total_input)) if total_input > 0 else 0.0
        weak_topn_total = sum(
            int(item.get("count") or 0)
            for item in topn
            if _scheduler_feedback_is_weak_stat_reason(item.get("reason_code"))
        )
        weak_ratio = (float(weak_topn_total) / float(total_failed)) if total_failed > 0 else 0.0
        for family, raw_count in family_counts.items():
            token = _scheduler_feedback_family(family)
            input_count = max(0, _scheduler_feedback_safe_int(raw_count))
            if not token or input_count <= 0:
                continue
            passed_count = min(input_count, int(round(input_count * pass_ratio)))
            submitted_count = min(input_count, int(round(input_count * submitted_ratio)))
            recorded_count = min(input_count, int(round(input_count * recorded_ratio)))
            failed_count = max(input_count - passed_count, 0)
            observations[token] = {
                "gate_3_input_count": input_count,
                "gate_3_passed_count": passed_count,
                "submitted_count": submitted_count,
                "gate_3_recorded_count": recorded_count,
                "gate_3_failed_count": failed_count,
                "created_audit_only_count": min(input_count, int(round(input_count * audit_ratio))),
                "weak_stat_failure_count": min(failed_count, int(round(failed_count * weak_ratio))),
                "reasons": [
                    item.get("reason_code")
                    for item in topn
                    if item.get("reason_code")
                ],
                "fallback_from_family_counts": True,
                "family_total": family_total,
            }

    aggregate = {
        "gate_3_input": total_input,
        "gate_3_passed": total_passed,
        "gate_3_failed": total_failed,
        "submitted": total_submitted,
        "gate_3_recorded": total_recorded,
        "created_audit_only": total_audit_only,
        "gate_3_failure_reason_topn": topn,
        "weak_stat_failure_count": sum(
            int(item.get("count") or 0)
            for item in topn
            if _scheduler_feedback_is_weak_stat_reason(item.get("reason_code"))
        ),
    }
    return observations, aggregate


def update_scheduler_family_gate_feedback(
    existing_feedback: dict[str, dict[str, Any]] | None,
    results: dict[str, Any],
    *,
    cycle_count: int,
    alpha: float = 0.3,
    ema_floor: float = 0.15,
    exploration_reset_interval: int = 20,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    observations, aggregate = _scheduler_feedback_family_observations(results)
    feedback: dict[str, dict[str, Any]] = {
        _scheduler_feedback_family(family): dict(bucket or {})
        for family, bucket in dict(existing_feedback or {}).items()
        if _scheduler_feedback_family(family)
    }
    active_families = set(observations.keys())
    alpha = min(max(float(alpha), 0.01), 1.0)
    ema_floor = max(0.0, float(ema_floor))

    for family, obs in observations.items():
        previous = dict(feedback.get(family) or {})
        input_count = max(0, _scheduler_feedback_safe_int(obs.get("gate_3_input_count")))
        passed_count = max(0, _scheduler_feedback_safe_int(obs.get("gate_3_passed_count")))
        submitted_count = max(0, _scheduler_feedback_safe_int(obs.get("submitted_count")))
        recorded_count = max(0, _scheduler_feedback_safe_int(obs.get("gate_3_recorded_count")))
        failed_count = max(0, _scheduler_feedback_safe_int(obs.get("gate_3_failed_count")))
        audit_only_count = max(0, _scheduler_feedback_safe_int(obs.get("created_audit_only_count")))
        weak_count = max(0, _scheduler_feedback_safe_int(obs.get("weak_stat_failure_count")))
        pass_signal = float(max(passed_count, submitted_count))
        prev_submit_ema = _scheduler_feedback_safe_float(previous.get("ema_submit_count"))
        submit_ema = round(alpha * pass_signal + (1.0 - alpha) * prev_submit_ema, 4)
        prev_input_ema = _scheduler_feedback_safe_float(previous.get("gate_3_input_count_ema"))
        input_ema = round(alpha * float(input_count) + (1.0 - alpha) * prev_input_ema, 4)
        prev_pass_ema = _scheduler_feedback_safe_float(previous.get("gate_3_passed_count_ema"))
        pass_ema = round(alpha * float(passed_count) + (1.0 - alpha) * prev_pass_ema, 4)
        prev_fail_ema = _scheduler_feedback_safe_float(previous.get("gate_3_failed_count_ema"))
        fail_ema = round(alpha * float(failed_count) + (1.0 - alpha) * prev_fail_ema, 4)
        failure_rate = round(float(failed_count) / float(input_count), 4) if input_count > 0 else 0.0
        pass_rate = round(float(passed_count) / float(input_count), 4) if input_count > 0 else 1.0
        weak_rate = round(float(weak_count) / float(input_count), 4) if input_count > 0 else 0.0
        previous_streak = _scheduler_feedback_safe_int(previous.get("gate_3_failure_streak"))
        failure_streak = previous_streak + 1 if input_count > 0 and passed_count <= 0 and failed_count > 0 else 0
        cooldown_active = bool(previous.get("cooldown_active")) and failure_rate >= 0.55
        suppressed = bool(previous.get("suppressed")) and failure_rate >= 0.70
        freeze_active = bool(previous.get("freeze_active")) and failure_rate >= 0.85
        if input_count >= 2 and failure_rate >= 0.75:
            cooldown_active = True
        if (input_count >= 3 and failure_rate >= 0.95) or (failure_streak >= 2 and failure_rate >= 0.90):
            suppressed = True
        if failure_streak >= 3 and input_ema >= 8.0 and failure_rate >= 0.98:
            freeze_active = True
        if passed_count > 0:
            cooldown_active = failure_rate >= 0.75
            suppressed = failure_rate >= 0.95 and input_count >= 4
            freeze_active = False
        reasons = _scheduler_feedback_reason_topn(obs.get("reasons"), aggregate.get("gate_3_failure_reason_topn"))
        entry = {
            **previous,
            "ema_submit_count": submit_ema,
            "strategy_count": max(input_count, int(round(input_ema))),
            "gate_3_input_count": input_count,
            "gate_3_passed_count": passed_count,
            "submitted_count": submitted_count,
            "gate_3_recorded_count": recorded_count,
            "gate_3_failed_count": failed_count,
            "created_audit_only_count": audit_only_count,
            "weak_stat_failure_count": weak_count,
            "gate_3_input_count_ema": input_ema,
            "gate_3_passed_count_ema": pass_ema,
            "gate_3_failed_count_ema": fail_ema,
            "gate_failure_rate": failure_rate,
            "submission_gate_failure_rate": failure_rate,
            "weak_stat_failure_rate": weak_rate,
            "paper_hit_ratio": pass_rate,
            "promotion_ready_ratio": pass_rate,
            "raw_validation_c_rate": min(1.0, failure_rate),
            "raw_validation_d_rate": min(1.0, weak_rate if weak_count > 0 else failure_rate),
            "strict_incubation_ready_rate": pass_rate,
            "last_failure_reason_topn": reasons,
            "gate_3_failure_streak": failure_streak,
            "cooldown_active": cooldown_active,
            "suppressed": suppressed,
            "suppress_active": suppressed,
            "freeze_active": freeze_active,
        }
        feedback[family] = entry

    for family in list(feedback.keys()):
        if family in active_families:
            continue
        previous = dict(feedback.get(family) or {})
        prev_submit_ema = _scheduler_feedback_safe_float(previous.get("ema_submit_count"))
        previous["ema_submit_count"] = max(ema_floor, round((1.0 - alpha) * prev_submit_ema, 4))
        previous["gate_3_input_count_ema"] = round(
            (1.0 - alpha) * _scheduler_feedback_safe_float(previous.get("gate_3_input_count_ema")),
            4,
        )
        previous["gate_3_passed_count_ema"] = round(
            (1.0 - alpha) * _scheduler_feedback_safe_float(previous.get("gate_3_passed_count_ema")),
            4,
        )
        previous["gate_3_failed_count_ema"] = round(
            (1.0 - alpha) * _scheduler_feedback_safe_float(previous.get("gate_3_failed_count_ema")),
            4,
        )
        decayed_failure_rate = round(
            (1.0 - alpha) * _scheduler_feedback_safe_float(previous.get("gate_failure_rate")),
            4,
        )
        previous["gate_failure_rate"] = decayed_failure_rate
        previous["submission_gate_failure_rate"] = decayed_failure_rate
        if decayed_failure_rate < 0.55:
            previous["cooldown_active"] = False
            previous["suppressed"] = False
            previous["suppress_active"] = False
            previous["freeze_active"] = False
        feedback[family] = previous

    if exploration_reset_interval > 0 and cycle_count > 0 and cycle_count % exploration_reset_interval == 0:
        for data in feedback.values():
            if _scheduler_feedback_safe_float(data.get("ema_submit_count")) < ema_floor + 0.05:
                data["ema_submit_count"] = 0.5

    update_summary = {
        **aggregate,
        "updated_family_count": len(active_families),
        "tracked_family_count": len(feedback),
        "active_families": sorted(active_families),
        "control_counts": dict(
            Counter(
                "freeze"
                if bucket.get("freeze_active")
                else "suppress"
                if bucket.get("suppressed")
                else "cooldown"
                if bucket.get("cooldown_active")
                else "normal"
                for bucket in feedback.values()
            )
        ),
    }
    return feedback, update_summary


def _classify_research_task_timeout_kind(
    task: dict[str, Any] | None,
    *,
    base_timeout_sec: float,
    effective_timeout_sec: float | None,
) -> str:
    """Pick a ResearchTaskTimeoutKind based on the task and the actual
    timeout budget that fired.

    Heuristic (kept conservative — see design.md E4):
      - If the task is a bulk_stock_matrix task, classify as BULK_RESEARCH.
      - Else if the effective timeout matches the bulk timeout cap (>= 240s
        as a soft signal), still classify as BULK_RESEARCH.
      - Else if the effective timeout matches the external LLM cap
        (typically 120s), classify as EXTERNAL_LLM.
      - Else default to PIPELINE_STAGE.
    """
    payload = dict(task or {})
    task_source = str(payload.get("task_source") or "").strip().lower()
    if task_source == "bulk_stock_matrix":
        return ResearchTaskTimeoutKind.BULK_RESEARCH

    eff = float(effective_timeout_sec or 0.0)
    base = float(base_timeout_sec or 0.0)

    # The bulk timeout cap is typically >= 240s; if the effective timeout
    # is close to that, treat it as bulk.
    if eff >= 240.0 and eff > base:
        return ResearchTaskTimeoutKind.BULK_RESEARCH

    # External LLM cap is typically <= 180s. If the effective timeout
    # equals (or is bound by) the external cap and the task uses external
    # LLM, classify as EXTERNAL_LLM.
    if (
        not bool(payload.get("disable_external_llm"))
        and eff <= 180.0
        and (eff < base if base > 0 else True)
    ):
        return ResearchTaskTimeoutKind.EXTERNAL_LLM

    return ResearchTaskTimeoutKind.PIPELINE_STAGE


class _StrategyFactorySchedulerLoopMixin:
        @staticmethod
# --- _factory_scheduler_loop_parts/normalizers.py ---
        def _build_event_task_evidence_items(task: dict) -> List[dict]:
            event_context = _extract_event_context(task)
            task_key = str((task or {}).get("task_key") or (task or {}).get("task_id") or "").strip()
            event_id = str(event_context.get("event_id") or "").strip()
            if not task_key or not event_id:
                return []

            evidence_bundle = dict((task or {}).get("evidence_bundle") or {})
            score_summary = dict(event_context.get("score_summary") or {})
            theme_code = str(event_context.get("theme_code") or "").strip()
            supporting_reasons = list(event_context.get("supporting_reasons") or [])
            target_symbols = list(event_context.get("target_symbols") or [])
            symbol_details = {
                str((item or {}).get("code") or (item or {}).get("symbol") or "").strip(): dict(item or {})
                for item in list(evidence_bundle.get("symbol_details") or [])
                if str((item or {}).get("code") or (item or {}).get("symbol") or "").strip()
            }
            summary_weight = float(score_summary.get("avg_final_score") or 0.0)
            items: List[dict] = [
                {
                    "task_key": task_key,
                    "event_id": event_id,
                    "theme_code": theme_code,
                    "symbol": None,
                    "evidence_type": "event_theme_context",
                    "weight": summary_weight,
                    "evidence_payload": {**event_context, "snapshot_date": (task or {}).get("snapshot_date")},
                }
            ]
            for reason in supporting_reasons[:4]:
                items.append({
                    "task_key": task_key,
                    "event_id": event_id,
                    "theme_code": theme_code,
                    "symbol": None,
                    "evidence_type": "supporting_reason",
                    "weight": summary_weight,
                    "evidence_payload": {
                        "reason": reason,
                        "event_id": event_id,
                        "theme_code": theme_code,
                        "direction": event_context.get("direction"),
                        "horizon": event_context.get("horizon"),
                    },
                })
            for rank, symbol in enumerate(target_symbols[:5], 1):
                detail = symbol_details.get(symbol) or {}
                items.append({
                    "task_key": task_key,
                    "event_id": event_id,
                    "theme_code": theme_code,
                    "symbol": symbol,
                    "evidence_type": "target_symbol",
                    "weight": round(max(summary_weight - (rank - 1) * 0.05, 0.0), 4),
                    "evidence_payload": {
                        "symbol": symbol,
                        "rank": rank,
                        "event_id": event_id,
                        "theme_code": theme_code,
                        "event_summary": event_context.get("event_summary"),
                        "direction": event_context.get("direction"),
                        "horizon": event_context.get("horizon"),
                        "score_summary": score_summary,
                        "symbol_detail": detail,
                    },
                })
            return items

        @staticmethod
        def _compact_task_mapping(
            payload: Optional[dict[str, Any]],
            *,
            keys: tuple[str, ...],
        ) -> dict[str, Any]:
            source = dict(payload or {})
            result: dict[str, Any] = {}
            for key in keys:
                value = source.get(key)
                if value in (None, "", [], {}):
                    continue
                result[key] = value
            return result

        @staticmethod
        def _build_task_scan_artifact(
            report: Optional[dict[str, Any]],
            *,
            task_source_counts: Optional[dict[str, Any]] = None,
            event_task_count: Optional[int] = None,
            snapshot_task_count: Optional[int] = None,
            bulk_stock_task_count: Optional[int] = None,
        ) -> dict[str, Any]:
            payload = dict(report or {})
            summary = dict(payload.get("summary") or {})
            source_counts = dict(task_source_counts or summary.get("task_sources") or {})
            resolved_event_task_count = (
                int(event_task_count)
                if event_task_count is not None
                else int(summary.get("event_task_count") or 0)
            )
            resolved_snapshot_task_count = (
                int(snapshot_task_count)
                if snapshot_task_count is not None
                else int(source_counts.get("snapshot") or 0)
            )
            resolved_bulk_stock_task_count = (
                int(bulk_stock_task_count)
                if bulk_stock_task_count is not None
                else int(summary.get("bulk_stock_task_count") or source_counts.get("bulk_stock_matrix") or 0)
            )
            return build_task_artifact(
                {
                    "task_scan": payload,
                    "task_source_counts": source_counts,
                    "event_task_count": resolved_event_task_count,
                    "snapshot_task_count": resolved_snapshot_task_count,
                    "bulk_stock_task_count": resolved_bulk_stock_task_count,
                }
            )

        @staticmethod
        def _normalize_external_request_status(status: Any) -> str:
            return str(status or "").strip().lower() or "unknown"

        @classmethod
        def _summarize_external_request_status_counts(
            cls,
            requests: Optional[list[dict[str, Any]]],
        ) -> dict[str, int]:
            counts: dict[str, int] = {}
            for item in list(requests or []):
                status = cls._normalize_external_request_status(dict(item or {}).get("status"))
                counts[status] = counts.get(status, 0) + 1
            return counts

        @classmethod
        def _count_external_network_requests(
            cls,
            requests: Optional[list[dict[str, Any]]],
        ) -> int:
            total = 0
            for item in list(requests or []):
                payload = dict(item or {})
                status = cls._normalize_external_request_status(payload.get("status"))
                if status in {"compatibility_skip", "cooldown_skip"}:
                    continue
                metrics = dict(payload.get("request_metrics") or {})
                try:
                    attempt_count = int(metrics.get("attempt_count") or 0)
                except Exception:
                    attempt_count = 0
                total += max(attempt_count, 1)
            return total

        @classmethod
        def _count_external_real_requests(
            cls,
            requests: Optional[list[dict[str, Any]]],
        ) -> int:
            total = 0
            for item in list(requests or []):
                status = cls._normalize_external_request_status(dict(item or {}).get("status"))
                if status in {"compatibility_skip", "cooldown_skip"}:
                    continue
                total += 1
            return total

        @classmethod
        def _request_is_compatibility_failure(
            cls,
            request: Optional[dict[str, Any]],
        ) -> bool:
            payload = dict(request or {})
            status = cls._normalize_external_request_status(payload.get("status"))
            if status in {"compatibility_skip", "cooldown_skip"}:
                return False
            metrics = dict(payload.get("request_metrics") or {})
            metric_status = cls._normalize_external_request_status(metrics.get("status"))
            error_type = str(payload.get("error_type") or metrics.get("last_error_type") or "").strip().lower()
            error_text = str(payload.get("error") or metrics.get("last_error") or "").strip().lower()
            return (
                metric_status == "compatibility_failed"
                or error_type == "providercompatibilityerror"
                or "missing extractable content" in error_text
            )

        @classmethod
        def _request_is_empty_200_response(
            cls,
            request: Optional[dict[str, Any]],
        ) -> bool:
            payload = dict(request or {})
            metrics = dict(payload.get("request_metrics") or {})
            if bool(metrics.get("empty_200_response")):
                return True
            if not cls._request_is_compatibility_failure(payload):
                return False
            error_text = str(payload.get("error") or metrics.get("last_error") or "").strip().lower()
            return "missing extractable content" in error_text

        @classmethod
        def _summarize_task_llm_generation(cls, llm_generation: Optional[dict[str, Any]]) -> dict[str, Any]:
            payload = dict(llm_generation or {})
            external = dict(payload.get("external_provider") or {})
            requests = list(external.get("requests") or [])
            external_summary = cls._compact_task_mapping(
                external,
                keys=(
                    "enabled",
                    "provider",
                    "model",
                    "status",
                    "selected_count",
                    "viable_selected_count",
                    "fallback_count",
                    "elapsed_seconds",
                    "last_error_type",
                    "last_error",
                    "stage_attempt_count",
                    "network_request_count",
                    "real_request_count",
                    "compatibility_skip_count",
                    "cooldown_skip_count",
                    "compatibility_failure_count",
                    "compatibility_failure_ratio",
                    "effective_response_count",
                    "effective_response_ratio",
                    "empty_200_response_count",
                ),
            )
            request_status_counts = dict(external.get("request_status_counts") or {}) or cls._summarize_external_request_status_counts(requests)
            if requests:
                real_request_count = int(external.get("real_request_count") or cls._count_external_real_requests(requests))
                compatibility_failure_count = int(
                    external.get("compatibility_failure_count")
                    or sum(1 for item in requests if cls._request_is_compatibility_failure(item))
                )
                effective_response_count = int(
                    external.get("effective_response_count")
                    or sum(
                        1
                        for item in requests
                        if cls._normalize_external_request_status(dict(item or {}).get("status")) == "succeeded"
                    )
                )
                empty_200_response_count = int(
                    external.get("empty_200_response_count")
                    or sum(1 for item in requests if cls._request_is_empty_200_response(item))
                )
                external_summary["request_count"] = len(requests)
                external_summary["stage_attempt_count"] = int(external.get("stage_attempt_count") or len(requests))
                external_summary["network_request_count"] = int(
                    external.get("network_request_count") or cls._count_external_network_requests(requests)
                )
                external_summary["real_request_count"] = real_request_count
                external_summary["compatibility_skip_count"] = int(
                    external.get("compatibility_skip_count")
                    or request_status_counts.get("compatibility_skip", 0)
                )
                external_summary["cooldown_skip_count"] = int(
                    external.get("cooldown_skip_count")
                    or request_status_counts.get("cooldown_skip", 0)
                )
                external_summary["compatibility_failure_count"] = compatibility_failure_count
                external_summary["effective_response_count"] = effective_response_count
                external_summary["empty_200_response_count"] = empty_200_response_count
                external_summary["compatibility_failure_ratio"] = (
                    external.get("compatibility_failure_ratio")
                    if external.get("compatibility_failure_ratio") is not None
                    else (round(compatibility_failure_count / real_request_count, 4) if real_request_count else 0.0)
                )
                external_summary["effective_response_ratio"] = (
                    external.get("effective_response_ratio")
                    if external.get("effective_response_ratio") is not None
                    else (round(effective_response_count / real_request_count, 4) if real_request_count else 0.0)
                )
            elif request_status_counts:
                external_summary["request_count"] = int(external.get("stage_attempt_count") or 0)
            if external.get("request_limits"):
                external_summary["request_limits"] = list(external.get("request_limits") or [])[:4]
            if request_status_counts:
                external_summary["request_status_counts"] = request_status_counts
            if requests:
                external_summary["requests_preview"] = [
                    {
                        **cls._compact_task_mapping(
                            dict(item or {}),
                            keys=(
                                "request_index",
                                "request_limit",
                                "status",
                                "returned_candidate_count",
                                "compiled_candidate_count",
                                "non_executable_candidate_count",
                                "viable_candidate_count",
                                "open_dsl_candidate_count",
                                "open_dsl_compiled_candidate_count",
                                "open_dsl_viable_candidate_count",
                                "open_dsl_rejected_count",
                                "error_type",
                                "error",
                            ),
                        ),
                        "request_metrics": cls._compact_task_mapping(
                            dict((item or {}).get("request_metrics") or {}),
                            keys=(
                                "attempt_count",
                                "prompt_chars",
                                "response_chars",
                                "elapsed_seconds",
                                "last_error_type",
                                "last_error",
                            ),
                        ),
                    }
                    for item in requests[:3]
                ]
            analysis = dict(external.get("analysis") or {})
            if analysis:
                external_summary["analysis"] = cls._compact_task_mapping(
                    analysis,
                    keys=(
                        "style_bias",
                        "market_regime",
                        "theme",
                        "direction",
                        "risk_hint",
                        "confidence",
                    ),
                )
            summary = cls._compact_task_mapping(
                payload,
                keys=(
                    "requested_limit",
                    "market_frame_ready",
                    "market_frame_rows",
                    "market_frame_source",
                    "selected_count",
                    "pipeline_run_timeout_sec",
                    "pipeline_staged_fallback_reason",
                    "task_timeout_skip",
                    "task_timeout_sec",
                    "task_timeout_policy",
                    "requeue_recommended",
                ),
            )
            if payload.get("selected_generators"):
                summary["selected_generators"] = dict(payload.get("selected_generators") or {})
            if payload.get("pipeline_staged_fallback_counts"):
                summary["pipeline_staged_fallback_counts"] = dict(
                    payload.get("pipeline_staged_fallback_counts") or {}
                )
            if payload.get("pipeline_staged_stage_fallback_reasons"):
                summary["pipeline_staged_stage_fallback_reasons"] = dict(
                    payload.get("pipeline_staged_stage_fallback_reasons") or {}
                )
            if payload.get("pipeline_staged_invalid_output_stage_ids"):
                summary["pipeline_staged_invalid_output_stage_ids"] = list(
                    payload.get("pipeline_staged_invalid_output_stage_ids") or []
                )
            if payload.get("research_context_summary"):
                summary["research_context_summary"] = dict(payload.get("research_context_summary") or {})
            if external_summary:
                summary["external_provider"] = external_summary
            return {key: value for key, value in summary.items() if value not in (None, "", [], {})}

        @classmethod
        def _summarize_research_task_for_task_run(cls, task: Optional[dict[str, Any]]) -> dict[str, Any]:
            payload = _normalize_research_task_contract(task or {}) if task else {}
            summary = cls._compact_task_mapping(
                payload,
                keys=(
                    "task_id",
                    "task_key",
                    "task_source",
                    "opportunity_type",
                    "theme_code",
                    "event_id",
                    "candidate_family",
                    "factor_name",
                    "generation_limit",
                    "source_candidate_artifact_id",
                    "task_run_id",
                    "evidence_count",
                    "preference_strength",
                    "validation_focus",
                ),
            )
            target_symbols = list(payload.get("target_symbols") or [])
            if target_symbols:
                summary["target_symbols"] = target_symbols[:12]
            preferred_strategy_types = [
                str(item).strip()
                for item in list(
                    payload.get("preferred_strategy_types")
                    or payload.get("strategy_preferences")
                    or []
                )
                if str(item).strip()
            ]
            if preferred_strategy_types:
                summary["preferred_strategy_types"] = preferred_strategy_types[:6]
                summary["strategy_preferences"] = preferred_strategy_types[:6]
            allowed_strategy_types = [
                str(item).strip()
                for item in list(payload.get("allowed_strategy_types") or [])
                if str(item).strip()
            ]
            if allowed_strategy_types:
                summary["allowed_strategy_types"] = allowed_strategy_types[:6]
            evidence_refs = list(payload.get("evidence_refs") or [])
            if evidence_refs:
                summary["evidence_preview"] = [
                    cls._compact_task_mapping(
                        dict(item or {}),
                        keys=("id", "evidence_type", "symbol", "weight"),
                    )
                    for item in evidence_refs[:3]
                ]
            return summary

        @classmethod
        def _build_research_task_run_result_summary(cls, task_result: Optional[dict[str, Any]]) -> dict[str, Any]:
            payload = dict(task_result or {})
            summary = {
                "storage_mode": "summary_only",
                "task": cls._summarize_research_task_for_task_run(payload.get("task")),
                "task_run_id": payload.get("task_run_id"),
                "task_source": payload.get("task_source"),
                "event_id": payload.get("event_id"),
                "theme_code": payload.get("theme_code"),
                "evidence_count": int(payload.get("evidence_count") or 0),
                "status": payload.get("status"),
                "generated_count": int(payload.get("generated_count") or 0),
                "reviewed_count": int(payload.get("reviewed_count") or 0),
                "external_llm_status": payload.get("external_llm_status"),
                "llm_generation": cls._summarize_task_llm_generation(payload.get("llm_generation")),
                "lifecycle": dict(payload.get("lifecycle") or {}),
                "lifecycle_summary": dict(payload.get("lifecycle_summary") or {}),
            }
            if payload.get("error") not in (None, ""):
                summary["error"] = payload.get("error")
            return {key: value for key, value in summary.items() if value not in (None, "", [], {})}

        async def _persist_task_evidence(self, db, task: dict) -> List[dict]:
            saved_rows: List[dict] = []
            seen: set[tuple[str, str, str]] = set()
            for item in self._build_event_task_evidence_items(task):
                dedupe_key = (
                    str(item.get("task_key") or ""),
                    str(item.get("evidence_type") or ""),
                    str(item.get("symbol") or ""),
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                row = await _call_optional_async(db, "save_factory_task_evidence", item, default=None)
                if row is not None:
                    saved_rows.append(dict(row))
            return saved_rows

        @staticmethod
        def _is_market_hours(now: datetime) -> bool:
            """判断是否为 A 股盘中时间（工作日 9:30-15:00）。"""
            if now.weekday() >= 5:  # 周六日
                return False
            t = now.time()
            return time(9, 30) <= t < time(15, 0)

        @classmethod
        def _bulk_stock_matrix_run_window_state(cls, now: datetime) -> dict[str, Any]:
            current_period = "market_hours" if cls._is_market_hours(now) else "off_hours"
            run_window = str(STOCK_STRATEGY_MATRIX_RUN_WINDOW or "always").strip().lower() or "always"
            configured_enabled = bool(STOCK_STRATEGY_MATRIX_ENABLED)
            run_window_active = configured_enabled and (
                run_window == "always" or run_window == current_period
            )
            skip_reason = None
            if not configured_enabled:
                skip_reason = "disabled"
            elif not run_window_active:
                skip_reason = "outside_run_window"
            return {
                "configured_enabled": configured_enabled,
                "run_window": run_window,
                "run_window_active": run_window_active,
                "current_period": current_period,
                "skip_reason": skip_reason,
            }

        def _recent_submit_average(self, window: int = 5) -> float:
            """PR-S7: 近 N 轮孵化预算 family 提交计数的均值，用于自适应间隔。

            数据来源：``self.last_result.summary.incubation_budget_family_counts``。
            没有任何历史时返回 -1.0（表示"未知"，调用方应按默认间隔处理）。
            """
            last_result = getattr(self, "last_result", None)
            if not last_result:
                return -1.0
            summary = dict((last_result or {}).get("summary") or {})
            counts = summary.get("incubation_budget_family_counts") or {}
            if not counts:
                return 0.0
            total = sum(int(v or 0) for v in counts.values())
            return float(total)

        def _compute_next_wait(self, now: datetime) -> float:
            """根据调度模式和当前时间计算下一次运行的等待秒数。"""
            # 首次运行使用启动延迟
            if self._cycle_count == 0:
                return float(AUTONOMY_STARTUP_DELAY_SEC)

            if self.schedule_mode == "daily":
                target = datetime.combine(now.date(), self.run_time, tzinfo=self._market_timezone)
                if target <= now:
                    target += timedelta(days=1)
                # 优化 1：跳过非交易日
                max_skip = 15
                for _ in range(max_skip):
                    if self._is_trading_day(target):
                        break
                    target += timedelta(days=1)
                return (target - now).total_seconds()

            # continuous 模式
            # 优化 1：使用交易日历判断
            if not self._is_trading_day(now):
                # 非交易日（节假日/周末）使用更长间隔
                non_trading_interval = int(
                    os.getenv("STRATEGY_FACTORY_NON_TRADING_DAY_INTERVAL_SEC", "7200") or 7200
                )
                return float(non_trading_interval)
            if self._is_market_hours(now):
                base_interval = float(FACTORY_MARKET_HOURS_INTERVAL_SEC)
                # PR-S7: 自适应调整 —— 高产出缩短盘中间隔，低产出延长
                if os.getenv("STRATEGY_FACTORY_ADAPTIVE_INTERVAL_ENABLED", "1").strip().lower() not in ("0", "false", "no"):
                    recent_avg = self._recent_submit_average()
                    if recent_avg >= 0.0:
                        if recent_avg < 1.0:
                            return min(base_interval * 1.5, base_interval + 360.0)
                        if recent_avg > 5.0:
                            # 高产出最多缩到 75%，且不低于 120s 保护下游
                            return max(base_interval * 0.75, 120.0)
                return base_interval
            return float(FACTORY_OFF_HOURS_INTERVAL_SEC)
# --- _factory_scheduler_loop_parts/policy.py ---

        @classmethod
        def _extract_bulk_stock_cursor(
            cls,
            summary: Optional[dict[str, Any]],
            *,
            source: str,
            run_id: Optional[str] = None,
        ) -> dict[str, Any]:
            return _extract_bulk_stock_cursor_payload(
                summary,
                source=source,
                run_id=run_id,
            )

        async def _resolve_bulk_stock_matrix_cursor(self, db) -> dict[str, Any]:
            return await _resolve_bulk_stock_matrix_cursor_payload(
                last_result=self.last_result,
                db=db,
                logger_=logger,
                call_optional_async=_call_optional_async,
            )

        @staticmethod
        def _maintenance_env_bool(name: str, default: bool = False) -> bool:
            raw = str(os.getenv(name) or "").strip().lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        @staticmethod
        def _maintenance_seconds(name: str, default: int) -> int:
            try:
                return max(1, int(os.getenv(name, str(default)) or default))
            except Exception:
                return max(1, int(default))

        @staticmethod
        def _maintenance_due(last_at: Optional[datetime], now: datetime, interval_seconds: int) -> bool:
            if last_at is None:
                return True
            return (now - last_at).total_seconds() >= float(interval_seconds)

        async def _drain_event_outbox_maintenance(self, db, *, limit: int) -> dict[str, Any]:
            from .research.event_task_generator import generate_tasks_from_active_events

            generated = await generate_tasks_from_active_events(
                db,
                force_enabled=True,
                persist_lineage=False,
                event_limit=max(1, limit),
            )
            tasks = list(generated.get("tasks") or [])
            lineage_records = list(generated.get("lineage_records") or [])
            lineage_by_key = {
                str(item.get("dedupe_key") or "").strip(): dict(item or {})
                for item in lineage_records
                if str(item.get("dedupe_key") or "").strip()
            }
            processed = 0
            skipped = 0
            failed = 0
            for task in tasks[: max(1, limit)]:
                context = dict(task.get("event_context") or {})
                dedupe_key = str(context.get("dedupe_key") or "").strip()
                record = lineage_by_key.get(dedupe_key)
                if not dedupe_key or not record:
                    skipped += 1
                    continue
                try:
                    claim = await db.claim_event_outbox(
                        {
                            "dedupe_key": dedupe_key,
                            "source_event_id": record.get("event_id") or context.get("event_id"),
                            "theme_code": record.get("theme_code") or task.get("candidate_family"),
                            "event_type": context.get("event_type") or task.get("opportunity_type"),
                        }
                    )
                    if not claim.get("claimed"):
                        skipped += 1
                        continue
                    await db.upsert_event_task_lineage(record)
                    await db.mark_event_outbox_processed(dedupe_key)
                    processed += 1
                except Exception as exc:
                    failed += 1
                    with suppress(Exception):
                        await db.mark_event_outbox_failed(dedupe_key, error=str(exc))
            return {
                "status": "completed",
                "generated_task_count": len(tasks),
                "candidate_lineage_count": len(lineage_records),
                "processed": processed,
                "skipped": skipped,
                "failed": failed,
            }

        async def _run_event_theme_maintenance_if_due(self, db=None) -> dict[str, Any]:
            now = self._now()
            if self._is_market_hours(now):
                return {"status": "skipped", "reason": "market_hours"}

            seed_enabled = self._maintenance_env_bool(
                "STRATEGY_FACTORY_THEME_SEED_AUTO_ENABLED",
                True,
            )
            exposure_enabled = self._maintenance_env_bool(
                "STRATEGY_FACTORY_THEME_EXPOSURE_AUTO_REFRESH_ENABLED",
                True,
            )
            outbox_enabled = self._maintenance_env_bool(
                "STRATEGY_FACTORY_EVENT_OUTBOX_AUTO_DRAIN_ENABLED",
                True,
            )
            regression_enabled = self._maintenance_env_bool(
                "STRATEGY_FACTORY_THEME_REGRESSION_AUTO_RUN_ENABLED",
                True,
            )
            if not (seed_enabled or exposure_enabled or outbox_enabled or regression_enabled):
                return {"status": "disabled"}

            resolved_db = self._load_db() if db is None else db
            results: dict[str, Any] = {
                "status": "completed",
                "ran": [],
                "skipped": [],
                "errors": [],
            }

            if seed_enabled:
                interval = self._maintenance_seconds(
                    "STRATEGY_FACTORY_THEME_SEED_INTERVAL_SEC",
                    86400,
                )
                if self._maintenance_due(getattr(self, "_last_theme_seed_at", None), now, interval):
                    try:
                        from .research.theme_seed import seed_default_theme_graph

                        results["theme_seed"] = await seed_default_theme_graph(
                            resolved_db,
                            overwrite=False,
                            updated_by="scheduler",
                        )
                        self._last_theme_seed_at = now
                        results["ran"].append("theme_seed")
                    except Exception as exc:
                        results["errors"].append({"step": "theme_seed", "error": str(exc)})
                else:
                    results["skipped"].append("theme_seed_not_due")

            if exposure_enabled:
                interval = self._maintenance_seconds(
                    "STRATEGY_FACTORY_THEME_EXPOSURE_REFRESH_INTERVAL_SEC",
                    86400,
                )
                if self._maintenance_due(getattr(self, "_last_theme_exposure_refresh_at", None), now, interval):
                    try:
                        from .research.theme_exposure_builder import ThemeExposureBuilder

                        batch_size = max(1, min(int(os.getenv("STRATEGY_FACTORY_THEME_EXPOSURE_BATCH_SIZE", "1000") or 1000), 10000))
                        results["theme_exposure_refresh"] = await ThemeExposureBuilder(
                            batch_size=batch_size,
                        ).build(resolved_db, batch_size=batch_size)
                        self._last_theme_exposure_refresh_at = now
                        results["ran"].append("theme_exposure_refresh")
                    except Exception as exc:
                        results["errors"].append({"step": "theme_exposure_refresh", "error": str(exc)})
                else:
                    results["skipped"].append("theme_exposure_refresh_not_due")

            if outbox_enabled:
                interval = self._maintenance_seconds(
                    "STRATEGY_FACTORY_EVENT_OUTBOX_DRAIN_INTERVAL_SEC",
                    1800,
                )
                if self._maintenance_due(getattr(self, "_last_event_outbox_drain_at", None), now, interval):
                    try:
                        limit = max(1, min(int(os.getenv("STRATEGY_FACTORY_EVENT_OUTBOX_DRAIN_LIMIT", "10") or 10), 100))
                        results["outbox_drain"] = await self._drain_event_outbox_maintenance(resolved_db, limit=limit)
                        self._last_event_outbox_drain_at = now
                        results["ran"].append("outbox_drain")
                    except Exception as exc:
                        results["errors"].append({"step": "outbox_drain", "error": str(exc)})
                else:
                    results["skipped"].append("outbox_drain_not_due")

            if regression_enabled:
                interval = self._maintenance_seconds(
                    "STRATEGY_FACTORY_THEME_REGRESSION_INTERVAL_SEC",
                    604800,
                )
                if self._maintenance_due(getattr(self, "_last_theme_regression_run_at", None), now, interval):
                    try:
                        from .research.theme_response_regression import ThemeResponseRegression

                        results["theme_regression_run"] = await ThemeResponseRegression().run_full_update(resolved_db)
                        self._last_theme_regression_run_at = now
                        results["ran"].append("theme_regression_run")
                    except Exception as exc:
                        results["errors"].append({"step": "theme_regression_run", "error": str(exc)})
                else:
                    results["skipped"].append("theme_regression_not_due")

            if results["errors"]:
                results["status"] = "partial"
            self._last_event_theme_maintenance_result = results
            return results

        async def _loop(self):
            while self._running:
                try:
                    now = self._now()
                    today_str = now.strftime("%Y-%m-%d")

                    # 日期变更 → 重置每日计数
                    if self._daily_run_date != today_str:
                        self._daily_run_date = today_str
                        self._daily_run_count = 0

                    # 优化 2：断路器状态机（CLOSED → OPEN → HALF_OPEN → CLOSED）
                    if self._circuit_state == "open":
                        if self._circuit_open_until is not None and now >= self._circuit_open_until:
                            # 冷却期结束 → 进入 HALF_OPEN 探测
                            self._circuit_state = "half_open"
                            self._metrics.record_half_open_probe()
                            logger.info(
                                "StrategyFactory: circuit breaker → HALF_OPEN, probing recovery..."
                            )
                        else:
                            sleep_sec = (self._circuit_open_until - now).total_seconds() if self._circuit_open_until else 300
                            logger.warning(
                                "StrategyFactory: circuit breaker OPEN, sleeping %.0fs until %s",
                                sleep_sec, (self._circuit_open_until or "unknown"),
                            )
                            await asyncio.sleep(min(sleep_sec + 1, 300))
                            continue

                    if self._circuit_state == "half_open":
                        # HALF_OPEN：执行一次探测
                        try:
                            self._metrics.record_cycle_start()
                            probe_result = await self.run_once()
                            if not is_product_success_status(probe_result.get("status")):
                                raise RuntimeError(
                                    "strategy factory probe returned non-success status: "
                                    f"{probe_result.get('status') or 'missing'}"
                                )
                            # 探测成功 → 完全恢复
                            self._circuit_state = "closed"
                            self._consecutive_failures = 0
                            self._circuit_open_until = None
                            self._circuit_open_backoff_sec = self._circuit_open_backoff_sec_initial
                            self._daily_run_count += 1
                            self._metrics.record_cycle_success(
                                (self._now() - now).total_seconds(), self._now()
                            )
                            logger.info("StrategyFactory: circuit breaker HALF_OPEN → CLOSED (probe succeeded)")
                        except Exception as probe_exc:
                            # 探测失败 → 重新打开，指数退避
                            self._circuit_open_backoff_sec = min(
                                self._circuit_open_backoff_sec * 2, 7200
                            )
                            self._circuit_state = "open"
                            self._circuit_open_until = self._now() + timedelta(
                                seconds=self._circuit_open_backoff_sec
                            )
                            self._metrics.record_cycle_failure(probe_exc, self._now())
                            logger.warning(
                                "StrategyFactory: HALF_OPEN probe failed, circuit → OPEN (backoff %ds): %s",
                                self._circuit_open_backoff_sec, probe_exc,
                            )
                        continue

                    # 达到每日上限 → 睡到午夜
                    if self._daily_run_count >= self.max_daily_runs:
                        tomorrow = datetime.combine(now.date() + timedelta(days=1), time(0, 0), tzinfo=self._market_timezone)
                        sleep_sec = (tomorrow - now).total_seconds() + 1
                        logger.info(
                            "StrategyFactory: daily limit reached (%d/%d), sleeping %.0fs until midnight",
                            self._daily_run_count, self.max_daily_runs, sleep_sec,
                        )
                        await asyncio.sleep(sleep_sec)
                        continue

                    wait = self._compute_next_wait(now)
                    logger.info(
                        "StrategyFactory [%s]: cycle #%d, today %d/%d runs, next in %.0fs",
                        self.schedule_mode, self._cycle_count, self._daily_run_count,
                        self.max_daily_runs, wait,
                    )
                    await asyncio.sleep(wait)

                    if self._running:
                        self._metrics.record_cycle_start()
                        cycle_start = self._now()
                        await self._run_event_theme_maintenance_if_due()
                        cycle_result = await self.run_once()
                        self._daily_run_count += 1
                        if not is_product_success_status(cycle_result.get("status")):
                            raise RuntimeError(
                                "strategy factory cycle returned non-success status: "
                                f"{cycle_result.get('status') or 'missing'}"
                            )
                        # 成功 → 重置断路器
                        self._consecutive_failures = 0
                        self._circuit_open_until = None
                        self._circuit_state = "closed"
                        self._metrics.record_cycle_success(
                            (self._now() - cycle_start).total_seconds(), self._now()
                        )
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    self._consecutive_failures += 1
                    self._metrics.record_cycle_failure(exc, self._now())
                    logger.error(
                        "StrategyFactory loop error (#%d consecutive): %s",
                        self._consecutive_failures, exc, exc_info=True,
                    )
                    if self._consecutive_failures >= self._max_consecutive_failures:
                        self._circuit_state = "open"
                        self._circuit_open_until = self._now() + timedelta(
                            seconds=self._circuit_open_backoff_sec
                        )
                        self._metrics.record_circuit_trip()
                        logger.critical(
                            "StrategyFactory: circuit breaker OPEN after %d consecutive failures, "
                            "will retry at %s",
                            self._consecutive_failures,
                            self._circuit_open_until.isoformat(),
                        )
                    # 优化 6：指数退避 + 抖动
                    await asyncio.sleep(self._compute_error_backoff())

        async def _generate_for_research_task(self, autonomy_gateway, db, snapshot: dict, task: dict) -> dict:
            effective_task = dict(task or {})
            provider_health = self._external_llm_health_snapshot(autonomy_gateway)
            if provider_health.get("scheduler_should_disable"):
                effective_task["disable_external_llm"] = True
                effective_task["external_llm_skip_reason"] = (
                    provider_health.get("scheduler_skip_reason") or "provider_health_blocked"
                )
            limit = max(
                1,
                min(int(effective_task.get("generation_limit") or AUTONOMY_CANDIDATES_PER_TASK), AUTONOMY_TASK_HARD_CAP),
            )
            source = f"strategy_factory:{effective_task.get('opportunity_type') or 'general'}"
            gateway_db = self._adapt_gateway_repository(db)
            task_id = str(effective_task.get("task_id") or effective_task.get("task_key") or source).strip() or source
            base_timeout_sec = self._resolve_research_task_timeout_sec()

            async def _run_generation(task_payload: dict[str, Any], *, local_fallback: bool = False) -> dict:
                timeout_sec = self._resolve_effective_research_task_timeout_sec(
                    autonomy_gateway,
                    task_payload,
                    base_timeout=base_timeout_sec,
                    local_fallback=local_fallback,
                )
                return await asyncio.wait_for(
                    autonomy_gateway.generate_factory_candidates(
                        gateway_db,
                        snapshot,
                        limit=limit,
                        research_task=task_payload,
                        source=source,
                    ),
                    timeout=timeout_sec,
                )

            try:
                return await _run_generation(effective_task)
            except asyncio.TimeoutError as exc:
                initial_timeout_sec = self._resolve_effective_research_task_timeout_sec(
                    autonomy_gateway,
                    effective_task,
                    base_timeout=base_timeout_sec,
                )
                # P3 (R7.3): classify the timeout source so dashboards can
                # tell apart external LLM gateway timeouts (network) from
                # pipeline-stage timeouts (LLM responded but a stage took
                # too long) from bulk-research timeouts (long-tail bulk
                # task hit the bulk timeout cap).
                timeout_kind = _classify_research_task_timeout_kind(
                    effective_task,
                    base_timeout_sec=base_timeout_sec,
                    effective_timeout_sec=initial_timeout_sec,
                )
                logger.warning(
                    "StrategyFactory: research task %s timed out after %.1fs "
                    "(kind=%s); skipping generation without local fallback",
                    task_id,
                    float(initial_timeout_sec or 0.0),
                    timeout_kind,
                )
                timeout_lifecycle = {
                    "state": "skipped",
                    "current_phase": "generating",
                    "failed_phase": None,
                    "terminal_phase": "skipped_timeout",
                    "phase_order": ["prepare", "generate", "review", "complete"],
                    "phase_status_counts": {"skipped": 1},
                    "completed_phase_count": 0,
                    "event_count": 1,
                    "events": [
                        {
                            "phase": "generating",
                            "status": "skipped",
                            "reason": "ResearchTaskTimeout",
                            "timeout_sec": round(float(initial_timeout_sec or 0.0), 4),
                        }
                    ],
                }
                return {
                    "status": "skipped_timeout",
                    "generated_count": 0,
                    "reviewed_count": 0,
                    "generation": {"count": 0, "candidates": []},
                    "candidates": [],
                    "experiments": [],
                    "llm_generation": {
                        "requested_limit": limit,
                        "selected_count": 0,
                        "research_task": dict(effective_task),
                        "task_timeout_skip": True,
                        "task_timeout_sec": round(float(initial_timeout_sec or 0.0), 4),
                        "task_timeout_policy": "skip_without_local_fallback",
                        "task_timeout_kind": timeout_kind,
                        "requeue_recommended": True,
                        "external_provider": {
                            "enabled": bool(not effective_task.get("disable_external_llm")),
                            "status": "skipped_timeout",
                            "requests": [],
                            "selected_count": 0,
                            "viable_selected_count": 0,
                            "fallback_count": 0,
                            "last_error_type": "ResearchTaskTimeout",
                            "last_error": f"research task {task_id} exceeded {initial_timeout_sec:g}s",
                            "elapsed_seconds": round(float(initial_timeout_sec or 0.0), 4),
                        },
                    },
                    "lifecycle": timeout_lifecycle,
                }

        async def _persist_full_market_topn(
            self,
            db,
            results: dict[str, Any],
            *,
            persistence_failures: list[dict[str, Any]],
        ) -> None:
            topn_payload = dict(results.get("full_market_topn") or {})
            score_rows = [
                dict(item or {})
                for item in list(results.get("_full_market_score_rows") or [])
                if isinstance(item, dict)
            ]
            run_id = str(results.get("run_id") or "").strip()
            trace_id = str(results.get("trace_id") or "").strip() or None
            if not topn_payload or not run_id:
                return

            try:
                portfolio_candidate = build_portfolio_candidate_from_topn(
                    topn_payload,
                    run_id=run_id,
                    trace_id=trace_id,
                )
            except Exception as exc:
                logger.warning(
                    "StrategyFactory: failed to build full-market Top N portfolio candidate for run %s: %s",
                    run_id,
                    exc,
                )
                self._record_persistence_failure(
                    persistence_failures,
                    "build_portfolio_candidate_from_topn",
                    exc,
                    stage="full_market_topn",
                )
                portfolio_candidate = None

            enriched_snapshot = {
                **topn_payload,
                "run_id": run_id,
                "trace_id": trace_id,
                "correlation_id": str(topn_payload.get("correlation_id") or run_id).strip() or run_id,
                "source_action": "factory_run_once",
            }
            if portfolio_candidate is not None:
                enriched_snapshot["portfolio_candidate_id"] = portfolio_candidate.get("id")

            if (
                portfolio_candidate is not None
                and hasattr(db, "save_strategy")
                and not _gate3_record_only_enabled()
            ):
                try:
                    await db.save_strategy(portfolio_candidate)
                except Exception as exc:
                    logger.warning(
                        "StrategyFactory: failed to persist Top N portfolio candidate for run %s: %s",
                        run_id,
                        exc,
                    )
                    self._record_persistence_failure(
                        persistence_failures,
                        "save_strategy:topn_portfolio_candidate",
                        exc,
                        stage="full_market_topn",
                    )

            if hasattr(db, "save_strategy_factory_topn_snapshot"):
                try:
                    saved_snapshot = await db.save_strategy_factory_topn_snapshot(enriched_snapshot)
                    if isinstance(saved_snapshot, dict):
                        enriched_snapshot = {**enriched_snapshot, **saved_snapshot}
                except Exception as exc:
                    logger.warning(
                        "StrategyFactory: failed to persist Top N snapshot for run %s: %s",
                        run_id,
                        exc,
                    )
                    self._record_persistence_failure(
                        persistence_failures,
                        "save_strategy_factory_topn_snapshot",
                        exc,
                        stage="full_market_topn",
                    )

            if score_rows and hasattr(db, "replace_strategy_factory_full_market_scores"):
                try:
                    await db.replace_strategy_factory_full_market_scores(
                        run_id=run_id,
                        snapshot_id=str(enriched_snapshot.get("snapshot_id") or "").strip() or f"fmt_{run_id}",
                        as_of_date=enriched_snapshot.get("as_of_date"),
                        trace_id=trace_id,
                        correlation_id=enriched_snapshot.get("correlation_id"),
                        rows=score_rows,
                    )
                except Exception as exc:
                    logger.warning(
                        "StrategyFactory: failed to persist full-market scores for run %s: %s",
                        run_id,
                        exc,
                    )
                    self._record_persistence_failure(
                        persistence_failures,
                        "replace_strategy_factory_full_market_scores",
                        exc,
                        stage="full_market_topn",
                    )

            results["full_market_topn"] = enriched_snapshot
            if isinstance(results.get("summary"), dict):
                results["summary"]["full_market_topn"] = enriched_snapshot

        async def _persist_run_result(
            self,
            db,
            results: dict[str, Any],
            *,
            persistence_failures: list[dict[str, Any]],
        ) -> None:
            await self._persist_run_artifacts(
                db,
                results,
                persistence_failures=persistence_failures,
            )
            await self._persist_full_market_topn(
                db,
                results,
                persistence_failures=persistence_failures,
            )
            self._apply_run_audit(results, persistence_failures=persistence_failures)
            if not hasattr(db, "save_strategy_factory_run"):
                return
            try:
                await db.save_strategy_factory_run(results)
            except Exception as exc:
                logger.warning("StrategyFactory: failed to persist run %s: %s", results.get("run_id"), exc)
                self._record_persistence_failure(
                    persistence_failures,
                    "save_strategy_factory_run",
                    exc,
                    stage="run",
                )
                self._apply_run_audit(results, persistence_failures=persistence_failures)

        async def _persist_run_status_marker(
            self,
            db,
            *,
            run_id: str,
            status: str,
            started_at,
            execution_mode: str,
            engine_version: str,
            trace_id: str,
            parity_role: str,
            read_only: bool,
            completed_at=None,
            elapsed_seconds: float | None = None,
            error: str | None = None,
            reason: str | None = None,
        ) -> None:
            if read_only or not hasattr(db, "save_strategy_factory_run"):
                return
            payload = {
                "run_id": str(run_id or "").strip(),
                "trace_id": str(trace_id or "").strip() or None,
                "started_at": started_at.isoformat() if hasattr(started_at, "isoformat") else started_at,
                "completed_at": completed_at.isoformat() if hasattr(completed_at, "isoformat") else completed_at,
                "elapsed_seconds": float(elapsed_seconds or 0.0),
                "status": str(status or "unknown").strip() or "unknown",
                "execution_mode": str(execution_mode or "").strip() or "legacy_primary",
                "engine_version": str(engine_version or "").strip() or FACTORY_ENGINE_VERSION,
                "summary": {
                    "trace_id": str(trace_id or "").strip() or None,
                    "run_status_marker": str(status or "unknown").strip() or "unknown",
                    "parity_role": str(parity_role or "primary").strip() or "primary",
                    "read_only": bool(read_only),
                    "reason": str(reason or "").strip() or None,
                },
                "stages": {},
                "snapshot_summary": {},
                "artifact_refs": [],
                "parity_result": {},
                "error": error,
            }
            if not payload["run_id"]:
                return
            try:
                await db.save_strategy_factory_run(payload)
            except Exception as exc:
                logger.warning(
                    "StrategyFactory: failed to persist run status marker %s for %s: %s",
                    payload.get("status"),
                    payload.get("run_id"),
                    exc,
                )

        async def _persist_run_artifacts(
            self,
            db,
            results: dict[str, Any],
            *,
            persistence_failures: list[dict[str, Any]],
        ) -> None:
            artifacts = [
                dict(item or {})
                for item in list(results.get("artifacts") or [])
                if isinstance(item, dict)
            ]
            if not artifacts:
                return
            if not hasattr(db, "save_strategy_factory_run_artifact"):
                results["artifact_refs"] = build_artifact_refs(artifacts)
                return
            persisted: list[dict[str, Any]] = []
            run_id = str(results.get("run_id") or "").strip()
            for artifact in artifacts:
                try:
                    row = await _call_optional_async(
                        db,
                        "save_strategy_factory_run_artifact",
                        {
                            **artifact,
                            "run_id": run_id,
                        },
                        default=None,
                    )
                except Exception as exc:
                    logger.warning(
                        "StrategyFactory: failed to persist artifact %s for run %s: %s",
                        artifact.get("artifact_type"),
                        run_id,
                        exc,
                    )
                    self._record_persistence_failure(
                        persistence_failures,
                        f"save_strategy_factory_run_artifact:{artifact.get('artifact_type')}",
                        exc,
                        stage="artifact",
                    )
                    continue
                if row is not None:
                    persisted.append(dict(row))
            results["artifact_refs"] = build_artifact_refs(persisted or artifacts)
# --- _factory_scheduler_loop_parts/policy_execution.py ---
        async def _execute_factory_cycle_once(
            self,
            resolved_db,
            *,
            previous_result: Optional[dict[str, Any]],
            execution_mode: FactoryExecutionMode | str,
            parity_role: str = "primary",
            read_only: bool = False,
            run_id: Optional[str] = None,
            trace_id: Optional[str] = None,
            target_codes: Optional[list[str]] = None,
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            start = self._now()
            resolved_mode = resolve_factory_execution_mode(
                execution_mode,
                default=self.execution_mode,
            )
            effective_run_id = (
                str(run_id or "").strip()
                or f"factory_run_{int(start.timestamp())}_{uuid4().hex[:8]}"
            )
            effective_trace_id = (
                str(trace_id or "").strip()
                or f"strategy_factory:{uuid4().hex[:12]}"
            )
            if parity_role and parity_role != "primary":
                effective_trace_id = f"{effective_trace_id}:{parity_role}"
            effective_engine_version = resolve_factory_engine_version(
                resolved_mode,
                default=str(getattr(self, "engine_version", None) or FACTORY_ENGINE_VERSION),
            )
            context = FactoryRunContext(
                db=resolved_db,
                factory_pkg=get_strategy_factory_package(),
                runtime_adapters=self._runtime_adapters,
                start=start,
                trace_id=effective_trace_id,
                run_id=effective_run_id,
                execution_mode=resolved_mode.value,
                engine_version=effective_engine_version,
                parity_role=str(parity_role or "primary"),
                read_only=bool(read_only),
                target_codes=_normalize_target_codes(target_codes or [], limit=64),
            )
            await self._persist_run_status_marker(
                resolved_db,
                run_id=effective_run_id,
                status="running",
                started_at=start,
                execution_mode=resolved_mode.value,
                engine_version=effective_engine_version,
                trace_id=effective_trace_id,
                parity_role=str(parity_role or "primary"),
                read_only=bool(read_only),
                reason="cycle_started",
            )
            try:
                if resolved_mode == FactoryExecutionMode.V2_PRIMARY:
                    # V2_PRIMARY is deprecated: it runs the same FactoryCycleRunner.
                    # Kept as alias for forward-compat; no separate engine.
                    from . import factory_scheduler as scheduler_module

                    outcome = await scheduler_module.FactoryCycleRunner(self, context).run()
                else:
                    from . import factory_scheduler as scheduler_module

                    outcome = await scheduler_module.FactoryCycleRunner(self, context).run()
            except asyncio.CancelledError:
                completed = self._now()
                await self._persist_run_status_marker(
                    resolved_db,
                    run_id=effective_run_id,
                    status="interrupted",
                    started_at=start,
                    completed_at=completed,
                    elapsed_seconds=round((completed - start).total_seconds(), 1),
                    execution_mode=resolved_mode.value,
                    engine_version=effective_engine_version,
                    trace_id=effective_trace_id,
                    parity_role=str(parity_role or "primary"),
                    read_only=bool(read_only),
                    error="StrategyFactory run_once cancelled before completion",
                    reason="cancelled",
                )
                raise
            except Exception as exc:
                completed = self._now()
                await self._persist_run_status_marker(
                    resolved_db,
                    run_id=effective_run_id,
                    status="failed",
                    started_at=start,
                    completed_at=completed,
                    elapsed_seconds=round((completed - start).total_seconds(), 1),
                    execution_mode=resolved_mode.value,
                    engine_version=effective_engine_version,
                    trace_id=effective_trace_id,
                    parity_role=str(parity_role or "primary"),
                    read_only=bool(read_only),
                    error=str(exc),
                    reason=exc.__class__.__name__,
                )
                raise
            results = dict(outcome.result or {})
            self._attach_runtime_governance(results, previous_result=previous_result)
            return results, list(outcome.persistence_failures or [])

        async def _execute_factory_run_once_mode(
            self,
            resolved_db,
            *,
            previous_result: Optional[dict[str, Any]],
            execution_mode: FactoryExecutionMode | str,
            target_codes: Optional[list[str]] = None,
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            resolved_mode = resolve_factory_execution_mode(
                execution_mode,
                default=self.execution_mode,
            )
            primary_result, persistence_failures = await self._execute_factory_cycle_once(
                resolved_db,
                previous_result=previous_result,
                execution_mode=resolved_mode,
                parity_role="primary",
                read_only=False,
                target_codes=target_codes,
            )
            def _set_paper_trading_cycle(payload: dict[str, Any]) -> None:
                primary_result["paper_trading_cycle"] = payload
                if isinstance(primary_result.get("summary"), dict):
                    primary_result["summary"]["paper_trading_cycle"] = payload

            if resolved_mode == FactoryExecutionMode.SHADOW_READONLY:
                _set_paper_trading_cycle({
                    "status": "skipped",
                    "skipped": True,
                    "reason": "shadow_readonly",
                    "write_path": "paper_trading_cycle",
                })
            elif _gate3_record_only_enabled():
                _set_paper_trading_cycle({
                    "status": "skipped",
                    "skipped": True,
                    "reason": "gate3_record_only",
                    "write_path": "paper_trading_cycle",
                })
            else:
                # PR-D1: 策略工厂 cycle 完成后，执行模拟盘日循环
                try:
                    from strategy_factory.application.research.paper_trading_scheduler import run_paper_trading_cycle
                    paper_timeout_sec = max(
                        1,
                        int(os.getenv("STRATEGY_FACTORY_PAPER_TRADING_CYCLE_TIMEOUT_SEC", "300") or 300),
                    )
                    logger.info(
                        "StrategyFactory: starting paper trading post-cycle timeout=%ss",
                        paper_timeout_sec,
                    )
                    paper_result = await asyncio.wait_for(
                        run_paper_trading_cycle(resolved_db),
                        timeout=paper_timeout_sec,
                    )
                    _set_paper_trading_cycle(paper_result)
                except asyncio.TimeoutError:
                    logger.warning(
                        "StrategyFactory: paper trading post-cycle timed out after %ss",
                        paper_timeout_sec,
                    )
                    _set_paper_trading_cycle({
                        "status": "timeout",
                        "error": f"paper trading cycle exceeded {paper_timeout_sec}s",
                    })
                except Exception as _paper_exc:
                    _set_paper_trading_cycle({"status": "failed", "error": str(_paper_exc)})
            if resolved_mode != FactoryExecutionMode.SHADOW_READONLY:
                return primary_result, persistence_failures

            shadow_run_id = f"{str(primary_result.get('run_id') or '').strip()}__shadow"
            shadow_trace_id = str(primary_result.get("trace_id") or "").strip()
            shadow_result: Optional[dict[str, Any]] = None
            try:
                shadow_result, shadow_failures = await self._execute_factory_cycle_once(
                    resolved_db,
                    previous_result=previous_result,
                    execution_mode=resolved_mode,
                    parity_role="shadow",
                    read_only=True,
                    run_id=shadow_run_id,
                    trace_id=shadow_trace_id,
                    target_codes=target_codes,
                )
                persistence_failures.extend(shadow_failures)
                parity_result = build_shadow_parity_result(primary_result, shadow_result)
                primary_result["shadow_run"] = {
                    "run_id": shadow_result.get("run_id"),
                    "trace_id": shadow_result.get("trace_id"),
                    "status": shadow_result.get("status"),
                    "summary": dict(shadow_result.get("summary") or {}),
                    "read_only": True,
                }
            except Exception as exc:
                logger.warning("StrategyFactory: shadow readonly execution failed: %s", exc, exc_info=True)
                parity_result = {
                    "comparison_contract_version": "strategy_factory.shadow_parity.v1",
                    "status": "shadow_failed",
                    "mismatch_count": 1,
                    "mismatches": ["shadow_run_error"],
                    "comparisons": {},
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }
            primary_result["parity_result"] = parity_result
            summary = dict(primary_result.get("summary") or {})
            summary["parity_status"] = parity_result.get("status")
            summary["shadow_readonly_enabled"] = True
            summary["shadow_run_id"] = shadow_run_id
            primary_result["summary"] = summary
            primary_result["artifacts"] = build_run_artifacts(primary_result)
            primary_result["artifact_refs"] = build_artifact_refs(primary_result.get("artifacts") or [])
            return primary_result, persistence_failures

        async def _prepare_shared_generation_context(self, autonomy_gateway, db, snapshot: dict[str, Any]) -> bool:
            autonomy_target = getattr(autonomy_gateway, "raw", autonomy_gateway)
            generation_service = getattr(autonomy_target, "generation_service", None)
            builder = getattr(generation_service, "build_shared_generation_context", None)
            if not callable(builder):
                return False
            try:
                snapshot["_shared_generation_context"] = await _call_optional_async(
                    generation_service,
                    "build_shared_generation_context",
                    db,
                    snapshot=snapshot,
                    default={},
                )
                return bool(snapshot.get("_shared_generation_context"))
            except Exception as exc:
                logger.warning("StrategyFactory: shared generation context preload failed: %s", exc)
                return False

        @staticmethod
        def _get_external_llm_provider(autonomy_gateway):
            autonomy_target = getattr(autonomy_gateway, "raw", autonomy_gateway)
            generation_service = getattr(autonomy_target, "generation_service", None)
            llm_generator = getattr(generation_service, "llm_generator", None) or getattr(autonomy_target, "llm_generator", None)
            return getattr(llm_generator, "external_provider", None)

        @classmethod
        def _external_llm_health_snapshot(cls, autonomy_gateway) -> dict[str, Any]:
            external_provider = cls._get_external_llm_provider(autonomy_gateway)
            if external_provider is None:
                return {}
            snapshot_getter = getattr(external_provider, "get_health_snapshot", None)
            if not callable(snapshot_getter):
                return {}
            try:
                snapshot = snapshot_getter()
            except Exception:
                return {}
            return dict(snapshot or {})

        @classmethod
        def _external_llm_should_participate(cls, autonomy_gateway) -> bool:
            external_provider = cls._get_external_llm_provider(autonomy_gateway)
            if external_provider is None:
                return False
            try:
                if callable(getattr(external_provider, "is_enabled", None)) and not external_provider.is_enabled():
                    return False
            except Exception as _swallowed:
                logger.warning("packages/strategy-factory/src/strategy_factory/application/_factory_scheduler_loop.py:2177: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                return False
            return not bool(cls._external_llm_health_snapshot(autonomy_gateway).get("scheduler_should_disable"))

        @classmethod
        def _resolve_external_llm_concurrency_limit(cls, autonomy_gateway) -> Optional[int]:
            external_provider = cls._get_external_llm_provider(autonomy_gateway)
            if external_provider is None or not cls._external_llm_should_participate(autonomy_gateway):
                return None
            try:
                limit = int(getattr(getattr(external_provider, "config", None), "max_concurrency", 0) or 0)
            except Exception as _swallowed:
                logger.warning("packages/strategy-factory/src/strategy_factory/application/_factory_scheduler_loop.py:2188: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                return None
            return max(1, limit) if limit > 0 else None

        @staticmethod
        def _env_bool(*names: str, default: bool) -> bool:
            for name in names:
                raw = os.getenv(str(name or "").strip())
                if raw is None:
                    continue
                text = str(raw).strip().lower()
                if text in {"1", "true", "yes", "y", "on"}:
                    return True
                if text in {"0", "false", "no", "n", "off"}:
                    return False
            return bool(default)

        @classmethod
        def _bulk_tasks_use_external_llm(cls, autonomy_gateway) -> bool:
            if not cls._external_llm_should_participate(autonomy_gateway):
                return False
            autonomy_target = getattr(autonomy_gateway, "raw", autonomy_gateway)
            generation_service = getattr(autonomy_target, "generation_service", None)
            resolver = getattr(generation_service, "_bulk_llm_enabled", None)
            if callable(resolver):
                try:
                    return bool(resolver())
                except Exception as _swallowed:
                    logger.warning("packages/strategy-factory/src/strategy_factory/application/_factory_scheduler_loop.py:2215: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                    pass
            return cls._env_bool(
                "STRATEGY_FACTORY_BULK_LLM_ENABLED",
                "STRATEGY_FACTORY_BULK_STOCK_MATRIX_LLM_ENABLED",
                default=False,
            )

        @classmethod
        def _research_task_uses_external_llm(cls, autonomy_gateway, task: dict[str, Any] | None) -> bool:
            payload = dict(task or {})
            if payload.get("disable_external_llm"):
                return False
            if not cls._external_llm_should_participate(autonomy_gateway):
                return False
            task_source = str(payload.get("task_source") or "").strip().lower()
            if task_source == "bulk_stock_matrix":
                return cls._bulk_tasks_use_external_llm(autonomy_gateway)
            return True

        @classmethod
        def _resolve_research_task_concurrency(cls, autonomy_gateway, *, has_bulk_tasks: bool = False) -> int:
            effective = RESEARCH_TASK_CONCURRENCY
            provider_limit = cls._resolve_external_llm_concurrency_limit(autonomy_gateway)
            if provider_limit is not None:
                effective = min(effective, provider_limit)
            if has_bulk_tasks and cls._bulk_tasks_use_external_llm(autonomy_gateway):
                bulk_target = max(1, int(STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY))
                if provider_limit is not None:
                    bulk_target = min(bulk_target, provider_limit)
                effective = max(effective, bulk_target)
            return max(1, effective)

        @classmethod
        def _resolve_bulk_research_task_concurrency(cls, autonomy_gateway, *, has_bulk_tasks: bool = False) -> int:
            if not has_bulk_tasks:
                return cls._resolve_research_task_concurrency(autonomy_gateway, has_bulk_tasks=False)
            if cls._bulk_tasks_use_external_llm(autonomy_gateway):
                return cls._resolve_research_task_concurrency(autonomy_gateway, has_bulk_tasks=True)
            return max(1, int(STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY))

        @staticmethod
        def _resolve_research_task_timeout_sec() -> float:
            raw = str(os.getenv("STRATEGY_FACTORY_RESEARCH_TASK_TIMEOUT_SEC", "180") or "180").strip()
            try:
                value = float(raw)
            except Exception:
                value = 180.0
            return max(15.0, min(value, 1800.0))

        @staticmethod
        def _research_task_uses_bulk_timeout(task: dict[str, Any] | None) -> bool:
            payload = dict(task or {})
            task_source = str(payload.get("task_source") or "").strip().lower()
            task_id = str(payload.get("task_id") or payload.get("task_key") or "").strip().lower()
            opportunity_type = str(payload.get("opportunity_type") or "").strip().lower()
            return (
                task_source in {"bulk_stock_matrix", "stock_strategy_matrix", "matrix"}
                or "bulk_matrix" in task_id
                or "bulk_stock_matrix" in task_id
                or opportunity_type in {"bulk_stock_matrix", "stock_strategy_matrix"}
            )

        @staticmethod
        def _resolve_run_once_timeout_sec() -> float:
            raw = str(os.getenv("STRATEGY_FACTORY_RUN_ONCE_TIMEOUT_SEC", "1800") or "1800").strip()
            try:
                value = float(raw)
            except Exception:
                value = 1800.0
            return max(60.0, min(value, 7200.0))

        @staticmethod
        def _resolve_external_research_task_timeout_cap_sec() -> float:
            # PR-R2: 默认从 20s 提到 120s。LLM 网关 P95 ~30s，fan-out 4 次 + staged pipeline
            # 多阶段需要 60–120s 才能完成。20s 导致每轮 4–8 个 task 被 timeout 降级。
            raw = str(
                os.getenv("STRATEGY_FACTORY_EXTERNAL_RESEARCH_TASK_TIMEOUT_SEC", "120")
                or "120"
            ).strip()
            try:
                value = float(raw)
            except Exception:
                value = 120.0
            return max(0.001, min(value, 600.0))

        @staticmethod
        def _resolve_bulk_research_task_timeout_sec() -> float:
            raw = str(os.getenv("STRATEGY_FACTORY_BULK_RESEARCH_TASK_TIMEOUT_SEC", "360") or "360").strip()
            try:
                value = float(raw)
            except Exception:
                value = 360.0
            return max(60.0, min(value, 1800.0))

        @staticmethod
        def _resolve_local_fallback_research_task_timeout_cap_sec() -> float:
            raw = str(
                os.getenv("STRATEGY_FACTORY_LOCAL_FALLBACK_TASK_TIMEOUT_SEC", "10")
                or "10"
            ).strip()
            try:
                value = float(raw)
            except Exception:
                value = 10.0
            return max(0.001, min(value, 120.0))
# --- _factory_scheduler_loop_parts/evaluation.py ---

        @classmethod
        def _resolve_effective_research_task_timeout_sec(
            cls,
            autonomy_gateway,
            task: dict[str, Any] | None,
            *,
            base_timeout: float,
            local_fallback: bool = False,
        ) -> float:
            effective_base_timeout = float(base_timeout or 0.0)
            if cls._research_task_uses_bulk_timeout(task):
                effective_base_timeout = max(
                    effective_base_timeout,
                    cls._resolve_bulk_research_task_timeout_sec(),
                )
            if not cls._research_task_uses_external_llm(autonomy_gateway, task):
                if local_fallback:
                    return min(
                        effective_base_timeout,
                        cls._resolve_local_fallback_research_task_timeout_cap_sec(),
                    )
                return effective_base_timeout

            external_cap = cls._resolve_external_research_task_timeout_cap_sec()
            if cls._research_task_uses_bulk_timeout(task):
                external_cap = max(external_cap, cls._resolve_bulk_research_task_timeout_sec())
            external_timeout = min(effective_base_timeout, external_cap)
            if not local_fallback:
                return external_timeout
            return min(
                external_timeout,
                cls._resolve_local_fallback_research_task_timeout_cap_sec(),
            )

        @staticmethod
        def _control_mode_from_severity(severity: int) -> str:
            normalized = max(0, int(severity or 0))
            if normalized >= 3:
                return "freeze"
            if normalized >= 2:
                return "suppress"
            if normalized >= 1:
                return "cooldown"
            return "normal"

        async def _load_recent_factory_run_summaries(
            self,
            db,
            *,
            limit: int = 4,
        ) -> list[dict[str, Any]]:
            summaries: list[dict[str, Any]] = []
            seen: set[str] = set()

            def _append(entry: Optional[dict[str, Any]]) -> None:
                payload = dict(entry or {})
                summary = dict(payload.get("summary") or {})
                if not summary:
                    return
                run_id = str(payload.get("run_id") or summary.get("run_id") or "").strip()
                marker = run_id or str(summary.get("trace_id") or summary.get("completed_at") or len(summaries))
                if marker in seen:
                    return
                seen.add(marker)
                summaries.append(summary)

            _append(dict(self.last_result or {}))
            fetch_limit = max(1, int(limit or 1))
            if hasattr(db, "list_strategy_factory_runs"):
                try:
                    rows = await _call_optional_async(
                        db,
                        "list_strategy_factory_runs",
                        fetch_limit,
                        default=[],
                    )
                except Exception:
                    rows = []
                for row in list(rows or []):
                    _append(dict(row or {}))
                    if len(summaries) >= fetch_limit:
                        break
            elif hasattr(db, "get_latest_strategy_factory_run"):
                try:
                    latest = await _call_optional_async(db, "get_latest_strategy_factory_run", default=None)
                except Exception:
                    latest = None
                _append(dict(latest or {}))
            return summaries[:fetch_limit]

        @classmethod
        def _build_external_provider_control(
            cls,
            recent_summaries: list[dict[str, Any]],
            provider_health: Optional[dict[str, Any]] = None,
        ) -> dict[str, Any]:
            summaries = [dict(item or {}) for item in list(recent_summaries or []) if isinstance(item, dict)]
            health = dict(provider_health or {})
            active_attempt_run_count = 0
            zero_attempt_run_streak = 0
            zero_attempt_streak_open = True
            for item in summaries:
                attempt_count = int(
                    item.get("external_llm_stage_attempt_count") or item.get("external_llm_attempt_count") or 0
                )
                real_request_count = int(item.get("external_llm_real_request_count") or 0)
                if attempt_count > 0 or real_request_count > 0:
                    active_attempt_run_count += 1
                    zero_attempt_streak_open = False
                    continue
                if zero_attempt_streak_open:
                    zero_attempt_run_streak += 1
            stage_attempt_count = sum(
                int(item.get("external_llm_stage_attempt_count") or item.get("external_llm_attempt_count") or 0)
                for item in summaries
            )
            real_request_count = sum(int(item.get("external_llm_real_request_count") or 0) for item in summaries)
            compatibility_skip_count = sum(int(item.get("external_llm_compatibility_skip_count") or 0) for item in summaries)
            compatibility_failure_count = sum(
                int(item.get("external_llm_compatibility_failure_count") or 0) for item in summaries
            )
            effective_response_count = sum(
                int(item.get("external_llm_effective_response_count") or 0) for item in summaries
            )
            empty_200_response_count = sum(
                int(item.get("external_llm_empty_200_response_count") or 0) for item in summaries
            )
            compatibility_skip_ratio = (
                round(compatibility_skip_count / stage_attempt_count, 4) if stage_attempt_count else 0.0
            )
            compatibility_failure_ratio = (
                round(compatibility_failure_count / real_request_count, 4) if real_request_count else 0.0
            )
            effective_response_ratio = (
                round(effective_response_count / real_request_count, 4) if real_request_count else 0.0
            )
            empty_200_response_ratio = (
                round(empty_200_response_count / real_request_count, 4) if real_request_count else 0.0
            )

            severity = 0
            reasons: list[str] = []
            health_status = _normalize_feedback_text(health.get("health_status"))
            if bool(health.get("scheduler_should_disable")) or health_status in {"failed", "error"}:
                severity = max(severity, 2)
                reasons.append(
                    str(health.get("scheduler_skip_reason") or health_status or "provider_health_blocked")
                )
            elif health_status in {"degraded", "warning"} or bool(health.get("compatibility_cooldown_active")):
                severity = max(severity, 1)
                reasons.append("provider_health_degraded")

            suppress_recovery_probe_recommended = False
            suppress_recovery_probe_reason = None
            if stage_attempt_count >= 3 and compatibility_skip_ratio >= 0.75:
                skip_severity = 2
                if (
                    real_request_count <= 0
                    and compatibility_failure_count <= 0
                    and active_attempt_run_count <= 1
                    and zero_attempt_run_streak >= 1
                    and not bool(health.get("scheduler_should_disable"))
                ):
                    skip_severity = 1
                    suppress_recovery_probe_recommended = True
                    suppress_recovery_probe_reason = "skip_only_history_without_recent_probe"
                severity = max(severity, skip_severity)
                reasons.append("compatibility_skip_ratio_too_high")
            elif stage_attempt_count >= 2 and compatibility_skip_ratio >= 0.4:
                severity = max(severity, 1)
                reasons.append("compatibility_skip_ratio_elevated")

            if real_request_count >= 2 and (
                effective_response_ratio <= 0.15
                or compatibility_failure_ratio >= 0.65
                or empty_200_response_ratio >= 0.5
            ):
                severity = max(severity, 2)
                reasons.append("effective_response_ratio_too_low")
            elif real_request_count >= 2 and (
                effective_response_ratio < 0.45
                or compatibility_failure_ratio >= 0.35
                or empty_200_response_ratio >= 0.25
            ):
                severity = max(severity, 1)
                reasons.append("effective_response_ratio_degraded")

            deduped_reasons: list[str] = []
            for reason in reasons:
                token = str(reason or "").strip()
                if token and token not in deduped_reasons:
                    deduped_reasons.append(token)

            control_mode = cls._control_mode_from_severity(severity)
            return {
                "control_mode": control_mode,
                "control_reasons": deduped_reasons,
                "health_status": health_status or None,
                "stage_attempt_count": stage_attempt_count,
                "real_request_count": real_request_count,
                "compatibility_skip_count": compatibility_skip_count,
                "compatibility_skip_ratio": compatibility_skip_ratio,
                "compatibility_failure_count": compatibility_failure_count,
                "compatibility_failure_ratio": compatibility_failure_ratio,
                "effective_response_count": effective_response_count,
                "effective_response_ratio": effective_response_ratio,
                "empty_200_response_count": empty_200_response_count,
                "empty_200_response_ratio": empty_200_response_ratio,
                "active_attempt_run_count": active_attempt_run_count,
                "zero_attempt_run_streak": zero_attempt_run_streak,
                "suppress_recovery_probe_recommended": suppress_recovery_probe_recommended,
                "suppress_recovery_probe_reason": suppress_recovery_probe_reason,
                "scheduler_should_disable": bool(health.get("scheduler_should_disable")),
                "scheduler_skip_reason": health.get("scheduler_skip_reason"),
            }

        @classmethod
        def _build_generator_mode_controls(
            cls,
            recent_summaries: list[dict[str, Any]],
            *,
            feedback_root: Any = None,
        ) -> dict[str, dict[str, Any]]:
            history_by_mode: dict[str, list[dict[str, Any]]] = {}
            for item in list(recent_summaries or []):
                summary = dict(item or {})
                mode_metrics = dict(summary.get("generator_mode_submission_metrics") or {})
                for mode_name, raw_metrics in mode_metrics.items():
                    normalized_mode = _normalize_feedback_text(mode_name)
                    if not normalized_mode:
                        continue
                    history_by_mode.setdefault(normalized_mode, []).append(dict(raw_metrics or {}))

            controls: dict[str, dict[str, Any]] = {}
            for mode_name in ("external_llm", "pipeline_staged", "rl_bandit"):
                history = history_by_mode.get(mode_name) or []
                stagnant_runs = 0
                last_metrics: dict[str, Any] = {}
                for metrics in history:
                    last_metrics = dict(metrics or {})
                    strategy_count = int(last_metrics.get("strategy_count") or 0)
                    if strategy_count <= 0:
                        break
                    created_total_count = int(last_metrics.get("created_total_count") or 0)
                    refresh_absorption_ratio = float(last_metrics.get("refresh_absorption_ratio") or 0.0)
                    tested_object_hash_changed_count = int(
                        last_metrics.get("tested_object_hash_changed_count") or 0
                    )
                    if (
                        created_total_count == 0
                        and refresh_absorption_ratio >= 0.6
                        and tested_object_hash_changed_count <= 0
                    ):
                        stagnant_runs += 1
                        continue
                    break
                if stagnant_runs <= 0:
                    continue
                severity = 2 if stagnant_runs >= 3 else 1
                controls[mode_name] = {
                    "control_mode": cls._control_mode_from_severity(severity),
                    "stagnant_runs": stagnant_runs,
                    "control_reasons": ["refresh_absorption_without_creation"],
                    "metrics": last_metrics,
                }
            feedback_controls = collect_generator_mode_feedback_controls(feedback_root)
            for mode_name, incoming_control in feedback_controls.items():
                normalized_mode = _normalize_feedback_text(mode_name)
                if not normalized_mode:
                    continue
                existing = dict(controls.get(normalized_mode) or {})
                existing_mode = _normalize_feedback_text(existing.get("control_mode")) or "normal"
                incoming_mode = _normalize_feedback_text(incoming_control.get("control_mode")) or "normal"
                existing_severity = CONTROL_MODE_SEVERITY.get(existing_mode, 0)
                incoming_severity = CONTROL_MODE_SEVERITY.get(incoming_mode, 0)
                merged_reasons: list[str] = []
                for reason in [
                    *list(existing.get("control_reasons") or []),
                    *list(incoming_control.get("control_reasons") or []),
                ]:
                    token = str(reason or "").strip()
                    if token and token not in merged_reasons:
                        merged_reasons.append(token)
                merged_sources: list[str] = []
                for source in [existing.get("source"), incoming_control.get("source")]:
                    token = str(source or "").strip()
                    if token and token not in merged_sources:
                        merged_sources.append(token)
                merged_families: list[str] = []
                for family in [
                    *list(existing.get("families") or []),
                    *list(incoming_control.get("families") or []),
                ]:
                    token = _normalize_feedback_text(family)
                    if token and token not in merged_families:
                        merged_families.append(token)
                winner = dict(existing or {})
                if incoming_severity >= existing_severity:
                    winner.update(dict(incoming_control or {}))
                winner["control_mode"] = (
                    incoming_mode if incoming_severity >= existing_severity else existing_mode
                ) or "normal"
                winner["control_reasons"] = merged_reasons
                winner["source"] = merged_sources[0] if len(merged_sources) == 1 else merged_sources
                winner["families"] = merged_families
                winner["feedback_observed_count"] = int(
                    incoming_control.get("feedback_observed_count")
                    or existing.get("feedback_observed_count")
                    or 0
                )
                controls[normalized_mode] = winner
            return controls

        @classmethod
        def _apply_scheduler_planning_controls(
            cls,
            tasks: list[dict[str, Any]],
            *,
            feedback_root: Any,
            provider_control: Optional[dict[str, Any]] = None,
            generator_mode_controls: Optional[dict[str, dict[str, Any]]] = None,
        ) -> list[dict[str, Any]]:
            return _apply_scheduler_planning_controls_payload(
                list(tasks or []),
                feedback_root=feedback_root,
                provider_control=provider_control or {},
                generator_mode_controls=generator_mode_controls or {},
            )

        @classmethod
        def _merge_autonomy_tasks_with_budget(
            cls,
            scanner,
            scan_tasks: list[dict[str, Any]],
            bulk_tasks: list[dict[str, Any]],
        ) -> tuple[list[dict[str, Any]], dict[str, int]]:
            return _merge_autonomy_tasks_with_budget_payload(
                scanner,
                scan_tasks,
                bulk_tasks,
            )
# --- _factory_scheduler_loop_parts/reporting.py ---

        async def _run_autonomy_batches(self, db, snapshot: dict) -> dict:
            factory_pkg = get_strategy_factory_package()
            scanner = factory_pkg.MarketOpportunityScanner()
            scan_report = await scanner.scan(db, snapshot)
            autonomy_gateway = self._get_autonomy_gateway()
            factor_research = dict(snapshot.get("factor_research") or {})
            feedback_root = extract_feedback_root(
                factor_research.get("lifecycle_feedback_input")
                or factor_research.get("budget_feedback")
                or {}
            )
            recent_run_summaries = await self._load_recent_factory_run_summaries(db, limit=4)
            external_provider_health = self._external_llm_health_snapshot(autonomy_gateway)
            external_provider_control = self._build_external_provider_control(
                recent_run_summaries,
                external_provider_health,
            )
            generator_mode_controls = self._build_generator_mode_controls(
                recent_run_summaries,
                feedback_root=feedback_root,
            )
            scan_tasks = self._apply_scheduler_planning_controls(
                list(scan_report.get("tasks") or []),
                feedback_root=feedback_root,
                provider_control=external_provider_control,
                generator_mode_controls=generator_mode_controls,
            )
            tasks = list(scan_tasks)
            scan_summary = dict(scan_report.get("summary") or {})
            scan_feedback_summary = summarize_task_feedback_controls(scan_tasks)
            scan_summary.update(
                {
                    "feedback_control_mode_counts": dict(scan_feedback_summary.get("feedback_control_mode_counts") or {}),
                    "feedback_legacy_control_mode_counts": dict(
                        scan_feedback_summary.get("feedback_legacy_control_mode_counts")
                        or scan_feedback_summary.get("feedback_control_mode_counts")
                        or {}
                    ),
                    "feedback_skill_control_mode_counts": dict(
                        scan_feedback_summary.get("feedback_skill_control_mode_counts") or {}
                    ),
                    "feedback_target_pool_control_mode_counts": dict(
                        scan_feedback_summary.get("feedback_target_pool_control_mode_counts") or {}
                    ),
                    "feedback_holding_bucket_control_mode_counts": dict(
                        scan_feedback_summary.get("feedback_holding_bucket_control_mode_counts") or {}
                    ),
                    "feedback_generator_mode_control_mode_counts": dict(
                        scan_feedback_summary.get("feedback_generator_mode_control_mode_counts") or {}
                    ),
                    "feedback_skill_target_pool_control_mode_counts": dict(
                        scan_feedback_summary.get("feedback_skill_target_pool_control_mode_counts")
                        or {}
                    ),
                    "feedback_skill_holding_bucket_control_mode_counts": dict(
                        scan_feedback_summary.get("feedback_skill_holding_bucket_control_mode_counts")
                        or {}
                    ),
                    "feedback_skill_generator_mode_control_mode_counts": dict(
                        scan_feedback_summary.get("feedback_skill_generator_mode_control_mode_counts")
                        or {}
                    ),
                    "feedback_cooldown_task_count": int(
                        scan_feedback_summary.get("feedback_cooldown_task_count") or 0
                    ),
                    "feedback_blocked_task_count": int(
                        scan_feedback_summary.get("feedback_blocked_task_count") or 0
                    ),
                    "suppressed_families": list(scan_feedback_summary.get("suppressed_families") or []),
                    "suppressed_target_pools": list(scan_feedback_summary.get("suppressed_target_pools") or []),
                    "suppressed_generator_modes": list(scan_feedback_summary.get("suppressed_generator_modes") or []),
                    "external_llm_provider_control_mode": external_provider_control.get("control_mode"),
                    "external_llm_provider_control_reasons": list(
                        external_provider_control.get("control_reasons") or []
                    ),
                    "generator_mode_controls": dict(generator_mode_controls or {}),
                }
            )
            scan_report = {**scan_report, "summary": scan_summary, "tasks": scan_tasks}
            scan_task_artifact = dict(scan_report.get("task_artifact") or {})
            if not scan_task_artifact:
                scan_task_artifact = self._build_task_scan_artifact(
                    scan_report,
                    task_source_counts=dict(scan_summary.get("task_sources") or {}),
                    event_task_count=int(scan_summary.get("event_task_count") or 0),
                    snapshot_task_count=int((scan_summary.get("task_sources") or {}).get("snapshot") or 0),
                    bulk_stock_task_count=0,
                )
            scan_summary.update(
                {
                    "task_artifact_contract_version": scan_task_artifact.get("contract_version"),
                    "task_artifact_available": bool(scan_task_artifact.get("available")),
                }
            )
            scan_report = {
                **scan_report,
                "summary": scan_summary,
                "task_artifact": scan_task_artifact,
            }
            bulk_cursor = await self._resolve_bulk_stock_matrix_cursor(db)
            bulk_window_state = self._bulk_stock_matrix_run_window_state(self._now())
            runtime_mode_flags = resolve_runtime_mode_flags(
                snapshot.get("factory_execution_mode")
                or snapshot.get("execution_mode")
                or getattr(self, "execution_mode", None)
            )
            if bool(runtime_mode_flags.get("stock_first_observe_mode")) and not bool(
                bulk_window_state.get("run_window_active")
            ):
                bulk_window_state = {
                    **bulk_window_state,
                    "configured_enabled": True,
                    "run_window_active": True,
                    "skip_reason": None,
                    "activation_source": "stock_first_observe_mode",
                }
            resume_from_cursor = bool(
                bulk_cursor.get("available")
                and bulk_cursor.get("enabled")
                and str(bulk_cursor.get("source") or "").strip().lower() in {"last_result", "persisted_run"}
                and not bool(bulk_window_state.get("run_window_active"))
            )
            if resume_from_cursor:
                bulk_window_state = {
                    **bulk_window_state,
                    "configured_enabled": True,
                    "run_window_active": True,
                    "skip_reason": None,
                }
            bulk_report: dict[str, Any] = _build_default_bulk_report_payload(
                bulk_window_state,
                bulk_cursor,
            )
            if bool(bulk_window_state.get("run_window_active")):
                try:
                    bulk_report = await StockStrategyMatrixPlanner().plan(
                        db,
                        {
                            **snapshot,
                            "bulk_stock_matrix_task_offset": int(bulk_cursor.get("next_task_offset") or 0),
                            "bulk_stock_matrix_universe_offset": int(bulk_cursor.get("next_universe_offset") or 0),
                            "bulk_stock_matrix_cycle_index": int(self._cycle_count),
                            "bulk_stock_matrix_cursor_source": bulk_cursor.get("source"),
                            "bulk_stock_matrix_cursor_resume_from_run_id": bulk_cursor.get("resume_from_run_id"),
                        },
                    )
                except Exception as exc:
                    logger.warning("StrategyFactory: bulk stock-strategy matrix planning failed: %s", exc)
                    bulk_report = _build_bulk_planner_error_report_payload(
                        bulk_window_state,
                        bulk_cursor,
                        exc,
                    )
            bulk_report = _normalize_bulk_report_summary_payload(
                bulk_report,
                bulk_window_state,
                bulk_cursor,
            )
            full_market_score_rows = [
                dict(item or {})
                for item in list(bulk_report.pop("full_market_score_rows", []) or [])
                if isinstance(item, dict)
            ]
            full_market_topn = dict(bulk_report.get("full_market_topn") or {})
            run_id_hint = str(
                snapshot.get("factory_run_id") or snapshot.get("run_id") or ""
            ).strip()
            trace_id_hint = str(snapshot.get("trace_id") or "").strip()
            if full_market_topn and run_id_hint:
                full_market_topn["snapshot_id"] = (
                    str(full_market_topn.get("snapshot_id") or "").strip()
                    or f"fmt_{run_id_hint}"
                )
                full_market_topn["portfolio_candidate_id"] = (
                    str(full_market_topn.get("portfolio_candidate_id") or "").strip()
                    or f"factory_topn_{run_id_hint}"
                )
                full_market_topn["run_id"] = run_id_hint
                full_market_topn["trace_id"] = trace_id_hint or None
                full_market_topn["correlation_id"] = run_id_hint
            bulk_summary = dict(bulk_report.get("summary") or {})
            bulk_tasks = self._apply_scheduler_planning_controls(
                list(bulk_report.get("tasks") or []),
                feedback_root=feedback_root,
                provider_control=external_provider_control,
                generator_mode_controls=generator_mode_controls,
            )
            bulk_feedback_summary = summarize_task_feedback_controls(bulk_tasks)
            bulk_summary.update(
                {
                    "feedback_control_mode_counts": dict(bulk_feedback_summary.get("feedback_control_mode_counts") or {}),
                    "feedback_legacy_control_mode_counts": dict(
                        bulk_feedback_summary.get("feedback_legacy_control_mode_counts")
                        or bulk_feedback_summary.get("feedback_control_mode_counts")
                        or {}
                    ),
                    "feedback_skill_control_mode_counts": dict(
                        bulk_feedback_summary.get("feedback_skill_control_mode_counts") or {}
                    ),
                    "feedback_target_pool_control_mode_counts": dict(
                        bulk_feedback_summary.get("feedback_target_pool_control_mode_counts") or {}
                    ),
                    "feedback_holding_bucket_control_mode_counts": dict(
                        bulk_feedback_summary.get("feedback_holding_bucket_control_mode_counts") or {}
                    ),
                    "feedback_generator_mode_control_mode_counts": dict(
                        bulk_feedback_summary.get("feedback_generator_mode_control_mode_counts") or {}
                    ),
                    "feedback_skill_target_pool_control_mode_counts": dict(
                        bulk_feedback_summary.get("feedback_skill_target_pool_control_mode_counts")
                        or {}
                    ),
                    "feedback_skill_holding_bucket_control_mode_counts": dict(
                        bulk_feedback_summary.get("feedback_skill_holding_bucket_control_mode_counts")
                        or {}
                    ),
                    "feedback_skill_generator_mode_control_mode_counts": dict(
                        bulk_feedback_summary.get("feedback_skill_generator_mode_control_mode_counts")
                        or {}
                    ),
                    "feedback_cooldown_task_count": int(
                        bulk_feedback_summary.get("feedback_cooldown_task_count") or 0
                    ),
                    "feedback_blocked_task_count": int(
                        bulk_feedback_summary.get("feedback_blocked_task_count") or 0
                    ),
                    "suppressed_families": list(bulk_feedback_summary.get("suppressed_families") or []),
                    "suppressed_target_pools": list(bulk_feedback_summary.get("suppressed_target_pools") or []),
                    "suppressed_generator_modes": list(bulk_feedback_summary.get("suppressed_generator_modes") or []),
                    "external_llm_provider_control_mode": external_provider_control.get("control_mode"),
                    "external_llm_provider_control_reasons": list(
                        external_provider_control.get("control_reasons") or []
                    ),
                    "generator_mode_controls": dict(generator_mode_controls or {}),
                }
            )
            bulk_report = {**bulk_report, "summary": bulk_summary, "tasks": bulk_tasks}
            bulk_task_artifact = dict(bulk_report.get("task_artifact") or {})
            if not bulk_task_artifact and bool((bulk_report.get("summary") or {}).get("enabled")):
                bulk_task_artifact = self._build_task_scan_artifact(
                    bulk_report,
                    task_source_counts={"bulk_stock_matrix": len(bulk_tasks)},
                    event_task_count=0,
                    snapshot_task_count=0,
                    bulk_stock_task_count=len(bulk_tasks),
                )
            if bulk_task_artifact:
                bulk_summary.update(
                    {
                        "task_artifact_contract_version": bulk_task_artifact.get("contract_version"),
                        "task_artifact_available": bool(bulk_task_artifact.get("available")),
                    }
                )
                bulk_report = {
                    **bulk_report,
                    "summary": bulk_summary,
                    "task_artifact": bulk_task_artifact,
                }
            if bulk_tasks:
                tasks, task_budget_meta = self._merge_autonomy_tasks_with_budget(
                    scanner,
                    scan_tasks,
                    bulk_tasks,
                )
            else:
                tasks = [
                    dict(task or {})
                    for task in list(scan_tasks or [])
                    if not bool(dict(task or {}).get("feedback_generation_blocked"))
                    and _normalize_feedback_text(dict(task or {}).get("feedback_control_mode")) not in {"suppress", "freeze"}
                ]
                task_budget_meta = _build_scan_only_task_budget_meta_payload(scan_tasks, tasks)
            task_source_counts = dict(scan_summary.get("task_sources") or scanner._build_task_source_counts(tasks))
            if bulk_tasks:
                task_source_counts = scanner._build_task_source_counts(tasks)
            event_task_count = int(scan_summary.get("event_task_count") or task_source_counts.get("event_driven", 0))
            combined_scan_report = _build_combined_scan_report_payload(
                scan_summary=scan_summary,
                tasks=tasks,
                task_source_counts=task_source_counts,
                event_task_count=event_task_count,
                bulk_tasks=bulk_tasks,
                bulk_report=bulk_report,
                bulk_cursor=bulk_cursor,
                task_budget_meta=task_budget_meta,
                external_provider_control=external_provider_control,
                generator_mode_controls=generator_mode_controls,
                opportunity_scan=scan_report,
            )
            autonomy_gateway = self._get_autonomy_gateway()
            generated_candidates: List[dict] = []
            all_experiments: List[dict] = []
            task_results: List[dict] = []
            external_status_counts: Dict[str, int] = {}
            total_attempt_count = 0
            total_network_request_count = 0
            total_real_request_count = 0
            total_compatibility_skip_count = 0
            total_cooldown_skip_count = 0
            total_compatibility_failure_count = 0
            total_effective_response_count = 0
            total_empty_200_response_count = 0
            total_request_status_counts: Dict[str, int] = {}
            total_selected_count = 0
            total_evidence_count = 0
            last_error_type = None
            last_error = None
            elapsed_seconds = 0.0
            persistence_failures: List[dict[str, Any]] = []
            _agg_lock = asyncio.Lock()
            shared_generation_context_preloaded = await self._prepare_shared_generation_context(autonomy_gateway, db, snapshot)
            has_bulk_tasks = bool([task for task in tasks if str(task.get("task_source") or "").strip().lower() == "bulk_stock_matrix"])
            effective_research_concurrency = self._resolve_research_task_concurrency(
                autonomy_gateway,
                has_bulk_tasks=has_bulk_tasks,
            )
            effective_bulk_research_concurrency = self._resolve_bulk_research_task_concurrency(
                autonomy_gateway,
                has_bulk_tasks=has_bulk_tasks,
            )
            split_bulk_concurrency = bool(
                has_bulk_tasks and effective_bulk_research_concurrency != effective_research_concurrency
            )
            sem = asyncio.Semaphore(effective_research_concurrency)
            bulk_sem = asyncio.Semaphore(effective_bulk_research_concurrency) if split_bulk_concurrency else sem

            async def _run_one_task(task: dict) -> None:
                nonlocal total_attempt_count, total_network_request_count
                nonlocal total_real_request_count
                nonlocal total_compatibility_skip_count, total_cooldown_skip_count
                nonlocal total_compatibility_failure_count, total_effective_response_count, total_empty_200_response_count
                nonlocal total_selected_count, total_evidence_count
                nonlocal last_error_type, last_error, elapsed_seconds
                task_source = str(dict(task or {}).get("task_source") or "").strip().lower()
                task_sem = bulk_sem if task_source == "bulk_stock_matrix" else sem
                def _record_task_persistence_failure(operation: str, exc: Exception) -> None:
                    logger.warning("StrategyFactory: %s failed: %s", operation, exc)
                    self._record_persistence_failure(
                        persistence_failures,
                        operation,
                        exc,
                        stage="autonomy",
                    )

                execution = await _execute_autonomy_task_payload(
                    _AutonomyTaskExecutionContext(
                        task=dict(task or {}),
                        task_semaphore=task_sem,
                        db=db,
                        snapshot=snapshot,
                        autonomy_gateway=autonomy_gateway,
                        persist_task_evidence=self._persist_task_evidence,
                        extract_event_context=_extract_event_context,
                        call_optional_async=_call_optional_async,
                        record_persistence_failure=_record_task_persistence_failure,
                        generate_for_research_task=self._generate_for_research_task,
                        extract_cycle_llm_generation=self._extract_cycle_llm_generation,
                        extract_cycle_lifecycle=self._extract_cycle_lifecycle,
                        extract_cycle_generated_count=self._extract_cycle_generated_count,
                        extract_cycle_reviewed_count=self._extract_cycle_reviewed_count,
                        extract_cycle_candidates=self._extract_cycle_candidates,
                        extract_cycle_experiments=self._extract_cycle_experiments,
                        enrich_candidate_targeting=self._enrich_candidate_targeting,
                        build_research_task_run_result_summary=self._build_research_task_run_result_summary,
                        summarize_request_status_counts=self._summarize_external_request_status_counts,
                        count_network_requests=self._count_external_network_requests,
                        count_real_requests=self._count_external_real_requests,
                        request_is_compatibility_failure=self._request_is_compatibility_failure,
                        request_is_empty_200_response=self._request_is_empty_200_response,
                        normalize_external_request_status=self._normalize_external_request_status,
                        summarize_autonomy_lifecycle=summarize_autonomy_lifecycle,
                        autonomy_phase_order=AUTONOMY_PHASE_ORDER,
                    )
                )
                async with _agg_lock:
                    generated_candidates.extend(execution.generated_candidates)
                    all_experiments.extend(execution.experiments)
                    external_status_counts[execution.external_status] = (
                        external_status_counts.get(execution.external_status, 0) + 1
                    )
                    total_attempt_count += int(execution.request_metrics.get("attempt_count") or 0)
                    total_network_request_count += int(
                        execution.request_metrics.get("network_request_count") or 0
                    )
                    total_real_request_count += int(execution.request_metrics.get("real_request_count") or 0)
                    total_compatibility_skip_count += int(
                        execution.request_metrics.get("compatibility_skip_count") or 0
                    )
                    total_cooldown_skip_count += int(
                        execution.request_metrics.get("cooldown_skip_count") or 0
                    )
                    total_compatibility_failure_count += int(
                        execution.request_metrics.get("compatibility_failure_count") or 0
                    )
                    total_effective_response_count += int(
                        execution.request_metrics.get("effective_response_count") or 0
                    )
                    total_empty_200_response_count += int(
                        execution.request_metrics.get("empty_200_response_count") or 0
                    )
                    for request_status, count in dict(
                        execution.request_metrics.get("request_status_counts") or {}
                    ).items():
                        total_request_status_counts[request_status] = (
                            total_request_status_counts.get(request_status, 0) + int(count or 0)
                        )
                    total_selected_count += int(execution.selected_count or 0)
                    total_evidence_count += int(execution.evidence_count or 0)
                    if execution.last_error_type:
                        last_error_type = execution.last_error_type
                        last_error = execution.last_error
                    elapsed_seconds += float(execution.elapsed_seconds or 0.0)
                    task_results.append(execution.task_result_summary)

            # 有界并发执行所有研究任务
            if tasks:
                logger.info(
                    "StrategyFactory: running %d research tasks with concurrency=%d",
                    len(tasks), effective_research_concurrency,
                )
                await asyncio.gather(*[_run_one_task(t) for t in tasks])

            lifecycle_metrics = self._aggregate_task_lifecycle_metrics(task_results)
            selected_feedback_summary = summarize_task_feedback_controls(tasks)
            stage = _build_autonomy_stage_summary_payload(
                task_results=task_results,
                task_source_counts=task_source_counts,
                event_task_count=event_task_count,
                bulk_report=bulk_report,
                bulk_cursor=bulk_cursor,
                generated_candidates=generated_candidates,
                all_experiments=all_experiments,
                external_status_counts=external_status_counts,
                total_attempt_count=total_attempt_count,
                total_network_request_count=total_network_request_count,
                total_real_request_count=total_real_request_count,
                total_compatibility_skip_count=total_compatibility_skip_count,
                total_cooldown_skip_count=total_cooldown_skip_count,
                total_compatibility_failure_count=total_compatibility_failure_count,
                total_effective_response_count=total_effective_response_count,
                total_empty_200_response_count=total_empty_200_response_count,
                total_request_status_counts=total_request_status_counts,
                total_selected_count=total_selected_count,
                total_evidence_count=total_evidence_count,
                last_error_type=last_error_type,
                last_error=last_error,
                elapsed_seconds=elapsed_seconds,
                external_provider_health=external_provider_health,
                effective_research_concurrency=effective_research_concurrency,
                has_bulk_tasks=has_bulk_tasks,
                effective_bulk_research_concurrency=effective_bulk_research_concurrency,
                bulk_tasks_use_external_llm=self._bulk_tasks_use_external_llm(autonomy_gateway),
                research_task_timeout_sec=self._resolve_research_task_timeout_sec(),
                task_budget_meta=task_budget_meta,
                selected_feedback_summary=selected_feedback_summary,
                external_provider_control=external_provider_control,
                generator_mode_controls=generator_mode_controls,
                shared_generation_context_preloaded=shared_generation_context_preloaded,
                persistence_failures=persistence_failures,
                lifecycle_metrics=lifecycle_metrics,
                combined_scan_report=combined_scan_report,
            )
            stage = _attach_autonomy_stage_artifacts_payload(
                stage=stage,
                scan_task_artifact=scan_task_artifact,
                bulk_task_artifact=bulk_task_artifact,
                generated_candidates=generated_candidates,
                all_experiments=all_experiments,
                build_task_artifact=build_task_artifact,
                build_candidate_artifact=build_candidate_artifact,
                build_research_evidence_artifact=build_research_evidence_artifact,
            )
            if full_market_topn:
                stage["full_market_topn"] = full_market_topn
            return {
                "stage": stage,
                "candidates": generated_candidates,
                "experiments": all_experiments,
                "full_market_topn": full_market_topn,
                "full_market_score_rows": full_market_score_rows,
            }

        async def run_once(self, db=None, *, execution_mode=None, dispatch_id: Optional[str] = None, target_codes: Optional[list[str]] = None) -> dict:
            """执行一次完整的策略工厂流程。"""
            current_loop = asyncio.get_running_loop()
            run_once_lock = self._get_run_once_task_lock()
            async with run_once_lock:
                task = self._run_once_task
                if task is not None and not task.done():
                    try:
                        task_loop = task.get_loop()
                    except Exception:
                        task_loop = current_loop
                    if task_loop is not current_loop:
                        logger.info(
                            "StrategyFactory run_once already in progress on another event loop; skipping join"
                        )
                        return {
                            "status": "skipped",
                            "reason": "run_once_in_progress_other_event_loop",
                            "summary": {
                                "skipped": True,
                                "skip_reason": "run_once_in_progress_other_event_loop",
                                "last_run": self.last_run.isoformat() if self.last_run else None,
                                "last_status": (self.last_result or {}).get("status")
                                if isinstance(self.last_result, dict)
                                else None,
                            },
                        }
                    logger.info("StrategyFactory run_once already in progress; joining in-flight execution")
                else:
                    async def _execute_once() -> dict:
                        resolved_db = self._load_db() if db is None else db
                        try:
                            restore_scheduler_state = getattr(self, "_restore_scheduler_state", None)
                            if callable(restore_scheduler_state):
                                restore_result = restore_scheduler_state(resolved_db)
                                if inspect.isawaitable(restore_result):
                                    await restore_result
                        except Exception as restore_exc:
                            logger.debug(
                                "StrategyFactory run_once: scheduler state restore failed: %s",
                                restore_exc,
                            )
                        previous_result = self.last_result
                        resolved_mode = resolve_factory_execution_mode(
                            execution_mode,
                            default=self.execution_mode,
                        )
                        board_task = None
                        claim_token = None
                        heartbeat_task = None
                        task_board = getattr(self, "_task_board", None)
                        try:
                            logger.info(
                                "StrategyFactory run_once: preparing execution mode=%s dispatch_id=%s target_codes=%d",
                                getattr(resolved_mode, "value", str(resolved_mode)),
                                dispatch_id or "-",
                                len(_normalize_target_codes(target_codes or [], limit=64)),
                            )
                            if task_board is not None:
                                claim_ttl_seconds = max(
                                    300,
                                    int(os.getenv("STRATEGY_FACTORY_TASK_BOARD_CLAIM_TTL_SEC", "1800") or 1800),
                                )
                                try:
                                    reclaimed = task_board.reclaim_stale(
                                        block_task_types=("research", "quality_gate"),
                                        block_reason=(
                                            "stale factory task reclaimed before new "
                                            "StrategyFactory run_once"
                                        ),
                                    )
                                    if reclaimed:
                                        logger.warning(
                                            "StrategyFactory run_once: reclaimed %d stale task_board tasks before start",
                                            len(reclaimed),
                                        )
                                except Exception as reclaim_exc:
                                    logger.debug(
                                        "StrategyFactory run_once: task_board stale reclaim failed: %s",
                                        reclaim_exc,
                                    )
                                try:
                                    def _pid_exists(pid: int) -> bool:
                                        if pid <= 0:
                                            return False
                                        if os.name == "nt":
                                            try:
                                                import ctypes

                                                kernel32 = ctypes.windll.kernel32
                                                handle = kernel32.OpenProcess(0x1000, False, pid)
                                                if handle:
                                                    kernel32.CloseHandle(handle)
                                                    return True
                                                return False
                                            except Exception:
                                                return True
                                        try:
                                            os.kill(pid, 0)
                                            return True
                                        except OSError as _swallowed:
                                            logger.warning("packages/strategy-factory/src/strategy_factory/application/_factory_scheduler_loop.py:3248: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                                            return False

                                    current_pid = os.getpid()
                                    active_run_once_tasks = task_board.list_tasks(
                                        statuses=("ready", "running"),
                                        task_type="research",
                                        title="Strategy factory run_once",
                                        limit=20,
                                    )
                                    blocked_active_count = 0
                                    for active_task in active_run_once_tasks:
                                        active_payload = dict(active_task.get("payload") or {})
                                        owner_pid = active_payload.get("owner_pid")
                                        try:
                                            owner_pid_int = int(owner_pid) if owner_pid is not None else 0
                                        except Exception:
                                            owner_pid_int = 0
                                        if owner_pid_int == current_pid:
                                            continue
                                        if owner_pid_int > 0 and _pid_exists(owner_pid_int):
                                            continue
                                        task_board.block_task(
                                            active_task["task_id"],
                                            "superseded by new StrategyFactory run_once after prior runner exit",
                                            claim_token=active_task.get("claim_token"),
                                        )
                                        blocked_active_count += 1
                                    if blocked_active_count:
                                        logger.warning(
                                            "StrategyFactory run_once: blocked %d active predecessor task_board run_once tasks",
                                            blocked_active_count,
                                        )
                                except Exception as predecessor_exc:
                                    logger.debug(
                                        "StrategyFactory run_once: active predecessor task_board cleanup failed: %s",
                                        predecessor_exc,
                                    )
                                board_task = task_board.create_task(
                                    task_type="research",
                                    title="Strategy factory run_once",
                                    payload={
                                        "execution_mode": getattr(resolved_mode, "value", str(resolved_mode)),
                                        "dispatch_id": dispatch_id,
                                        "owner_pid": os.getpid(),
                                        "target_codes": _normalize_target_codes(target_codes or [], limit=64),
                                    },
                                )
                                claimed_task = task_board.claim_task(
                                    board_task["task_id"],
                                    worker_id="strategy_factory_scheduler",
                                    ttl_seconds=claim_ttl_seconds,
                                )
                                if claimed_task is not None:
                                    board_task = claimed_task
                                    claim_token = claimed_task.get("claim_token")
                                logger.info(
                                    "StrategyFactory run_once: task board claimed task_id=%s ttl=%ss",
                                    (board_task or {}).get("task_id"),
                                    claim_ttl_seconds,
                                )
                                try:
                                    configured_heartbeat_seconds = int(
                                        os.getenv(
                                            "STRATEGY_FACTORY_TASK_BOARD_HEARTBEAT_SEC",
                                            "60",
                                        )
                                        or 60
                                    )
                                except Exception:
                                    configured_heartbeat_seconds = 60
                                try:
                                    minimum_heartbeat_seconds = int(
                                        os.getenv(
                                            "STRATEGY_FACTORY_TASK_BOARD_HEARTBEAT_MIN_SEC",
                                            "30",
                                        )
                                        or 30
                                    )
                                except Exception:
                                    minimum_heartbeat_seconds = 30
                                minimum_heartbeat_seconds = max(1, minimum_heartbeat_seconds)
                                heartbeat_interval_seconds = max(
                                    minimum_heartbeat_seconds,
                                    min(
                                        max(minimum_heartbeat_seconds, claim_ttl_seconds // 3),
                                        configured_heartbeat_seconds,
                                    ),
                                )

                                async def _heartbeat_task_board_claim() -> None:
                                    while True:
                                        await asyncio.sleep(heartbeat_interval_seconds)
                                        try:
                                            task_board.heartbeat(
                                                board_task["task_id"],
                                                claim_token,
                                                ttl_seconds=claim_ttl_seconds,
                                            )
                                        except Exception as heartbeat_exc:
                                            logger.debug(
                                                "StrategyFactory run_once: task_board heartbeat failed: %s",
                                                heartbeat_exc,
                                            )

                                heartbeat_task = asyncio.create_task(
                                    _heartbeat_task_board_claim(),
                                    name=f"strategy-factory-task-board-heartbeat:{board_task['task_id']}",
                                )
                            run_once_timeout_sec = self._resolve_run_once_timeout_sec()
                            try:
                                results, persistence_failures = await asyncio.wait_for(
                                    self._execute_factory_run_once_mode(
                                        resolved_db,
                                        previous_result=previous_result,
                                        execution_mode=resolved_mode,
                                        target_codes=_normalize_target_codes(target_codes or [], limit=64),
                                    ),
                                    timeout=run_once_timeout_sec,
                                )
                            except asyncio.TimeoutError as timeout_exc:
                                raise TimeoutError(
                                    f"StrategyFactory run_once exceeded {run_once_timeout_sec:g}s"
                                ) from timeout_exc
                            logger.info(
                                "StrategyFactory run_once: cycle returned run_id=%s status=%s",
                                results.get("run_id"),
                                results.get("status"),
                            )
                            try:
                                _alpha = 0.3
                                _ema_floor = float(os.getenv("STRATEGY_FACTORY_EMA_FLOOR", "0.15") or 0.15)
                                _exploration_reset_interval = int(
                                    os.getenv("STRATEGY_FACTORY_EMA_EXPLORATION_RESET_INTERVAL", "20") or 20
                                )
                                completed_cycle_count = max(0, int(getattr(self, "_cycle_count", 0) or 0)) + 1
                                updated_feedback, feedback_update = update_scheduler_family_gate_feedback(
                                    dict(getattr(self, "_family_gate_feedback", {}) or {}),
                                    results,
                                    cycle_count=completed_cycle_count,
                                    alpha=_alpha,
                                    ema_floor=_ema_floor,
                                    exploration_reset_interval=_exploration_reset_interval,
                                )
                                self._family_gate_feedback = updated_feedback
                                self._cycle_count = completed_cycle_count
                                summary = dict(results.get("summary") or {})
                                summary["family_gate_feedback_update"] = feedback_update
                                summary["scheduler_cycle_count"] = self._cycle_count
                                feedback_control_counts = dict(feedback_update.get("control_counts") or {})
                                summary["family_gate_feedback_control_counts"] = feedback_control_counts
                                summary["family_gate_feedback_updated_family_count"] = int(
                                    feedback_update.get("updated_family_count") or 0
                                )
                                summary["family_gate_feedback_tracked_family_count"] = int(
                                    feedback_update.get("tracked_family_count") or 0
                                )
                                summary["family_gate_feedback_active_families"] = list(
                                    feedback_update.get("active_families") or []
                                )
                                summary["family_gate_feedback_gate_3_input"] = int(
                                    feedback_update.get("gate_3_input") or 0
                                )
                                summary["family_gate_feedback_gate_3_passed"] = int(
                                    feedback_update.get("gate_3_passed") or 0
                                )
                                summary["family_gate_feedback_gate_3_failed"] = int(
                                    feedback_update.get("gate_3_failed") or 0
                                )
                                summary["family_gate_feedback_submitted"] = int(
                                    feedback_update.get("submitted") or 0
                                )
                                summary["family_gate_feedback_gate_3_recorded"] = int(
                                    feedback_update.get("gate_3_recorded") or 0
                                )
                                summary["family_gate_feedback_created_audit_only"] = int(
                                    feedback_update.get("created_audit_only") or 0
                                )
                                summary["family_gate_feedback_failure_reason_topn"] = list(
                                    feedback_update.get("gate_3_failure_reason_topn") or []
                                )
                                results["summary"] = summary
                            except Exception as feedback_exc:
                                logger.debug(
                                    "StrategyFactory run_once: family gate feedback update failed: %s",
                                    feedback_exc,
                                )
                            if task_board is not None and board_task is not None:
                                task_board.heartbeat(
                                    board_task["task_id"],
                                    claim_token,
                                    ttl_seconds=claim_ttl_seconds,
                                )
                            self.last_run = self._now()
                            self.last_result = results
                            await self._persist_run_result(
                                resolved_db,
                                results,
                                persistence_failures=persistence_failures,
                            )
                            logger.info(
                                "StrategyFactory run_once: persisted run_id=%s status=%s artifact_refs=%d",
                                results.get("run_id"),
                                results.get("status"),
                                len(results.get("artifact_refs") or []),
                            )
                            if task_board is not None and board_task is not None:
                                summary = dict(results.get("summary") or {})
                                completed_task = task_board.complete_task(
                                    board_task["task_id"],
                                    claim_token=claim_token,
                                    artifact_refs=list(results.get("artifact_refs") or []),
                                    result={
                                        "run_id": results.get("run_id"),
                                        "status": results.get("status"),
                                        "completed_at": results.get("completed_at"),
                                        "elapsed_seconds": results.get("elapsed_seconds"),
                                        "summary": {
                                            "trace_id": summary.get("trace_id"),
                                            "snapshot_completion_ratio": summary.get("snapshot_completion_ratio"),
                                            "snapshot_degraded": summary.get("snapshot_degraded"),
                                            "candidates_spawned": summary.get("candidates_spawned"),
                                            "autonomy_generated": summary.get("autonomy_generated"),
                                            "autonomy_task_count": summary.get("autonomy_task_count"),
                                            "autonomy_completed_task_count": summary.get("autonomy_completed_task_count"),
                                            "autonomy_failed_task_count": summary.get("autonomy_failed_task_count"),
                                        },
                                    },
                                )
                                results["task_board"] = {
                                    "task_id": board_task["task_id"],
                                    "status": (completed_task or {}).get("status"),
                                }
                                logger.info(
                                    "StrategyFactory run_once: task board completed task_id=%s status=%s",
                                    board_task["task_id"],
                                    (completed_task or {}).get("status"),
                                )
                        except asyncio.CancelledError:
                            if task_board is not None and board_task is not None:
                                try:
                                    task_board.block_task(
                                        board_task["task_id"],
                                        "StrategyFactory run_once cancelled before completion",
                                        claim_token=claim_token,
                                    )
                                except Exception as block_exc:
                                    logger.debug("StrategyFactory run_once: task_board cancel block failed: %s", block_exc)
                            raise
                        except Exception as exc:
                            if task_board is not None and board_task is not None:
                                try:
                                    task_board.block_task(board_task["task_id"], str(exc), claim_token=claim_token)
                                except Exception as block_exc:
                                    logger.debug("StrategyFactory run_once: task_board.block_task failed: %s", block_exc)
                            raise
                        finally:
                            if heartbeat_task is not None:
                                heartbeat_task.cancel()
                                with suppress(asyncio.CancelledError):
                                    await heartbeat_task

                        # P2-D：用本次孵化预算 family 计数更新反馈 EMA（α=0.3）
                        # 优化 3：EMA 饥饿保护（下限 + 周期性探索重置）
                        try:
                            family_counts: Dict[str, int] = dict(
                                (results.get("summary") or {}).get("incubation_budget_family_counts") or {}
                            )
                            if False and family_counts:
                                _alpha = 0.3
                                _ema_floor = float(os.getenv("STRATEGY_FACTORY_EMA_FLOOR", "0.15") or 0.15)
                                _exploration_reset_interval = int(
                                    os.getenv("STRATEGY_FACTORY_EMA_EXPLORATION_RESET_INTERVAL", "20") or 20
                                )
                                for family, count in family_counts.items():
                                    prev = dict(self._family_gate_feedback.get(family) or {})
                                    prev_ema = float(prev.get("ema_submit_count") or 0.0)
                                    new_ema = round(_alpha * float(count) + (1.0 - _alpha) * prev_ema, 4)
                                    self._family_gate_feedback[family] = {"ema_submit_count": new_ema}
                                # 衰减未出现的 family（带下限保护）
                                for family in list(self._family_gate_feedback):
                                    if family not in family_counts:
                                        prev_ema = float((self._family_gate_feedback[family] or {}).get("ema_submit_count") or 0.0)
                                        new_ema = max(_ema_floor, round((1.0 - _alpha) * prev_ema, 4))
                                        self._family_gate_feedback[family]["ema_submit_count"] = new_ema
                                # 周期性探索重置：每 N 轮重置低 EMA 的 family
                                if _exploration_reset_interval > 0 and self._cycle_count % _exploration_reset_interval == 0:
                                    for family, data in self._family_gate_feedback.items():
                                        if data.get("ema_submit_count", 0) < _ema_floor + 0.05:
                                            data["ema_submit_count"] = 0.5
                        except Exception as ema_exc:
                            logger.debug("StrategyFactory run_once: family gate feedback EMA update failed: %s", ema_exc)

                        # 优化 4：EMA 反馈状态持久化
                        try:
                            _save_scheduler_state = getattr(resolved_db, "save_scheduler_state", None)
                            if callable(_save_scheduler_state):
                                import inspect as _inspect
                                _state_payload = {
                                    "family_gate_feedback": self._family_gate_feedback,
                                    "cycle_count": self._cycle_count,
                                    "consecutive_failures": self._consecutive_failures,
                                    "circuit_state": getattr(self, "_circuit_state", "closed"),
                                }
                                _save_result = _save_scheduler_state(_state_payload)
                                if _inspect.isawaitable(_save_result):
                                    await _save_result
                                self._metrics.ema_feedback_persisted = True
                        except Exception as persist_exc:
                            logger.debug("StrategyFactory run_once: scheduler state persistence failed: %s", persist_exc)
                            self._metrics.ema_feedback_persisted = False

                        if dispatch_id:
                            summary = dict(results.get("summary") or {})
                            summary["dispatch_id"] = dispatch_id
                            results["summary"] = summary
                        logger.info(
                            "StrategyFactory run_once: returning run_id=%s status=%s",
                            results.get("run_id"),
                            results.get("status"),
                        )
                        return results

                    task = asyncio.create_task(_execute_once(), name="strategy-factory-run-once")
                    self._run_once_task = task

            try:
                return await asyncio.shield(task)
            finally:
                async with run_once_lock:
                    if self._run_once_task is task and task.done():
                        self._run_once_task = None
# --- _factory_scheduler_loop_parts/models.py ---

        @staticmethod
        def _env_flag(name: str, default: bool = False) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return bool(default)
            normalized = str(raw).strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
            return bool(default)

        @classmethod
        def _full_cycle_parallel_enabled(cls) -> bool:
            return cls._env_flag("STRATEGY_FACTORY_ALLOW_PARALLEL_FULL_CYCLES", False)

        @classmethod
        def _resolve_dispatch_concurrency_limit(cls) -> int:
            raw = str(os.getenv("STRATEGY_FACTORY_MAX_CONCURRENT_DISPATCHES", "1") or "1").strip()
            try:
                configured = int(raw)
            except Exception:
                configured = 1
            configured = max(1, min(configured, 16))
            if not cls._full_cycle_parallel_enabled():
                return 1
            return configured

        def _get_dispatch_semaphore(self) -> asyncio.Semaphore:
            loop = asyncio.get_running_loop()
            limit = self._resolve_dispatch_concurrency_limit()
            if (
                getattr(self, "_dispatch_semaphore", None) is None
                or getattr(self, "_dispatch_semaphore_loop", None) is not loop
                or int(getattr(self, "_dispatch_semaphore_limit", 0) or 0) != limit
            ):
                self._dispatch_semaphore = asyncio.Semaphore(limit)
                self._dispatch_semaphore_loop = loop
                self._dispatch_semaphore_limit = limit
            return self._dispatch_semaphore

        def _active_dispatch_ids(self) -> list[str]:
            return [
                str(dispatch_id)
                for dispatch_id, task in dict(getattr(self, "_dispatch_tasks", {}) or {}).items()
                if task is not None and not task.done()
            ]

        async def _run_parallel_dispatch_cycle(
            self,
            resolved_db,
            *,
            resolved_mode,
            dispatch_id: str,
            target_codes: Optional[list[str]] = None,
        ) -> dict[str, Any]:
            try:
                restore_scheduler_state = getattr(self, "_restore_scheduler_state", None)
                if callable(restore_scheduler_state):
                    restore_result = restore_scheduler_state(resolved_db)
                    if inspect.isawaitable(restore_result):
                        await restore_result
            except Exception as restore_exc:
                logger.debug(
                    "StrategyFactory parallel dispatch: scheduler state restore failed: %s",
                    restore_exc,
                )

            previous_result = self.last_result
            run_once_timeout_sec = self._resolve_run_once_timeout_sec()
            try:
                results, persistence_failures = await asyncio.wait_for(
                    self._execute_factory_run_once_mode(
                        resolved_db,
                        previous_result=previous_result,
                        execution_mode=resolved_mode,
                        target_codes=_normalize_target_codes(target_codes or [], limit=64),
                    ),
                    timeout=run_once_timeout_sec,
                )
            except asyncio.TimeoutError as timeout_exc:
                raise TimeoutError(
                    f"StrategyFactory parallel dispatch exceeded {run_once_timeout_sec:g}s"
                ) from timeout_exc

            summary = dict(results.get("summary") or {})
            summary["dispatch_id"] = dispatch_id
            summary["parallel_full_cycle"] = True
            summary["scheduler_feedback_update_skipped"] = "parallel_full_cycle"
            results["summary"] = summary
            self.last_run = self._now()
            self.last_result = results
            await self._persist_run_result(
                resolved_db,
                results,
                persistence_failures=persistence_failures,
            )
            return results

        async def dispatch_run(
            self,
            db=None,
            *,
            execution_mode=None,
            target_codes: Optional[list[str]] = None,
        ) -> dict[str, Any]:
            resolved_db = self._load_db() if db is None else db
            resolved_mode = resolve_factory_execution_mode(
                execution_mode,
                default=self.execution_mode,
            )
            normalized_target_codes = _normalize_target_codes(target_codes or [], limit=64)
            dispatch_id = f"factory_dispatch_{uuid4().hex[:12]}"
            requested_at = self._now().isoformat()
            concurrency_limit = self._resolve_dispatch_concurrency_limit()
            parallel_full_cycles = self._full_cycle_parallel_enabled()
            payload = {
                "dispatch_id": dispatch_id,
                "status": "queued",
                "execution_mode": resolved_mode.value,
                "requested_at": requested_at,
                "message": "Strategy Factory request queued.",
                "metadata": {
                    "engine_version": str(getattr(self, "engine_version", None) or FACTORY_ENGINE_VERSION),
                    "dispatch_concurrency_limit": concurrency_limit,
                    "parallel_full_cycles": parallel_full_cycles,
                    "target_codes": normalized_target_codes,
                },
            }
            persisted = await _call_optional_async(
                resolved_db,
                "create_strategy_factory_dispatch",
                payload,
                default=payload,
            )
            self._latest_dispatch_id = dispatch_id

            async def _run_dispatch() -> None:
                try:
                    semaphore = self._get_dispatch_semaphore()
                    async with semaphore:
                        self._active_dispatch_id = dispatch_id
                        await _call_optional_async(
                            resolved_db,
                            "update_strategy_factory_dispatch",
                            dispatch_id,
                            status="running",
                            started_at=self._now().isoformat(),
                            message="Strategy Factory dispatch running.",
                            metadata={
                                "engine_version": str(getattr(self, "engine_version", None) or FACTORY_ENGINE_VERSION),
                                "dispatch_concurrency_limit": concurrency_limit,
                                "parallel_full_cycles": parallel_full_cycles,
                                "target_codes": normalized_target_codes,
                            },
                            default=None,
                        )
                        if parallel_full_cycles:
                            result = await self._run_parallel_dispatch_cycle(
                                resolved_db,
                                resolved_mode=resolved_mode,
                                dispatch_id=dispatch_id,
                                target_codes=normalized_target_codes,
                            )
                        else:
                            result = await self.run_once(
                                resolved_db,
                                execution_mode=resolved_mode,
                                dispatch_id=dispatch_id,
                                target_codes=normalized_target_codes,
                            )
                        result_status_raw = str(result.get("status") or "").strip().lower()
                        result_status = (
                            result_status_raw
                            if result_status_raw in {"success", "partial", "skipped", "failed"}
                            else "success"
                        )
                        parity_result = dict(result.get("parity_result") or {})
                        await _call_optional_async(
                            resolved_db,
                            "update_strategy_factory_dispatch",
                            dispatch_id,
                            status=result_status,
                            completed_at=self._now().isoformat(),
                            run_id=str(result.get("run_id") or "").strip() or None,
                            message=f"Strategy Factory dispatch completed with status={result_status}.",
                            metadata={
                                "engine_version": str(getattr(self, "engine_version", None) or FACTORY_ENGINE_VERSION),
                                "dispatch_concurrency_limit": concurrency_limit,
                                "parallel_full_cycles": parallel_full_cycles,
                                "target_codes": normalized_target_codes,
                                "result_status": result.get("status"),
                                "parity_status": parity_result.get("status"),
                                "artifact_refs": list(result.get("artifact_refs") or []),
                                "degraded": result_status == "partial",
                            },
                            default=None,
                        )
                except Exception as exc:
                    logger.error(
                        "StrategyFactory dispatch %s failed: %s",
                        dispatch_id,
                        exc,
                        exc_info=True,
                    )
                    await _call_optional_async(
                        resolved_db,
                        "update_strategy_factory_dispatch",
                        dispatch_id,
                        status="failed",
                        completed_at=self._now().isoformat(),
                        message="Strategy Factory dispatch failed.",
                        error=str(exc),
                        metadata={
                            "engine_version": str(getattr(self, "engine_version", None) or FACTORY_ENGINE_VERSION),
                            "dispatch_concurrency_limit": concurrency_limit,
                            "parallel_full_cycles": parallel_full_cycles,
                            "target_codes": normalized_target_codes,
                            "error_type": exc.__class__.__name__,
                        },
                        default=None,
                    )
                finally:
                    self._dispatch_tasks.pop(dispatch_id, None)
                    if self._active_dispatch_id == dispatch_id:
                        active_ids = self._active_dispatch_ids()
                        self._active_dispatch_id = active_ids[-1] if active_ids else None

            task = asyncio.create_task(_run_dispatch(), name=f"strategy-factory-dispatch:{dispatch_id}")
            self._dispatch_tasks[dispatch_id] = task
            return {
                **dict(persisted or payload),
                "accepted": True,
                "queued": True,
                "already_running": False,
                "dispatch_concurrency_limit": concurrency_limit,
                "parallel_full_cycles": parallel_full_cycles,
                "active_dispatch_count": len(self._active_dispatch_ids()),
            }

        async def get_dispatch_status(self, dispatch_id: str, db=None) -> Optional[dict[str, Any]]:
            resolved_db = self._load_db() if db is None else db
            token = str(dispatch_id or "").strip()
            if not token:
                return None
            row = await _call_optional_async(
                resolved_db,
                "get_strategy_factory_dispatch",
                token,
                default=None,
            )
            if row is None:
                return None
            result = dict(row or {})
            task = self._dispatch_tasks.get(token)
            if task is not None and not task.done():
                result["background_task_active"] = True
            return result

        def status(self) -> dict:
            bulk_window_state = self._bulk_stock_matrix_run_window_state(self._now())
            bulk_stock_matrix_cursor = self._extract_bulk_stock_cursor(
                ((self.last_result or {}).get("summary") or {}),
                source="last_result" if self.last_result else "default",
                run_id=(self.last_result or {}).get("run_id"),
            )
            bulk_stock_matrix_config = {
                "enabled": bool(STOCK_STRATEGY_MATRIX_ENABLED),
                "universe_limit": int(STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT),
                "families_per_stock": int(STOCK_STRATEGY_MATRIX_FAMILIES_PER_STOCK),
                "max_tasks_per_run": int(STOCK_STRATEGY_MATRIX_MAX_TASKS_PER_RUN),
                "max_candidates_per_run": int(STOCK_STRATEGY_MATRIX_MAX_CANDIDATES_PER_RUN),
                "generation_limit_per_task": int(STOCK_STRATEGY_MATRIX_GENERATION_LIMIT_PER_TASK),
                "batch_size": int(STOCK_STRATEGY_MATRIX_BATCH_SIZE),
                "bulk_concurrency": int(STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY),
                "run_window": str(STOCK_STRATEGY_MATRIX_RUN_WINDOW),
                "run_window_active": bool(bulk_window_state.get("run_window_active")),
                "run_window_current_period": bulk_window_state.get("current_period"),
                "tasks_per_shard": int(STOCK_STRATEGY_MATRIX_TASKS_PER_SHARD),
                "pre_gate_enabled": bool(FACTORY_PRE_GATE_ENABLED),
            }
            last_summary = (self.last_result or {}).get("summary") if self.last_result else None
            latest_parity_result = dict((self.last_result or {}).get("parity_result") or {})
            effective_execution_mode = (
                str((self.last_result or {}).get("execution_mode") or "").strip()
                or resolve_factory_execution_mode(
                    getattr(self, "execution_mode", FactoryExecutionMode.LEGACY_PRIMARY),
                    default=FactoryExecutionMode.LEGACY_PRIMARY,
                ).value
            )
            active_dispatch_ids = self._active_dispatch_ids()
            return {
                "running": self._running,
                "schedule_mode": self.schedule_mode,
                "run_time": str(self.run_time),
                "execution_mode": effective_execution_mode,
                "engine_version": str(getattr(self, "engine_version", None) or FACTORY_ENGINE_VERSION),
                "runtime_enabled": is_factory_runtime_enabled(),
                "event_runtime_mode": resolve_event_runtime_mode(),
                "factor_auto_refresh_enabled": is_factory_factor_auto_refresh_enabled(),
                "factor_refresh_timeout_sec": FACTORY_FACTOR_REFRESH_TIMEOUT_SEC,
                "readiness_hard_block_enabled": is_factory_readiness_hard_block_enabled(),
                "readiness_min_score": FACTORY_READINESS_MIN_SCORE,
                "readiness_min_completion_ratio": FACTORY_READINESS_MIN_COMPLETION_RATIO,
                "last_run": str(self.last_run) if self.last_run else None,
                "last_result": self.last_result,
                "last_summary": last_summary,
                "latest_parity_result": latest_parity_result or None,
                "scheduler_slo": dict((last_summary or {}).get("scheduler_slo") or {}) if last_summary else None,
                "architecture_review": dict((last_summary or {}).get("architecture_review") or {}) if last_summary else None,
                "bulk_stock_matrix_config": bulk_stock_matrix_config,
                "bulk_stock_matrix_cursor": bulk_stock_matrix_cursor,
                "daily_run_count": self._daily_run_count,
                "max_daily_runs": self.max_daily_runs,
                "cycle_count": self._cycle_count,
                "active_dispatch_id": str(getattr(self, "_active_dispatch_id", "") or "").strip() or None,
                "active_dispatch_ids": active_dispatch_ids,
                "active_dispatch_count": len(active_dispatch_ids),
                "dispatch_concurrency_limit": self._resolve_dispatch_concurrency_limit(),
                "parallel_full_cycles": self._full_cycle_parallel_enabled(),
                "latest_dispatch_id": str(getattr(self, "_latest_dispatch_id", "") or "").strip() or None,
            }
