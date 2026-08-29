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


class _TopnScoresMixin:
    def _decode_strategy_factory_topn_snapshot(self, row: dict) -> dict:
        result = dict(row or {})
        for key in ("selection_rules", "constituents", "metadata"):
            default = [] if key == "constituents" else {}
            result[key] = self._decode_json_field(result.get(key), default)
        as_of_date = result.get("as_of_date")
        result["as_of_date"] = (
            as_of_date.isoformat()
            if hasattr(as_of_date, "isoformat")
            else str(as_of_date or "").strip() or None
        )
        return result

    def _decode_strategy_factory_full_market_score(self, row: dict) -> dict:
        result = dict(row or {})
        result["component_scores"] = self._decode_json_field(result.get("component_scores"), {})
        result["family_candidates"] = self._decode_json_field(result.get("family_candidates"), [])
        as_of_date = result.get("as_of_date")
        result["as_of_date"] = (
            as_of_date.isoformat()
            if hasattr(as_of_date, "isoformat")
            else str(as_of_date or "").strip() or None
        )
        return result

    async def save_strategy_factory_topn_snapshot(self, payload: dict) -> dict:
        data = dict(payload or {})
        snapshot_id = str(data.get("snapshot_id") or "").strip()
        run_id = str(data.get("run_id") or "").strip()
        if not snapshot_id:
            raise ValueError("snapshot_id is required")
        if not run_id:
            raise ValueError("run_id is required")
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_factory_topn_snapshots
                    (snapshot_id, run_id, as_of_date, trace_id, correlation_id, source_action,
                     universe_count, eligible_count, topn_n, selection_rules, constituents,
                     portfolio_candidate_id, metadata, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (snapshot_id) DO UPDATE SET
                    run_id = EXCLUDED.run_id,
                    as_of_date = EXCLUDED.as_of_date,
                    trace_id = EXCLUDED.trace_id,
                    correlation_id = EXCLUDED.correlation_id,
                    source_action = EXCLUDED.source_action,
                    universe_count = EXCLUDED.universe_count,
                    eligible_count = EXCLUDED.eligible_count,
                    topn_n = EXCLUDED.topn_n,
                    selection_rules = EXCLUDED.selection_rules,
                    constituents = EXCLUDED.constituents,
                    portfolio_candidate_id = EXCLUDED.portfolio_candidate_id,
                    metadata = EXCLUDED.metadata,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                snapshot_id,
                run_id,
                self._coerce_date(data.get("as_of_date")),
                str(data.get("trace_id") or "").strip() or None,
                str(data.get("correlation_id") or "").strip() or None,
                str(data.get("source_action") or "").strip() or None,
                int(data.get("universe_count") or 0),
                int(data.get("eligible_count") or 0),
                max(1, min(int(data.get("topn_n") or 20), full_market_score_topn())),
                bounded_json_text(
                    "strategy_factory_topn_snapshots.selection_rules",
                    data.get("selection_rules") or {},
                    max_bytes=strategy_json_field_max_bytes(),
                ),
                bounded_json_text(
                    "strategy_factory_topn_snapshots.constituents",
                    list(data.get("constituents") or [])[: full_market_score_topn()],
                    max_bytes=strategy_json_field_max_bytes(),
                ),
                str(data.get("portfolio_candidate_id") or "").strip() or None,
                bounded_json_text(
                    "strategy_factory_topn_snapshots.metadata",
                    data.get("metadata") or {},
                    max_bytes=strategy_json_field_max_bytes(),
                ),
            )
        return self._decode_strategy_factory_topn_snapshot(dict(row))

    async def get_strategy_factory_topn_snapshot(self, run_id: str) -> Optional[dict]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return None
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM strategy_factory_topn_snapshots
                WHERE run_id = $1
                LIMIT 1
                """,
                normalized_run_id,
            )
        if not row:
            return None
        return self._decode_strategy_factory_topn_snapshot(dict(row))

    async def get_latest_strategy_factory_topn_snapshot(self) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM strategy_factory_topn_snapshots
                ORDER BY as_of_date DESC NULLS LAST, updated_at DESC
                LIMIT 1
                """
            )
        if not row:
            return None
        return self._decode_strategy_factory_topn_snapshot(dict(row))

    async def replace_strategy_factory_full_market_scores(
        self,
        *,
        run_id: str,
        snapshot_id: str,
        as_of_date,
        trace_id: str | None,
        correlation_id: str | None,
        rows: list[dict],
    ) -> int:
        normalized_run_id = str(run_id or "").strip()
        normalized_snapshot_id = str(snapshot_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required")
        if not normalized_snapshot_id:
            raise ValueError("snapshot_id is required")
        encoded_as_of = self._coerce_date(as_of_date)
        normalized_trace_id = str(trace_id or "").strip() or None
        normalized_correlation_id = str(correlation_id or "").strip() or None
        normalized_rows = [
            dict(item or {})
            for item in list(rows or [])
            if isinstance(item, dict) and str(dict(item or {}).get("code") or "").strip()
        ]
        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    DELETE FROM strategy_factory_full_market_scores
                    WHERE run_id = $1
                    """,
                    normalized_run_id,
                )
                if normalized_rows:
                    await conn.executemany(
                        """
                        INSERT INTO strategy_factory_full_market_scores
                            (run_id, snapshot_id, as_of_date, trace_id, correlation_id, code, rank,
                             composite_score, industry, market_cap, component_scores, family_candidates,
                             eligible, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, CURRENT_TIMESTAMP)
                        """,
                        [
                            (
                                normalized_run_id,
                                normalized_snapshot_id,
                                encoded_as_of,
                                normalized_trace_id,
                                normalized_correlation_id,
                                str(item.get("code") or "").strip(),
                                int(item.get("rank") or 0),
                                float(item.get("composite_score") or 0.0),
                                str(item.get("industry") or "").strip() or None,
                                float(item.get("market_cap") or 0.0),
                                bounded_json_text(
                                    "strategy_factory_full_market_scores.component_scores",
                                    item.get("component_scores") or {},
                                    max_bytes=strategy_json_field_max_bytes(),
                                ),
                                bounded_json_text(
                                    "strategy_factory_full_market_scores.family_candidates",
                                    item.get("family_candidates") or [],
                                    max_bytes=strategy_json_field_max_bytes(),
                                ),
                                bool(item.get("eligible", True)),
                            )
                            for item in normalized_rows
                        ],
                    )
                retention_runs = full_market_score_retention_runs()
                if retention_runs > 0:
                    keep_rows = await conn.fetch(
                        """
                        SELECT run_id
                        FROM strategy_factory_full_market_scores
                        GROUP BY run_id
                        ORDER BY MAX(COALESCE(as_of_date, '')) DESC, MAX(created_at) DESC, run_id DESC
                        LIMIT $1
                        """,
                        retention_runs,
                    )
                    keep_run_ids = [
                        str(row.get("run_id") or "").strip()
                        for row in keep_rows
                        if str(row.get("run_id") or "").strip()
                    ]
                    if keep_run_ids:
                        await conn.execute(
                            """
                            DELETE FROM strategy_factory_full_market_scores
                            WHERE NOT (run_id = ANY($1))
                            """,
                            keep_run_ids,
                        )
        return len(normalized_rows)

    async def count_strategy_factory_full_market_scores(self, run_id: str) -> int:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return 0
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS score_count
                FROM strategy_factory_full_market_scores
                WHERE run_id = $1
                """,
                normalized_run_id,
            )
        return int((dict(row or {})).get("score_count") or 0)

    async def list_strategy_factory_full_market_scores(self, run_id: str, limit: int = 20) -> List[dict]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return []
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM strategy_factory_full_market_scores
                WHERE run_id = $1
                ORDER BY rank ASC, code ASC
                LIMIT $2
                """,
                normalized_run_id,
                max(1, min(int(limit or 20), 500)),
            )
        return [self._decode_strategy_factory_full_market_score(dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # strategy factory event-driven store
    # ------------------------------------------------------------------
