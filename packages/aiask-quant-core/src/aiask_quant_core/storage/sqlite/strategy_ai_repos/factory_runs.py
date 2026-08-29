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


class _FactoryRunsMixin:
    async def save_strategy_factory_run(self, run: dict) -> None:
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        started_at = self._coerce_timestamp(run.get("started_at")) or datetime.now(timezone.utc)
        completed_at = self._coerce_timestamp(run.get("completed_at"))
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strategy_factory_runs
                    (run_id, status, started_at, completed_at, elapsed_seconds, execution_mode,
                     engine_version, parity_status, summary, stages, snapshot_summary, artifact_refs,
                     parity_result, error)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                        $10, $11, $12, $13, $14)
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    completed_at = EXCLUDED.completed_at,
                    elapsed_seconds = EXCLUDED.elapsed_seconds,
                    execution_mode = EXCLUDED.execution_mode,
                    engine_version = EXCLUDED.engine_version,
                    parity_status = EXCLUDED.parity_status,
                    summary = EXCLUDED.summary,
                    stages = EXCLUDED.stages,
                    snapshot_summary = EXCLUDED.snapshot_summary,
                    artifact_refs = EXCLUDED.artifact_refs,
                    parity_result = EXCLUDED.parity_result,
                    error = EXCLUDED.error
                """,
                run_id,
                str(run.get("status") or "unknown"),
                started_at,
                completed_at,
                float(run.get("elapsed_seconds") or 0),
                str(run.get("execution_mode") or "legacy_primary"),
                str(run.get("engine_version") or "strategy_factory.v2"),
                (
                    str((run.get("parity_result") or {}).get("status") or "").strip()
                    or None
                ),
                self._encode_factory_run_json("summary", run.get("summary") or {}),
                self._encode_factory_run_json("stages", run.get("stages") or {}),
                self._encode_factory_run_json("snapshot_summary", run.get("snapshot_summary") or {}),
                json.dumps(run.get("artifact_refs") or [], ensure_ascii=False, default=str),
                json.dumps(run.get("parity_result") or {}, ensure_ascii=False, default=str),
                run.get("error"),
            )

    def _decode_factory_run_artifact(self, row: dict) -> dict:
        result = dict(row)
        result["payload_json"] = self._decode_json_field(result.get("payload_json"), {})
        return result

    async def save_strategy_factory_run_artifact(self, payload: dict) -> dict:
        data = dict(payload or {})
        run_id = str(data.get("run_id") or "").strip()
        artifact_type = str(data.get("artifact_type") or "").strip()
        artifact_version = str(data.get("artifact_version") or "1").strip() or "1"
        if not run_id:
            raise ValueError("run_id is required")
        if not artifact_type:
            raise ValueError("artifact_type is required")
        raw_payload = data.get("payload_json") or {}
        encoded_payload = self._encode_bounded_storage_json(
            f"strategy_factory_run_artifacts.{artifact_type}.payload_json",
            raw_payload,
            max_bytes=self._factory_artifact_payload_max_bytes(),
        )
        payload_was_dropped = False
        try:
            decoded_payload = json.loads(encoded_payload or "{}")
            payload_was_dropped = str(
                dict(decoded_payload or {}).get("storage_mode") or ""
            ) == "dropped_large_payload"
        except Exception:
            payload_was_dropped = False
        storage_mode = str(data.get("storage_mode") or "inline_compact_json").strip() or "inline_compact_json"
        if payload_was_dropped:
            storage_mode = "dropped_large_payload"
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_factory_run_artifacts
                    (run_id, artifact_type, artifact_version, payload_json, payload_hash, storage_mode)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                run_id,
                artifact_type,
                artifact_version,
                encoded_payload,
                str(data.get("payload_hash") or "").strip() or None,
                storage_mode,
            )
        return self._decode_factory_run_artifact(dict(row))

    async def list_strategy_factory_run_artifacts(self, run_id: str) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM strategy_factory_run_artifacts
                WHERE run_id = $1
                ORDER BY created_at ASC, id ASC
                """,
                run_id,
            )
        return [self._decode_factory_run_artifact(dict(row)) for row in rows]

    async def list_strategy_factory_run_artifact_refs(self, run_id: str) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, run_id, artifact_type, artifact_version, payload_hash, storage_mode, created_at
                FROM strategy_factory_run_artifacts
                WHERE run_id = $1
                ORDER BY created_at ASC, id ASC
                """,
                run_id,
            )
        return [dict(row) for row in rows]

    async def save_scheduler_state(self, payload: dict) -> dict:
        data = dict(payload or {})
        state_key = str(data.pop("state_key", "") or "strategy_factory_scheduler").strip()
        if not state_key:
            state_key = "strategy_factory_scheduler"
        encoded_payload = bounded_json_text(
            "strategy_factory_scheduler_state.payload_json",
            data,
            max_bytes=strategy_json_field_max_bytes(),
        )
        async with self.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_factory_scheduler_state (
                    state_key TEXT PRIMARY KEY,
                    payload_json TEXT DEFAULT '{}',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_factory_scheduler_state_updated_at
                ON strategy_factory_scheduler_state(updated_at DESC);
                """
            )
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_factory_scheduler_state
                    (state_key, payload_json, updated_at)
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (state_key) DO UPDATE SET
                    payload_json = EXCLUDED.payload_json,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING state_key, payload_json, updated_at
                """,
                state_key,
                encoded_payload,
            )
        result = dict(row or {})
        result["payload_json"] = self._decode_json_field(result.get("payload_json"), {})
        return result

    async def load_scheduler_state(self, state_key: str = "strategy_factory_scheduler") -> dict:
        resolved_key = str(state_key or "strategy_factory_scheduler").strip() or "strategy_factory_scheduler"
        async with self.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_factory_scheduler_state (
                    state_key TEXT PRIMARY KEY,
                    payload_json TEXT DEFAULT '{}',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_factory_scheduler_state_updated_at
                ON strategy_factory_scheduler_state(updated_at DESC);
                """
            )
            row = await conn.fetchrow(
                """
                SELECT state_key, payload_json, updated_at
                FROM strategy_factory_scheduler_state
                WHERE state_key = $1
                LIMIT 1
                """,
                resolved_key,
            )
        if not row:
            return {}
        result = dict(row)
        payload = self._decode_json_field(result.get("payload_json"), {})
        if not isinstance(payload, dict):
            return {}
        payload.setdefault("state_key", result.get("state_key"))
        payload.setdefault("updated_at", result.get("updated_at"))
        return payload

    async def list_strategy_factory_runs(self, limit: int = 20) -> List[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM strategy_factory_runs
                ORDER BY started_at DESC
                LIMIT $1
                """,
                max(1, min(int(limit or 20), 100)),
            )
        return [self._decode_factory_run(dict(row)) for row in rows]

    async def get_strategy_factory_run(self, run_id: str) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM strategy_factory_runs
                WHERE run_id = $1
                LIMIT 1
                """,
                run_id,
            )
        if not row:
            return None
        return self._decode_factory_run(dict(row))

    async def get_latest_strategy_factory_run(self) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM strategy_factory_runs
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
        if not row:
            return None
        return self._decode_factory_run(dict(row))
