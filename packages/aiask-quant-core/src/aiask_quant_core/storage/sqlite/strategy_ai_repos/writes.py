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


class _WritesMixin:
    @classmethod
    def _summarize_large_factory_run_field(
        cls,
        field_name: str,
        value: Any,
        *,
        size_bytes: int,
    ) -> dict[str, Any]:
        payload = dict(value) if isinstance(value, dict) else {}
        summary: dict[str, Any] = {
            "storage_mode": "inline_fallback_summary",
            "field_name": str(field_name or "unknown"),
            "truncated": True,
            "original_size_bytes": int(size_bytes),
        }
        if not payload:
            return summary
        summary["top_level_keys"] = sorted(str(key) for key in payload.keys())[:80]

        if field_name == "stages":
            summary["stage_count"] = len(payload)
            summary["stage_names"] = [str(key) for key in list(payload.keys())[:20]]
            for stage_name, stage_payload in payload.items():
                summary[str(stage_name)] = cls._summarize_factory_stage_payload(
                    str(stage_name),
                    stage_payload,
                )
            return summary

        if field_name == "snapshot_summary":
            summary.update(
                cls._compact_mapping(
                    payload,
                    keys=(
                        "date",
                        "fear_greed",
                        "fear_greed_index",
                        "fg_level",
                        "listed_count",
                        "incubating_count",
                        "degraded",
                        "completion_ratio",
                        "failure_reason_count",
                    ),
                )
            )
            missing_sources = cls._preview_plain_list(payload.get("missing_sources"), limit=12)
            if missing_sources:
                summary["missing_sources"] = missing_sources
            factor_research = dict(payload.get("factor_research") or {})
            if factor_research:
                factor_summary = dict(factor_research.get("summary") or {})
                active_candidate_pool = dict(factor_research.get("active_candidate_pool") or {})
                factor_research_summary: dict[str, Any] = {}
                if factor_summary:
                    factor_research_summary["summary"] = cls._summarize_scalar_mapping(
                        factor_summary,
                        limit=40,
                    )
                if active_candidate_pool:
                    compact_pool = cls._compact_mapping(
                        active_candidate_pool,
                        keys=("count", "family_count"),
                    )
                    top_candidates = list(active_candidate_pool.get("top_candidates") or [])
                    if top_candidates:
                        compact_pool["top_candidates"] = [
                            cls._compact_mapping(
                                dict(item or {}),
                                keys=("name", "family", "priority", "score", "source"),
                            )
                            for item in top_candidates[:10]
                        ]
                        compact_pool["top_candidate_count"] = len(top_candidates)
                    if compact_pool:
                        factor_research_summary["active_candidate_pool"] = compact_pool
                source_chain = cls._preview_plain_list(factor_research.get("source_chain"), limit=10)
                if source_chain:
                    factor_research_summary["source_chain"] = source_chain
                if factor_research.get("degraded") not in (None, "", [], {}):
                    factor_research_summary["degraded"] = bool(factor_research.get("degraded"))
                if factor_research_summary:
                    summary["factor_research"] = factor_research_summary
            return summary

        if field_name == "summary":
            for key, item in payload.items():
                if isinstance(item, (str, int, float, bool)) or item is None:
                    summary[key] = item
                    continue
                if key == "autonomy_task_briefs":
                    values = list(item or [])
                    summary["autonomy_task_briefs"] = [
                        cls._compact_mapping(
                            dict(entry or {}),
                            keys=(
                                "task_id",
                                "task_source",
                                "opportunity_type",
                                "candidate_family",
                                "factor_name",
                                "generation_limit",
                                "generated_count",
                            ),
                        )
                        for entry in values[:20]
                    ]
                    summary["autonomy_task_brief_count"] = len(values)
                    continue
                if isinstance(item, dict):
                    nested = cls._summarize_scalar_mapping(item, limit=40)
                    if nested:
                        summary[key] = nested
                    continue
                if isinstance(item, list):
                    preview = cls._preview_plain_list(item, limit=20)
                    if preview:
                        summary[key] = preview
                    summary[f"{key}_count"] = len(item)
            return summary

        return summary

    @classmethod
    def _encode_factory_run_json(cls, field_name: str, value: Any) -> str:
        value = cls._scrub_storage_json(value or {})
        encoded = json.dumps(value or {}, ensure_ascii=False, default=str)
        size_bytes = len(encoded.encode("utf-8"))
        limit = cls._factory_run_field_max_bytes(field_name)
        if size_bytes <= limit:
            return encoded
        if str(field_name or "").strip().lower() == "stages":
            compacted, changed = cls._compact_factory_run_stages_to_fit(
                value,
                max_bytes=limit,
            )
            if changed:
                compacted_json = json.dumps(compacted, ensure_ascii=False, default=str)
                compacted_size = len(compacted_json.encode("utf-8"))
                if compacted_size <= limit:
                    logger.info(
                        "strategy_factory_runs.%s compacted inline (%s bytes -> %s bytes, limit=%s)",
                        field_name,
                        size_bytes,
                        compacted_size,
                        limit,
                    )
                    return compacted_json
        logger.warning(
            "strategy_factory_runs.%s exceeds soft limit (%s bytes > %s bytes); storing fallback summary instead",
            field_name,
            size_bytes,
            limit,
        )
        fallback = cls._summarize_large_factory_run_field(
            field_name,
            value,
            size_bytes=size_bytes,
        )
        fallback_json = json.dumps(fallback, ensure_ascii=False, default=str)
        if len(fallback_json.encode("utf-8")) <= limit:
            return fallback_json
        minimal = {
            "storage_mode": "inline_fallback_summary",
            "field_name": str(field_name or "unknown"),
            "truncated": True,
            "original_size_bytes": int(size_bytes),
            "fallback_truncated": True,
            "top_level_keys": sorted(str(key) for key in list((value or {}).keys())[:80]),
        }
        return json.dumps(minimal, ensure_ascii=False, default=str)

    @classmethod
    def _summarize_large_generation_experiment_field(
        cls,
        field_name: str,
        value: Any,
        *,
        size_bytes: int,
    ) -> dict[str, Any]:
        payload = dict(value) if isinstance(value, dict) else {}
        summary: dict[str, Any] = {
            "storage_mode": "inline_fallback_summary",
            "field_name": str(field_name or "unknown"),
            "truncated": True,
            "original_size_bytes": int(size_bytes),
        }
        if not payload:
            return summary
        summary["top_level_keys"] = sorted(str(key) for key in payload.keys())[:50]

        scalar_preview = {
            key: payload.get(key)
            for key in list(payload.keys())[:20]
            if isinstance(payload.get(key), (str, int, float, bool)) or payload.get(key) is None
        }
        if scalar_preview:
            summary["scalar_preview"] = scalar_preview

        if field_name == "parameters":
            summary["parameter_keys"] = sorted(str(key) for key in payload.keys())[:50]
            target_symbols = cls._preview_plain_list(payload.get("target_symbols"), limit=12)
            if target_symbols:
                summary["target_symbols"] = target_symbols
            return summary

        if field_name == "strategy_spec":
            for key in ("strategy_type", "name", "description"):
                if payload.get(key) not in (None, "", [], {}):
                    summary[key] = payload.get(key)
            tags = cls._preview_plain_list(payload.get("tags"), limit=12)
            if tags:
                summary["tags"] = tags
            target_symbols = cls._preview_plain_list(payload.get("target_symbols"), limit=12)
            if target_symbols:
                summary["target_symbols"] = target_symbols
            research_task = dict(payload.get("research_task") or {})
            if research_task:
                summary["research_task"] = {
                    key: research_task.get(key)
                    for key in (
                        "task_id",
                        "task_key",
                        "task_source",
                        "theme_code",
                        "event_id",
                        "candidate_family",
                        "factor_name",
                    )
                    if research_task.get(key) not in (None, "", [], {})
                }
            event_context = dict(payload.get("event_context") or {})
            if event_context:
                summary["event_context"] = {
                    key: event_context.get(key)
                    for key in (
                        "task_source",
                        "event_id",
                        "theme_code",
                        "event_type",
                        "candidate_family",
                        "factor_name",
                    )
                    if event_context.get(key) not in (None, "", [], {})
                }
            return summary

        if field_name == "evaluation":
            for key in ("source", "task_run_id"):
                if payload.get(key) not in (None, "", [], {}):
                    summary[key] = payload.get(key)
            committee_review = dict(payload.get("committee_review") or {})
            if committee_review:
                summary["committee_review"] = {
                    key: committee_review.get(key)
                    for key in ("decision", "final_score", "rank", "review_rank", "is_champion")
                    if committee_review.get(key) not in (None, "", [], {})
                }
            submission_result = dict(payload.get("submission_result") or {})
            if submission_result:
                summary["submission_result"] = {
                    key: submission_result.get(key)
                    for key in ("passed", "duplicate", "reason_code", "strategy_id")
                    if submission_result.get(key) not in (None, "", [], {})
                }
            return summary

        if field_name == "result":
            for key in (
                "status",
                "strategy_id",
                "passed",
                "duplicate",
                "reason_code",
                "admission_stage",
                "submission_lane",
                "research_candidate_ready",
                "incubation_candidate_ready",
                "live_candidate_ready",
            ):
                if payload.get(key) not in (None, "", [], {}):
                    summary[key] = payload.get(key)
            return summary

        return summary

    @classmethod
    def _encode_generation_experiment_json(cls, field_name: str, value: Any) -> str:
        limit = cls._generation_experiment_field_max_bytes()
        return bounded_json_text(
            f"strategy_generation_experiments.{field_name}",
            value or {},
            max_bytes=limit,
        )

    @classmethod
    def _summarize_large_task_run_result(cls, result: Any, *, size_bytes: int) -> dict:
        payload = dict(result) if isinstance(result, dict) else {}
        summary = {
            "storage_mode": "inline_fallback_summary",
            "truncated": True,
            "original_size_bytes": int(size_bytes),
        }
        if payload:
            summary["top_level_keys"] = sorted(str(key) for key in payload.keys())[:50]
            for key in (
                "task_run_id",
                "status",
                "source",
                "snapshot_date",
                "generated_count",
                "reviewed_count",
                "rejected_count",
            ):
                value = payload.get(key)
                if value not in (None, "", [], {}):
                    summary[key] = value
            artifact = payload.get("full_result_artifact")
            if isinstance(artifact, dict) and artifact:
                summary["full_result_artifact"] = {
                    key: artifact.get(key)
                    for key in ("artifact_id", "path", "format", "size_bytes")
                    if artifact.get(key) not in (None, "", [], {})
                }
            lifecycle = payload.get("lifecycle")
            if isinstance(lifecycle, dict) and lifecycle:
                summary["lifecycle"] = {
                    key: lifecycle.get(key)
                    for key in (
                        "state",
                        "current_phase",
                        "failed_phase",
                        "terminal_phase",
                        "phase_status_counts",
                    )
                    if lifecycle.get(key) not in (None, "", [], {})
                }
        return summary

    @classmethod
    def _encode_task_run_result_json(cls, result: Optional[dict]) -> Optional[str]:
        if result is None:
            return None
        limit = cls._task_run_result_max_bytes()
        return bounded_json_text(
            "strategy_task_runs.result",
            result or {},
            max_bytes=limit,
        )

    # ------------------------------------------------------------------
    # generation experiments
    # ------------------------------------------------------------------

    def _decode_generation_experiment(self, row: dict) -> dict:
        result = dict(row)
        for key in ("parameters", "strategy_spec", "evaluation", "result"):
            result[key] = self._decode_json_field(result.get(key), {})
        return result

    async def save_strategy_generation_experiment(self, experiment: dict) -> dict:
        payload = dict(experiment or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_generation_experiments
                    (experiment_id, strategy_id, parent_strategy_id, generated_strategy_id, task_run_id,
                     source, generator_type, optimizer_type, status, hypothesis,
                     prompt, parameters, strategy_spec, evaluation, result, parent_experiment_id,
                     artifact_id, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5,
                        $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15, $16,
                        $17, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (experiment_id) DO UPDATE SET
                    strategy_id = EXCLUDED.strategy_id,
                    parent_strategy_id = EXCLUDED.parent_strategy_id,
                    generated_strategy_id = EXCLUDED.generated_strategy_id,
                    task_run_id = EXCLUDED.task_run_id,
                    source = EXCLUDED.source,
                    generator_type = EXCLUDED.generator_type,
                    optimizer_type = EXCLUDED.optimizer_type,
                    status = EXCLUDED.status,
                    hypothesis = EXCLUDED.hypothesis,
                    prompt = EXCLUDED.prompt,
                    parameters = EXCLUDED.parameters,
                    strategy_spec = EXCLUDED.strategy_spec,
                    evaluation = EXCLUDED.evaluation,
                    result = EXCLUDED.result,
                    parent_experiment_id = EXCLUDED.parent_experiment_id,
                    artifact_id = EXCLUDED.artifact_id,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                str(payload.get("experiment_id") or ""),
                payload.get("strategy_id"),
                payload.get("parent_strategy_id"),
                payload.get("generated_strategy_id"),
                payload.get("task_run_id"),
                str(payload.get("source") or "unknown"),
                str(payload.get("generator_type") or "rule"),
                payload.get("optimizer_type"),
                str(payload.get("status") or "draft"),
                payload.get("hypothesis"),
                payload.get("prompt"),
                self._encode_generation_experiment_json("parameters", payload.get("parameters") or {}),
                self._encode_generation_experiment_json("strategy_spec", payload.get("strategy_spec") or {}),
                self._encode_generation_experiment_json("evaluation", payload.get("evaluation") or {}),
                self._encode_generation_experiment_json("result", payload.get("result") or {}),
                payload.get("parent_experiment_id"),
                payload.get("artifact_id"),
            )
        return self._decode_generation_experiment(dict(row))

    async def get_strategy_generation_experiment(self, experiment_id: str) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM strategy_generation_experiments
                WHERE experiment_id = $1
                LIMIT 1
                """,
                experiment_id,
            )
        if not row:
            return None
        return self._decode_generation_experiment(dict(row))

    async def list_strategy_generation_experiments(
        self,
        strategy_id: Optional[str] = None,
        parent_strategy_id: Optional[str] = None,
        generated_strategy_id: Optional[str] = None,
        task_run_id: Optional[int] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_generation_experiments WHERE 1=1"
            params: list = []
            idx = 1
            if strategy_id:
                sql += f" AND (strategy_id = ${idx} OR parent_strategy_id = ${idx} OR generated_strategy_id = ${idx})"
                params.append(strategy_id)
                idx += 1
            if parent_strategy_id:
                sql += f" AND parent_strategy_id = ${idx}"
                params.append(parent_strategy_id)
                idx += 1
            if generated_strategy_id:
                sql += f" AND generated_strategy_id = ${idx}"
                params.append(generated_strategy_id)
                idx += 1
            if task_run_id is not None:
                sql += f" AND task_run_id = ${idx}"
                params.append(int(task_run_id))
                idx += 1
            if status:
                sql += f" AND status = ${idx}"
                params.append(status)
                idx += 1
            if source:
                sql += f" AND source = ${idx}"
                params.append(source)
                idx += 1
            sql += f" ORDER BY created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 20), 200)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_generation_experiment(dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # task runs
    # ------------------------------------------------------------------

    def _decode_task_run(self, row: dict) -> dict:
        result = dict(row)
        result["payload"] = self._decode_json_field(result.get("payload"), {})
        result["result"] = self._decode_json_field(result.get("result"), {})
        return result

    async def save_strategy_task_run(self, run: dict) -> dict:
        payload = dict(run or {})
        started_at = self._coerce_timestamp(payload.get("started_at"))
        completed_at = self._coerce_timestamp(payload.get("completed_at"))
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_task_runs
                    (strategy_id, task_name, task_scope, task_key, status, trace_id, payload, result, error, started_at, completed_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, COALESCE($10, CURRENT_TIMESTAMP), $11)
                RETURNING *
                """,
                payload.get("strategy_id"),
                str(payload.get("task_name") or "unknown"),
                payload.get("task_scope"),
                payload.get("task_key"),
                str(payload.get("status") or "running"),
                payload.get("trace_id"),
                bounded_json_text(
                    "strategy_task_runs.payload",
                    payload.get("payload") or {},
                    max_bytes=strategy_json_field_max_bytes(),
                ),
                self._encode_task_run_result_json(payload.get("result") or {}),
                payload.get("error"),
                started_at,
                completed_at,
            )
        return self._decode_task_run(dict(row))

    async def update_strategy_task_run(
        self,
        run_id: int,
        status: Optional[str] = None,
        result: Optional[dict] = None,
        error: Optional[str] = None,
        completed_at=None,
    ) -> Optional[dict]:
        completed_at_value = self._coerce_timestamp(completed_at)
        result_json = self._encode_task_run_result_json(result)
        sql = """
            UPDATE strategy_task_runs
            SET status = COALESCE($2, status),
                result = CASE WHEN $3 IS NULL THEN result ELSE $3 END,
                error = COALESCE($4, error),
                completed_at = COALESCE($5, completed_at, CURRENT_TIMESTAMP)
            WHERE id = $1
            RETURNING *
            """
        for attempt in range(3):
            try:
                async with self.acquire() as conn:
                    row = await conn.fetchrow(
                        sql,
                        int(run_id),
                        status,
                        result_json,
                        error,
                        completed_at_value,
                    )
                if not row:
                    return None
                return self._decode_task_run(dict(row))
            except asyncio.TimeoutError:
                if attempt >= 2:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))
            except Exception:
                raise

    async def list_strategy_task_runs(
        self,
        strategy_id: Optional[str] = None,
        task_name: Optional[str] = None,
        task_scope: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_task_runs WHERE 1=1"
            params: list = []
            idx = 1
            if strategy_id:
                sql += f" AND strategy_id = ${idx}"
                params.append(strategy_id)
                idx += 1
            if task_name:
                sql += f" AND task_name = ${idx}"
                params.append(task_name)
                idx += 1
            if task_scope:
                sql += f" AND task_scope = ${idx}"
                params.append(task_scope)
                idx += 1
            if status:
                sql += f" AND status = ${idx}"
                params.append(status)
                idx += 1
            sql += f" ORDER BY started_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 20), 500)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_task_run(dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # factory runs
    # ------------------------------------------------------------------

    def _decode_factory_run(self, row: dict) -> dict:
        result = dict(row)
        for key in ("summary", "stages", "snapshot_summary", "artifact_refs", "parity_result"):
            result[key] = self._decode_json_field(result.get(key), {})
        if not isinstance(result.get("artifact_refs"), list):
            result["artifact_refs"] = []
        return result
