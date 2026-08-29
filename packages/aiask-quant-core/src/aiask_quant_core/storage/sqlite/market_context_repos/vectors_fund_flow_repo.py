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


class _VectorsFundFlowMixin:
    async def save_vector_documents(self, stock_code: str, doc_type: str, items: Iterable[dict[str, Any]]) -> int:
        # DEPRECATED: writes to the legacy `vector_documents` table.
        # Migrate callers to save_market_documents (unified vector schema).
        # See docs/data/vector-legacy-deprecation-plan.md — write path
        # scheduled to be removed by 2026-08-01.
        logger.warning(
            "[deprecation] save_vector_documents writing to legacy vector_documents "
            "table; migrate to market_documents by 2026-08-01 "
            "(see docs/data/vector-legacy-deprecation-plan.md)"
        )
        code = str(stock_code or "").strip()
        normalized_doc_type = str(doc_type or "").strip().lower()
        raw_items = [dict(item) for item in list(items or []) if isinstance(item, dict)]
        rows = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            content = self._pick_document_content(item)
            if not content:
                continue
            rows.append(
                (
                    code,
                    normalized_doc_type,
                    content,
                    self._coerce_context_date(item.get("date") or item.get("time")),
                )
            )

        if not code or not normalized_doc_type or not rows:
            return 0

        inserted = 0
        async with self.acquire() as conn:
            for row in rows:
                result = await conn.fetchval(
                    """
                    INSERT INTO vector_documents (stock_code, doc_type, content, date, created_at)
                    SELECT $1, $2, $3, $4, CURRENT_TIMESTAMP
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM vector_documents
                        WHERE stock_code = $1
                          AND doc_type = $2
                          AND content = $3
                          AND COALESCE(date, '1970-01-01') = COALESCE($4, '1970-01-01')
                    )
                    RETURNING 1
                    """,
                    *row,
                )
                if result:
                    inserted += 1
        if raw_items:
            try:
                await self.save_market_documents(code, normalized_doc_type, raw_items)
            except Exception as exc:
                # Best-effort double-write to market_documents — failure here
                # leaves the legacy table populated but the unified store
                # behind. Log loudly instead of silently swallowing so the
                # gap is visible to monitoring; the original transaction
                # for the legacy table has already committed.
                logger.warning(
                    "market_context: save_market_documents fallback failed for "
                    "code=%s doc_type=%s items=%d: %s",
                    code, normalized_doc_type, len(raw_items), exc, exc_info=True,
                )
        return inserted

    async def save_research_reports(self, stock_code: str, reports: Iterable[dict[str, Any]]) -> int:
        code = str(stock_code or "").strip()
        rows = []
        for item in list(reports or []):
            if not isinstance(item, dict):
                continue
            title = self._clean_context_text(item.get("title"), max_len=500)
            institution = self._clean_context_text(item.get("institution"), max_len=200)
            publish_date = self._coerce_context_date(item.get("date") or item.get("publish_date"))
            summary = self._clean_context_text(item.get("summary") or item.get("content") or item.get("text"))
            if not title and not summary:
                continue
            rows.append(
                (
                    code,
                    title,
                    self._clean_context_text(item.get("rating"), max_len=120),
                    self._coerce_context_float(item.get("targetPrice") or item.get("target_price")),
                    institution,
                    self._clean_context_text(item.get("author") or item.get("analyst"), max_len=200),
                    publish_date,
                    summary,
                    self._clean_context_text(item.get("url") or item.get("pdf_url"), max_len=1000),
                )
            )

        if not code or not rows:
            return 0

        inserted = 0
        async with self.acquire() as conn:
            for row in rows:
                result = await conn.fetchval(
                    """
                    INSERT INTO research_reports (
                        code, title, rating, target_price, institution, analyst, publish_date, summary, pdf_url, created_at
                    )
                    SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM research_reports
                        WHERE code = $1
                          AND COALESCE(title, '') = COALESCE($2, '')
                          AND COALESCE(institution, '') = COALESCE($5, '')
                          AND COALESCE(publish_date, '1970-01-01') = COALESCE($7, '1970-01-01')
                    )
                    RETURNING 1
                    """,
                    *row,
                )
                if result:
                    inserted += 1
        return inserted

    async def save_stock_fund_flow(
        self,
        stock_code: str,
        payload: dict[str, Any],
        *,
        trade_date: Any = None,
    ) -> int:
        code = str(stock_code or payload.get("code") or "").strip()
        if not code or not isinstance(payload, dict):
            return 0

        resolved_trade_date = self._coerce_context_date(trade_date or payload.get("tradeDate") or payload.get("trade_date")) or date.today()
        row = (
            code,
            resolved_trade_date,
            self._clean_context_text(payload.get("name"), max_len=200),
            self._coerce_context_float(payload.get("mainNetInflow") or payload.get("main_net_inflow") or payload.get("net_inflow")),
            self._coerce_context_float(payload.get("mainInflowPercent") or payload.get("main_inflow_percent")),
            self._coerce_context_float(payload.get("superLargeNetInflow") or payload.get("super_large_net_inflow")),
            self._coerce_context_float(payload.get("largeNetInflow") or payload.get("large_net_inflow")),
            self._coerce_context_float(payload.get("middleNetInflow") or payload.get("middle_net_inflow")),
            self._coerce_context_float(payload.get("smallNetInflow") or payload.get("small_net_inflow") or payload.get("retail_net_inflow")),
            self._clean_context_text(payload.get("source"), max_len=120),
        )

        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO stock_fund_flow (
                    code, trade_date, name, main_net_inflow, main_inflow_percent,
                    super_large_net_inflow, large_net_inflow, middle_net_inflow,
                    small_net_inflow, source, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, CURRENT_TIMESTAMP)
                ON CONFLICT (code, trade_date) DO UPDATE SET
                    name = EXCLUDED.name,
                    main_net_inflow = EXCLUDED.main_net_inflow,
                    main_inflow_percent = EXCLUDED.main_inflow_percent,
                    super_large_net_inflow = EXCLUDED.super_large_net_inflow,
                    large_net_inflow = EXCLUDED.large_net_inflow,
                    middle_net_inflow = EXCLUDED.middle_net_inflow,
                    small_net_inflow = EXCLUDED.small_net_inflow,
                    source = EXCLUDED.source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                *row,
            )
        return 1
