"""Unified vector storage mixin for market / quant / strategy derived objects."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from typing import Any, Iterable, List, Optional


class _VectorUnifiedDocsMixin:
        async def search_market_doc_chunks(
            self,
            *,
            query_text: Optional[str] = None,
            query_embedding: Optional[List[float]] = None,
            stock_code: Optional[str] = None,
            doc_types: Optional[List[str]] = None,
            start_date: Any = None,
            end_date: Any = None,
            limit: int = 10,
        ) -> List[dict]:
            normalized_query_text = self._normalize_hybrid_query_text(query_text)
            if normalized_query_text and not query_embedding:
                try:
                    from aiask_quant_core.storage.runtime_hooks import get_text_embedding_service_factory

                    service_factory = get_text_embedding_service_factory()
                    service = service_factory() if callable(service_factory) else None
                    if service is not None and service.is_enabled():
                        query_embedding = await service.embed_text(normalized_query_text)
                except Exception:
                    query_embedding = None

            dense_rows: dict[str, dict] = {}
            if query_embedding:
                dense_limit = max(10, min(int(limit or 10) * 3, 100))
                for scoped_collection_name, scoped_profile_type in self._market_doc_search_scopes(doc_types):
                    dense_result = await self.search_vector_collection(
                        collection_name=scoped_collection_name,
                        query_embedding=query_embedding,
                        profile_type=scoped_profile_type,
                        stock_code=stock_code,
                        limit=dense_limit,
                        metric="cosine",
                    )
                    for row in list((dense_result or {}).get("items") or []):
                        entity_id = str(row.get("entity_id") or "")
                        if not entity_id:
                            continue
                        current = dense_rows.get(entity_id)
                        current_similarity = float((current or {}).get("similarity") or -1.0)
                        candidate_similarity = float(row.get("similarity") or -1.0)
                        if current is None or candidate_similarity > current_similarity:
                            dense_rows[entity_id] = row

            fts_rows: dict[str, dict] = {}
            fts_query = self._fts5_query(normalized_query_text) if normalized_query_text else ""
            if fts_query:
                try:
                    async with self.acquire() as conn:
                        params: list[Any] = [fts_query]
                        where_clauses = ["market_doc_chunks_fts MATCH $1"]
                        idx = 2
                        if stock_code:
                            where_clauses.append(f"c.stock_code = ${idx}")
                            params.append(stock_code)
                            idx += 1
                        if doc_types:
                            where_clauses.append(f"LOWER(COALESCE(c.doc_type, '')) IN (${idx})")
                            params.append([str(item or "").strip().lower() for item in doc_types if str(item or "").strip()])
                            idx += 1
                        if start_date is not None:
                            where_clauses.append(f"c.published_at >= ${idx}")
                            params.append(self._coerce_timestamp(start_date))
                            idx += 1
                        if end_date is not None:
                            where_clauses.append(f"c.published_at <= ${idx}")
                            params.append(self._coerce_timestamp(end_date))
                            idx += 1
                        params.append(max(20, min(int(limit or 10) * 10, 300)))
                        rows = await conn.fetch(
                            f"""
                            SELECT
                                d.doc_uid,
                                d.url,
                                d.author,
                                c.id,
                                c.doc_id,
                                c.chunk_no,
                                c.stock_code,
                                c.doc_type,
                                c.source,
                                c.title,
                                c.chunk_text,
                                c.published_at,
                                c.metadata,
                                d.doc_uid || ':' || c.chunk_no AS entity_id,
                                bm25(market_doc_chunks_fts) AS fts_rank
                            FROM market_doc_chunks_fts
                            JOIN market_doc_chunks c ON c.id = market_doc_chunks_fts.rowid
                            JOIN market_documents d ON d.id = c.doc_id
                            WHERE {' AND '.join(where_clauses)}
                            ORDER BY fts_rank ASC, c.published_at DESC, c.id DESC
                            LIMIT ${len(params)}
                            """,
                            *params,
                        )
                    total = max(len(rows), 1)
                    for idx, row in enumerate(rows):
                        payload = dict(row)
                        entity_id = str(payload.get("entity_id") or "")
                        if not entity_id:
                            continue
                        payload["fts_score"] = round(1.0 - (idx / total), 6)
                        fts_rows[entity_id] = payload
                except Exception:
                    fts_rows = {}

            async with self.acquire() as conn:
                params: list[Any] = []
                where_clauses = ["1=1"]
                idx = 1
                if stock_code:
                    where_clauses.append(f"c.stock_code = ${idx}")
                    params.append(stock_code)
                    idx += 1
                if doc_types:
                    where_clauses.append(f"LOWER(COALESCE(c.doc_type, '')) IN (${idx})")
                    params.append([str(item or "").strip().lower() for item in doc_types if str(item or "").strip()])
                    idx += 1
                if start_date is not None:
                    where_clauses.append(f"c.published_at >= ${idx}")
                    params.append(self._coerce_timestamp(start_date))
                    idx += 1
                if end_date is not None:
                    where_clauses.append(f"c.published_at <= ${idx}")
                    params.append(self._coerce_timestamp(end_date))
                    idx += 1
                params.append(max(50, min(int(limit or 10) * 20, 500)))
                rows = await conn.fetch(
                    f"""
                    SELECT
                        d.doc_uid,
                        d.url,
                        d.author,
                        c.id,
                        c.doc_id,
                        c.chunk_no,
                        c.stock_code,
                        c.doc_type,
                        c.source,
                        c.title,
                        c.chunk_text,
                        c.published_at,
                        c.metadata,
                        d.doc_uid || ':' || c.chunk_no AS entity_id
                    FROM market_doc_chunks c
                    JOIN market_documents d ON d.id = c.doc_id
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY c.published_at DESC NULLS LAST, c.id DESC
                    LIMIT ${len(params)}
                    """,
                    *params,
                )
            merged_rows: dict[str, dict] = {
                str(row.get("entity_id") or ""): dict(row)
                for row in fts_rows.values()
                if str(row.get("entity_id") or "")
            }
            for row in rows:
                payload = dict(row)
                entity_id = str(payload.get("entity_id") or "")
                if not entity_id:
                    continue
                if entity_id in merged_rows:
                    merged_rows[entity_id].update({key: value for key, value in payload.items() if value is not None})
                else:
                    merged_rows[entity_id] = payload
            results = []
            for row in merged_rows.values():
                payload = dict(row)
                entity_id = str(payload.get("entity_id") or "")
                dense = dense_rows.get(entity_id, {})
                dense_score = self._hybrid_dense_score(dense.get("similarity"))
                fts_score = self._hybrid_dense_score((fts_rows.get(entity_id) or {}).get("fts_score"))
                lexical_score = (
                    self._hybrid_lexical_score(
                        normalized_query_text,
                        str(payload.get("title") or ""),
                        str(payload.get("chunk_text") or ""),
                    )
                    if normalized_query_text
                    else None
                )
                base_hybrid = self._hybrid_score(dense_score=dense_score, lexical_score=lexical_score)
                if base_hybrid is not None and fts_score is not None:
                    hybrid_score = round(base_hybrid * 0.8 + fts_score * 0.2, 6)
                elif fts_score is not None:
                    hybrid_score = fts_score
                else:
                    hybrid_score = base_hybrid
                if normalized_query_text and not dense_score and not fts_score and not (lexical_score and lexical_score > 0):
                    continue
                results.append(
                    {
                        "entity_id": entity_id,
                        "doc_uid": payload.get("doc_uid"),
                        "chunk_id": payload.get("id"),
                        "doc_id": payload.get("doc_id"),
                        "chunk_no": payload.get("chunk_no"),
                        "stock_code": payload.get("stock_code"),
                        "doc_type": payload.get("doc_type"),
                        "source": payload.get("source"),
                        "title": payload.get("title"),
                        "content": payload.get("chunk_text"),
                        "summary": str(payload.get("chunk_text") or "")[:240],
                        "url": payload.get("url"),
                        "author": payload.get("author"),
                        "published_at": payload.get("published_at"),
                        "metadata": self._decode_json_field(payload.get("metadata"), {}),
                        "dense_score": dense_score,
                        "fts_score": fts_score,
                        "lexical_score": lexical_score,
                        "hybrid_score": hybrid_score,
                        "similarity": hybrid_score if hybrid_score is not None else dense_score,
                    }
                )
            if normalized_query_text or dense_rows:
                results.sort(
                    key=lambda item: (
                        float(item.get("hybrid_score") or item.get("dense_score") or -1.0),
                        float(item.get("dense_score") or -1.0),
                        float(item.get("lexical_score") or -1.0),
                        item.get("published_at").isoformat() if hasattr(item.get("published_at"), "isoformat") else str(item.get("published_at") or ""),
                    ),
                    reverse=True,
                )
            return results[: max(1, min(int(limit or 10), 100))]

        async def search_kline_pattern_windows(
            self,
            *,
            query_embedding: List[float],
            window_size: int,
            vector_method: str = "returns",
            metric: str = "cosine",
            period: str = "daily",
            adjust: str = "",
            version: Optional[str] = None,
            stock_codes: Optional[List[str]] = None,
            exclude_stock_code: Optional[str] = None,
            limit: int = 10,
        ) -> List[dict]:
            profile_type = self._kline_pattern_profile_type(
                window_size=window_size,
                vector_method=vector_method,
                period=period,
                adjust=adjust,
            )
            search_result = {"items": []}
            for collection_name in (
                self._kline_collection_name(vector_method, window_size),
                "kline_pattern_embeddings",
            ):
                if not collection_name:
                    continue
                search_result = await self.search_vector_collection(
                    collection_name=collection_name,
                    query_embedding=query_embedding,
                    version=version,
                    profile_type=profile_type,
                    stock_codes=stock_codes,
                    exclude_stock_code=exclude_stock_code,
                    limit=max(10, min(int(limit or 10) * 8, 200)),
                    metric=metric,
                )
                if list((search_result or {}).get("items") or []):
                    break
            rows = list((search_result or {}).get("items") or [])
            if not rows:
                return []

            entity_ids = [str(row.get("entity_id") or "").strip() for row in rows if str(row.get("entity_id") or "").strip()]
            if not entity_ids:
                return []
            allowed_codes = {str(item).strip() for item in list(stock_codes or []) if str(item).strip()}
            excluded = str(exclude_stock_code or "").strip()
            async with self.acquire() as conn:
                window_rows = await conn.fetch(
                    """
                    SELECT *
                    FROM kline_pattern_windows
                    WHERE window_uid IN ($1)
                    """,
                    entity_ids,
                )
            window_map = {
                str(self._decode_kline_pattern_window(dict(row)).get("window_uid") or ""): self._decode_kline_pattern_window(dict(row))
                for row in window_rows
            }
            results: list[dict] = []
            for row in rows:
                window_uid = str(row.get("entity_id") or "").strip()
                window = dict(window_map.get(window_uid) or {})
                if not window:
                    continue
                stock_code = str(window.get("stock_code") or "").strip()
                if allowed_codes and stock_code not in allowed_codes:
                    continue
                if excluded and stock_code == excluded:
                    continue
                metadata = dict(row.get("metadata") or {})
                payload = dict(window.get("payload") or {})
                results.append(
                    {
                        "window_uid": window_uid,
                        "stock_code": stock_code,
                        "stock_name": metadata.get("stock_name") or payload.get("stock_name"),
                        "start_date": window.get("start_date"),
                        "end_date": window.get("end_date"),
                        "period": window.get("period"),
                        "adjust": window.get("adjust"),
                        "window_size": window.get("window_size"),
                        "vector_method": window.get("vector_method"),
                        "metric": window.get("metric"),
                        "vector_dim": window.get("vector_dim"),
                        "similarity": row.get("similarity"),
                        "forward_return_5d": window.get("forward_return_5d"),
                        "forward_return_10d": window.get("forward_return_10d"),
                        "forward_return_20d": window.get("forward_return_20d"),
                        "payload": payload,
                        "metadata": window.get("metadata") or {},
                    }
                )
            results.sort(key=lambda item: float(item.get("similarity") or -1.0), reverse=True)
            return results[: max(1, min(int(limit or 10), 100))]
