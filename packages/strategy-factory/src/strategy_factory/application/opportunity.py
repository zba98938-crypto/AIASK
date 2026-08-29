"""策略工厂机会扫描与研究任务生成。"""

from __future__ import annotations

from typing import Any, Dict, List

from ..domain.constants import AUTONOMY_MAX_RESEARCH_TASKS, OPPORTUNITY_UNIVERSE_LIMIT
from ._stock_universe_loader import filter_stock_universe_rows_by_codes, load_stock_universe_rows
from ._opportunity_event import _MarketOpportunityScannerEventMixin
from ._opportunity_snapshot import _MarketOpportunityScannerSnapshotMixin
from ._opportunity_utils import _MarketOpportunityScannerUtilityMixin
from .research_plane_contract import build_task_artifact


class MarketOpportunityScanner(
    _MarketOpportunityScannerUtilityMixin,
    _MarketOpportunityScannerEventMixin,
    _MarketOpportunityScannerSnapshotMixin,
):
    """根据市场快照与事件证据生成自治研究任务。"""

    def __init__(self):
        self.last_report: dict = {
            "summary": {"task_count": 0, "task_types": {}, "themes": [], "task_sources": {}},
            "tasks": [],
        }

    def get_last_report(self) -> dict:
        return dict(self.last_report)

    async def scan(self, db, snapshot: dict[str, Any]) -> dict[str, Any]:
        snapshot = dict(snapshot or {})
        universe_page_size = max(100, min(int(OPPORTUNITY_UNIVERSE_LIMIT), 1000))
        try:
            universe_rows, universe_meta = await load_stock_universe_rows(
                db,
                limit=OPPORTUNITY_UNIVERSE_LIMIT,
                page_size=universe_page_size,
                start_offset=0,
            )
        except Exception:
            universe_rows, universe_meta = [], {
                "pages_loaded": 0,
                "loaded_count": 0,
                "complete": False,
                "truncated": False,
                "page_size": universe_page_size,
            }

        target_codes = self._normalize_codes(
            snapshot.get("candidate_codes")
            or snapshot.get("target_codes")
            or snapshot.get("requested_target_codes"),
            limit=64,
        )
        rows, target_filter_meta = filter_stock_universe_rows_by_codes(universe_rows, target_codes)
        universe_meta = {**dict(universe_meta or {}), **target_filter_meta}
        tasks: List[dict] = []

        # PR-6: Generate tasks from manually injected events (theme graph propagation)
        manual_event_tasks: List[dict] = []
        manual_event_meta: dict = {}
        try:
            from .research.event_task_generator import generate_tasks_from_active_events
            manual_event_result = await generate_tasks_from_active_events(
                db,
                snapshot,
                claim_outbox=True,
            )
            if manual_event_result.get("enabled"):
                manual_event_tasks = list(manual_event_result.get("tasks") or [])
                manual_event_meta = {
                    "manual_event_count": int(manual_event_result.get("event_count") or 0),
                    "manual_event_task_count": len(manual_event_tasks),
                    "manual_event_impact_count": int(manual_event_result.get("impact_count") or 0),
                    "manual_event_outbox_claimed": int(manual_event_result.get("outbox_claimed") or 0),
                    "manual_event_outbox_skipped": int(manual_event_result.get("outbox_skipped") or 0),
                    "manual_event_outbox_failed": int(manual_event_result.get("outbox_failed") or 0),
                }
        except Exception as exc:
            import logging
            logging.getLogger(__name__).debug("MarketOpportunityScanner: manual event tasks failed: %s", exc)
            manual_event_meta = {"manual_event_error": str(exc)}

        event_tasks = self._deduplicate_tasks(self._build_event_driven_tasks(snapshot, rows))
        snapshot_tasks = self._deduplicate_tasks(self._build_snapshot_tasks(snapshot, rows))

        # Merge: manual events (highest priority) → auto events → snapshot
        if manual_event_tasks:
            tasks.extend(manual_event_tasks)
        if event_tasks:
            tasks.extend(event_tasks)
            tasks.extend(self._select_snapshot_tasks_for_event_mix(event_tasks, snapshot_tasks))
        else:
            tasks.extend(snapshot_tasks)

        tasks = self._deduplicate_tasks(tasks)
        tasks.sort(key=self._task_sort_key, reverse=True)
        tasks = tasks[:AUTONOMY_MAX_RESEARCH_TASKS]
        task_sources = self._build_task_source_counts(tasks)

        type_counts: Dict[str, int] = {}
        for task in tasks:
            opportunity_type = str(task.get("opportunity_type") or "unknown")
            type_counts[opportunity_type] = type_counts.get(opportunity_type, 0) + 1

        # PR-C (Phase 1, 2026-05-24): event-driven tasks are now uniformly
        # tagged ``task_source="event_driven"`` and distinguished by
        # ``event_source`` (manual / news_llm / macro_shock / market_anomaly /
        # price_inference). Both old and new statistic keys are derived
        # from the merged task list rather than the manual-only sub-list,
        # so downstream dashboards stay consistent across the migration.
        event_driven_tasks = [
            item for item in tasks
            if str(item.get("task_source") or "").strip() == "event_driven"
        ]
        event_state = dict(snapshot.get("event_driven") or {})
        event_source_counts: Dict[str, int] = {}
        for item in event_driven_tasks:
            origin = str(item.get("event_source") or "").strip() or "unknown"
            event_source_counts[origin] = event_source_counts.get(origin, 0) + 1

        report = {
            "summary": {
                "task_count": len(tasks),
                "task_types": type_counts,
                "themes": [str(item.get("theme") or "") for item in tasks],
                "task_sources": dict(task_sources),
                "event_task_count": len(event_driven_tasks),
                # manual_event_task_count is now defined as
                # event_driven AND event_source==manual (PR-C verification:
                # 7.2 new test). Fallback to len(manual_event_tasks) when
                # the merged list is empty so legacy callers still see a
                # non-negative number.
                "manual_event_task_count": event_source_counts.get(
                    "manual", len(manual_event_tasks)
                ),
                "event_source_counts": event_source_counts,
                "event_cluster_source_counts": dict(event_state.get("event_source_counts") or {}),
                "event_source_tier_counts": dict(event_state.get("source_tier_counts") or {}),
                "verified_event_count": int(event_state.get("verified_event_count") or 0),
                "provisional_event_count": int(event_state.get("provisional_event_count") or 0),
                "single_anchor_event_count": int(event_state.get("single_anchor_event_count") or 0),
                "multi_source_confirmed_event_count": int(event_state.get("multi_source_confirmed_event_count") or 0),
                "conflict_event_count": int(event_state.get("conflict_event_count") or 0),
                "news_only_rejected_count": int(event_state.get("news_only_rejected_count") or 0),
                "stale_event_count": int(event_state.get("stale_event_count") or 0),
                "post_hoc_rejected_count": int(event_state.get("post_hoc_rejected_count") or 0),
                **manual_event_meta,
                "max_tasks": AUTONOMY_MAX_RESEARCH_TASKS,
                "universe_limit": int(OPPORTUNITY_UNIVERSE_LIMIT),
                "universe_page_size": int(universe_meta.get("page_size") or universe_page_size),
                "universe_row_count": len(rows),
                "universe_pages_loaded": int(universe_meta.get("pages_loaded") or 0),
                "universe_complete": bool(universe_meta.get("complete")),
                "universe_truncated": bool(universe_meta.get("truncated")),
                "target_code_filter_applied": bool(universe_meta.get("target_code_filter_applied")),
                "requested_target_codes": list(universe_meta.get("requested_target_codes") or []),
                "target_missing_codes": list(universe_meta.get("target_missing_codes") or []),
            },
            "tasks": tasks,
        }
        task_artifact = build_task_artifact(
            {
                "task_scan": report,
                "task_source_counts": dict(task_sources),
                "event_task_count": int(report["summary"].get("event_task_count") or 0),
                "snapshot_task_count": int(task_sources.get("snapshot") or 0),
                "bulk_stock_task_count": 0,
            }
        )
        report["task_artifact"] = task_artifact
        report["summary"] = {
            **dict(report.get("summary") or {}),
            "task_artifact_contract_version": task_artifact.get("contract_version"),
            "task_artifact_available": bool(task_artifact.get("available")),
        }
        self.last_report = report
        return self.get_last_report()


__all__ = ["MarketOpportunityScanner"]
