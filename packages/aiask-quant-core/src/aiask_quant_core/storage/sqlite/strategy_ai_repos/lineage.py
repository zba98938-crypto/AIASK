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


class _LineageMixin:
    async def list_event_injections(self, *, status: str = None, source: str = None, limit: int = 50) -> list:
        async with self.acquire() as conn:
            conditions = []
            params = []
            idx = 1
            if status:
                conditions.append(f"status = ${idx}")
                params.append(str(status).strip())
                idx += 1
            if source:
                conditions.append(f"source = ${idx}")
                params.append(str(source).strip())
                idx += 1
            where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(max(1, min(int(limit), 200)))
            sql = f"SELECT * FROM strategy_factory_event_injections{where} ORDER BY created_at DESC LIMIT ${idx}"
            rows = await conn.fetch(sql, *params)
        result = []
        for row in rows:
            item = dict(row)
            for json_field in ("primary_themes", "evidence"):
                raw = item.get(json_field)
                if isinstance(raw, str):
                    try:
                        item[json_field] = __import__("json").loads(raw)
                    except Exception:
                        pass
            result.append(item)
        return result

    async def upsert_event_injection(self, payload: dict) -> dict:
        import json as _json
        from uuid import uuid4
        event_id = str(payload.get("event_id") or f"manual_{uuid4().hex[:12]}").strip()
        # PR-B1: approver_id / approved_at 支持. handler 传入这两个字段时
        # 必须真正写到 DB（之前只在 handler 返回值里），否则审批结果完全
        # 不持久化，事件审计无效。
        approver_id = payload.get("approver_id")
        approved_at = payload.get("approved_at")
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strategy_factory_event_injections
                    (event_id, source, event_name, event_type, direction,
                     confidence, intensity, horizon, scope, primary_themes,
                     rationale, evidence, valid_from, valid_until, status,
                     operator_id, approver_id, approved_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
                ON CONFLICT(event_id) DO UPDATE SET
                    event_name = EXCLUDED.event_name,
                    event_type = EXCLUDED.event_type,
                    direction = EXCLUDED.direction,
                    confidence = EXCLUDED.confidence,
                    intensity = EXCLUDED.intensity,
                    horizon = EXCLUDED.horizon,
                    scope = EXCLUDED.scope,
                    primary_themes = EXCLUDED.primary_themes,
                    rationale = EXCLUDED.rationale,
                    evidence = EXCLUDED.evidence,
                    valid_from = EXCLUDED.valid_from,
                    valid_until = EXCLUDED.valid_until,
                    status = EXCLUDED.status,
                    approver_id = COALESCE(EXCLUDED.approver_id, strategy_factory_event_injections.approver_id),
                    approved_at = COALESCE(EXCLUDED.approved_at, strategy_factory_event_injections.approved_at),
                    updated_at = CURRENT_TIMESTAMP
                """,
                event_id,
                str(payload.get("source") or "manual"),
                str(payload.get("event_name") or ""),
                str(payload.get("event_type") or ""),
                payload.get("direction"),
                float(payload.get("confidence") or 0.7),
                float(payload.get("intensity") or 0.5),
                str(payload.get("horizon") or "swing_5_20d"),
                str(payload.get("scope") or "theme"),
                _json.dumps(payload.get("primary_themes") or [], ensure_ascii=False),
                payload.get("rationale"),
                bounded_json_text(
                    "strategy_factory_event_injections.evidence",
                    payload.get("evidence") or {},
                    max_bytes=strategy_json_field_max_bytes(),
                ),
                str(payload.get("valid_from") or ""),
                str(payload.get("valid_until") or ""),
                str(payload.get("status") or "pending_review"),
                payload.get("operator_id"),
                approver_id,
                approved_at,
            )
        return {"event_id": event_id, "status": payload.get("status") or "pending_review"}

    async def patch_event_injection(self, event_id: str, fields: dict) -> dict:
        """Patch only explicitly provided event injection fields.

        ``upsert_event_injection`` is intentionally a full create/update API and
        supplies defaults for missing fields. Status-only flows such as approve,
        pause, expire, and reject must not use it because defaults would wipe
        event metadata on conflict.
        """

        import json as _json

        event_id_str = str(event_id or "").strip()
        if not event_id_str:
            raise ValueError("event_id is required")

        allowed_columns = {
            "source",
            "event_name",
            "event_type",
            "direction",
            "confidence",
            "intensity",
            "horizon",
            "scope",
            "primary_themes",
            "rationale",
            "evidence",
            "valid_from",
            "valid_until",
            "status",
            "operator_id",
            "approver_id",
            "approved_at",
        }
        patch: dict = {}
        for key, value in dict(fields or {}).items():
            if key in allowed_columns:
                patch[key] = value
        if not patch:
            return {"event_id": event_id_str, "updated": 0, "reason": "no_allowed_fields"}

        assignments: list[str] = []
        params: list = []
        idx = 1
        for column, value in patch.items():
            if column == "primary_themes":
                value = _json.dumps(value or [], ensure_ascii=False)
            elif column == "evidence":
                value = bounded_json_text(
                    "strategy_factory_event_injections.evidence",
                    value or {},
                    max_bytes=strategy_json_field_max_bytes(),
                )
            elif column in {"confidence", "intensity"} and value is not None:
                value = float(value)
            elif column not in {"direction", "rationale", "operator_id", "approver_id", "approved_at"}:
                value = str(value or "").strip()
            assignments.append(f"{column} = ${idx}")
            params.append(value)
            idx += 1

        params.append(event_id_str)
        async with self.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE strategy_factory_event_injections
                SET {", ".join(assignments)}, updated_at = CURRENT_TIMESTAMP
                WHERE event_id = ${idx}
                """,
                *params,
            )
            row = await conn.fetchrow(
                "SELECT * FROM strategy_factory_event_injections WHERE event_id = $1",
                event_id_str,
            )

        if row is None:
            return {"event_id": event_id_str, "updated": 0, "missing": True}
        item = dict(row)
        for json_field in ("primary_themes", "evidence"):
            raw = item.get(json_field)
            if isinstance(raw, str):
                try:
                    item[json_field] = _json.loads(raw)
                except Exception:
                    pass
        return {"event_id": event_id_str, "updated": 1, "event": item}

    async def update_event_outcome(self, event_id: str, *, actual_outcome: str, outcome_notes: str = None) -> dict:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE strategy_factory_event_injections
                SET actual_outcome = $1, outcome_notes = $2,
                    outcome_recorded_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE event_id = $3
                """,
                str(actual_outcome).strip(),
                outcome_notes,
                str(event_id).strip(),
            )
        return {"event_id": event_id, "actual_outcome": actual_outcome}

    async def upsert_event_task_lineage(self, payload: dict) -> dict:
        import json as _json
        dedupe_key = str(payload.get("dedupe_key") or "").strip()
        event_id = str(payload.get("event_id") or "")
        task_id = str(payload.get("task_id") or "")
        theme_code = str(payload.get("theme_code") or "")
        async with self.acquire() as conn:
            if dedupe_key:
                existing = await conn.fetchrow(
                    "SELECT lineage_id, event_id, task_id FROM strategy_factory_event_task_lineage "
                    "WHERE dedupe_key = $1 ORDER BY lineage_id DESC LIMIT 1",
                    dedupe_key,
                )
                if existing is not None:
                    row = dict(existing)
                    return {
                        "event_id": row.get("event_id"),
                        "task_id": row.get("task_id"),
                        "dedupe_key": dedupe_key,
                        "lineage_id": row.get("lineage_id"),
                        "inserted": 0,
                    }
            await conn.execute(
                """
                INSERT INTO strategy_factory_event_task_lineage
                    (dedupe_key, event_id, task_id, theme_code, impact_direction,
                     impact_magnitude, target_symbols, target_count, breadth_resolved)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                dedupe_key or None,
                event_id,
                task_id,
                theme_code,
                str(payload.get("impact_direction") or "positive"),
                float(payload.get("impact_magnitude") or 0),
                _json.dumps(payload.get("target_symbols") or [], ensure_ascii=False),
                int(payload.get("target_count") or 0),
                str(payload.get("breadth_resolved") or "medium"),
            )
        return {
            "event_id": payload.get("event_id"),
            "task_id": payload.get("task_id"),
            "dedupe_key": dedupe_key or None,
            "inserted": 1,
        }

    async def list_event_task_lineage(
        self,
        *,
        event_id: str = None,
        task_id: str = None,
        limit: int = 100,
    ) -> list:
        """Read persisted event -> task -> gate lineage rows."""

        conditions = []
        params = []
        idx = 1
        if event_id:
            conditions.append(f"l.event_id = ${idx}")
            params.append(str(event_id).strip())
            idx += 1
        if task_id:
            conditions.append(f"l.task_id = ${idx}")
            params.append(str(task_id).strip())
            idx += 1
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(max(1, min(int(limit or 100), 1000)))
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    l.*,
                    e.event_name,
                    e.event_type,
                    e.source AS event_source,
                    e.status AS event_status,
                    e.valid_from,
                    e.valid_until,
                    e.actual_outcome
                FROM strategy_factory_event_task_lineage l
                LEFT JOIN strategy_factory_event_injections e
                  ON e.event_id = l.event_id
                {where}
                ORDER BY l.generated_at DESC, l.lineage_id DESC
                LIMIT ${idx}
                """,
                *params,
            )
        result = []
        for row in rows:
            item = dict(row)
            raw_symbols = item.get("target_symbols")
            if isinstance(raw_symbols, str):
                try:
                    item["target_symbols"] = __import__("json").loads(raw_symbols)
                except Exception:
                    item["target_symbols"] = []
            result.append(item)
        return result

    async def update_event_task_lineage_gates(
        self,
        *,
        event_id: str,
        task_id: str,
        gate_1_passed=None,
        gate_2_passed=None,
        gate_3_passed=None,
        strategies_submitted=None,
    ) -> dict:
        """Patch Gate / submission state on an existing lineage row.

        方案 §6 Phase 0 实施第 4 项：lineage 表必须支持按
        ``(event_id, task_id)`` 写回 gate_1/2/3 + strategies_submitted。
        允许部分字段为 ``None``（保持原值），避免上游忘传字段时把已通过
        的 Gate 重置成 NULL。
        """

        event_id_str = str(event_id or "").strip()
        task_id_str = str(task_id or "").strip()
        if not event_id_str or not task_id_str:
            raise ValueError("event_id and task_id are required")

        sets: list[str] = []
        params: list = []
        idx = 1

        def _add(column: str, value):
            nonlocal idx
            if value is None:
                return
            sets.append(f"{column} = ${idx}")
            params.append(value)
            idx += 1

        if gate_1_passed is not None:
            _add("gate_1_passed", int(bool(gate_1_passed)))
        if gate_2_passed is not None:
            _add("gate_2_passed", int(bool(gate_2_passed)))
        if gate_3_passed is not None:
            _add("gate_3_passed", int(bool(gate_3_passed)))
        if strategies_submitted is not None:
            _add("strategies_submitted", int(strategies_submitted))

        if not sets:
            return {"event_id": event_id_str, "task_id": task_id_str, "updated": 0}

        params.extend([event_id_str, task_id_str])
        sql = (
            "UPDATE strategy_factory_event_task_lineage "
            f"SET {', '.join(sets)} "
            f"WHERE event_id = ${idx} AND task_id = ${idx + 1}"
        )

        async with self.acquire() as conn:
            await conn.execute(sql, *params)
            row = await conn.fetchrow(
                "SELECT lineage_id FROM strategy_factory_event_task_lineage "
                "WHERE event_id = $1 AND task_id = $2 ORDER BY lineage_id DESC LIMIT 1",
                event_id_str,
                task_id_str,
            )
        return {
            "event_id": event_id_str,
            "task_id": task_id_str,
            "updated": 1 if row is not None else 0,
            "lineage_id": dict(row).get("lineage_id") if row else None,
        }
