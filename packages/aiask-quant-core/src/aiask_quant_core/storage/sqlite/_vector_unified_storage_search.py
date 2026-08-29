"""Unified vector storage mixin for market / quant / strategy derived objects."""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime
from typing import Any, Iterable, List, Optional

logger = logging.getLogger(__name__)


class _VectorUnifiedStorageSearchMixin:
        async def search_vector_collection(
            self,
            *,
            collection_name: str,
            query_embedding: List[float],
            index_version: Optional[str] = None,
            version: Optional[str] = None,
            profile_type: Optional[str] = None,
            stock_code: Optional[str] = None,
            stock_codes: Optional[List[str]] = None,
            entity_id: Optional[str] = None,
            entity_ids: Optional[List[str]] = None,
            exclude_stock_code: Optional[str] = None,
            exclude_entity_id: Optional[str] = None,
            limit: int = 20,
            metric: str = "cosine",
        ) -> dict:
            requested_collection = str(collection_name or "").strip()
            resolved_limit = max(1, min(int(limit or 20), 500))
            try:
                raw_query_embedding = [] if query_embedding is None else list(query_embedding)
                resolved_query_embedding = [float(item) for item in raw_query_embedding]
            except (TypeError, ValueError):
                return {
                    "items": [],
                    "collection_name": requested_collection,
                    "backend_used": "invalid_query",
                    "fallback_used": False,
                    "fallback_reason": "invalid_query_embedding",
                    "active_version": None,
                    "index_version": index_version,
                    "profile_version": version,
                }
            if any(not math.isfinite(item) for item in resolved_query_embedding):
                return {
                    "items": [],
                    "collection_name": requested_collection,
                    "backend_used": "invalid_query",
                    "fallback_used": False,
                    "fallback_reason": "invalid_query_embedding",
                    "active_version": None,
                    "index_version": index_version,
                    "profile_version": version,
                    "query_vector_dim": len(resolved_query_embedding),
                }
            if not requested_collection or not resolved_query_embedding:
                return {
                    "items": [],
                    "collection_name": requested_collection,
                    "backend_used": "unavailable",
                    "fallback_used": False,
                    "fallback_reason": "empty_query",
                    "active_version": None,
                    "index_version": index_version,
                    "profile_version": version,
                }

            resolved_collection = requested_collection
            collection = None
            collection_candidates = self._vector_collection_candidates(requested_collection, profile_type)
            collection_resolution = "requested"
            for candidate in collection_candidates:
                collection = await self.get_vector_collection(candidate)
                if collection:
                    resolved_collection = candidate
                    if candidate != requested_collection:
                        collection_resolution = "profile_scoped"
                    break
            if collection is None and collection_candidates:
                resolved_collection = collection_candidates[0]
                if resolved_collection != requested_collection:
                    collection_resolution = "profile_scoped_unseeded"

            active_version = str((collection or {}).get("active_version") or "").strip() or None
            resolved_index_version = str(index_version or active_version or "").strip() or None
            snapshot = None
            resolved_profile_version = str(version or "").strip() or None
            if resolved_index_version:
                snapshots = await self.list_vector_index_snapshots(
                    collection_name=resolved_collection,
                    index_version=resolved_index_version,
                    profile_type=profile_type,
                    latest_only=True,
                    limit=1,
                )
                snapshot = snapshots[0] if snapshots else None
                if snapshot and not resolved_profile_version:
                    resolved_profile_version = (
                        str((snapshot.get("metadata") or {}).get("profile_version") or "").strip()
                        or str(snapshot.get("index_version") or "").strip()
                        or None
                    )
            if not resolved_profile_version and resolved_index_version:
                resolved_profile_version = resolved_index_version

            query_vector_dim = len(resolved_query_embedding)
            expected_vector_dim = 0
            expected_vector_dim_source = None
            if snapshot:
                snapshot_dim = int(snapshot.get("vector_dim") or 0)
                if snapshot_dim > 0:
                    expected_vector_dim = snapshot_dim
                    expected_vector_dim_source = "active_snapshot"
            if expected_vector_dim <= 0:
                version_dim_match = re.search(r"(?:^|__)d(\d+)(?:$|__)", str(resolved_profile_version or resolved_index_version or ""))
                if version_dim_match:
                    expected_vector_dim = int(version_dim_match.group(1))
                    expected_vector_dim_source = "version_suffix"
            if expected_vector_dim <= 0:
                try:
                    contract = await self.get_vector_dimension_contract(
                        collection_name=resolved_collection,
                        profile_type=profile_type,
                        model_id=str((collection or {}).get("model_id") or "") or None,
                        version=resolved_profile_version or resolved_index_version or "",
                    )
                except Exception:
                    contract = None
                contract_dim = int((contract or {}).get("vector_dim") or 0) if isinstance(contract, dict) else 0
                if contract_dim > 0:
                    expected_vector_dim = contract_dim
                    expected_vector_dim_source = "dimension_contract"
            if expected_vector_dim <= 0:
                collection_dim = int((collection or {}).get("vector_dim") or 0) if isinstance(collection, dict) else 0
                if collection_dim > 0:
                    expected_vector_dim = collection_dim
                    expected_vector_dim_source = "collection"

            if expected_vector_dim > 0 and query_vector_dim != expected_vector_dim:
                return {
                    "items": [],
                    "collection_name": resolved_collection,
                    "backend_used": "dimension_mismatch",
                    "fallback_used": False,
                    "fallback_reason": "query_dimension_mismatch",
                    "active_version": active_version,
                    "index_version": resolved_index_version or resolved_profile_version,
                    "profile_version": resolved_profile_version,
                    "snapshot": snapshot,
                    "query_vector_dim": query_vector_dim,
                    "expected_vector_dim": expected_vector_dim,
                    "expected_vector_dim_source": expected_vector_dim_source,
                    "collection_resolution": collection_resolution,
                }

            resolved_entity_ids = [str(item).strip() for item in ([entity_id] if entity_id else list(entity_ids or [])) if str(item).strip()]
            allowed_stock_codes = {str(item).strip() for item in list(stock_codes or []) if str(item).strip()}
            allowed_entity_ids = set(resolved_entity_ids)
            if entity_id:
                allowed_entity_ids.add(str(entity_id).strip())
            query_bucket_id, candidate_bucket_ids = self._resolve_query_buckets(snapshot, resolved_query_embedding)

            ann_fallback_reason = None
            if resolved_index_version:
                try:
                    index_rows = await self.search_vector_index_items_by_embedding(
                        query_embedding=resolved_query_embedding,
                        collection_name=resolved_collection,
                        index_version=resolved_index_version,
                        profile_type=profile_type,
                        stock_code=stock_code,
                        stock_codes=stock_codes,
                        entity_ids=resolved_entity_ids or None,
                        bucket_ids=candidate_bucket_ids or None,
                        exclude_stock_code=exclude_stock_code,
                        exclude_entity_id=exclude_entity_id,
                        limit=resolved_limit,
                        metric=metric,
                        index_params=dict((snapshot or {}).get("index_params") or {}),
                    )
                    if index_rows:
                        return {
                            "items": index_rows[:resolved_limit],
                            "collection_name": resolved_collection,
                            "backend_used": "sqlite_python_index_item",
                            "fallback_used": False,
                            "fallback_reason": None,
                            "active_version": active_version,
                            "index_version": resolved_index_version or resolved_profile_version,
                            "profile_version": resolved_profile_version,
                            "snapshot": snapshot,
                            "query_bucket_id": query_bucket_id,
                            "candidate_bucket_ids": candidate_bucket_ids,
                            "collection_resolution": collection_resolution,
                        }
                    ann_fallback_reason = "index_item_empty_result"
                except Exception as exc:
                    ann_fallback_reason = f"index_item_exception:{type(exc).__name__}"

            profile_fallback_reason = None
            try:
                dense_rows = await self.search_vector_profiles_by_embedding(
                    query_embedding=resolved_query_embedding,
                    collection_name=resolved_collection,
                    version=resolved_profile_version,
                    profile_type=profile_type,
                    stock_code=stock_code,
                    stock_codes=stock_codes,
                    entity_ids=resolved_entity_ids or None,
                    exclude_stock_code=exclude_stock_code,
                    exclude_entity_id=exclude_entity_id,
                    limit=resolved_limit,
                    metric=metric,
                    index_params=dict((snapshot or {}).get("index_params") or {}),
                )
                if dense_rows:
                    return {
                        "items": dense_rows[:resolved_limit],
                        "collection_name": resolved_collection,
                        "backend_used": "sqlite_python_profile",
                        "fallback_used": bool(ann_fallback_reason),
                        "fallback_reason": ann_fallback_reason,
                        "active_version": active_version,
                        "index_version": resolved_index_version or resolved_profile_version,
                        "profile_version": resolved_profile_version,
                        "snapshot": snapshot,
                        "query_bucket_id": query_bucket_id,
                        "candidate_bucket_ids": candidate_bucket_ids,
                        "collection_resolution": collection_resolution,
                    }
                profile_fallback_reason = "sqlite_python_empty_result"
            except Exception as exc:
                profile_fallback_reason = f"sqlite_python_exception:{type(exc).__name__}"

            exact_rows: list[dict] = []
            if resolved_index_version:
                try:
                    index_items = await self.list_vector_index_items(
                        collection_name=resolved_collection,
                        index_version=resolved_index_version,
                        bucket_ids=candidate_bucket_ids or None,
                        profile_type=profile_type,
                        stock_code=stock_code,
                        stock_codes=stock_codes,
                        entity_ids=resolved_entity_ids or None,
                        exclude_stock_code=exclude_stock_code,
                        exclude_entity_id=exclude_entity_id,
                        limit=max(100, min(resolved_limit * 20, 5000)),
                    )
                    for row in index_items:
                        if int(row.get("vector_dim") or len(row.get("embedding") or [])) != query_vector_dim:
                            continue
                        candidate_stock_code = str(row.get("stock_code") or "").strip()
                        candidate_entity_id = str(row.get("entity_id") or "").strip()
                        if allowed_stock_codes and candidate_stock_code not in allowed_stock_codes:
                            continue
                        if allowed_entity_ids and candidate_entity_id not in allowed_entity_ids:
                            continue
                        if exclude_stock_code and candidate_stock_code == str(exclude_stock_code).strip():
                            continue
                        if exclude_entity_id and candidate_entity_id == str(exclude_entity_id).strip():
                            continue
                        exact_rows.append(
                            {
                                **row,
                                "similarity": self._vector_similarity(
                                    resolved_query_embedding,
                                    list(row.get("embedding") or []),
                                    metric=metric,
                                ),
                            }
                        )
                except Exception:
                    exact_rows = []

            if not exact_rows:
                rows = await self.list_vector_profiles(
                    collection_name=resolved_collection,
                    entity_type=None,
                    entity_id=None,
                    entity_ids=resolved_entity_ids or None,
                    stock_code=stock_code,
                    stock_codes=stock_codes,
                    profile_type=profile_type,
                    version=resolved_profile_version,
                    exclude_stock_code=exclude_stock_code,
                    exclude_entity_id=exclude_entity_id,
                    limit=max(100, min(resolved_limit * 20, 5000)),
                )
                for row in rows:
                    if int(row.get("vector_dim") or len(row.get("embedding") or [])) != query_vector_dim:
                        continue
                    candidate_stock_code = str(row.get("stock_code") or "").strip()
                    candidate_entity_id = str(row.get("entity_id") or "").strip()
                    if allowed_stock_codes and candidate_stock_code not in allowed_stock_codes:
                        continue
                    if allowed_entity_ids and candidate_entity_id not in allowed_entity_ids:
                        continue
                    if exclude_stock_code and candidate_stock_code == str(exclude_stock_code).strip():
                        continue
                    if exclude_entity_id and candidate_entity_id == str(exclude_entity_id).strip():
                        continue
                    exact_rows.append(
                        {
                            **row,
                            "similarity": self._vector_similarity(
                                resolved_query_embedding,
                                list(row.get("embedding") or []),
                                metric=metric,
                            ),
                        }
                    )

            exact_rows.sort(key=lambda item: float(item.get("similarity") or 0.0), reverse=True)
            return {
                "items": exact_rows[:resolved_limit],
                "collection_name": resolved_collection,
                "backend_used": "exact_json",
                "fallback_used": True,
                "fallback_reason": ann_fallback_reason or profile_fallback_reason or "sqlite_python_unavailable",
                "active_version": active_version,
                "index_version": resolved_index_version or resolved_profile_version,
                "profile_version": resolved_profile_version,
                "snapshot": snapshot,
                "query_bucket_id": query_bucket_id,
                "candidate_bucket_ids": candidate_bucket_ids,
                "collection_resolution": collection_resolution,
            }

        async def save_vector_index_snapshot(self, snapshot: dict) -> dict:
            payload = dict(snapshot or {})
            resolved_collection_name = self._resolve_vector_collection_name(
                payload.get("collection_name"),
                payload.get("profile_type"),
            )
            activation_requested = bool(payload.get("activated_at")) or str(payload.get("status") or "").strip().lower() == "active"
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO vector_index_snapshots (
                        collection_name, index_version, status, model_id, profile_type, metric, vector_dim,
                        sample_count, bucket_count, index_params, metrics, metadata, built_at, activated_at, created_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, CURRENT_TIMESTAMP)
                    ON CONFLICT (collection_name, index_version) DO UPDATE SET
                        status = EXCLUDED.status,
                        model_id = EXCLUDED.model_id,
                        profile_type = EXCLUDED.profile_type,
                        metric = EXCLUDED.metric,
                        vector_dim = EXCLUDED.vector_dim,
                        sample_count = EXCLUDED.sample_count,
                        bucket_count = EXCLUDED.bucket_count,
                        index_params = EXCLUDED.index_params,
                        metrics = EXCLUDED.metrics,
                        metadata = EXCLUDED.metadata,
                        built_at = EXCLUDED.built_at,
                        activated_at = EXCLUDED.activated_at
                    RETURNING *
                    """,
                    resolved_collection_name,
                    str(payload.get("index_version") or "v1"),
                    str(payload.get("status") or "building"),
                    str(payload.get("model_id") or "unknown"),
                    payload.get("profile_type"),
                    str(payload.get("metric") or "cosine"),
                    int(payload.get("vector_dim") or 0),
                    int(payload.get("sample_count") or 0),
                    int(payload.get("bucket_count") or 0),
                    json.dumps(payload.get("index_params") or {}, ensure_ascii=False, default=str),
                    json.dumps(payload.get("metrics") or {}, ensure_ascii=False, default=str),
                    json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
                    self._coerce_timestamp(payload.get("built_at")),
                    self._coerce_timestamp(payload.get("activated_at")),
                )
                if activation_requested:
                    # NOTE: Avoid PG-style `metadata = COALESCE(metadata,'{}') || $N`
                    # which on SQLite is plain string concatenation, breaking JSON
                    # parse downstream. Read existing metadata, merge in Python,
                    # then write each row back atomically inside the transaction.
                    profile_type = self._normalize_profile_type(payload.get("profile_type"))
                    stale_patch = {
                        "replaced_by": str(payload.get("index_version") or "v1"),
                        "stale_reason": "superseded_activation",
                    }
                    select_sql = """
                        SELECT id, metadata FROM vector_index_snapshots
                        WHERE collection_name = $1
                          AND index_version != $2
                          AND status = 'active'
                    """
                    select_args: list[Any] = [
                        resolved_collection_name,
                        str(payload.get("index_version") or "v1"),
                    ]
                    if profile_type:
                        select_sql += " AND COALESCE(profile_type, '') = $3"
                        select_args.append(profile_type)
                    else:
                        select_sql += " AND $3 IS NULL"
                        select_args.append(None)
                    rows_to_stale = await conn.fetch(select_sql, *select_args)
                    for stale_row in rows_to_stale:
                        try:
                            existing_meta_raw = stale_row["metadata"]
                        except (KeyError, TypeError):
                            existing_meta_raw = None
                        merged_meta: dict = {}
                        if existing_meta_raw:
                            try:
                                if isinstance(existing_meta_raw, dict):
                                    merged_meta = dict(existing_meta_raw)
                                elif isinstance(existing_meta_raw, str):
                                    parsed = json.loads(existing_meta_raw)
                                    if isinstance(parsed, dict):
                                        merged_meta = parsed
                            except (TypeError, json.JSONDecodeError):
                                merged_meta = {}
                        merged_meta.update(stale_patch)
                        await conn.execute(
                            "UPDATE vector_index_snapshots SET status='stale', metadata=$1 WHERE id=$2",
                            json.dumps(merged_meta, ensure_ascii=False, default=str),
                            stale_row["id"],
                        )
                    await conn.execute(
                        """
                        UPDATE vector_collections
                        SET active_version = $2,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE collection_name = $1
                        """,
                        resolved_collection_name,
                        str(payload.get("index_version") or "v1"),
                    )
            return self._decode_unified_vector_snapshot(dict(row))

        async def save_kline_pattern_window(self, payload: dict) -> dict:
            item = dict(payload or {})
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO kline_pattern_windows (
                        window_uid, stock_code, end_date, start_date, period, adjust,
                        window_size, vector_method, metric, vector_dim,
                        forward_return_5d, forward_return_10d, forward_return_20d,
                        payload, metadata, created_at, updated_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6,
                        $7, $8, $9, $10,
                        $11, $12, $13,
                        $14, $15, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (window_uid) DO UPDATE SET
                        stock_code = EXCLUDED.stock_code,
                        end_date = EXCLUDED.end_date,
                        start_date = EXCLUDED.start_date,
                        period = EXCLUDED.period,
                        adjust = EXCLUDED.adjust,
                        window_size = EXCLUDED.window_size,
                        vector_method = EXCLUDED.vector_method,
                        metric = EXCLUDED.metric,
                        vector_dim = EXCLUDED.vector_dim,
                        forward_return_5d = EXCLUDED.forward_return_5d,
                        forward_return_10d = EXCLUDED.forward_return_10d,
                        forward_return_20d = EXCLUDED.forward_return_20d,
                        payload = EXCLUDED.payload,
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING *
                    """,
                    str(item.get("window_uid") or ""),
                    str(item.get("stock_code") or ""),
                    self._coerce_date_value(item.get("end_date")),
                    self._coerce_date_value(item.get("start_date")),
                    str(item.get("period") or "daily"),
                    str(item.get("adjust") or ""),
                    int(item.get("window_size") or 0),
                    str(item.get("vector_method") or "returns"),
                    str(item.get("metric") or "cosine"),
                    int(item.get("vector_dim") or 0),
                    item.get("forward_return_5d"),
                    item.get("forward_return_10d"),
                    item.get("forward_return_20d"),
                    json.dumps(item.get("payload") or {}, ensure_ascii=False, default=str),
                    json.dumps(item.get("metadata") or {}, ensure_ascii=False, default=str),
                )
            return self._decode_kline_pattern_window(dict(row))

        async def list_kline_pattern_windows(
            self,
            *,
            stock_code: Optional[str] = None,
            period: Optional[str] = None,
            adjust: Optional[str] = None,
            vector_method: Optional[str] = None,
            window_size: Optional[int] = None,
            limit: int = 100,
        ) -> List[dict]:
            async with self.acquire() as conn:
                sql = "SELECT * FROM kline_pattern_windows WHERE 1=1"
                params: list[Any] = []
                idx = 1
                for field, value in (
                    ("stock_code", stock_code),
                    ("period", period),
                    ("adjust", adjust),
                    ("vector_method", vector_method),
                ):
                    if value not in (None, ""):
                        sql += f" AND {field} = ${idx}"
                        params.append(value)
                        idx += 1
                if window_size is not None:
                    sql += f" AND window_size = ${idx}"
                    params.append(int(window_size))
                    idx += 1
                sql += f" ORDER BY end_date DESC, updated_at DESC LIMIT ${idx}"
                params.append(max(1, min(int(limit or 100), 5000)))
                rows = await conn.fetch(sql, *params)
            return [self._decode_kline_pattern_window(dict(row)) for row in rows]

        async def list_vector_index_snapshots(
            self,
            *,
            collection_name: Optional[str] = None,
            index_version: Optional[str] = None,
            profile_type: Optional[str] = None,
            status: Optional[str] = None,
            latest_only: bool = False,
            limit: int = 100,
        ) -> List[dict]:
            async with self.acquire() as conn:
                where_parts = ["1=1"]
                params: list[Any] = []
                idx = 1
                if collection_name:
                    where_parts.append(f"collection_name = ${idx}")
                    params.append(collection_name)
                    idx += 1
                if index_version:
                    where_parts.append(f"index_version = ${idx}")
                    params.append(index_version)
                    idx += 1
                if profile_type:
                    where_parts.append(f"COALESCE(profile_type, '') = ${idx}")
                    params.append(str(profile_type or "").strip())
                    idx += 1
                if status:
                    where_parts.append(f"status = ${idx}")
                    params.append(status)
                    idx += 1
                where_sql = " AND ".join(where_parts)
                order_sql = "COALESCE(activated_at, built_at, created_at) DESC, id DESC"
                if latest_only:
                    sql = f"""
                        WITH ranked AS (
                            SELECT *,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY collection_name, index_version, COALESCE(profile_type, '')
                                       ORDER BY {order_sql}
                                   ) AS rn
                            FROM vector_index_snapshots
                            WHERE {where_sql}
                        )
                        SELECT *
                        FROM ranked
                        WHERE rn = 1
                        ORDER BY {order_sql}
                        LIMIT ${idx}
                    """
                else:
                    sql = f"""
                        SELECT *
                        FROM vector_index_snapshots
                        WHERE {where_sql}
                        ORDER BY {order_sql}
                        LIMIT ${idx}
                    """
                params.append(max(1, min(int(limit or 100), 5000)))
                rows = await conn.fetch(sql, *params)
            return [self._decode_unified_vector_snapshot(dict(row)) for row in rows]
