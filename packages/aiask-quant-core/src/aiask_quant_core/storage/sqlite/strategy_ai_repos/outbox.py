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


class _OutboxMixin:
    async def list_event_outbox_state(
        self,
        *,
        status: str = None,
        event_id: str = None,
        limit: int = 100,
    ) -> list:
        conditions = []
        params = []
        idx = 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(str(status).strip())
            idx += 1
        if event_id:
            conditions.append(f"source_event_id = ${idx}")
            params.append(str(event_id).strip())
            idx += 1
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(max(1, min(int(limit or 100), 1000)))
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM strategy_factory_event_outbox_state
                {where}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ${idx}
                """,
                *params,
            )
        return [dict(row) for row in rows]

    async def get_event_outbox_status(self, *, limit: int = 50) -> dict:
        async with self.acquire() as conn:
            counts = await conn.fetch(
                """
                SELECT status, COUNT(*) AS count
                FROM strategy_factory_event_outbox_state
                GROUP BY status
                ORDER BY status
                """
            )
            latest = await conn.fetch(
                """
                SELECT *
                FROM strategy_factory_event_outbox_state
                ORDER BY updated_at DESC, created_at DESC
                LIMIT $1
                """,
                max(1, min(int(limit or 50), 200)),
            )
        return {
            "source": "strategy_factory_event_outbox_state",
            "counts": {str(row.get("status") or "unknown"): int(row.get("count") or 0) for row in counts},
            "latest": [dict(row) for row in latest],
        }

    # ------------------------------------------------------------------
    # PR-B1 (2026-05-24): 事件驱动 DAO 补齐
    #
    # 上一版 ThemeExposureBuilder 调用 ``upsert_theme_exposure`` 但 DAO 不存在；
    # event_task_lineage 也只能 INSERT，无法回写 Gate 状态；事件驱动 outbox
    # 全靠 strategy_domain_events 主表（孵化工厂热写路径），缺幂等保障。
    # 本节补齐这 6 个 DAO，全部不影响其它子系统现有写入。
    # ------------------------------------------------------------------

    async def claim_event_outbox(self, payload: dict) -> dict:
        """Idempotent outbox claim by ``dedupe_key``.

        Returns ``{"claimed": bool, "status": str, "attempts": int}``.
        ``claimed=False`` means another worker already took the slot or
        the same dedupe_key is in a terminal state (``processed`` /
        ``abandoned``); the caller must skip processing.

        方案 §3.1 + §8: dedupe_key 是消费幂等的边界，主键约束让 SQLite
        强制单调。
        """

        dedupe_key = str(payload.get("dedupe_key") or "").strip()
        source_event_id = str(payload.get("source_event_id") or "").strip()
        if not dedupe_key or not source_event_id:
            raise ValueError("dedupe_key and source_event_id are required")

        theme_code = payload.get("theme_code")
        event_type = payload.get("event_type")

        async with self.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT status, attempts FROM strategy_factory_event_outbox_state "
                "WHERE dedupe_key = $1",
                dedupe_key,
            )
            if existing is None:
                await conn.execute(
                    """
                    INSERT INTO strategy_factory_event_outbox_state
                        (dedupe_key, source_event_id, theme_code, event_type,
                         status, attempts, claimed_at)
                    VALUES ($1, $2, $3, $4, 'processing', 1, CURRENT_TIMESTAMP)
                    """,
                    dedupe_key,
                    source_event_id,
                    theme_code,
                    event_type,
                )
                return {
                    "claimed": True,
                    "status": "processing",
                    "attempts": 1,
                    "dedupe_key": dedupe_key,
                }

            existing_status = str(existing["status"] or "").strip()
            existing_attempts = int(existing["attempts"] or 0)
            # Terminal states are not re-claimable; return claimed=False to
            # signal the publisher to skip without raising.
            if existing_status in ("processed", "abandoned"):
                return {
                    "claimed": False,
                    "status": existing_status,
                    "attempts": existing_attempts,
                    "dedupe_key": dedupe_key,
                }
            # In-flight: tighten attempts and refresh claimed_at; the row
            # remains 'processing' so a parallel worker (should not exist
            # under v1 single-worker rule, but guard anyway) sees a busy
            # slot.
            await conn.execute(
                """
                UPDATE strategy_factory_event_outbox_state
                SET status = 'processing',
                    attempts = attempts + 1,
                    claimed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE dedupe_key = $1
                """,
                dedupe_key,
            )
            return {
                "claimed": True,
                "status": "processing",
                "attempts": existing_attempts + 1,
                "dedupe_key": dedupe_key,
            }

    async def mark_event_outbox_processed(self, dedupe_key: str) -> dict:
        """Mark an outbox row as ``processed`` (terminal). Idempotent."""
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("dedupe_key is required")
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE strategy_factory_event_outbox_state
                SET status = 'processed',
                    processed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    last_error = NULL
                WHERE dedupe_key = $1
                """,
                key,
            )
        return {"dedupe_key": key, "status": "processed"}

    async def mark_event_outbox_failed(
        self,
        dedupe_key: str,
        *,
        error: str,
        abandon: bool = False,
    ) -> dict:
        """Mark an outbox row as ``failed`` (retryable) or ``abandoned`` (terminal)."""
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("dedupe_key is required")
        target_status = "abandoned" if abandon else "failed"
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE strategy_factory_event_outbox_state
                SET status = $1,
                    failed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    last_error = $2
                WHERE dedupe_key = $3
                """,
                target_status,
                str(error or "").strip()[:1024] if error else None,
                key,
            )
        return {"dedupe_key": key, "status": target_status}
