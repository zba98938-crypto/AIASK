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


class _StockRadarMixin:
    async def upsert_stock_radar_run(self, item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item or {})
        run_id = self._clean_context_text(payload.get("run_id"), max_len=220) or self._build_stock_radar_run_id(payload.get("mode"))
        mode = self._clean_context_text(payload.get("mode") or "dry_run", max_len=80) or "dry_run"
        status = self._clean_context_text(payload.get("status") or "running", max_len=80) or "running"
        started_at = self._clean_context_text(payload.get("started_at") or datetime.now(timezone.utc).isoformat(), max_len=80)
        completed_at = self._clean_context_text(payload.get("completed_at"), max_len=80) or None
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO stock_radar_runs (
                    run_id, mode, status, started_at, completed_at, summary, degraded_flags,
                    error, metadata, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (run_id) DO UPDATE SET
                    mode = EXCLUDED.mode,
                    status = EXCLUDED.status,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at,
                    summary = EXCLUDED.summary,
                    degraded_flags = EXCLUDED.degraded_flags,
                    error = EXCLUDED.error,
                    metadata = EXCLUDED.metadata,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                run_id,
                mode,
                status,
                started_at,
                completed_at,
                self._radar_json_text("summary", payload.get("summary"), default={}),
                self._radar_json_text("degraded_flags", payload.get("degraded_flags"), default=[]),
                self._clean_context_text(payload.get("error"), max_len=2000) or None,
                self._radar_json_text("metadata", payload.get("metadata"), default={}),
            )
        return self._decode_stock_radar_run(dict(row))

    async def get_stock_radar_run(self, run_id: str) -> dict[str, Any] | None:
        token = self._clean_context_text(run_id, max_len=220)
        if not token:
            return None
        async with self.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM stock_radar_runs WHERE run_id = $1", token)
        return self._decode_stock_radar_run(dict(row)) if row else None

    async def list_stock_radar_runs(
        self,
        *,
        status: str | None = None,
        mode: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(self._clean_context_text(status, max_len=80))
            idx += 1
        if mode:
            conditions.append(f"mode = ${idx}")
            params.append(self._clean_context_text(mode, max_len=80))
            idx += 1
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(max(1, min(int(limit or 20), 200)))
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM stock_radar_runs
                {where}
                ORDER BY started_at DESC, updated_at DESC
                LIMIT ${idx}
                """,
                *params,
            )
        return [self._decode_stock_radar_run(dict(row)) for row in rows]

    async def upsert_stock_radar_candidate(self, item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item or {})
        run_id = self._clean_context_text(payload.get("run_id"), max_len=220)
        symbol = self._clean_context_text(payload.get("symbol") or payload.get("stock_code") or payload.get("code"), max_len=40)
        if not run_id:
            raise ValueError("run_id is required")
        if not symbol:
            raise ValueError("symbol is required")
        radar_score = self._coerce_context_float(payload.get("radar_score"))
        radar_score = max(0.0, min(float(radar_score if radar_score is not None else 0.0), 100.0))
        tier = self._stock_radar_tier(payload.get("tier"), score=radar_score)
        candidate_id = self._build_stock_radar_candidate_id({**payload, "run_id": run_id, "symbol": symbol})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO stock_radar_candidates (
                    candidate_id, run_id, symbol, stock_name, tier, radar_score, event_id,
                    event_type, direction, summary, source_doc_uids, source_chain, extraction,
                    confirmations, risk_flags, push_status, created_at, updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7,
                    $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (candidate_id) DO UPDATE SET
                    run_id = EXCLUDED.run_id,
                    symbol = EXCLUDED.symbol,
                    stock_name = EXCLUDED.stock_name,
                    tier = EXCLUDED.tier,
                    radar_score = EXCLUDED.radar_score,
                    event_id = EXCLUDED.event_id,
                    event_type = EXCLUDED.event_type,
                    direction = EXCLUDED.direction,
                    summary = EXCLUDED.summary,
                    source_doc_uids = EXCLUDED.source_doc_uids,
                    source_chain = EXCLUDED.source_chain,
                    extraction = EXCLUDED.extraction,
                    confirmations = EXCLUDED.confirmations,
                    risk_flags = EXCLUDED.risk_flags,
                    push_status = EXCLUDED.push_status,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                candidate_id,
                run_id,
                symbol,
                self._clean_context_text(payload.get("stock_name") or payload.get("name"), max_len=160) or None,
                tier,
                radar_score,
                self._clean_context_text(payload.get("event_id"), max_len=220) or None,
                self._clean_context_text(payload.get("event_type") or "unknown", max_len=120) or "unknown",
                self._clean_context_text(payload.get("direction") or "neutral", max_len=40) or "neutral",
                self._clean_context_text(payload.get("summary"), max_len=1200) or None,
                self._radar_json_text("source_doc_uids", payload.get("source_doc_uids"), default=[]),
                self._radar_json_text("source_chain", payload.get("source_chain"), default=[]),
                self._radar_json_text("extraction", payload.get("extraction"), default={}),
                self._radar_json_text("confirmations", payload.get("confirmations"), default={}),
                self._radar_json_text("risk_flags", payload.get("risk_flags"), default=[]),
                self._clean_context_text(payload.get("push_status") or "pending", max_len=80) or "pending",
            )
        return self._decode_stock_radar_candidate(dict(row))

    async def list_stock_radar_candidates(
        self,
        *,
        run_id: str | None = None,
        tier: str | None = None,
        symbol: str | None = None,
        min_score: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1
        if run_id:
            conditions.append(f"run_id = ${idx}")
            params.append(self._clean_context_text(run_id, max_len=220))
            idx += 1
        if tier:
            conditions.append(f"tier = ${idx}")
            params.append(self._stock_radar_tier(tier))
            idx += 1
        if symbol:
            conditions.append(f"symbol = ${idx}")
            params.append(self._clean_context_text(symbol, max_len=40))
            idx += 1
        if min_score is not None:
            conditions.append(f"radar_score >= ${idx}")
            params.append(float(min_score))
            idx += 1
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(max(1, min(int(limit or 100), 1000)))
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM stock_radar_candidates
                {where}
                ORDER BY radar_score DESC, updated_at DESC
                LIMIT ${idx}
                """,
                *params,
            )
        return [self._decode_stock_radar_candidate(dict(row)) for row in rows]

    async def summarize_stock_radar(
        self,
        *,
        run_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        runs = [await self.get_stock_radar_run(run_id)] if run_id else await self.list_stock_radar_runs(limit=1)
        run = next((item for item in runs if item), None)
        if run is None:
            return {
                "object": "stock_radar.status",
                "status": "empty",
                "configured": True,
                "latest_run": None,
                "counts": {},
                "candidates": [],
                "digest_preview": "",
            }
        candidates = await self.list_stock_radar_candidates(run_id=str(run.get("run_id") or ""), limit=limit)
        counts: dict[str, int] = {}
        summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
        summary_counts = summary.get("tier_counts") if isinstance(summary.get("tier_counts"), dict) else {}
        if summary_counts:
            counts = {str(key): int(value or 0) for key, value in summary_counts.items()}
        else:
            for item in candidates:
                tier = str(item.get("tier") or "unknown")
                counts[tier] = int(counts.get(tier) or 0) + 1
        lines = ["AIASK Stock Radar Digest", f"run={run.get('run_id')} status={run.get('status')}"]
        for item in candidates[: min(len(candidates), 8)]:
            flags = ", ".join(str(flag) for flag in list(item.get("risk_flags") or [])[:3])
            suffix = f" risk={flags}" if flags else ""
            lines.append(
                f"{item.get('symbol')} {item.get('stock_name') or ''} "
                f"{round(float(item.get('radar_score') or 0), 1)} {item.get('tier')} "
                f"{item.get('event_type')}: {item.get('summary') or ''}{suffix}".strip()
            )
        return {
            "object": "stock_radar.status",
            "status": run.get("status") or "unknown",
            "configured": True,
            "latest_run": run,
            "counts": counts,
            "candidates": candidates,
            "digest_preview": "\n".join(lines),
        }

    async def save_stock_radar_push_log(self, item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item or {})
        push_id = self._clean_context_text(payload.get("push_id"), max_len=220)
        if not push_id:
            basis = "|".join(
                [
                    self._clean_context_text(payload.get("run_id"), max_len=220),
                    self._clean_context_text(payload.get("channel") or payload.get("platform"), max_len=80),
                    self._clean_context_text(payload.get("target"), max_len=220),
                    datetime.now(timezone.utc).isoformat(),
                    uuid.uuid4().hex[:10],
                ]
            )
            push_id = f"radar_push_{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:24]}"
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO stock_radar_push_logs (
                    push_id, run_id, channel, platform, target, status, message_preview,
                    candidate_count, error, metadata, created_at, sent_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, CURRENT_TIMESTAMP, $11)
                ON CONFLICT (push_id) DO UPDATE SET
                    run_id = EXCLUDED.run_id,
                    channel = EXCLUDED.channel,
                    platform = EXCLUDED.platform,
                    target = EXCLUDED.target,
                    status = EXCLUDED.status,
                    message_preview = EXCLUDED.message_preview,
                    candidate_count = EXCLUDED.candidate_count,
                    error = EXCLUDED.error,
                    metadata = EXCLUDED.metadata,
                    sent_at = EXCLUDED.sent_at
                RETURNING *
                """,
                push_id,
                self._clean_context_text(payload.get("run_id"), max_len=220) or None,
                self._clean_context_text(payload.get("channel") or payload.get("platform") or "preview", max_len=80) or "preview",
                self._clean_context_text(payload.get("platform"), max_len=80) or None,
                self._clean_context_text(payload.get("target"), max_len=220) or None,
                self._clean_context_text(payload.get("status") or "preview", max_len=80) or "preview",
                self._clean_context_text(payload.get("message_preview"), max_len=4000) or None,
                int(payload.get("candidate_count") or 0),
                self._clean_context_text(payload.get("error"), max_len=2000) or None,
                self._radar_json_text("metadata", payload.get("metadata"), default={}),
                self._clean_context_text(payload.get("sent_at"), max_len=80) or None,
            )
        return self._decode_stock_radar_push_log(dict(row))

    async def list_stock_radar_push_logs(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1
        if run_id:
            conditions.append(f"run_id = ${idx}")
            params.append(self._clean_context_text(run_id, max_len=220))
            idx += 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(self._clean_context_text(status, max_len=80))
            idx += 1
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(max(1, min(int(limit or 50), 500)))
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM stock_radar_push_logs
                {where}
                ORDER BY created_at DESC
                LIMIT ${idx}
                """,
                *params,
            )
        return [self._decode_stock_radar_push_log(dict(row)) for row in rows]
