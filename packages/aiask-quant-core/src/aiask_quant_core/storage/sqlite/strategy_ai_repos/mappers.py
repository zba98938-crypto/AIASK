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


class _MappersMixin:
    async def save_factory_task_evidence(self, item: dict) -> dict:
        payload = dict(item or {})
        task_key = str(payload.get("task_key") or "").strip()
        evidence_type = str(payload.get("evidence_type") or "").strip()
        if not task_key or not evidence_type:
            raise ValueError("task_key and evidence_type are required")
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_factory_task_evidence
                    (task_key, event_id, theme_code, symbol, evidence_type, weight, evidence_payload, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, CURRENT_TIMESTAMP)
                RETURNING *
                """,
                task_key,
                payload.get("event_id"),
                str(payload.get("theme_code") or ""),
                payload.get("symbol"),
                evidence_type,
                float(payload.get("weight") or 0.0),
                bounded_json_text(
                    "strategy_factory_task_evidence.evidence_payload",
                    payload.get("evidence_payload") or {},
                    max_bytes=strategy_json_field_max_bytes(),
                ),
            )
        return self._decode_factory_task_evidence(dict(row))

    async def list_factory_task_evidence(
        self,
        task_key: Optional[str] = None,
        event_id: Optional[str] = None,
        evidence_type: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_factory_task_evidence WHERE 1=1"
            params: list = []
            idx = 1
            if task_key:
                sql += f" AND task_key = ${idx}"
                params.append(str(task_key))
                idx += 1
            if event_id:
                sql += f" AND event_id = ${idx}"
                params.append(str(event_id))
                idx += 1
            if evidence_type:
                sql += f" AND evidence_type = ${idx}"
                params.append(str(evidence_type))
                idx += 1
            sql += f" ORDER BY created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 200), 500)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_factory_task_evidence(dict(row)) for row in rows]
