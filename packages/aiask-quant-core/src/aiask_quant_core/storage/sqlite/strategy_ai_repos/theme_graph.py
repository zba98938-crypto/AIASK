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


class _ThemeGraphMixin:
    async def list_theme_nodes(self, *, is_active: bool = True, limit: int = 200) -> list:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_factory_theme_nodes WHERE is_active = $1 ORDER BY theme_code LIMIT $2"
            rows = await conn.fetch(sql, 1 if is_active else 0, max(1, min(int(limit), 500)))
        result = []
        for row in rows:
            item = dict(row)
            for json_field in ("aliases", "industry_tags"):
                raw = item.get(json_field)
                if isinstance(raw, str):
                    try:
                        item[json_field] = __import__("json").loads(raw)
                    except Exception:
                        item[json_field] = []
            result.append(item)
        return result

    async def get_theme_node(self, theme_code: str):
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM strategy_factory_theme_nodes WHERE theme_code = $1",
                str(theme_code).strip(),
            )
        if row is None:
            return None
        item = dict(row)
        for json_field in ("aliases", "industry_tags"):
            raw = item.get(json_field)
            if isinstance(raw, str):
                try:
                    item[json_field] = __import__("json").loads(raw)
                except Exception:
                    item[json_field] = []
        return item

    async def upsert_theme_node(self, payload: dict) -> dict:
        import json as _json
        theme_code = str(payload.get("theme_code") or "").strip()
        if not theme_code:
            raise ValueError("theme_code is required")
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strategy_factory_theme_nodes
                    (theme_code, theme_name, parent_theme_code, breadth, default_horizon,
                     aliases, industry_tags, description, shock_detection_profile,
                     benchmark_index_code, manual_locked, is_active, updated_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT(theme_code) DO UPDATE SET
                    theme_name = EXCLUDED.theme_name,
                    parent_theme_code = EXCLUDED.parent_theme_code,
                    breadth = EXCLUDED.breadth,
                    default_horizon = EXCLUDED.default_horizon,
                    aliases = EXCLUDED.aliases,
                    industry_tags = EXCLUDED.industry_tags,
                    description = EXCLUDED.description,
                    shock_detection_profile = EXCLUDED.shock_detection_profile,
                    benchmark_index_code = EXCLUDED.benchmark_index_code,
                    manual_locked = EXCLUDED.manual_locked,
                    is_active = EXCLUDED.is_active,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = CURRENT_TIMESTAMP
                """,
                theme_code,
                str(payload.get("theme_name") or theme_code),
                payload.get("parent_theme_code"),
                str(payload.get("breadth") or "medium"),
                str(payload.get("default_horizon") or "swing_5_20d"),
                _json.dumps(payload.get("aliases") or [], ensure_ascii=False),
                _json.dumps(payload.get("industry_tags") or [], ensure_ascii=False),
                payload.get("description"),
                str(payload.get("shock_detection_profile") or "fast"),
                str(payload.get("benchmark_index_code") or "000300"),
                int(payload.get("manual_locked") or 0),
                int(payload.get("is_active", 1)),
                payload.get("updated_by"),
            )
        return await self.get_theme_node(theme_code) or {"theme_code": theme_code}

    async def list_theme_edges(self, *, source: str = None, target: str = None, is_active: bool = True, limit: int = 200) -> list:
        async with self.acquire() as conn:
            conditions = ["is_active = $1"]
            params = [1 if is_active else 0]
            idx = 2
            if source:
                conditions.append(f"source_theme_code = ${idx}")
                params.append(str(source).strip())
                idx += 1
            if target:
                conditions.append(f"target_theme_code = ${idx}")
                params.append(str(target).strip())
                idx += 1
            where = " AND ".join(conditions)
            params.append(max(1, min(int(limit), 500)))
            sql = f"SELECT * FROM strategy_factory_theme_edges WHERE {where} ORDER BY source_theme_code, target_theme_code LIMIT ${idx}"
            rows = await conn.fetch(sql, *params)
        result = []
        for row in rows:
            item = dict(row)
            raw_evidence = item.get("evidence")
            if isinstance(raw_evidence, str):
                try:
                    item["evidence"] = __import__("json").loads(raw_evidence)
                except Exception:
                    item["evidence"] = {}
            result.append(item)
        return result

    async def upsert_theme_edge(self, payload: dict) -> dict:
        import json as _json
        source = str(payload.get("source_theme_code") or "").strip()
        target = str(payload.get("target_theme_code") or "").strip()
        relation = str(payload.get("relation_type") or "").strip()
        if not source or not target or not relation:
            raise ValueError("source_theme_code, target_theme_code, relation_type are required")
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strategy_factory_theme_edges
                    (source_theme_code, target_theme_code, relation_type,
                     direction_sign, magnitude_factor, lag_days, confidence,
                     confidence_source, manual_locked, evidence, is_active, updated_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT(source_theme_code, target_theme_code, relation_type) DO UPDATE SET
                    direction_sign = EXCLUDED.direction_sign,
                    magnitude_factor = EXCLUDED.magnitude_factor,
                    lag_days = EXCLUDED.lag_days,
                    confidence = EXCLUDED.confidence,
                    confidence_source = EXCLUDED.confidence_source,
                    manual_locked = EXCLUDED.manual_locked,
                    evidence = EXCLUDED.evidence,
                    is_active = EXCLUDED.is_active,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = CURRENT_TIMESTAMP
                """,
                source, target, relation,
                int(payload.get("direction_sign") or 1),
                float(payload.get("magnitude_factor") or 0.5),
                int(payload.get("lag_days") or 0),
                float(payload.get("confidence") or 0.5),
                str(payload.get("confidence_source") or "manual"),
                int(payload.get("manual_locked") or 0),
                bounded_json_text(
                    "strategy_factory_theme_edges.evidence",
                    payload.get("evidence") or {},
                    max_bytes=strategy_json_field_max_bytes(),
                ),
                int(payload.get("is_active", 1)),
                payload.get("updated_by"),
            )
            row = await conn.fetchrow(
                "SELECT * FROM strategy_factory_theme_edges WHERE source_theme_code = $1 AND target_theme_code = $2 AND relation_type = $3",
                source, target, relation,
            )
        return dict(row) if row else {"source_theme_code": source, "target_theme_code": target}

    async def list_theme_exposure(self, theme_code: str = None, min_exposure: float = 0.3, limit: int = 30) -> list:
        """Query theme exposure matrix for a specific theme."""
        async with self.acquire() as conn:
            if theme_code:
                rows = await conn.fetch(
                    "SELECT * FROM strategy_factory_theme_exposure WHERE theme_code = $1 AND exposure_score >= $2 ORDER BY exposure_score DESC LIMIT $3",
                    theme_code, min_exposure, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM strategy_factory_theme_exposure WHERE exposure_score >= $1 ORDER BY exposure_score DESC LIMIT $2",
                    min_exposure, limit,
                )
        return [dict(r) for r in rows]

    async def list_company_concept_blocks(
        self,
        symbols=None,
        theme_code: str = None,
        limit: int = 50000,
    ) -> list:
        """List TDX-only stock -> concept block mappings.

        This deliberately reads only local TDX/cache tables. It does not call
        Tushare, AKShare, eFinance, Baostock, or any online fallback.
        """

        normalized_symbols = [
            str(item).strip()
            for item in list(symbols or [])
            if str(item).strip()
        ]
        theme_filter = str(theme_code or "").strip().lower()
        row_limit = max(1, min(int(limit or 50000), 200000))
        async with self.acquire() as conn:
            code_col = await self._stocks_code_column(conn)
            symbol_clause = ""
            params: list = []
            idx = 1
            if normalized_symbols:
                symbol_clause = f" AND s.{code_col} IN (${idx})"
                params.append(normalized_symbols)
                idx += 1
            params.append(row_limit)
            relation_rows = await conn.fetch(
                f"""
                SELECT
                    s.{code_col} AS symbol,
                    s.stock_name,
                    s.industry,
                    s.tdx_industry,
                    s.list_status,
                    s.market_cap,
                    r.block_code,
                    COALESCE(r.block_name, mb.block_name, r.block_code) AS block_name,
                    COALESCE(r.block_type, '') AS block_type,
                    COALESCE(r.gp_num, mb.stock_count, 0) AS member_count,
                    extra.trade_date,
                    extra.turnover_rate,
                    extra.zsz,
                    extra.ltsz,
                    extra.tp_flag
                FROM stocks s
                JOIN tdx_relation r ON r.code = s.{code_col}
                LEFT JOIN market_blocks mb ON mb.block_code = r.block_code
                LEFT JOIN (
                    SELECT e.*
                    FROM tdx_stock_extra e
                    JOIN (
                        SELECT code, MAX(trade_date) AS trade_date
                        FROM tdx_stock_extra
                        GROUP BY code
                    ) latest
                      ON latest.code = e.code AND latest.trade_date = e.trade_date
                ) extra ON extra.code = s.{code_col}
                WHERE (
                    LOWER(COALESCE(r.block_type, '')) LIKE '%concept%'
                    OR COALESCE(r.block_type, '') LIKE '%概念%'
                ){symbol_clause}
                ORDER BY s.{code_col}, block_name
                LIMIT ${idx}
                """,
                *params,
            )

            symbol_clause = ""
            params = []
            idx = 1
            if normalized_symbols:
                symbol_clause = f" AND s.{code_col} IN (${idx})"
                params.append(normalized_symbols)
                idx += 1
            params.append(row_limit)
            block_rows = await conn.fetch(
                f"""
                SELECT
                    s.{code_col} AS symbol,
                    s.stock_name,
                    s.industry,
                    s.tdx_industry,
                    s.list_status,
                    s.market_cap,
                    bs.block_code,
                    COALESCE(concept_blocks.block_name, mb.block_name, bs.block_code) AS block_name,
                    concept_blocks.block_type AS block_type,
                    COALESCE(concept_blocks.member_count, mb.stock_count, 0) AS member_count,
                    extra.trade_date,
                    extra.turnover_rate,
                    extra.zsz,
                    extra.ltsz,
                    extra.tp_flag
                FROM stocks s
                JOIN block_stocks bs ON bs.stock_code = s.{code_col}
                JOIN (
                    SELECT
                        block_code,
                        MAX(block_name) AS block_name,
                        MAX(block_type) AS block_type,
                        MAX(COALESCE(gp_num, 0)) AS member_count
                    FROM tdx_relation
                    WHERE (
                        LOWER(COALESCE(block_type, '')) LIKE '%concept%'
                        OR COALESCE(block_type, '') LIKE '%概念%'
                    )
                    GROUP BY block_code
                ) concept_blocks ON concept_blocks.block_code = bs.block_code
                LEFT JOIN market_blocks mb ON mb.block_code = bs.block_code
                LEFT JOIN (
                    SELECT e.*
                    FROM tdx_stock_extra e
                    JOIN (
                        SELECT code, MAX(trade_date) AS trade_date
                        FROM tdx_stock_extra
                        GROUP BY code
                    ) latest
                      ON latest.code = e.code AND latest.trade_date = e.trade_date
                ) extra ON extra.code = s.{code_col}
                WHERE 1 = 1 {symbol_clause}
                ORDER BY s.{code_col}, block_name
                LIMIT ${idx}
                """,
                *params,
            )

        merged: dict[tuple[str, str], dict] = {}
        for row in list(relation_rows or []) + list(block_rows or []):
            item = dict(row)
            if theme_filter:
                haystack = " ".join(
                    str(item.get(key) or "").lower()
                    for key in ("block_code", "block_name", "block_type")
                )
                if theme_filter not in haystack:
                    continue
            key = (str(item.get("symbol") or ""), str(item.get("block_code") or ""))
            if not key[0] or not key[1]:
                continue
            merged.setdefault(key, item)
            if len(merged) >= row_limit:
                break
        return list(merged.values())

    async def list_industry_blocks(self, symbols=None, limit: int = 50000) -> list:
        """List TDX-only stock industry rows from local stocks/tdx_relation."""

        normalized_symbols = [
            str(item).strip()
            for item in list(symbols or [])
            if str(item).strip()
        ]
        row_limit = max(1, min(int(limit or 50000), 200000))
        async with self.acquire() as conn:
            code_col = await self._stocks_code_column(conn)
            symbol_clause = ""
            params: list = []
            idx = 1
            if normalized_symbols:
                symbol_clause = f" AND s.{code_col} IN (${idx})"
                params.append(normalized_symbols)
                idx += 1
            params.append(row_limit)
            stock_rows = await conn.fetch(
                f"""
                SELECT
                    s.{code_col} AS symbol,
                    s.stock_name,
                    COALESCE(NULLIF(s.tdx_industry, ''), NULLIF(s.industry, ''), NULLIF(s.sector, '')) AS industry_name,
                    NULL AS block_code,
                    'stocks' AS industry_source,
                    s.industry,
                    s.tdx_industry,
                    s.sector,
                    s.list_status,
                    s.market_cap,
                    extra.trade_date,
                    extra.turnover_rate,
                    extra.zsz,
                    extra.ltsz,
                    extra.tp_flag
                FROM stocks s
                LEFT JOIN (
                    SELECT e.*
                    FROM tdx_stock_extra e
                    JOIN (
                        SELECT code, MAX(trade_date) AS trade_date
                        FROM tdx_stock_extra
                        GROUP BY code
                    ) latest
                      ON latest.code = e.code AND latest.trade_date = e.trade_date
                ) extra ON extra.code = s.{code_col}
                WHERE COALESCE(NULLIF(s.tdx_industry, ''), NULLIF(s.industry, ''), NULLIF(s.sector, '')) IS NOT NULL
                {symbol_clause}
                ORDER BY s.{code_col}
                LIMIT ${idx}
                """,
                *params,
            )

            symbol_clause = ""
            params = []
            idx = 1
            if normalized_symbols:
                symbol_clause = f" AND s.{code_col} IN (${idx})"
                params.append(normalized_symbols)
                idx += 1
            params.append(row_limit)
            relation_rows = await conn.fetch(
                f"""
                SELECT
                    s.{code_col} AS symbol,
                    s.stock_name,
                    COALESCE(r.block_name, r.block_code) AS industry_name,
                    r.block_code,
                    'tdx_relation' AS industry_source,
                    s.industry,
                    s.tdx_industry,
                    s.sector,
                    s.list_status,
                    s.market_cap,
                    extra.trade_date,
                    extra.turnover_rate,
                    extra.zsz,
                    extra.ltsz,
                    extra.tp_flag,
                    COALESCE(r.gp_num, 0) AS member_count
                FROM stocks s
                JOIN tdx_relation r ON r.code = s.{code_col}
                LEFT JOIN (
                    SELECT e.*
                    FROM tdx_stock_extra e
                    JOIN (
                        SELECT code, MAX(trade_date) AS trade_date
                        FROM tdx_stock_extra
                        GROUP BY code
                    ) latest
                      ON latest.code = e.code AND latest.trade_date = e.trade_date
                ) extra ON extra.code = s.{code_col}
                WHERE (
                    LOWER(COALESCE(r.block_type, '')) LIKE '%industry%'
                    OR COALESCE(r.block_type, '') LIKE '%行业%'
                ){symbol_clause}
                ORDER BY s.{code_col}, industry_name
                LIMIT ${idx}
                """,
                *params,
            )

        merged: dict[tuple[str, str, str], dict] = {}
        for row in list(stock_rows or []) + list(relation_rows or []):
            item = dict(row)
            symbol = str(item.get("symbol") or "")
            industry_name = str(item.get("industry_name") or "").strip()
            if not symbol or not industry_name:
                continue
            source = str(item.get("industry_source") or "")
            merged.setdefault((symbol, industry_name, source), item)
            if len(merged) >= row_limit:
                break
        return list(merged.values())

    async def get_theme_exposure_status(self) -> dict:
        """Return aggregate status for the theme exposure matrix."""

        async with self.acquire() as conn:
            summary = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS row_count,
                    COUNT(DISTINCT symbol) AS symbol_count,
                    COUNT(DISTINCT theme_code) AS theme_count,
                    MAX(updated_at) AS latest_updated_at,
                    AVG(exposure_score) AS avg_exposure,
                    MAX(exposure_score) AS max_exposure
                FROM strategy_factory_theme_exposure
                """
            )
            latest_rows = await conn.fetch(
                """
                SELECT theme_code, COUNT(*) AS row_count, MAX(updated_at) AS latest_updated_at
                FROM strategy_factory_theme_exposure
                GROUP BY theme_code
                ORDER BY latest_updated_at DESC
                LIMIT 20
                """
            )
        payload = dict(summary or {})
        payload["latest_themes"] = [dict(row) for row in latest_rows]
        payload["source"] = "strategy_factory_theme_exposure"
        return payload

    async def upsert_theme_exposure(self, payload: dict) -> dict:
        """Single-row upsert for ``strategy_factory_theme_exposure``.

        Used by the on-demand path (preview / single recompute). Bulk
        rebuilds should call :meth:`bulk_upsert_theme_exposure`.
        """
        import json as _json

        symbol = str(payload.get("symbol") or "").strip()
        theme_code = str(payload.get("theme_code") or "").strip()
        if not symbol or not theme_code:
            raise ValueError("symbol and theme_code are required")

        evidence = payload.get("evidence")
        if isinstance(evidence, (dict, list)):
            evidence_text = _json.dumps(evidence, ensure_ascii=False)
        elif isinstance(evidence, str):
            evidence_text = evidence
        else:
            evidence_text = "{}"

        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strategy_factory_theme_exposure
                    (symbol, theme_code, exposure_score, industry_match_level,
                     name_match_score, mainbz_match_score, historical_beta,
                     evidence)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT(symbol, theme_code) DO UPDATE SET
                    exposure_score       = EXCLUDED.exposure_score,
                    industry_match_level = EXCLUDED.industry_match_level,
                    name_match_score     = EXCLUDED.name_match_score,
                    mainbz_match_score   = EXCLUDED.mainbz_match_score,
                    historical_beta      = EXCLUDED.historical_beta,
                    evidence             = EXCLUDED.evidence,
                    updated_at           = CURRENT_TIMESTAMP
                """,
                symbol,
                theme_code,
                float(payload.get("exposure_score") or 0),
                int(payload.get("industry_match_level") or 0),
                float(payload.get("name_match_score") or 0),
                float(payload.get("mainbz_match_score") or 0),
                float(payload.get("historical_beta") or 0),
                evidence_text,
            )
        return {"symbol": symbol, "theme_code": theme_code, "written": 1}

    async def bulk_upsert_theme_exposure(self, rows, *, batch_size: int = 1000) -> dict:
        """Batched upsert for ``strategy_factory_theme_exposure``.

        方案 §6 Phase 6 验收要求：暴露度全量构建必须批量写，不能逐条
        await（6,000 标的 × 200 主题 = 120 万次写）。每批包在单事务里，
        借助 SQLite WAL + ``synchronous=NORMAL`` 已经在 SQLiteAdapter
        启动时配置（``schema_base.py``）。

        Args:
            rows: iterable of payload dicts (same shape as
                  ``upsert_theme_exposure`` accepts).
            batch_size: 每批行数，默认 1000。

        Returns:
            ``{"written": int, "batch_count": int, "skipped": int}`` —
            ``skipped`` 是因 symbol/theme_code 缺失被丢弃的行。
        """
        import json as _json

        prepared: list[tuple] = []
        skipped = 0
        for row in rows or []:
            symbol = str((row or {}).get("symbol") or "").strip()
            theme_code = str((row or {}).get("theme_code") or "").strip()
            if not symbol or not theme_code:
                skipped += 1
                continue
            evidence = (row or {}).get("evidence")
            if isinstance(evidence, (dict, list)):
                evidence_text = _json.dumps(evidence, ensure_ascii=False)
            elif isinstance(evidence, str):
                evidence_text = evidence
            else:
                evidence_text = "{}"
            prepared.append((
                symbol,
                theme_code,
                float(row.get("exposure_score") or 0),
                int(row.get("industry_match_level") or 0),
                float(row.get("name_match_score") or 0),
                float(row.get("mainbz_match_score") or 0),
                float(row.get("historical_beta") or 0),
                evidence_text,
            ))

        if not prepared:
            return {"written": 0, "batch_count": 0, "skipped": skipped}

        sql = (
            "INSERT INTO strategy_factory_theme_exposure "
            "(symbol, theme_code, exposure_score, industry_match_level, "
            " name_match_score, mainbz_match_score, historical_beta, evidence) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "ON CONFLICT(symbol, theme_code) DO UPDATE SET "
            "  exposure_score       = EXCLUDED.exposure_score, "
            "  industry_match_level = EXCLUDED.industry_match_level, "
            "  name_match_score     = EXCLUDED.name_match_score, "
            "  mainbz_match_score   = EXCLUDED.mainbz_match_score, "
            "  historical_beta      = EXCLUDED.historical_beta, "
            "  evidence             = EXCLUDED.evidence, "
            "  updated_at           = CURRENT_TIMESTAMP"
        )

        written = 0
        batch_count = 0
        async with self.acquire() as conn:
            for start in range(0, len(prepared), max(1, int(batch_size))):
                chunk = prepared[start:start + batch_size]
                # The aiask_quant_core SQLite wrapper exposes ``executemany``
                # via the underlying connection; fall back to per-row
                # ``execute`` if the wrapper does not support it.
                try:
                    await conn.executemany(sql, chunk)
                except AttributeError:
                    for params in chunk:
                        await conn.execute(sql, *params)
                written += len(chunk)
                batch_count += 1
        return {"written": written, "batch_count": batch_count, "skipped": skipped}
