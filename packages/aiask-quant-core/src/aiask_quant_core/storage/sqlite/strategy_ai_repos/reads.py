from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..strategy_factory_json_budget import (
    bounded_json_text,
    full_market_score_retention_runs,
    full_market_score_topn,
    strategy_json_field_max_bytes,
)

logger = logging.getLogger(__name__)


class _ReadsMixin:
    """AI 生成实验 / 任务运行 / 工厂运行"""

    @staticmethod
    def _generation_experiment_field_max_bytes() -> int:
        raw = str(os.getenv("STRATEGY_GENERATION_EXPERIMENT_FIELD_MAX_BYTES") or "").strip()
        try:
            value = int(raw) if raw else strategy_json_field_max_bytes()
        except Exception:
            value = strategy_json_field_max_bytes()
        return max(4096, value)

    @staticmethod
    def _task_run_result_max_bytes() -> int:
        raw = str(os.getenv("STRATEGY_TASK_RUN_RESULT_MAX_BYTES") or "").strip()
        try:
            value = int(raw) if raw else strategy_json_field_max_bytes()
        except Exception:
            value = strategy_json_field_max_bytes()
        return max(4096, value)

    @staticmethod
    def _factory_run_field_max_bytes(field_name: str) -> int:
        normalized = str(field_name or "").strip().lower()
        env_name = f"STRATEGY_FACTORY_RUN_{normalized.upper()}_MAX_BYTES"
        raw = str(os.getenv(env_name) or os.getenv("STRATEGY_FACTORY_RUN_FIELD_MAX_BYTES") or "").strip()
        defaults = {
            "summary": 64 * 1024,
            "stages": 128 * 1024,
            "snapshot_summary": 64 * 1024,
        }
        try:
            value = int(raw) if raw else defaults.get(normalized, 1024 * 1024)
        except Exception:
            value = defaults.get(normalized, 1024 * 1024)
        return max(4096, value)

    @staticmethod
    def _factory_artifact_payload_max_bytes() -> int:
        raw = str(os.getenv("STRATEGY_FACTORY_ARTIFACT_PAYLOAD_MAX_BYTES") or "").strip()
        try:
            value = int(raw) if raw else strategy_json_field_max_bytes()
        except Exception:
            value = strategy_json_field_max_bytes()
        return max(4096, value)

    @staticmethod
    def _large_json_collection_keys() -> set[str]:
        return {
            "passed_candidates",
            "failed_candidates",
            "trades",
            "fills",
            "orders",
            "positions",
            "round_trip_positions",
            "equity_curve",
            "cash_curve",
            "gross_exposure_curve",
            "net_exposure_curve",
            "component_metrics",
            "event_window_metrics",
            "raw_events",
            "samples",
            "klines",
            "ohlcv",
        }

    @classmethod
    def _json_field_size_bytes(cls, value: Any) -> int:
        try:
            return len(json.dumps(value or {}, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception:
            return 0

    @classmethod
    def _large_json_node_summary(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {
                "storage_mode": "dropped_large_payload",
                "node_type": "dict",
                "key_count": len(value),
                "keys": sorted(str(key) for key in list(value.keys())[:24]),
                "size_bytes": cls._json_field_size_bytes(value),
            }
        if isinstance(value, (list, tuple)):
            return {
                "storage_mode": "dropped_large_payload",
                "node_type": "list",
                "item_count": len(value),
                "size_bytes": cls._json_field_size_bytes(value),
            }
        return {
            "storage_mode": "dropped_large_payload",
            "node_type": type(value).__name__,
            "size_bytes": cls._json_field_size_bytes(value),
        }

    @classmethod
    def _scrub_storage_json(cls, value: Any, *, depth: int = 0) -> Any:
        if value in (None, "", [], {}):
            return {} if isinstance(value, dict) else [] if isinstance(value, list) else value
        if isinstance(value, (str, int, float, bool)):
            return value
        if depth >= 4:
            return cls._large_json_node_summary(value)
        if isinstance(value, dict):
            compact: dict[str, Any] = {}
            heavy_keys = cls._large_json_collection_keys()
            for raw_key, item in value.items():
                key = str(raw_key)
                if item in (None, "", [], {}):
                    continue
                if key in heavy_keys and isinstance(item, (dict, list, tuple)):
                    compact[f"{key}_summary"] = cls._large_json_node_summary(item)
                    continue
                if key in {"passed", "failed", "candidates", "results", "items"} and isinstance(item, list):
                    compact[f"{key}_summary"] = cls._large_json_node_summary(item)
                    continue
                compact[key] = cls._scrub_storage_json(item, depth=depth + 1)
            return compact
        if isinstance(value, (list, tuple)):
            values = list(value)
            preview = [
                cls._scrub_storage_json(item, depth=depth + 1)
                for item in values[:12]
            ]
            if len(values) > 12:
                preview.append({"truncated_item_count": len(values) - 12})
            return preview
        return str(value)

    @classmethod
    def _encode_bounded_storage_json(cls, field_name: str, value: Any, *, max_bytes: int) -> str:
        return bounded_json_text(field_name, value or {}, max_bytes=max_bytes)

    @staticmethod
    def _compact_mapping(value: Any, *, keys: tuple[str, ...]) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        for key in keys:
            item = value.get(key)
            if item in (None, "", [], {}):
                continue
            result[key] = item
        return result

    @staticmethod
    def _preview_plain_list(value: Any, *, limit: int = 12) -> list[Any]:
        if not isinstance(value, list):
            return []
        preview: list[Any] = []
        for item in value[:limit]:
            if isinstance(item, (str, int, float, bool)) or item is None:
                preview.append(item)
            elif isinstance(item, dict):
                preview.append({key: item.get(key) for key in list(item.keys())[:6]})
            else:
                preview.append(str(item))
        return preview

    @classmethod
    def _summarize_scalar_mapping(cls, value: Any, *, limit: int = 20) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        keys = list(value.keys())
        for key in keys[:limit]:
            item = value.get(key)
            normalized_key = str(key)
            if isinstance(item, (str, int, float, bool)) or item is None:
                result[normalized_key] = item
                continue
            if isinstance(item, list):
                preview = cls._preview_plain_list(item, limit=8)
                if preview:
                    result[normalized_key] = preview
                result[f"{normalized_key}_count"] = len(item)
                continue
            if isinstance(item, dict):
                nested = {
                    str(nested_key): nested_value
                    for nested_key, nested_value in list(item.items())[:8]
                    if isinstance(nested_value, (str, int, float, bool)) or nested_value is None
                }
                if nested:
                    result[normalized_key] = nested
                else:
                    result[normalized_key] = {
                        "count": len(item),
                        "keys": sorted(str(nested_key) for nested_key in list(item.keys())[:20]),
                    }
                continue
            result[normalized_key] = str(item)
        if len(keys) > limit:
            result["truncated_key_count"] = len(keys) - limit
        return result

    @classmethod
    def _summarize_factory_task_result_preview(cls, value: Any) -> dict[str, Any]:
        payload = dict(value) if isinstance(value, dict) else {}
        summary = cls._compact_mapping(
            payload,
            keys=(
                "task_run_id",
                "task_source",
                "event_id",
                "theme_code",
                "evidence_count",
                "status",
                "generated_count",
                "reviewed_count",
                "external_llm_status",
                "error",
            ),
        )
        task = dict(payload.get("task") or {})
        if task:
            summary["task"] = cls._compact_mapping(
                task,
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
                ),
            )
            target_symbols = cls._preview_plain_list(task.get("target_symbols"), limit=8)
            if target_symbols:
                summary["task"]["target_symbols"] = target_symbols
        lifecycle_summary = dict(payload.get("lifecycle_summary") or {})
        if lifecycle_summary:
            summary["lifecycle_summary"] = cls._compact_mapping(
                lifecycle_summary,
                keys=(
                    "state",
                    "current_phase",
                    "failed_phase",
                    "terminal_phase",
                    "completed_phase_count",
                    "event_count",
                ),
            )
            phase_status_counts = cls._summarize_scalar_mapping(
                lifecycle_summary.get("phase_status_counts"),
                limit=12,
            )
            if phase_status_counts:
                summary["lifecycle_summary"]["phase_status_counts"] = phase_status_counts
        return summary

    @classmethod
    def _summarize_factory_stage_payload(cls, stage_name: str, value: Any) -> dict[str, Any]:
        payload = dict(value) if isinstance(value, dict) else {}
        summary = cls._compact_mapping(
            payload,
            keys=(
                "stage",
                "trace_id",
                "status",
                "ok",
                "hard_failure",
                "degraded",
                "skip_reason",
                "error",
            ),
        )
        for key in (
            "task_count",
            "event_task_count",
            "snapshot_task_count",
            "bulk_stock_task_count",
            "event_evidence_count",
            "completed_task_count",
            "failed_task_count",
            "generated_count",
            "experiment_count",
            "input_count",
            "passed_count",
            "failed_count",
            "count",
            "warning_count",
            "blocker_count",
            "critical_blocker_count",
            "readiness_score",
            "can_proceed",
            "external_llm_status",
            "external_llm_attempt_count",
            "external_llm_stage_attempt_count",
            "external_llm_network_request_count",
            "external_llm_compatibility_skip_count",
            "external_llm_cooldown_skip_count",
            "external_llm_selected_count",
            "external_llm_elapsed_seconds",
            "research_task_concurrency",
            "bulk_task_concurrency",
            "research_task_timeout_sec",
            "persistence_failure_count",
            "submitted",
            "gate_3_input",
            "gate_3_passed",
            "gate_3_failed",
            "created",
            "created_total",
            "created_strategy_pool",
            "created_audit_only",
            "eliminated",
        ):
            item = payload.get(key)
            if item not in (None, "", [], {}):
                summary[key] = item

        for key in (
            "task_source_counts",
            "external_llm_status_counts",
            "external_llm_request_status_counts",
            "lifecycle_state_counts",
            "phase_status_counts",
            "failed_phase_counts",
        ):
            item = cls._summarize_scalar_mapping(payload.get(key), limit=20)
            if item:
                summary[key] = item

        task_run_ids = cls._preview_plain_list(payload.get("task_run_ids"), limit=12)
        if task_run_ids:
            summary["task_run_ids"] = task_run_ids
            summary["task_run_id_count"] = len(list(payload.get("task_run_ids") or []))

        persistence_failures = list(payload.get("persistence_failures") or [])
        if persistence_failures:
            summary["persistence_failures"] = [
                cls._compact_mapping(
                    dict(item or {}),
                    keys=("operation", "stage", "error_type", "error"),
                )
                for item in persistence_failures[:8]
            ]
            summary["persistence_failure_count"] = len(persistence_failures)

        if stage_name == "autonomy":
            task_scan = dict(payload.get("task_scan") or {})
            task_scan_summary = dict(task_scan.get("summary") or {})
            if task_scan_summary:
                summary["task_scan"] = {"summary": cls._summarize_scalar_mapping(task_scan_summary, limit=80)}
            task_results = list(payload.get("task_results") or [])
            if task_results:
                summary["task_results"] = [
                    cls._summarize_factory_task_result_preview(item)
                    for item in task_results[:20]
                ]
                summary["task_result_count"] = len(task_results)
            observable_phases = cls._preview_plain_list(payload.get("observable_phases"), limit=12)
            if observable_phases:
                summary["observable_phases"] = observable_phases
            task_artifact = cls._summarize_autonomy_task_artifact(payload.get("task_artifact"))
            if task_artifact:
                summary["task_artifact"] = task_artifact
            candidate_artifact = cls._summarize_autonomy_candidate_artifact(
                payload.get("candidate_artifact")
            )
            if candidate_artifact:
                summary["candidate_artifact"] = candidate_artifact
            evidence_artifact = cls._summarize_autonomy_evidence_artifact(
                payload.get("evidence_artifact")
            )
            if evidence_artifact:
                summary["evidence_artifact"] = evidence_artifact
            return summary

        if stage_name == "snapshot_summary":
            return summary

        if stage_name in {"quality_gate", "backtest", "deduplicate", "submit", "factor_research"}:
            nested_summary = cls._summarize_scalar_mapping(payload.get("summary"), limit=40)
            if nested_summary:
                summary["summary"] = nested_summary

        if stage_name == "submit":
            for key in ("gate_3_failure_reason_topn", "items", "strategies"):
                values = list(payload.get(key) or [])
                if not values:
                    continue
                summary[key] = [
                    cls._compact_mapping(
                        dict(item or {}),
                        keys=(
                            "experiment_id",
                            "strategy_id",
                            "passed",
                            "duplicate",
                            "reason_code",
                            "status",
                        ),
                    )
                    for item in values[:10]
                ]
                summary[f"{key}_count"] = len(values)
            incubation_budget_summary = cls._summarize_scalar_mapping(
                payload.get("incubation_budget_summary"),
                limit=20,
            )
            if incubation_budget_summary:
                summary["incubation_budget_summary"] = incubation_budget_summary
            return summary

        if stage_name == "elimination":
            items = list(payload.get("items") or [])
            if items:
                summary["items"] = [
                    cls._compact_mapping(
                        dict(item or {}),
                        keys=("strategy_id", "reason", "red_flags", "status"),
                    )
                    for item in items[:10]
                ]
                summary["item_count"] = len(items)
            return summary

        for key, item in list(payload.items())[:40]:
            if key in summary:
                continue
            if isinstance(item, (str, int, float, bool)) or item is None:
                summary[key] = item
            elif isinstance(item, dict):
                nested = cls._summarize_scalar_mapping(item, limit=16)
                if nested:
                    summary[key] = nested
            elif isinstance(item, list):
                preview = cls._preview_plain_list(item, limit=8)
                if preview:
                    summary[key] = preview
                summary[f"{key}_count"] = len(item)
        return summary

    @classmethod
    def _summarize_autonomy_task_artifact(cls, value: Any) -> dict[str, Any]:
        payload = dict(value) if isinstance(value, dict) else {}
        if not payload:
            return {}
        summary = cls._compact_mapping(
            payload,
            keys=(
                "contract_version",
                "available",
                "planned_task_count",
                "executed_task_count",
                "completed_task_count",
                "failed_task_count",
                "generated_candidate_count",
                "event_task_count",
                "snapshot_task_count",
                "bulk_stock_task_count",
                "bulk_stock_matrix_enabled",
                "bulk_stock_matrix_stock_count",
                "bulk_stock_matrix_eligible_stock_count",
                "governed_candidate_activation_task_count",
                "open_research_task_count",
            ),
        )
        for key in (
            "task_source_counts",
            "task_origin_counts",
            "feedback_control_mode_counts",
            "feedback_target_pool_control_mode_counts",
            "feedback_holding_bucket_control_mode_counts",
            "feedback_generator_mode_control_mode_counts",
        ):
            item = cls._summarize_scalar_mapping(payload.get(key), limit=20)
            if item:
                summary[key] = item
        for source_key, target_key in (
            ("planned_task_briefs", "planned_task_briefs"),
            ("task_result_briefs", "task_result_briefs"),
        ):
            values = list(payload.get(source_key) or [])
            if not values:
                continue
            summary[target_key] = [
                cls._compact_mapping(
                    dict(item or {}),
                    keys=(
                        "task_id",
                        "task_source",
                        "opportunity_type",
                        "candidate_family",
                        "factor_name",
                        "generation_limit",
                        "generated_count",
                        "status",
                    ),
                )
                for item in values[:10]
            ]
            summary[f"{target_key}_count"] = len(values)
        return summary

    @classmethod
    def _summarize_autonomy_candidate_artifact(cls, value: Any) -> dict[str, Any]:
        payload = dict(value) if isinstance(value, dict) else {}
        if not payload:
            return {}
        summary = cls._compact_mapping(
            payload,
            keys=(
                "contract_version",
                "available",
                "candidate_count",
                "targeted_candidate_count",
                "experiment_linked_count",
                "candidate_contract_ready_count",
                "candidate_evidence_ready_count",
                "local_rule_candidate_count",
                "external_autonomy_candidate_count",
                "governed_candidate_activation_count",
            ),
        )
        for key in (
            "candidate_origin_counts",
            "generator_type_counts",
            "task_source_counts",
            "family_counts",
        ):
            item = cls._summarize_scalar_mapping(payload.get(key), limit=20)
            if item:
                summary[key] = item
        briefs = list(payload.get("candidate_briefs") or [])
        if briefs:
            summary["candidate_briefs"] = [
                cls._compact_mapping(
                    dict(item or {}),
                    keys=(
                        "name",
                        "strategy_type",
                        "family",
                        "task_source",
                        "generator_type",
                        "origin",
                        "candidate_contract_ready",
                        "evidence_ready",
                        "experiment_id",
                    ),
                )
                for item in briefs[:12]
            ]
            summary["candidate_brief_count"] = len(briefs)
        return summary

    @classmethod
    def _summarize_autonomy_evidence_artifact(cls, value: Any) -> dict[str, Any]:
        payload = dict(value) if isinstance(value, dict) else {}
        if not payload:
            return {}
        summary = cls._compact_mapping(
            payload,
            keys=(
                "contract_version",
                "available",
                "task_evidence_count",
                "task_run_count",
                "governed_candidate_activation_task_count",
                "experiment_count",
                "external_llm_status",
                "external_llm_attempt_count",
                "external_llm_network_request_count",
                "external_llm_real_request_count",
                "external_llm_selected_count",
                "external_llm_compatibility_skip_count",
                "external_llm_cooldown_skip_count",
                "external_llm_compatibility_failure_count",
                "external_llm_effective_response_count",
                "external_llm_empty_200_response_count",
                "external_llm_effective_response_ratio",
                "external_llm_provider_health_status",
                "persistence_failure_count",
                "last_error_type",
                "last_error",
            ),
        )
        for key in (
            "task_result_status_counts",
            "task_origin_counts",
            "external_llm_status_counts",
        ):
            item = cls._summarize_scalar_mapping(payload.get(key), limit=20)
            if item:
                summary[key] = item
        task_run_ids = cls._preview_plain_list(payload.get("task_run_ids"), limit=12)
        if task_run_ids:
            summary["task_run_ids"] = task_run_ids
            summary["task_run_id_count"] = len(list(payload.get("task_run_ids") or []))
        briefs = list(payload.get("experiment_briefs") or [])
        if briefs:
            summary["experiment_briefs"] = [
                cls._compact_mapping(
                    dict(item or {}),
                    keys=(
                        "artifact_id",
                        "strategy_type",
                        "generator_type",
                        "task_source",
                        "source",
                        "task_run_id",
                        "candidate_contract_ready",
                        "evidence_ready",
                    ),
                )
                for item in briefs[:12]
            ]
            summary["experiment_brief_count"] = len(briefs)
        return summary

    @classmethod
    def _compact_factory_stage_payload(cls, stage_name: str, value: Any) -> dict[str, Any]:
        payload = dict(value) if isinstance(value, dict) else {}
        original_size_bytes = len(
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        ) if payload else 0
        compacted = cls._summarize_factory_stage_payload(stage_name, payload)
        if not compacted:
            compacted = {
                "stage": str(stage_name or "").strip() or None,
                "truncated": True,
            }
        compacted["storage_mode"] = "inline_compact_stage"
        compacted["truncated"] = True
        compacted["original_size_bytes"] = int(original_size_bytes)
        return compacted

    @classmethod
    def _compact_factory_run_stages_to_fit(
        cls,
        value: Any,
        *,
        max_bytes: int,
    ) -> tuple[dict[str, Any], bool]:
        payload = dict(value) if isinstance(value, dict) else {}
        if not payload:
            return payload, False

        compacted: dict[str, Any] = {
            str(stage_name): stage_payload
            for stage_name, stage_payload in payload.items()
        }
        changed = False

        def _encoded_size(current: dict[str, Any]) -> int:
            return len(json.dumps(current, ensure_ascii=False, default=str).encode("utf-8"))

        current_size = _encoded_size(compacted)
        if current_size <= max_bytes:
            return compacted, False

        stage_sizes: list[tuple[str, int]] = []
        for stage_name, stage_payload in compacted.items():
            if not isinstance(stage_payload, dict):
                continue
            stage_size = len(
                json.dumps(stage_payload, ensure_ascii=False, default=str).encode("utf-8")
            )
            stage_sizes.append((str(stage_name), stage_size))
        stage_sizes.sort(key=lambda item: item[1], reverse=True)

        for stage_name, original_stage_size in stage_sizes:
            if current_size <= max_bytes:
                break
            stage_payload = compacted.get(stage_name)
            if not isinstance(stage_payload, dict):
                continue
            if str(stage_payload.get("storage_mode") or "").strip().lower() == "inline_compact_stage":
                continue
            compact_stage = cls._compact_factory_stage_payload(stage_name, stage_payload)
            compact_stage_size = len(
                json.dumps(compact_stage, ensure_ascii=False, default=str).encode("utf-8")
            )
            if compact_stage_size >= original_stage_size:
                continue
            compacted[stage_name] = compact_stage
            current_size = _encoded_size(compacted)
            changed = True

        return compacted, changed
