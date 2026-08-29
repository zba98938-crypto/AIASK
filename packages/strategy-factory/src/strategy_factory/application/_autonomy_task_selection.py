"""Shared helpers for autonomy task planning and selection."""

from __future__ import annotations

from typing import Any

from ._budget_feedback import (
    apply_feedback_controls_to_task,
    normalize_text as _normalize_feedback_text,
    relax_feedback_control_for_research_task,
    summarize_task_feedback_controls,
)
from ..domain.constants import (
    AUTONOMY_MAX_BULK_RESEARCH_TASKS,
    AUTONOMY_MAX_RESEARCH_TASKS,
    AUTONOMY_RESERVED_BULK_RESEARCH_TASKS,
)


def apply_scheduler_planning_controls(
    tasks: list[dict[str, Any]],
    *,
    feedback_root: dict[str, Any],
    provider_control: dict[str, Any],
    generator_mode_controls: dict[str, Any],
) -> list[dict[str, Any]]:
    provider = dict(provider_control or {})
    mode_controls = {
        _normalize_feedback_text(key): dict(value or {})
        for key, value in dict(generator_mode_controls or {}).items()
        if _normalize_feedback_text(key)
    }
    provider_mode = _normalize_feedback_text(provider.get("control_mode")) or "normal"
    provider_reason = (
        (list(provider.get("control_reasons") or []) or [provider.get("scheduler_skip_reason")])[0]
        if provider_mode != "normal"
        else None
    )
    resolved: list[dict[str, Any]] = []
    for item in list(tasks or []):
        task = apply_feedback_controls_to_task(item, feedback_root)
        task = relax_feedback_control_for_research_task(task)
        task_source = _normalize_feedback_text(task.get("task_source"))
        task["external_llm_provider_control_mode"] = provider_mode
        if provider_mode in {"suppress", "freeze"}:
            task["disable_external_llm"] = True
            task["external_llm_skip_reason"] = provider_reason or "provider_control_suppress"
        elif provider_mode == "cooldown" and task_source == "bulk_stock_matrix":
            task["disable_external_llm"] = True
            task["external_llm_skip_reason"] = provider_reason or "provider_control_cooldown"

        external_mode_control = dict(mode_controls.get("external_llm") or {})
        external_mode = _normalize_feedback_text(external_mode_control.get("control_mode")) or "normal"
        if external_mode != "normal":
            task["disable_external_llm"] = True
            task["external_llm_skip_reason"] = (
                (list(external_mode_control.get("control_reasons") or []) or ["external_llm_mode_control"])[0]
            )

        pipeline_mode_control = dict(mode_controls.get("pipeline_staged") or {})
        pipeline_mode = _normalize_feedback_text(pipeline_mode_control.get("control_mode")) or "normal"
        if pipeline_mode != "normal":
            task["disable_pipeline_staged"] = True
            task["pipeline_staged_skip_reason"] = (
                (list(pipeline_mode_control.get("control_reasons") or []) or ["pipeline_staged_mode_control"])[0]
            )

        optimizer_mode_control = dict(mode_controls.get("rl_bandit") or {})
        optimizer_mode = _normalize_feedback_text(optimizer_mode_control.get("control_mode")) or "normal"
        if optimizer_mode != "normal":
            task["disable_optimizer"] = True
            task["optimizer_skip_reason"] = (
                (list(optimizer_mode_control.get("control_reasons") or []) or ["rl_bandit_mode_control"])[0]
            )

        resolved.append(task)
    return resolved


def _build_feedback_budget_meta(
    *,
    planning_feedback_summary: dict[str, Any],
    selected_feedback_summary: dict[str, Any],
    scan_task_budget: int,
    bulk_task_budget: int,
    selected_scan_count: int,
    selected_bulk_count: int,
    planned_bulk_count: int,
    bulk_selection_mode: str | None = None,
) -> dict[str, Any]:
    summary = {
        "max_research_tasks": int(scan_task_budget),
        "max_bulk_research_tasks": int(bulk_task_budget),
        "combined_research_task_budget": int(scan_task_budget + bulk_task_budget),
        "scan_research_task_budget": int(scan_task_budget),
        "reserved_bulk_task_budget": int(bulk_task_budget or AUTONOMY_RESERVED_BULK_RESEARCH_TASKS),
        "selected_scan_task_count": int(selected_scan_count),
        "selected_bulk_task_count": int(selected_bulk_count),
        "planned_bulk_task_count": int(planned_bulk_count),
        "clipped_bulk_task_count": int(max(0, planned_bulk_count - selected_bulk_count)),
        "planned_feedback_control_mode_counts": dict(
            planning_feedback_summary.get("feedback_control_mode_counts") or {}
        ),
        "planned_feedback_legacy_control_mode_counts": dict(
            planning_feedback_summary.get("feedback_legacy_control_mode_counts")
            or planning_feedback_summary.get("feedback_control_mode_counts")
            or {}
        ),
        "planned_feedback_skill_control_mode_counts": dict(
            planning_feedback_summary.get("feedback_skill_control_mode_counts") or {}
        ),
        "planned_feedback_target_pool_control_mode_counts": dict(
            planning_feedback_summary.get("feedback_target_pool_control_mode_counts") or {}
        ),
        "planned_feedback_holding_bucket_control_mode_counts": dict(
            planning_feedback_summary.get("feedback_holding_bucket_control_mode_counts") or {}
        ),
        "planned_feedback_generator_mode_control_mode_counts": dict(
            planning_feedback_summary.get("feedback_generator_mode_control_mode_counts") or {}
        ),
        "planned_feedback_skill_target_pool_control_mode_counts": dict(
            planning_feedback_summary.get("feedback_skill_target_pool_control_mode_counts") or {}
        ),
        "planned_feedback_skill_holding_bucket_control_mode_counts": dict(
            planning_feedback_summary.get("feedback_skill_holding_bucket_control_mode_counts")
            or {}
        ),
        "planned_feedback_skill_generator_mode_control_mode_counts": dict(
            planning_feedback_summary.get("feedback_skill_generator_mode_control_mode_counts")
            or {}
        ),
        "planned_feedback_cooldown_task_count": int(
            planning_feedback_summary.get("feedback_cooldown_task_count") or 0
        ),
        "planned_feedback_limited_task_count": int(
            planning_feedback_summary.get("feedback_limited_task_count") or 0
        ),
        "planned_feedback_relaxed_task_count": int(
            planning_feedback_summary.get("feedback_relaxed_task_count") or 0
        ),
        "blocked_feedback_task_count": int(
            planning_feedback_summary.get("feedback_blocked_task_count") or 0
        ),
        "suppressed_families": list(planning_feedback_summary.get("suppressed_families") or []),
        "suppressed_target_pools": list(planning_feedback_summary.get("suppressed_target_pools") or []),
        "suppressed_holding_buckets": list(
            planning_feedback_summary.get("suppressed_holding_buckets") or []
        ),
        "suppressed_generator_modes": list(planning_feedback_summary.get("suppressed_generator_modes") or []),
        "selected_feedback_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_control_mode_counts") or {}
        ),
        "selected_feedback_legacy_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_legacy_control_mode_counts")
            or selected_feedback_summary.get("feedback_control_mode_counts")
            or {}
        ),
        "selected_feedback_skill_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_skill_control_mode_counts") or {}
        ),
        "selected_feedback_target_pool_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_target_pool_control_mode_counts") or {}
        ),
        "selected_feedback_holding_bucket_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_holding_bucket_control_mode_counts") or {}
        ),
        "selected_feedback_generator_mode_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_generator_mode_control_mode_counts") or {}
        ),
        "selected_feedback_skill_target_pool_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_skill_target_pool_control_mode_counts") or {}
        ),
        "selected_feedback_skill_holding_bucket_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_skill_holding_bucket_control_mode_counts")
            or {}
        ),
        "selected_feedback_skill_generator_mode_control_mode_counts": dict(
            selected_feedback_summary.get("feedback_skill_generator_mode_control_mode_counts")
            or {}
        ),
        "selected_feedback_limited_task_count": int(
            selected_feedback_summary.get("feedback_limited_task_count") or 0
        ),
        "selected_feedback_relaxed_task_count": int(
            selected_feedback_summary.get("feedback_relaxed_task_count") or 0
        ),
    }
    if bulk_selection_mode:
        summary["bulk_selection_mode"] = bulk_selection_mode
    return summary


def build_scan_only_task_budget_meta(
    scan_tasks: list[dict[str, Any]],
    selected_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    planning_feedback_summary = summarize_task_feedback_controls(scan_tasks)
    selected_feedback_summary = summarize_task_feedback_controls(selected_tasks)
    return _build_feedback_budget_meta(
        planning_feedback_summary=planning_feedback_summary,
        selected_feedback_summary=selected_feedback_summary,
        scan_task_budget=int(AUTONOMY_MAX_RESEARCH_TASKS),
        bulk_task_budget=0,
        selected_scan_count=len(selected_tasks),
        selected_bulk_count=0,
        planned_bulk_count=0,
    )


def merge_autonomy_tasks_with_budget(
    scanner,
    scan_tasks: list[dict[str, Any]],
    bulk_tasks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep scan and bulk lanes on separate task budgets."""

    def _task_family_key(task: dict[str, Any]) -> str:
        payload = dict(task or {})
        research_task = dict(payload.get("research_task") or {})
        for source in (payload, research_task):
            for key in ("candidate_family", "candidate_family_id", "strategy_family", "family"):
                value = str(source.get(key) or "").strip().lower()
                if value:
                    return value
        return str(
            payload.get("opportunity_type")
            or payload.get("strategy_type")
            or payload.get("task_source")
            or "unknown"
        ).strip().lower() or "unknown"

    def _interleave_by_family(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for task in list(tasks or []):
            family = _task_family_key(task)
            if family not in buckets:
                buckets[family] = []
                order.append(family)
            buckets[family].append(task)
        if len(order) <= 1:
            return list(tasks or [])
        interleaved: list[dict[str, Any]] = []
        remaining = sum(len(bucket) for bucket in buckets.values())
        while remaining > 0:
            progressed = False
            for family in order:
                bucket = buckets.get(family) or []
                if not bucket:
                    continue
                interleaved.append(bucket.pop(0))
                remaining -= 1
                progressed = True
            if not progressed:
                break
        return interleaved

    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return 0.0

    def _feedback_blocked(task: dict[str, Any]) -> bool:
        return bool(
            dict(task or {}).get("feedback_generation_blocked")
            or _normalize_feedback_text(dict(task or {}).get("feedback_control_mode")) in {"suppress", "freeze"}
        )

    def _uses_bulk_matrix_plan(task: dict[str, Any]) -> bool:
        payload = dict(task or {})
        if str(payload.get("task_source") or "").strip().lower() != "bulk_stock_matrix":
            return False
        if any(
            _safe_int(payload.get(key)) > 0
            for key in (
                "matrix_budget_slot",
                "matrix_plan_slot",
                "matrix_allocation_pass",
                "matrix_family_rank",
                "matrix_stock_rank",
                "matrix_shard_id",
                "matrix_batch_id",
            )
        ):
            return True
        return (
            _safe_float(payload.get("stock_family_priority")) > 0.0
            or bool(payload.get("stock_family_allocation_source"))
        )

    def _bulk_task_plan_key(task: dict[str, Any]) -> tuple[Any, ...]:
        payload = dict(task or {})
        if _uses_bulk_matrix_plan(payload):
            return (
                0,
                _safe_int(payload.get("matrix_budget_slot")) or 10**9,
                _safe_int(payload.get("matrix_plan_slot")) or 10**9,
                _safe_int(payload.get("matrix_allocation_pass")) or 10**9,
                _safe_int(payload.get("matrix_family_rank")) or 10**9,
                _safe_int(payload.get("matrix_stock_rank")) or 10**9,
                _safe_int(payload.get("matrix_shard_id")) or 10**9,
                _safe_int(payload.get("matrix_batch_id")) or 10**9,
                -_safe_float(payload.get("stock_family_priority")),
                -_safe_float(payload.get("matrix_priority_score")),
                -_safe_float(payload.get("priority")),
                str(payload.get("task_id") or payload.get("task_key") or ""),
            )
        return (
            1,
            -_safe_float(scanner._task_sort_key(payload)),
            str(payload.get("task_id") or payload.get("task_key") or ""),
        )

    planning_feedback_summary = summarize_task_feedback_controls([*list(scan_tasks or []), *list(bulk_tasks or [])])

    normalized_scan_tasks = [
        task
        for task in scanner._deduplicate_tasks(list(scan_tasks or []))
        if not _feedback_blocked(task)
    ]
    normalized_scan_tasks.sort(key=scanner._task_sort_key, reverse=True)
    normalized_bulk_tasks = [
        task
        for task in scanner._deduplicate_tasks(list(bulk_tasks or []))
        if not _feedback_blocked(task)
    ]
    bulk_selection_mode = "family_interleave"
    if any(_uses_bulk_matrix_plan(task) for task in normalized_bulk_tasks):
        normalized_bulk_tasks.sort(key=_bulk_task_plan_key)
        bulk_selection_mode = "matrix_plan_slot"
    else:
        normalized_bulk_tasks.sort(key=scanner._task_sort_key, reverse=True)
    scan_task_budget = max(0, int(AUTONOMY_MAX_RESEARCH_TASKS))
    bulk_task_budget = 0
    if normalized_bulk_tasks:
        bulk_task_budget = min(
            len(normalized_bulk_tasks),
            max(0, int(AUTONOMY_MAX_BULK_RESEARCH_TASKS)),
        )
    if len(normalized_scan_tasks) > scan_task_budget:
        normalized_scan_tasks = normalized_scan_tasks[:scan_task_budget]
    selected_bulk_tasks = list(normalized_bulk_tasks[:bulk_task_budget])
    if bulk_selection_mode == "family_interleave":
        selected_bulk_tasks = _interleave_by_family(selected_bulk_tasks)

    merged_tasks = scanner._deduplicate_tasks([*normalized_scan_tasks, *selected_bulk_tasks])
    selected_feedback_summary = summarize_task_feedback_controls(merged_tasks)

    selected_bulk_count = len(
        [
            task
            for task in merged_tasks
            if str((task or {}).get("task_source") or "").strip().lower() == "bulk_stock_matrix"
        ]
    )
    selected_scan_count = max(0, len(merged_tasks) - selected_bulk_count)
    planned_bulk_count = len(normalized_bulk_tasks)
    return merged_tasks, _build_feedback_budget_meta(
        planning_feedback_summary=planning_feedback_summary,
        selected_feedback_summary=selected_feedback_summary,
        scan_task_budget=scan_task_budget,
        bulk_task_budget=bulk_task_budget,
        selected_scan_count=selected_scan_count,
        selected_bulk_count=selected_bulk_count,
        planned_bulk_count=planned_bulk_count,
        bulk_selection_mode=bulk_selection_mode,
    )
