"""SQLite adapter mixin for factor/decision text context storage."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from aiask_quant_core.vector_collection_scope import resolve_dimension_scoped_version, resolve_vector_collection_name
from ..strategy_factory_json_budget import bounded_json_text


logger = logging.getLogger(__name__)


class _EventsMixin:
    async def upsert_market_event_normalized(self, item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item or {})
        event_id = self._clean_context_text(payload.get("event_id"), max_len=200)
        event_type = self._clean_context_text(payload.get("event_type") or "other", max_len=120)
        event_name = self._clean_context_text(payload.get("event_name") or payload.get("summary"), max_len=300)
        if not event_id:
            basis = "|".join(
                [
                    event_type,
                    event_name,
                    str(payload.get("publish_time") or payload.get("evidence_time") or ""),
                    ",".join(str(item) for item in self._json_list(payload.get("source_doc_uids"))),
                ]
            )
            event_id = f"mevt_{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:24]}"
        if not event_name:
            raise ValueError("event_name is required")
        source_tier = self._normalize_source_tier(payload.get("source_tier"))
        reliability_score = self._coerce_context_float(payload.get("reliability_score"))
        if reliability_score is None:
            reliability_score = self._default_reliability_score(source_tier)
        status = self._clean_context_text(payload.get("status") or "provisional", max_len=80) or "provisional"
        checksum = self._clean_context_text(payload.get("checksum"), max_len=128)
        if not checksum:
            checksum_basis = "|".join(
                [
                    event_type,
                    event_name,
                    str(payload.get("publish_time") or ""),
                    json.dumps(self._json_list(payload.get("source_doc_uids")), ensure_ascii=False, sort_keys=True),
                ]
            )
            checksum = hashlib.sha1(checksum_basis.encode("utf-8")).hexdigest()
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO market_events_normalized (
                    event_id, event_type, event_name, summary, entity_codes, theme_codes, direction,
                    event_time, publish_time, evidence_time, source_doc_uids, source_tier, source_types,
                    provider_chain, reliability_score, cross_source_count, status, reject_reason,
                    freshness_status, event_anchor_id, checksum, metadata, created_at, updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7,
                    $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, $17, $18,
                    $19, $20, $21, $22, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (event_id) DO UPDATE SET
                    event_type = EXCLUDED.event_type,
                    event_name = EXCLUDED.event_name,
                    summary = EXCLUDED.summary,
                    entity_codes = EXCLUDED.entity_codes,
                    theme_codes = EXCLUDED.theme_codes,
                    direction = EXCLUDED.direction,
                    event_time = EXCLUDED.event_time,
                    publish_time = EXCLUDED.publish_time,
                    evidence_time = EXCLUDED.evidence_time,
                    source_doc_uids = EXCLUDED.source_doc_uids,
                    source_tier = EXCLUDED.source_tier,
                    source_types = EXCLUDED.source_types,
                    provider_chain = EXCLUDED.provider_chain,
                    reliability_score = EXCLUDED.reliability_score,
                    cross_source_count = EXCLUDED.cross_source_count,
                    status = EXCLUDED.status,
                    reject_reason = EXCLUDED.reject_reason,
                    freshness_status = EXCLUDED.freshness_status,
                    event_anchor_id = EXCLUDED.event_anchor_id,
                    checksum = EXCLUDED.checksum,
                    metadata = EXCLUDED.metadata,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                event_id,
                event_type,
                event_name,
                self._clean_context_text(payload.get("summary"), max_len=1000) or None,
                json.dumps(self._json_list(payload.get("entity_codes")), ensure_ascii=False, default=str),
                json.dumps(self._json_list(payload.get("theme_codes")), ensure_ascii=False, default=str),
                self._clean_context_text(payload.get("direction") or "neutral", max_len=40) or "neutral",
                self._clean_context_text(payload.get("event_time"), max_len=80) or None,
                self._clean_context_text(payload.get("publish_time"), max_len=80) or None,
                self._clean_context_text(payload.get("evidence_time"), max_len=80) or None,
                json.dumps(self._json_list(payload.get("source_doc_uids")), ensure_ascii=False, default=str),
                source_tier,
                json.dumps(self._json_list(payload.get("source_types")), ensure_ascii=False, default=str),
                json.dumps(self._json_list(payload.get("provider_chain")), ensure_ascii=False, default=str),
                float(reliability_score),
                int(payload.get("cross_source_count") or 0),
                status,
                self._clean_context_text(payload.get("reject_reason"), max_len=500) or None,
                self._clean_context_text(payload.get("freshness_status") or "unknown", max_len=80) or "unknown",
                self._clean_context_text(payload.get("event_anchor_id") or event_id, max_len=200) or event_id,
                checksum,
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
            )
        return self._decode_market_event_normalized(dict(row))

    async def list_market_events_normalized(
        self,
        *,
        status: str | None = None,
        source_tier: str | None = None,
        event_type: str | None = None,
        event_signature: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(str(status).strip())
            idx += 1
        if source_tier:
            conditions.append(f"source_tier = ${idx}")
            params.append(self._normalize_source_tier(source_tier))
            idx += 1
        if event_type:
            conditions.append(f"event_type = ${idx}")
            params.append(str(event_type).strip())
            idx += 1
        if event_signature:
            conditions.append(f"json_extract(metadata, '$.event_signature') = ${idx}")
            params.append(str(event_signature).strip())
            idx += 1
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(max(1, min(int(limit or 100), 1000)))
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM market_events_normalized
                {where}
                ORDER BY evidence_time DESC, reliability_score DESC, updated_at DESC
                LIMIT ${idx}
                """,
                *params,
            )
        return [self._decode_market_event_normalized(dict(row)) for row in rows]

    async def count_market_events_normalized(self) -> dict[str, Any]:
        async with self.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM market_events_normalized") or 0
            by_status = await conn.fetch(
                """
                SELECT status, COUNT(*) AS count
                FROM market_events_normalized
                GROUP BY status
                ORDER BY status
                """
            )
            by_tier = await conn.fetch(
                """
                SELECT source_tier, COUNT(*) AS count
                FROM market_events_normalized
                GROUP BY source_tier
                ORDER BY source_tier
                """
            )
        return {
            "total": int(total or 0),
            "by_status": {str(row.get("status") or ""): int(row.get("count") or 0) for row in by_status},
            "by_source_tier": {str(row.get("source_tier") or ""): int(row.get("count") or 0) for row in by_tier},
        }
