"""Helpers for bulk stock matrix planner report defaults and normalization."""

from __future__ import annotations

from typing import Any

from ..domain.constants import (
    STOCK_STRATEGY_MATRIX_BATCH_SIZE,
    STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY,
    STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
)


def _base_bulk_summary(
    bulk_window_state: dict[str, Any],
    bulk_cursor: dict[str, Any],
) -> dict[str, Any]:
    return {
        "enabled": bool(bulk_window_state.get("run_window_active")),
        "configured_enabled": bool(bulk_window_state.get("configured_enabled")),
        "task_count": 0,
        "stock_count": 0,
        "family_counts": {},
        "planned_family_counts": {},
        "universe_limit": STOCK_STRATEGY_MATRIX_UNIVERSE_LIMIT,
        "batch_size": STOCK_STRATEGY_MATRIX_BATCH_SIZE,
        "bulk_concurrency": STOCK_STRATEGY_MATRIX_BULK_CONCURRENCY,
        "requested_universe_offset": int(bulk_cursor.get("next_universe_offset") or 0),
        "effective_universe_offset": 0,
        "universe_offset_fallback": False,
        "next_universe_offset": 0,
        "cursor_wrapped": False,
        "cursor_mode": bulk_cursor.get("cursor_mode") or "task_offset",
        "requested_task_offset": int(bulk_cursor.get("next_task_offset") or 0),
        "effective_task_offset": 0,
        "task_offset_fallback": False,
        "next_task_offset": 0,
        "task_cursor_wrapped": False,
        "planned_task_count": int(bulk_cursor.get("planned_task_count") or 0),
        "planned_candidate_count": 0,
        "loaded_stock_count": 0,
        "pages_loaded": 0,
        "analysis_complete": False,
        "analysis_stock_coverage_ratio": 0.0,
        "cursor_source": bulk_cursor.get("source"),
        "cursor_resume_from_run_id": bulk_cursor.get("resume_from_run_id"),
        "run_window": bulk_window_state.get("run_window"),
        "run_window_active": bool(bulk_window_state.get("run_window_active")),
        "run_window_current_period": bulk_window_state.get("current_period"),
        "skip_reason": bulk_window_state.get("skip_reason"),
        "selected_shard_count": 0,
        "selected_shard_ids": [],
    }


def build_default_bulk_report(
    bulk_window_state: dict[str, Any],
    bulk_cursor: dict[str, Any],
) -> dict[str, Any]:
    return {
        "summary": _base_bulk_summary(bulk_window_state, bulk_cursor),
        "tasks": [],
    }


def build_bulk_planner_error_report(
    bulk_window_state: dict[str, Any],
    bulk_cursor: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    summary = _base_bulk_summary(bulk_window_state, bulk_cursor)
    summary.update(
        {
            "enabled": False,
            "skip_reason": "planner_error",
            "error": str(exc),
        }
    )
    return {
        "summary": summary,
        "tasks": [],
    }


def normalize_bulk_report_summary(
    bulk_report: dict[str, Any] | None,
    bulk_window_state: dict[str, Any],
    bulk_cursor: dict[str, Any],
) -> dict[str, Any]:
    report = dict(bulk_report or {})
    summary = dict(report.get("summary") or {})
    for key, value in _base_bulk_summary(bulk_window_state, bulk_cursor).items():
        summary.setdefault(key, value)
    return {
        **report,
        "summary": summary,
    }
