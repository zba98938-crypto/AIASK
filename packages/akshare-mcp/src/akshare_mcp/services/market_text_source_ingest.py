"""Public market text ingestion for SQLite vector search.

This module turns publicly accessible news / notices / research metadata into
the unified ``market_documents`` / ``market_doc_chunks`` / vector collections.
It intentionally stores public summaries and metadata, not paid report bodies.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import requests

from ..vector_collection_scope import normalize_market_doc_types, resolve_vector_collection_name
from .market_event_sources import (
    bridge_normalized_events_to_strategy_factory,
    event_source_status,
    fetch_official_market_event_documents,
    persist_normalized_events,
)

EASTMONEY_NEWS_URL = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.eastmoney.com/",
}


def _clean_text(value: Any, limit: int = 20000) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return ""
    return " ".join(text.split())[:limit]


def _positive_int(value: Any, default: int, *, minimum: int = 0, maximum: int = 5000) -> int:
    try:
        resolved = int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        resolved = int(default)
    return max(minimum, min(resolved, maximum))


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _normalize_codes(raw: Any) -> list[str]:
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple, set)) else str(raw).replace(";", ",").split(",")
    codes: list[str] = []
    seen: set[str] = set()
    for item in items:
        code = _clean_text(item, 20)
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _parse_code_from_notice_url(url: Any) -> str:
    matched = re.search(r"/detail/(\d{6})/", str(url or ""))
    return matched.group(1) if matched else ""


def fetch_eastmoney_finance_news(limit: int) -> list[dict[str, Any]]:
    """Fetch public Eastmoney finance headlines and summaries."""

    resolved_limit = _positive_int(limit, 50, minimum=1, maximum=500)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    while len(items) < resolved_limit and page <= 10:
        page_size = min(50, max(1, resolved_limit - len(items)))
        params = {
            "client": "web",
            "biz": "web_news_col",
            "column": "350",
            "order": "1",
            "needInteractData": "0",
            "page_index": str(page),
            "page_size": str(page_size),
            "req_trace": f"{int(time.time() * 1000)}{random.randint(100, 999)}",
        }
        response = requests.get(EASTMONEY_NEWS_URL, params=params, headers=EASTMONEY_HEADERS, timeout=15)
        response.raise_for_status()
        payload = response.json()
        rows = ((payload.get("data") or {}).get("list") or []) if isinstance(payload, dict) else []
        if not rows:
            break
        for row in rows:
            title = _clean_text(row.get("title"), 500)
            summary = _clean_text(row.get("summary"), 4000)
            url = _clean_text(row.get("uniqueUrl") or row.get("url"), 1000)
            source_id = _clean_text(row.get("code"), 200) or url or title
            if not title or source_id in seen:
                continue
            seen.add(source_id)
            items.append(
                {
                    "doc_uid": f"eastmoney_news:{source_id}",
                    "title": title,
                    "summary": summary,
                    "content": " ".join(part for part in (title, summary) if part),
                    "date": _clean_text(row.get("showTime"), 40),
                    "published_at": _clean_text(row.get("showTime"), 40),
                    "source": _clean_text(row.get("mediaName"), 120) or "eastmoney_finance_news",
                    "source_tier": "tier_c",
                    "reliability_score": 0.42,
                    "url": url,
                    "provider": "eastmoney_finance_news",
                    "original_id": source_id,
                }
            )
            if len(items) >= resolved_limit:
                break
        page += 1
    return items


def _map_notice_item(item: dict[str, Any]) -> dict[str, Any]:
    code = _clean_text(item.get("code") or item.get("stock_code") or _parse_code_from_notice_url(item.get("url")), 20)
    title = _clean_text(item.get("title"), 500)
    notice_type = _clean_text(item.get("type") or item.get("notice_type"), 120)
    body = " ".join(part for part in (title, notice_type) if part)
    return {
        "doc_uid": _clean_text(item.get("url"), 1000) or f"eastmoney_notice:{code}:{item.get('date')}:{title}",
        "title": title,
        "summary": body,
        "content": body,
        "date": _clean_text(item.get("date"), 40),
        "published_at": _clean_text(item.get("date"), 40),
        "source": _clean_text(item.get("source"), 120) or "eastmoney_notice",
        "source_tier": _clean_text(item.get("source_tier"), 40) or "tier_c",
        "reliability_score": item.get("reliability_score") if item.get("reliability_score") is not None else 0.48,
        "url": _clean_text(item.get("url"), 1000),
        "provider": "eastmoney_notice",
        "original_id": _clean_text(item.get("art_code") or item.get("original_id"), 200),
        "notice_type": notice_type,
        "code": code,
    }


def _merge_event_summary(bucket: dict[str, Any], event_summary: dict[str, Any]) -> None:
    for key in ("total", "verified", "provisional", "degraded", "rejected"):
        bucket[key] = int(bucket.get(key) or 0) + int(event_summary.get(key) or 0)
    bucket.setdefault("latest", []).extend(list(event_summary.get("latest") or [])[:5])


def _map_research_item(item: dict[str, Any]) -> dict[str, Any]:
    title = _clean_text(item.get("title"), 500)
    institution = _clean_text(item.get("institution") or item.get("source"), 200)
    author = _clean_text(item.get("author") or item.get("analyst"), 200)
    rating = _clean_text(item.get("rating"), 120)
    summary = _clean_text(item.get("summary") or item.get("content") or item.get("text"), 4000)
    body_parts = [
        title,
        f"机构:{institution}" if institution else "",
        f"评级:{rating}" if rating else "",
        summary,
    ]
    return {
        "title": title,
        "summary": " ".join(part for part in body_parts[1:] if part),
        "content": " ".join(part for part in body_parts if part),
        "date": _clean_text(item.get("date") or item.get("publish_date"), 40),
        "published_at": _clean_text(item.get("date") or item.get("publish_date"), 40),
        "source": institution or "akshare_research_report_em",
        "source_tier": _clean_text(item.get("source_tier"), 40) or "tier_c",
        "reliability_score": item.get("reliability_score") if item.get("reliability_score") is not None else 0.5,
        "url": _clean_text(item.get("url") or item.get("pdf_url"), 1000),
        "provider": "akshare_stock_research_report_em",
        "original_id": _clean_text(item.get("original_id") or item.get("report_id"), 200),
        "author": author,
        "rating": rating,
        "institution": institution,
    }


async def _insert_news_cache(db, rows: list[dict[str, Any]], *, stock_code: str, news_type: str) -> int:
    inserted = 0
    async with db.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS news_cache (
                id INTEGER PRIMARY KEY,
                stock_code TEXT,
                news_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT,
                source TEXT,
                url TEXT,
                publish_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for item in rows:
            title = _clean_text(item.get("title"), 500)
            if not title:
                continue
            result = await conn.fetchval(
                """
                INSERT INTO news_cache (stock_code, news_type, title, content, source, url, publish_date, created_at)
                SELECT $1, $2, $3, $4, $5, $6, $7, CURRENT_TIMESTAMP
                WHERE NOT EXISTS (
                    SELECT 1 FROM news_cache
                    WHERE COALESCE(stock_code, '') = COALESCE($1, '')
                      AND news_type = $2
                      AND title = $3
                      AND COALESCE(publish_date, '') = COALESCE($7, '')
                )
                RETURNING 1
                """,
                stock_code,
                news_type,
                title,
                _clean_text(item.get("content") or item.get("summary"), 4000),
                _clean_text(item.get("source"), 120),
                _clean_text(item.get("url"), 1000),
                _clean_text(item.get("date") or item.get("published_at"), 40),
            )
            if result:
                inserted += 1
    return inserted


async def _select_stock_universe(db, *, limit: int, extra_codes: list[str]) -> list[str]:
    resolved_limit = _positive_int(limit, 30, minimum=0, maximum=1000)
    codes: list[str] = []
    seen: set[str] = set()
    for code in extra_codes:
        if re.fullmatch(r"\d{6}", code) and code not in seen:
            seen.add(code)
            codes.append(code)
    if resolved_limit <= 0:
        return codes
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT stock_code
            FROM stocks
            WHERE stock_code IS NOT NULL AND length(stock_code) = 6
            ORDER BY COALESCE(market_cap, 0) DESC, stock_code ASC
            LIMIT $1
            """,
            max(1, resolved_limit * 2),
        )
    for row in rows:
        code = _clean_text(dict(row).get("stock_code"), 20)
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
        if len(codes) >= resolved_limit:
            break
    return codes[:resolved_limit]


def _merge_saved_totals(target: dict[str, Any], saved: dict[str, Any]) -> None:
    for key in ("documents", "chunks", "embedded_chunks", "headline_labels"):
        target[key] = int(target.get(key) or 0) + int(saved.get(key) or 0)
    for key, value in dict(saved.get("profile_version_counts") or {}).items():
        bucket = target.setdefault("profile_version_counts", {})
        bucket[str(key)] = int(bucket.get(str(key)) or 0) + int(value or 0)
    for vector_dim in list(saved.get("vector_dims") or []):
        dims = target.setdefault("vector_dims", [])
        resolved_dim = int(vector_dim or 0)
        if resolved_dim > 0 and resolved_dim not in dims:
            dims.append(resolved_dim)


def _snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    qa = dict(snapshot.get("qa") or {})
    snap = dict(snapshot.get("snapshot") or {})
    return {
        "collection_name": snapshot.get("collection_name") or snap.get("collection_name"),
        "profile_type": snapshot.get("profile_type") or snap.get("profile_type"),
        "profile_version": snapshot.get("profile_version"),
        "index_version": snapshot.get("index_version") or snap.get("index_version"),
        "status": snapshot.get("status") or snap.get("status"),
        "degraded": bool(snapshot.get("degraded")),
        "quality_flags": list(snapshot.get("quality_flags") or []),
        "sample_count": int(snapshot.get("sample_count") or snap.get("sample_count") or 0),
        "items_count": int(snapshot.get("items_count") or (snap.get("metrics") or {}).get("items_count") or 0),
        "bucket_count": int(snapshot.get("bucket_count") or snap.get("bucket_count") or 0),
        "qa_status": qa.get("status"),
    }


async def _build_market_doc_snapshots(
    db,
    *,
    doc_types: list[str],
    activate: bool,
    dry_run: bool,
) -> list[dict[str, Any]]:
    if dry_run:
        return [
            {
                "collection_name": resolve_vector_collection_name("market_doc_chunks", doc_type),
                "profile_type": doc_type,
                "status": "skipped",
                "reason": "dry_run",
            }
            for doc_type in doc_types
        ]

    from .unified_vector_governance import build_vector_collection_snapshot

    snapshots: list[dict[str, Any]] = []
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT collection_name, profile_type, version, vector_dim, COUNT(*) AS rows
            FROM vector_profiles
            WHERE collection_name LIKE 'market_doc_chunks__%'
            GROUP BY collection_name, profile_type, version, vector_dim
            ORDER BY collection_name, version
            """
        )
    requested = {resolve_vector_collection_name("market_doc_chunks", doc_type) for doc_type in doc_types}
    for row in rows:
        item = dict(row)
        collection_name = str(item.get("collection_name") or "")
        if collection_name not in requested or int(item.get("rows") or 0) <= 0:
            continue
        snapshot = await build_vector_collection_snapshot(
            db,
            collection_name=collection_name,
            version=str(item.get("version") or "v1"),
            index_version=str(item.get("version") or "v1"),
            profile_type=str(item.get("profile_type") or "") or None,
            activate=activate,
            source="data_sync_manager.market_text_source_ingest",
        )
        snapshots.append(_snapshot_summary(snapshot))
    return snapshots


async def _load_final_counts(db) -> dict[str, Any]:
    async with db.acquire() as conn:
        docs = await conn.fetch(
            "SELECT doc_type, COUNT(*) AS rows FROM market_documents GROUP BY doc_type ORDER BY doc_type"
        )
        chunks = await conn.fetch(
            "SELECT doc_type, COUNT(*) AS rows FROM market_doc_chunks GROUP BY doc_type ORDER BY doc_type"
        )
        profiles = await conn.fetch(
            """
            SELECT collection_name, profile_type, version, vector_dim, COUNT(*) AS rows
            FROM vector_profiles
            WHERE collection_name LIKE 'market_doc_chunks__%'
            GROUP BY collection_name, profile_type, version, vector_dim
            ORDER BY collection_name
            """
        )
        fts_rows = await conn.fetchval("SELECT COUNT(*) FROM market_doc_chunks_fts")
        integrity = await conn.fetchval("PRAGMA integrity_check")
    return {
        "market_documents_by_type": [dict(row) for row in docs],
        "market_doc_chunks_by_type": [dict(row) for row in chunks],
        "market_doc_vector_profiles": [dict(row) for row in profiles],
        "fts_rows": int(fts_rows or 0),
        "integrity_check": integrity,
    }


class MarketEventIngestSupport:
    """AKShare-host support object for the canonical Strategy Factory runtime."""

    def status(self) -> dict[str, Any]:
        return {
            "provider": "akshare_mcp",
            "event_source_status": event_source_status(),
        }

    def _resolve_runtime_args(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        requested_doc_types = normalize_market_doc_types(kwargs.get("doc_types")) or [
            "news",
            "notice",
            "research",
        ]
        requested_codes = _normalize_codes(kwargs.get("stock_codes"))
        resolved_news_limit = _positive_int(kwargs.get("news_limit"), 50, minimum=0, maximum=1000)
        resolved_notice_limit = _positive_int(kwargs.get("notice_limit"), 80, minimum=0, maximum=1000)
        resolved_official_notice_limit = _positive_int(
            kwargs.get("official_notice_limit"), 30, minimum=0, maximum=1000
        )
        resolved_notice_days = _positive_int(kwargs.get("notice_days"), 30, minimum=1, maximum=365)
        resolved_code_notice_limit = _positive_int(kwargs.get("code_notice_limit"), 2, minimum=0, maximum=100)
        resolved_code_notice_code_limit = _positive_int(
            kwargs.get("code_notice_code_limit"), 20, minimum=0, maximum=1000
        )
        resolved_research_code_limit = _positive_int(
            kwargs.get("research_code_limit"), 30, minimum=0, maximum=1000
        )
        resolved_research_per_code = _positive_int(kwargs.get("research_per_code"), 2, minimum=0, maximum=50)
        resolved_chunk_size = _positive_int(kwargs.get("chunk_size"), 1000, minimum=200, maximum=4000)
        resolved_overlap = _positive_int(kwargs.get("overlap"), 120, minimum=0, maximum=1500)
        resolved_embed = _as_bool(kwargs.get("embed"), True)
        resolved_build_snapshot = _as_bool(kwargs.get("build_snapshot"), True)
        resolved_activate_snapshot = _as_bool(kwargs.get("activate_snapshot"), True)
        resolved_allow_network = _as_bool(kwargs.get("allow_network"), True)
        resolved_dry_run = _as_bool(kwargs.get("dry_run"), False)
        resolved_version = str(kwargs.get("version") or "v1").strip() or "v1"

        end_date = date.today()
        start_date = end_date - timedelta(days=resolved_notice_days)
        args_payload = {
            "stock_codes": requested_codes,
            "news_limit": resolved_news_limit,
            "notice_limit": resolved_notice_limit,
            "official_notice_limit": resolved_official_notice_limit,
            "notice_days": resolved_notice_days,
            "code_notice_limit": resolved_code_notice_limit,
            "code_notice_code_limit": resolved_code_notice_code_limit,
            "research_code_limit": resolved_research_code_limit,
            "research_per_code": resolved_research_per_code,
            "chunk_size": resolved_chunk_size,
            "overlap": resolved_overlap,
            "version": resolved_version,
            "embed": resolved_embed,
            "build_snapshot": resolved_build_snapshot,
            "activate_snapshot": resolved_activate_snapshot,
            "allow_network": resolved_allow_network,
            "dry_run": resolved_dry_run,
        }
        return {
            "requested_doc_types": requested_doc_types,
            "requested_codes": requested_codes,
            "news_limit": resolved_news_limit,
            "notice_limit": resolved_notice_limit,
            "official_notice_limit": resolved_official_notice_limit,
            "notice_days": resolved_notice_days,
            "code_notice_limit": resolved_code_notice_limit,
            "code_notice_code_limit": resolved_code_notice_code_limit,
            "research_code_limit": resolved_research_code_limit,
            "research_per_code": resolved_research_per_code,
            "chunk_size": resolved_chunk_size,
            "overlap": resolved_overlap,
            "version": resolved_version,
            "embed": resolved_embed,
            "build_snapshot": resolved_build_snapshot,
            "activate_snapshot": resolved_activate_snapshot,
            "allow_network": resolved_allow_network,
            "dry_run": resolved_dry_run,
            "start_date": start_date,
            "end_date": end_date,
            "args_payload": args_payload,
        }

    @staticmethod
    def _event_source_status() -> dict[str, Any]:
        return event_source_status()

    @staticmethod
    async def _build_market_doc_snapshots(
        db,
        *,
        doc_types: list[str],
        activate: bool,
        dry_run: bool,
    ) -> list[dict[str, Any]]:
        return await _build_market_doc_snapshots(
            db,
            doc_types=doc_types,
            activate=activate,
            dry_run=dry_run,
        )

    @staticmethod
    async def _load_final_counts(db) -> dict[str, Any]:
        return await _load_final_counts(db)

    @staticmethod
    async def _persist_normalized_events(
        db,
        stock_code: str,
        doc_type: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await persist_normalized_events(db, stock_code, doc_type, items)

    @staticmethod
    async def _bridge_normalized_events_to_strategy_factory(
        db,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await bridge_normalized_events_to_strategy_factory(db, limit=limit)

    @staticmethod
    async def _insert_news_cache(
        db,
        rows: list[dict[str, Any]],
        *,
        stock_code: str,
        news_type: str,
    ) -> int:
        return await _insert_news_cache(db, rows, stock_code=stock_code, news_type=news_type)

    @staticmethod
    async def _select_stock_universe(
        db,
        *,
        limit: int,
        extra_codes: list[str],
    ) -> list[str]:
        return await _select_stock_universe(db, limit=limit, extra_codes=extra_codes)

    @staticmethod
    def _merge_saved_totals(target: dict[str, Any], saved: dict[str, Any]) -> None:
        _merge_saved_totals(target, saved)

    @staticmethod
    def _merge_event_summary(bucket: dict[str, Any], event_summary: dict[str, Any]) -> None:
        _merge_event_summary(bucket, event_summary)

    @staticmethod
    def _clean_text(value: Any, limit: int = 20000) -> str:
        return _clean_text(value, limit)

    @staticmethod
    def _map_notice_item(item: dict[str, Any]) -> dict[str, Any]:
        return _map_notice_item(item)

    @staticmethod
    def _map_research_item(item: dict[str, Any]) -> dict[str, Any]:
        return _map_research_item(item)

    @staticmethod
    def _fetch_official_market_event_documents(
        start_iso: str,
        end_iso: str,
        *,
        limit: int,
        stock_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        return fetch_official_market_event_documents(
            start_iso,
            end_iso,
            limit=limit,
            stock_codes=stock_codes,
        )

    @staticmethod
    def _fetch_notice_head(
        start_date: date,
        end_date: date,
        notice_limit: int,
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        from ..tools.news.notices import fetch_market_notice_head

        raw_notices: list[dict[str, Any]] = []
        if notice_limit <= 0:
            return raw_notices
        try:
            raw_notices = fetch_market_notice_head(
                start_date.isoformat(),
                end_date.isoformat(),
                max_items=notice_limit,
            )
        except Exception as exc:
            result.setdefault("errors", []).append(
                {"source": "eastmoney_notice_head", "error": f"{type(exc).__name__}: {exc}"}
            )
        return raw_notices

    async def _fetch_code_notice_items(
        self,
        *,
        start_date: date,
        end_date: date,
        code: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        from ..tools.news.notices import get_stock_notices

        response = get_stock_notices(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            stock_code=code,
            types=["全部"],
            prefer_db=False,
        )
        events = ((response.get("data") or {}).get("events") or []) if isinstance(response, dict) else []
        mapped = [self._map_notice_item(item) for item in events[:limit]]
        return [item for item in mapped if item.get("title")]

    async def _ingest_news(
        self,
        db,
        result: dict[str, Any],
        *,
        news_limit: int,
        embed: bool,
        chunk_size: int,
        overlap: int,
        version: str,
        dry_run: bool,
    ) -> None:
        try:
            news_items = fetch_eastmoney_finance_news(news_limit)
        except Exception as exc:
            news_items = []
            result["errors"].append({"source": "eastmoney_finance_news", "error": f"{type(exc).__name__}: {exc}"})
        result["fetched"]["news"] = len(news_items)
        if not news_items:
            return
        if dry_run:
            result["saved"]["news"] = {"candidate_docs": len(news_items), "dry_run": True}
            return
        cache_rows = await self._insert_news_cache(db, news_items, stock_code="MARKET", news_type="news")
        saved = await db.save_market_documents(
            "MARKET",
            "news",
            news_items,
            embed=embed,
            chunk_size=chunk_size,
            overlap=overlap,
            version=version,
        )
        result["saved"]["news"] = {**saved, "news_cache_inserted": cache_rows}
        result["normalized_events"]["news"] = await self._persist_normalized_events(
            db,
            "MARKET",
            "news",
            news_items,
        )

    async def _ingest_research(
        self,
        db,
        result: dict[str, Any],
        *,
        requested_codes: list[str],
        notice_codes: list[str],
        research_code_limit: int,
        research_per_code: int,
        embed: bool,
        chunk_size: int,
        overlap: int,
        version: str,
        dry_run: bool,
    ) -> None:
        from ..tools.news.research import get_research_reports

        universe = await self._select_stock_universe(
            db,
            limit=research_code_limit,
            extra_codes=requested_codes or notice_codes[:10],
        )
        research_saved = {
            "fetched": 0,
            "legacy_inserted": 0,
            "documents": 0,
            "chunks": 0,
            "embedded_chunks": 0,
            "headline_labels": 0,
            "failed_codes": [],
        }
        for code in universe:
            try:
                response = get_research_reports(code=code, limit=research_per_code, prefer_db=False)
                payload = response.get("data") if isinstance(response, dict) else {}
                rows = list((payload or {}).get("reports") or []) if isinstance(payload, dict) else []
                mapped = [self._map_research_item(item) for item in rows[:research_per_code]]
                mapped = [item for item in mapped if item.get("title") or item.get("summary")]
                research_saved["fetched"] += len(mapped)
                if mapped and not dry_run:
                    legacy_inserted = await db.save_research_reports(code, mapped)
                    saved = await db.save_market_documents(
                        code,
                        "research",
                        mapped,
                        embed=embed,
                        chunk_size=chunk_size,
                        overlap=overlap,
                        version=version,
                    )
                    research_saved["legacy_inserted"] += int(legacy_inserted or 0)
                    self._merge_saved_totals(research_saved, saved)
                    event_summary = await self._persist_normalized_events(db, code, "research", mapped)
                    bucket = result["normalized_events"].setdefault(
                        "research",
                        {"total": 0, "verified": 0, "provisional": 0, "degraded": 0, "rejected": 0, "latest": []},
                    )
                    self._merge_event_summary(bucket, event_summary)
            except Exception as exc:
                research_saved["failed_codes"].append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
        result["fetched"]["research_universe"] = universe
        result["saved"]["research"] = research_saved


def get_market_event_ingest_support() -> MarketEventIngestSupport:
    return MarketEventIngestSupport()


async def run_market_text_source_ingest(
    db,
    *,
    stock_codes: Any = None,
    doc_types: Any = None,
    news_limit: Any = 50,
    notice_limit: Any = 80,
    official_notice_limit: Any = 30,
    notice_days: Any = 30,
    code_notice_limit: Any = 2,
    code_notice_code_limit: Any = 20,
    research_code_limit: Any = 30,
    research_per_code: Any = 2,
    chunk_size: Any = 1000,
    overlap: Any = 120,
    version: str = "v1",
    embed: Any = True,
    build_snapshot: Any = True,
    activate_snapshot: Any = True,
    allow_network: Any = True,
    dry_run: Any = False,
) -> dict[str, Any]:
    """Compatibility entrypoint delegating to the canonical Strategy Factory runtime."""

    from strategy_factory.runtime.market_event_ingest import build_market_event_ingest_runtime

    runtime = build_market_event_ingest_runtime(
        db_provider=lambda: db,
        support=get_market_event_ingest_support(),
    )
    return await runtime.run_once(
        db=db,
        stock_codes=stock_codes,
        doc_types=doc_types,
        news_limit=news_limit,
        notice_limit=notice_limit,
        official_notice_limit=official_notice_limit,
        notice_days=notice_days,
        code_notice_limit=code_notice_limit,
        code_notice_code_limit=code_notice_code_limit,
        research_code_limit=research_code_limit,
        research_per_code=research_per_code,
        chunk_size=chunk_size,
        overlap=overlap,
        version=version,
        embed=embed,
        build_snapshot=build_snapshot,
        activate_snapshot=activate_snapshot,
        allow_network=allow_network,
        dry_run=dry_run,
    )
