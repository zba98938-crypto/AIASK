"""Shared constants for stock deep analysis."""

from __future__ import annotations

ANALYSIS_STRATEGY = "stock_deep_analysis"
ANALYSIS_VERSION = "stock-deep-analysis.v1"
SUPPORTED_ANALYSIS_TASKS = {"quick_scan", "deep_analysis", "recover_gaps", "rebuild_report", "trade_plan"}
_SUMMARY_ONLY_FIELDS = (
    "run_id",
    "task",
    "status",
    "code",
    "name",
    "market",
    "current_stage",
    "report_ready",
    "digest",
    "gap_count",
    "artifact_ids",
    "resource_uris",
    "updated_at",
)
