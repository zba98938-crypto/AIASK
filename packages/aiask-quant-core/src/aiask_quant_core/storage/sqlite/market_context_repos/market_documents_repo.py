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


class _DocumentsMixin:
    async def save_market_documents(
        self,
        stock_code: str,
        doc_type: str,
        items: Iterable[dict[str, Any]],
        *,
        embed: bool = True,
        chunk_size: int = 800,
        overlap: int = 120,
        collection_name: str = "market_doc_chunks",
        version: str = "v1",
    ) -> dict[str, Any]:
        code = str(stock_code or "").strip()
        normalized_doc_type = str(doc_type or "").strip().lower()
        documents = [dict(item) for item in list(items or []) if isinstance(item, dict)]
        if not code or not normalized_doc_type or not documents:
            return {
                "documents": 0,
                "chunks": 0,
                "embedded_chunks": 0,
                "collection_name": resolve_vector_collection_name(collection_name, normalized_doc_type),
                "vector_status": "skipped",
                "degraded": False,
                "quality_flags": [],
                "vector_error": None,
                "embedding_attempted": 0,
                "embedding_coverage": 0.0,
            }
        resolved_collection_name = resolve_vector_collection_name(collection_name, normalized_doc_type)

        inserted_docs = 0
        inserted_chunks = 0
        inserted_labels = 0
        chunk_rows: list[dict[str, Any]] = []
        headline_rows: list[dict[str, Any]] = []
        quality_flags: list[str] = []
        vector_error: Optional[str] = None
        async with self.acquire() as conn:
            async with conn.transaction():
                for item in documents:
                    title = self._pick_document_title(item)
                    summary = self._pick_document_summary(item)
                    body = self._pick_document_body(item)
                    if not body:
                        continue
                    doc_uid = self._build_market_doc_uid(code, normalized_doc_type, item)
                    source = self._pick_document_source(normalized_doc_type, item)
                    provider = self._pick_document_provider(item, source=source)
                    source_tier = self._normalize_source_tier(item.get("source_tier"), provider=provider, source=source)
                    url = self._pick_document_url(item)
                    author = self._clean_context_text(item.get("author") or item.get("analyst"), max_len=200)
                    published_date = self._coerce_context_date(item.get("published_at") or item.get("date") or item.get("time"))
                    published_at = datetime.combine(published_date, datetime.min.time()) if published_date else None
                    fetched_at = self._coerce_context_datetime(item.get("fetched_at")) or datetime.now(timezone.utc)
                    checksum = self._build_document_checksum(item, title=title, body=body, url=url, published_at=published_at)
                    reliability_score = self._coerce_context_float(item.get("reliability_score"))
                    if reliability_score is None:
                        reliability_score = self._default_reliability_score(source_tier)
                    crawl_status = self._clean_context_text(item.get("crawl_status") or "ok", max_len=80) or "ok"
                    original_id = self._pick_document_original_id(item)
                    upstream_meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                    doc_meta = {
                        **dict(upstream_meta or {}),
                        "doc_type": normalized_doc_type,
                        "source": source,
                        "source_tier": source_tier,
                        "provider": provider,
                        "original_id": original_id,
                        "checksum": checksum,
                        "reliability_score": round(float(reliability_score), 4),
                        "crawl_status": crawl_status,
                        "raw_title": item.get("title") or item.get("headline") or item.get("name"),
                        "raw_date": item.get("date") or item.get("time"),
                    }
                    row = await conn.fetchrow(
                        """
                        INSERT INTO market_documents (
                            doc_uid, stock_code, doc_type, source, source_tier, provider, original_id,
                            title, summary, body, url, author, published_at, fetched_at, checksum,
                            reliability_score, crawl_status, metadata, created_at, updated_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7,
                                $8, $9, $10, $11, $12, $13, $14, $15,
                                $16, $17, $18, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ON CONFLICT (doc_uid) DO UPDATE SET
                            stock_code = EXCLUDED.stock_code,
                            doc_type = EXCLUDED.doc_type,
                            source = EXCLUDED.source,
                            source_tier = EXCLUDED.source_tier,
                            provider = EXCLUDED.provider,
                            original_id = EXCLUDED.original_id,
                            title = EXCLUDED.title,
                            summary = EXCLUDED.summary,
                            body = EXCLUDED.body,
                            url = EXCLUDED.url,
                            author = EXCLUDED.author,
                            published_at = EXCLUDED.published_at,
                            fetched_at = EXCLUDED.fetched_at,
                            checksum = EXCLUDED.checksum,
                            reliability_score = EXCLUDED.reliability_score,
                            crawl_status = EXCLUDED.crawl_status,
                            metadata = EXCLUDED.metadata,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING id, doc_uid
                        """,
                        doc_uid,
                        code,
                        normalized_doc_type,
                        source,
                        source_tier,
                        provider or None,
                        original_id or None,
                        title or None,
                        summary or None,
                        body,
                        url or None,
                        author or None,
                        published_at,
                        fetched_at,
                        checksum,
                        float(reliability_score),
                        crawl_status,
                        json.dumps(doc_meta, ensure_ascii=False, default=str),
                    )
                    if row:
                        inserted_docs += 1
                    doc_id = dict(row or {}).get("id")
                    if doc_id is None:
                        continue
                    headline_rows.extend(
                        self._extract_headline_label_rows(
                            code,
                            normalized_doc_type,
                            item,
                            doc_id=doc_id,
                            doc_uid=doc_uid,
                            published_at=published_at,
                        )
                    )
                    for headline_row in headline_rows[-1:]:
                        headline_inserted = await conn.fetchval(
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
                            headline_row.get("label_id"),
                            headline_row.get("doc_id"),
                            headline_row.get("doc_uid"),
                            headline_row.get("stock_code"),
                            headline_row.get("doc_type"),
                            headline_row.get("published_at"),
                            headline_row.get("headline"),
                            headline_row.get("label"),
                            headline_row.get("event_type"),
                            headline_row.get("direction"),
                            headline_row.get("horizon_days"),
                            headline_row.get("intensity"),
                            headline_row.get("confidence"),
                            json.dumps(headline_row.get("keywords") or [], ensure_ascii=False, default=str),
                            json.dumps(headline_row.get("payload") or {}, ensure_ascii=False, default=str),
                        )
                        if headline_inserted:
                            inserted_labels += 1
                    await conn.execute("DELETE FROM market_doc_chunks WHERE doc_id = $1", doc_id)
                    chunks = self._chunk_document_text(body, chunk_size=chunk_size, overlap=overlap)
                    for chunk_no, chunk_text in enumerate(chunks):
                        chunk_meta = {
                            "doc_uid": doc_uid,
                            "url": url,
                            "author": author,
                            "summary": summary[:280] if summary else "",
                            "source_tier": source_tier,
                            "provider": provider,
                            "checksum": checksum,
                            "reliability_score": round(float(reliability_score), 4),
                        }
                        chunk_row = await conn.fetchrow(
                            """
                            INSERT INTO market_doc_chunks (
                                doc_id, chunk_no, stock_code, doc_type, source, title, chunk_text,
                                token_count, char_count, language, published_at, metadata, created_at, updated_at
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'zh', $10, $11, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            RETURNING id
                            """,
                            doc_id,
                            int(chunk_no),
                            code,
                            normalized_doc_type,
                            source,
                            title or None,
                            chunk_text,
                            self._count_text_tokens(chunk_text),
                            len(chunk_text),
                            published_at,
                            json.dumps(chunk_meta, ensure_ascii=False, default=str),
                        )
                        if chunk_row:
                            inserted_chunks += 1
                        chunk_rows.append(
                            {
                                "doc_id": doc_id,
                                "doc_uid": doc_uid,
                                "chunk_id": dict(chunk_row or {}).get("id"),
                                "chunk_no": int(chunk_no),
                                "stock_code": code,
                                "doc_type": normalized_doc_type,
                                "source": source,
                                "source_tier": source_tier,
                                "provider": provider,
                                "title": title,
                                "chunk_text": chunk_text,
                                "published_at": published_at,
                                "url": url,
                                "author": author,
                                "summary": summary,
                            }
                        )

        embedded_chunks = 0
        embedding_attempted = 0
        vector_status = "disabled" if not embed else "not_requested"
        degraded = False
        profile_version_counts: dict[str, int] = {}
        vector_dims: set[int] = set()
        if embed and chunk_rows and hasattr(self, "save_vector_profile"):
            try:
                from aiask_quant_core.storage.runtime_hooks import get_text_embedding_service_factory

                service_factory = get_text_embedding_service_factory()
                service = service_factory() if callable(service_factory) else None
                if service is not None and service.is_enabled():
                    vector_status = "running"
                    profile_type = normalized_doc_type
                    ensured_index_keys: set[tuple[str, int]] = set()
                    existing_collection = None
                    if hasattr(self, "get_vector_collection"):
                        try:
                            existing_collection = await self.get_vector_collection(resolved_collection_name)
                        except Exception:
                            existing_collection = None
                    for chunk in chunk_rows:
                        embedding_attempted += 1
                        if hasattr(service, "embed_text_with_info"):
                            embedding_result = await service.embed_text_with_info(str(chunk.get("chunk_text") or ""))
                            vector = list(dict(embedding_result or {}).get("embedding") or [])
                            embedding_provider = str(dict(embedding_result or {}).get("provider") or "")
                            requested_provider = str(dict(embedding_result or {}).get("requested_provider") or "")
                            fallback_used = bool(dict(embedding_result or {}).get("fallback_used"))
                            fallback_error = dict(embedding_result or {}).get("fallback_error")
                        else:
                            vector = await service.embed_text(str(chunk.get("chunk_text") or ""))
                            embedding_provider = ""
                            requested_provider = ""
                            fallback_used = False
                            fallback_error = None
                        if not vector:
                            degraded = True
                            if "empty_embedding" not in quality_flags:
                                quality_flags.append("empty_embedding")
                            continue
                        model_id = str(getattr(getattr(service, "config", None), "model", None) or "text-embedding-3-small")
                        vector_dim = len(vector)
                        vector_dims.add(int(vector_dim))
                        profile_version = resolve_dimension_scoped_version(version, vector_dim)
                        profile_version_counts[profile_version] = int(profile_version_counts.get(profile_version) or 0) + 1
                        collection_vector_dim = int(existing_collection.get("vector_dim") or 0) if isinstance(existing_collection, dict) else 0
                        collection_vector_dim = max(collection_vector_dim, int(vector_dim or 0))
                        await self.save_vector_collection(
                            {
                                "collection_name": resolved_collection_name,
                                "entity_family": "document_chunk",
                                "backend": self.get_vector_backend(),
                                "metric": "cosine",
                                "model_id": model_id,
                                "vector_dim": collection_vector_dim or vector_dim,
                                "normalization": "unit",
                                "status": "active",
                                "metadata": {
                                    "domain": "market",
                                    "doc_type": normalized_doc_type,
                                    "base_version": str(version or "v1"),
                                    "active_profile_versions": sorted(profile_version_counts.keys()),
                                },
                            }
                        )
                        existing_collection = {
                            **dict(existing_collection or {}),
                            "vector_dim": collection_vector_dim or vector_dim,
                            "model_id": model_id,
                        }
                        entity_id = f"{chunk.get('doc_uid')}:{chunk.get('chunk_no')}"
                        await self.save_vector_profile(
                            {
                                "collection_name": resolved_collection_name,
                                "entity_type": "market_doc_chunk",
                                "entity_id": entity_id,
                                "stock_code": chunk.get("stock_code"),
                                "profile_type": profile_type,
                                "model_id": model_id,
                                "vector_dim": vector_dim,
                                "metric": "cosine",
                                "version": profile_version,
                                "signature": hashlib.sha1(
                                    f"{entity_id}|{model_id}|{chunk.get('chunk_text')}".encode("utf-8")
                                ).hexdigest(),
                                "embedding": vector,
                                "metadata": {
                                    "doc_id": chunk.get("doc_id"),
                                    "chunk_id": chunk.get("chunk_id"),
                                    "chunk_no": chunk.get("chunk_no"),
                                    "doc_uid": chunk.get("doc_uid"),
                                    "doc_type": chunk.get("doc_type"),
                                    "source": chunk.get("source"),
                                    "source_tier": chunk.get("source_tier"),
                                    "provider": chunk.get("provider"),
                                    "title": chunk.get("title"),
                                    "published_at": chunk.get("published_at").isoformat() if isinstance(chunk.get("published_at"), datetime) else None,
                                    "url": chunk.get("url"),
                                    "author": chunk.get("author"),
                                    "base_version": str(version or "v1"),
                                    "profile_version": profile_version,
                                    "embedding_provider": embedding_provider or None,
                                    "requested_embedding_provider": requested_provider or None,
                                    "embedding_fallback_used": fallback_used,
                                    "embedding_fallback_error": fallback_error,
                                },
                            }
                        )
                        embedded_chunks += 1
                        index_key = (profile_version, int(vector_dim or 0))
                        if index_key not in ensured_index_keys and hasattr(self, "ensure_vector_profile_sqlite_python_index"):
                            await self.ensure_vector_profile_sqlite_python_index(
                                collection_name=resolved_collection_name,
                                version=profile_version,
                                vector_dim=vector_dim,
                                profile_type=profile_type,
                                metric="cosine",
                            )
                            ensured_index_keys.add(index_key)
                        if fallback_used:
                            degraded = True
                            if "embedding_provider_fallback_used" not in quality_flags:
                                quality_flags.append("embedding_provider_fallback_used")
                    if embedded_chunks >= len(chunk_rows):
                        vector_status = "ready"
                    else:
                        degraded = True
                        vector_status = "degraded"
                        if "partial_embedding_coverage" not in quality_flags:
                            quality_flags.append("partial_embedding_coverage")
                else:
                    degraded = True
                    vector_status = "degraded"
                    if "embedding_service_disabled" not in quality_flags:
                        quality_flags.append("embedding_service_disabled")
            except Exception as exc:
                degraded = True
                vector_status = "degraded"
                vector_error = f"{type(exc).__name__}: {exc}"
                if "vector_generation_failed" not in quality_flags:
                    quality_flags.append("vector_generation_failed")

        return {
            "documents": inserted_docs,
            "chunks": inserted_chunks,
            "embedded_chunks": embedded_chunks,
            "headline_labels": inserted_labels,
            "collection_name": resolved_collection_name,
            "vector_status": vector_status,
            "degraded": degraded,
            "quality_flags": quality_flags,
            "vector_error": vector_error,
            "embedding_attempted": embedding_attempted,
            "embedding_coverage": round(float(embedded_chunks) / float(max(len(chunk_rows), 1)), 6) if chunk_rows else 0.0,
            "profile_versions": sorted(profile_version_counts.keys()),
            "profile_version_counts": profile_version_counts,
            "vector_dims": sorted(vector_dims),
        }

    async def merge_market_document_metadata(self, doc_uid: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
        token = self._clean_context_text(doc_uid, max_len=240)
        patch = dict(metadata or {}) if isinstance(metadata, dict) else {}
        if not token or not patch:
            return None
        async with self.acquire() as conn:
            existing_row = await conn.fetchrow(
                """
                SELECT doc_uid, metadata
                FROM market_documents
                WHERE doc_uid = $1
                """,
                token,
            )
            if not existing_row:
                return None
            existing = self._decode_json_field(existing_row.get("metadata"), {})
            if not isinstance(existing, dict):
                existing = {}
            merged = {**existing, **patch}
            row = await conn.fetchrow(
                """
                UPDATE market_documents
                SET metadata = $2,
                    updated_at = CURRENT_TIMESTAMP
                WHERE doc_uid = $1
                RETURNING doc_uid, metadata
                """,
                token,
                bounded_json_text("market_documents.metadata", merged, max_bytes=24 * 1024),
            )
        if not row:
            return None
        payload = dict(row)
        payload["metadata"] = self._decode_json_field(payload.get("metadata"), {})
        return payload
