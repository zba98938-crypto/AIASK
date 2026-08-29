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


class _DispatchMixin:
    def _decode_factory_dispatch(self, row: dict) -> dict:
        result = dict(row)
        result["metadata"] = self._decode_json_field(result.get("metadata"), {})
        return result

    async def create_strategy_factory_dispatch(self, payload: dict) -> dict:
        data = dict(payload or {})
        dispatch_id = str(data.get("dispatch_id") or "").strip()
        if not dispatch_id:
            raise ValueError("dispatch_id is required")
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_factory_dispatches
                    (dispatch_id, status, execution_mode, requested_at, started_at, completed_at, run_id, message, error, metadata)
                VALUES ($1, $2, $3, COALESCE($4, CURRENT_TIMESTAMP), $5, $6, $7, $8, $9, $10)
                ON CONFLICT (dispatch_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    execution_mode = EXCLUDED.execution_mode,
                    requested_at = EXCLUDED.requested_at,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at,
                    run_id = EXCLUDED.run_id,
                    message = EXCLUDED.message,
                    error = EXCLUDED.error,
                    metadata = EXCLUDED.metadata,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                dispatch_id,
                str(data.get("status") or "queued"),
                str(data.get("execution_mode") or "legacy_primary"),
                self._coerce_timestamp(data.get("requested_at")),
                self._coerce_timestamp(data.get("started_at")),
                self._coerce_timestamp(data.get("completed_at")),
                str(data.get("run_id") or "").strip() or None,
                data.get("message"),
                data.get("error"),
                bounded_json_text(
                    "strategy_factory_dispatches.metadata",
                    data.get("metadata") or {},
                    max_bytes=strategy_json_field_max_bytes(),
                ),
            )
        return self._decode_factory_dispatch(dict(row))

    async def update_strategy_factory_dispatch(self, dispatch_id: str, **kwargs) -> Optional[dict]:
        existing = await self.get_strategy_factory_dispatch(dispatch_id)
        if not existing:
            return None
        merged = {**existing, **dict(kwargs or {}), "dispatch_id": dispatch_id}
        return await self.create_strategy_factory_dispatch(merged)

    async def get_strategy_factory_dispatch(self, dispatch_id: str) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM strategy_factory_dispatches
                WHERE dispatch_id = $1
                LIMIT 1
                """,
                dispatch_id,
            )
        if not row:
            return None
        return self._decode_factory_dispatch(dict(row))

    async def list_strategy_factory_dispatches(self, status: str | None = None, limit: int = 20) -> List[dict]:
        resolved_status = str(status or "").strip()
        resolved_limit = max(1, min(int(limit or 20), 100))
        async with self.acquire() as conn:
            if resolved_status:
                rows = await conn.fetch(
                    """
                    SELECT * FROM strategy_factory_dispatches
                    WHERE status = $1
                    ORDER BY requested_at ASC, id ASC
                    LIMIT $2
                    """,
                    resolved_status,
                    resolved_limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM strategy_factory_dispatches
                    ORDER BY requested_at DESC, id DESC
                    LIMIT $1
                    """,
                    resolved_limit,
                )
        return [self._decode_factory_dispatch(dict(row)) for row in rows]
