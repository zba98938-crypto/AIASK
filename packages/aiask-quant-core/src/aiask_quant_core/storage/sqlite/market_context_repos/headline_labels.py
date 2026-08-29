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


class _HeadlineMixin:
    async def save_market_headline_labels(
        self,
        stock_code: str,
        doc_type: str,
        items: Iterable[dict[str, Any]],
        *,
        doc_uid_map: Optional[dict[str, tuple[int | None, datetime | None]]] = None,
    ) -> int:
        code = str(stock_code or "").strip()
        normalized_doc_type = str(doc_type or "").strip().lower()
        rows: list[dict[str, Any]] = []
        for item in list(items or []):
            if not isinstance(item, dict):
                continue
            doc_uid = self._build_market_doc_uid(code, normalized_doc_type, item)
            mapped = dict(doc_uid_map or {}).get(doc_uid) or (None, None)
            rows.extend(
                self._extract_headline_label_rows(
                    code,
                    normalized_doc_type,
                    item,
                    doc_id=mapped[0],
                    doc_uid=doc_uid,
                    published_at=mapped[1],
                )
            )
        if not rows:
            return 0
        inserted = 0
        async with self.acquire() as conn:
            for row in rows:
                result = await conn.fetchval(
                    """
                    INSERT INTO market_headline_labels (
                        label_id, doc_id, doc_uid, stock_code, doc_type, published_at, headline, label,
                        event_type, direction, horizon_days, intensity, confidence, keywords, payload, created_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8,
                        $9, $10, $11, $12, $13, $14, $15, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (label_id) DO UPDATE SET
                        doc_id = COALESCE(EXCLUDED.doc_id, market_headline_labels.doc_id),
                        published_at = COALESCE(EXCLUDED.published_at, market_headline_labels.published_at),
                        headline = COALESCE(EXCLUDED.headline, market_headline_labels.headline),
                        label = COALESCE(EXCLUDED.label, market_headline_labels.label),
                        event_type = COALESCE(EXCLUDED.event_type, market_headline_labels.event_type),
                        direction = COALESCE(EXCLUDED.direction, market_headline_labels.direction),
                        horizon_days = COALESCE(EXCLUDED.horizon_days, market_headline_labels.horizon_days),
                        intensity = COALESCE(EXCLUDED.intensity, market_headline_labels.intensity),
                        confidence = COALESCE(EXCLUDED.confidence, market_headline_labels.confidence),
                        keywords = COALESCE(EXCLUDED.keywords, market_headline_labels.keywords),
                        payload = COALESCE(EXCLUDED.payload, market_headline_labels.payload)
                    RETURNING 1
                    """,
                    row.get("label_id"),
                    row.get("doc_id"),
                    row.get("doc_uid"),
                    row.get("stock_code"),
                    row.get("doc_type"),
                    row.get("published_at"),
                    row.get("headline"),
                    row.get("label"),
                    row.get("event_type"),
                    row.get("direction"),
                    row.get("horizon_days"),
                    row.get("intensity"),
                    row.get("confidence"),
                    json.dumps(row.get("keywords") or [], ensure_ascii=False, default=str),
                    json.dumps(row.get("payload") or {}, ensure_ascii=False, default=str),
                )
                if result:
                    inserted += 1
        return inserted

    async def list_market_headline_labels(
        self,
        stock_code: str,
        *,
        doc_type: Optional[str] = None,
        label: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM market_headline_labels WHERE stock_code = $1"
            params: list[Any] = [str(stock_code or "").strip()]
            idx = 2
            if doc_type:
                sql += f" AND doc_type = ${idx}"
                params.append(str(doc_type).strip().lower())
                idx += 1
            if label:
                sql += f" AND label = ${idx}"
                params.append(str(label).strip().lower())
                idx += 1
            sql += f" ORDER BY published_at DESC NULLS LAST, created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 200), 2000)))
            rows = await conn.fetch(sql, *params)
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["keywords"] = self._decode_json_field(payload.get("keywords"), [])
            payload["payload"] = self._decode_json_field(payload.get("payload"), {})
            result.append(payload)
        return result
