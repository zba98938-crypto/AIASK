"""Unified vector storage mixin for market / quant / strategy derived objects."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from typing import Any, Iterable, List, Optional


class _VectorUnifiedIndexesMixin:
        async def replace_vector_index_items(self, collection_name: str, index_version: str, items: Iterable[dict]) -> dict:
            resolved_collection = str(collection_name or "")
            resolved_version = str(index_version or "v1")
            payloads = []
            for item in list(items or []):
                payload = dict(item or {})
                try:
                    embedding = [float(value) for value in list(payload.get("embedding") or [])]
                except (TypeError, ValueError) as exc:
                    raise ValueError("embedding must be a numeric vector") from exc
                if any(not math.isfinite(value) for value in embedding):
                    raise ValueError("embedding must contain only finite values")
                payload["embedding"] = embedding
                payloads.append(payload)
            async with self.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM vector_index_items WHERE collection_name = $1 AND index_version = $2",
                        resolved_collection,
                        resolved_version,
                    )
                    rows = []
                    for payload in payloads:
                        rows.append(
                            (
                                resolved_collection,
                                resolved_version,
                                payload.get("profile_id"),
                                str(payload.get("entity_type") or "generic"),
                                str(payload.get("entity_id") or ""),
                                payload.get("stock_code"),
                                payload.get("profile_type"),
                                str(payload.get("model_id") or "unknown"),
                                str(payload.get("metric") or "cosine"),
                                int(payload.get("vector_dim") or len(payload.get("embedding") or [])),
                                payload.get("bucket_id"),
                                float(payload.get("coarse_score") or 0.0),
                                json.dumps(payload.get("embedding") or [], ensure_ascii=False, default=str),
                                json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
                            )
                        )
                    if rows:
                        await conn.executemany(
                            """
                            INSERT INTO vector_index_items (
                                collection_name, index_version, profile_id, entity_type, entity_id, stock_code,
                                profile_type, model_id, metric, vector_dim, bucket_id, coarse_score,
                                embedding_json, metadata, created_at
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, CURRENT_TIMESTAMP)
                            ON CONFLICT (collection_name, index_version, profile_id) DO UPDATE SET
                                entity_type = EXCLUDED.entity_type,
                                entity_id = EXCLUDED.entity_id,
                                stock_code = EXCLUDED.stock_code,
                                profile_type = EXCLUDED.profile_type,
                                model_id = EXCLUDED.model_id,
                                metric = EXCLUDED.metric,
                                vector_dim = EXCLUDED.vector_dim,
                                bucket_id = EXCLUDED.bucket_id,
                                coarse_score = EXCLUDED.coarse_score,
                                embedding_json = EXCLUDED.embedding_json,
                                metadata = EXCLUDED.metadata
                            """,
                            rows,
                        )
                    if getattr(self, "supports_sqlite_python", lambda: False)() and rows:
                        mapping_rows = await conn.fetch(
                            """
                            SELECT id, profile_id
                            FROM vector_index_items
                            WHERE collection_name = $1 AND index_version = $2
                            """,
                            resolved_collection,
                            resolved_version,
                        )
                        row_ids = {
                            str(dict(row).get("profile_id")): dict(row).get("id")
                            for row in mapping_rows
                            if dict(row).get("profile_id") is not None
                        }
                        store_rows = []
                        for payload in payloads:
                            vector_literal = self._encode_sqlite_python(payload.get("embedding") or [])
                            row_id = row_ids.get(str(payload.get("profile_id")))
                            if not vector_literal or row_id is None:
                                continue
                            store_rows.append(
                                (
                                    row_id,
                                    resolved_collection,
                                    resolved_version,
                                    payload.get("profile_id"),
                                    str(payload.get("entity_type") or "generic"),
                                    str(payload.get("entity_id") or ""),
                                    payload.get("stock_code"),
                                    payload.get("profile_type"),
                                    str(payload.get("model_id") or "unknown"),
                                    str(payload.get("metric") or "cosine"),
                                    int(payload.get("vector_dim") or len(payload.get("embedding") or [])),
                                    payload.get("bucket_id"),
                                    vector_literal,
                                    json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
                                )
                            )
                        if store_rows:
                            await conn.executemany(
                                """
                                INSERT INTO vector_index_item_store (
                                    item_id, collection_name, index_version, profile_id, entity_type, entity_id,
                                    stock_code, profile_type, model_id, metric, vector_dim, bucket_id, embedding, metadata, updated_at
                                )
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, CURRENT_TIMESTAMP)
                                ON CONFLICT (item_id) DO UPDATE SET
                                    collection_name = EXCLUDED.collection_name,
                                    index_version = EXCLUDED.index_version,
                                    profile_id = EXCLUDED.profile_id,
                                    entity_type = EXCLUDED.entity_type,
                                    entity_id = EXCLUDED.entity_id,
                                    stock_code = EXCLUDED.stock_code,
                                    profile_type = EXCLUDED.profile_type,
                                    model_id = EXCLUDED.model_id,
                                    metric = EXCLUDED.metric,
                                    vector_dim = EXCLUDED.vector_dim,
                                    bucket_id = EXCLUDED.bucket_id,
                                    embedding = EXCLUDED.embedding,
                                    metadata = EXCLUDED.metadata,
                                    updated_at = CURRENT_TIMESTAMP
                                """,
                                store_rows,
                            )
            return {"collection_name": resolved_collection, "index_version": resolved_version, "count": len(rows)}

        async def list_vector_index_items(
            self,
            *,
            collection_name: Optional[str] = None,
            index_version: Optional[str] = None,
            bucket_ids: Optional[List[str]] = None,
            profile_type: Optional[str] = None,
            stock_code: Optional[str] = None,
            stock_codes: Optional[List[str]] = None,
            entity_ids: Optional[List[str]] = None,
            exclude_stock_code: Optional[str] = None,
            exclude_entity_id: Optional[str] = None,
            limit: int = 200,
        ) -> List[dict]:
            async with self.acquire() as conn:
                sql = "SELECT * FROM vector_index_items WHERE 1=1"
                params: list[Any] = []
                idx = 1
                if collection_name:
                    sql += f" AND collection_name = ${idx}"
                    params.append(collection_name)
                    idx += 1
                if index_version:
                    sql += f" AND index_version = ${idx}"
                    params.append(index_version)
                    idx += 1
                if bucket_ids:
                    sql += f" AND bucket_id IN (${idx})"
                    params.append([str(item) for item in bucket_ids])
                    idx += 1
                if profile_type:
                    sql += f" AND profile_type = ${idx}"
                    params.append(str(profile_type))
                    idx += 1
                if stock_code:
                    sql += f" AND stock_code = ${idx}"
                    params.append(stock_code)
                    idx += 1
                if stock_codes:
                    sql += f" AND stock_code IN (${idx})"
                    params.append([str(item).strip() for item in list(stock_codes or []) if str(item).strip()])
                    idx += 1
                if entity_ids:
                    sql += f" AND entity_id IN (${idx})"
                    params.append([str(item).strip() for item in list(entity_ids or []) if str(item).strip()])
                    idx += 1
                if exclude_stock_code:
                    sql += f" AND COALESCE(stock_code, '') != ${idx}"
                    params.append(str(exclude_stock_code))
                    idx += 1
                if exclude_entity_id:
                    sql += f" AND entity_id != ${idx}"
                    params.append(str(exclude_entity_id))
                    idx += 1
                sql += f" ORDER BY coarse_score DESC, created_at DESC LIMIT ${idx}"
                params.append(max(1, min(int(limit or 200), 5000)))
                rows = await conn.fetch(sql, *params)
            return [self._decode_unified_vector_item(dict(row)) for row in rows]

        async def ensure_vector_profile_sqlite_python_index(
            self,
            *,
            collection_name: str,
            version: str,
            vector_dim: int,
            profile_type: Optional[str] = None,
            metric: str = "cosine",
            index_params: Optional[dict] = None,
        ) -> Optional[str]:
            return None

        async def ensure_vector_index_item_sqlite_python_index(
            self,
            *,
            collection_name: str,
            index_version: str,
            vector_dim: int,
            metric: str = "cosine",
            index_params: Optional[dict] = None,
        ) -> Optional[str]:
            return None

        async def list_vector_hnsw_indexes(
            self,
            *,
            collection_name: Optional[str] = None,
            index_version: Optional[str] = None,
            limit: int = 200,
        ) -> List[dict]:
            return []

        async def cleanup_vector_collection_history(
            self,
            *,
            collection_name: str,
            keep_versions: int = 1,
            dry_run: bool = True,
            cleanup_hnsw: bool = True,
            limit_versions: int = 200,
            protect_versions: Optional[List[str]] = None,
            profile_type: Optional[str] = None,
        ) -> dict:
            resolved_collection = str(collection_name or "").strip()
            if not resolved_collection:
                raise ValueError("collection_name is required")

            resolved_keep_versions = max(0, int(keep_versions or 0))
            resolved_limit_versions = max(1, min(int(limit_versions or 200), 1000))
            resolved_profile_type = str(profile_type or "").strip() or None
            protected_input = [str(item).strip() for item in list(protect_versions or []) if str(item).strip()]

            async with self.acquire() as conn:
                collection_row = await conn.fetchrow(
                    "SELECT * FROM vector_collections WHERE collection_name = $1",
                    resolved_collection,
                )

                profile_params: list[Any] = [resolved_collection]
                profile_filter = "collection_name = $1"
                if resolved_profile_type:
                    profile_filter += " AND profile_type = $2"
                    profile_params.append(resolved_profile_type)

                snapshot_params: list[Any] = [resolved_collection]
                snapshot_filter = "collection_name = $1"
                if resolved_profile_type:
                    snapshot_filter += " AND profile_type = $2"
                    snapshot_params.append(resolved_profile_type)

                profile_rows = await conn.fetch(
                    f"""
                    SELECT version, COUNT(*) AS profile_rows, MAX(updated_at) AS last_seen
                    FROM vector_profiles
                    WHERE {profile_filter}
                    GROUP BY version
                    """,
                    *profile_params,
                )
                store_rows = await conn.fetch(
                    f"""
                    SELECT version, COUNT(*) AS profile_store_rows, MAX(updated_at) AS last_seen
                    FROM vector_profile_store
                    WHERE {profile_filter}
                    GROUP BY version
                    """,
                    *profile_params,
                ) if getattr(self, "supports_sqlite_python", lambda: False)() else []
                snapshot_rows = await conn.fetch(
                    f"""
                    SELECT *
                    FROM vector_index_snapshots
                    WHERE {snapshot_filter}
                    ORDER BY COALESCE(activated_at, built_at, created_at) DESC, created_at DESC, id DESC
                    """,
                    *snapshot_params,
                )
                item_rows = await conn.fetch(
                    f"""
                    SELECT index_version AS version, COUNT(*) AS index_item_rows, MAX(created_at) AS last_seen
                    FROM vector_index_items
                    WHERE {snapshot_filter}
                    GROUP BY index_version
                    """,
                    *snapshot_params,
                )
                item_store_rows = await conn.fetch(
                    f"""
                    SELECT index_version AS version, COUNT(*) AS index_item_store_rows, MAX(updated_at) AS last_seen
                    FROM vector_index_item_store
                    WHERE {snapshot_filter}
                    GROUP BY index_version
                    """,
                    *snapshot_params,
                ) if getattr(self, "supports_sqlite_python", lambda: False)() else []

            active_collection_version = str((dict(collection_row or {}).get("active_version") or "")).strip()
            if resolved_profile_type:
                active_collection_version = ""
            latest_snapshot = dict(snapshot_rows[0]) if snapshot_rows else None
            latest_snapshot_version = str((latest_snapshot or {}).get("index_version") or "").strip()
            if resolved_profile_type and latest_snapshot_version:
                active_collection_version = latest_snapshot_version

            versions: dict[str, dict] = {}

            def _get_bucket(version: str) -> dict:
                return versions.setdefault(
                    version,
                    {
                        "collection_name": resolved_collection,
                        "index_version": version,
                        "profile_type": resolved_profile_type,
                        "profile_rows": 0,
                        "profile_store_rows": 0,
                        "index_item_rows": 0,
                        "index_item_store_rows": 0,
                        "snapshot_count": 0,
                        "snapshot_status": None,
                        "backend": str((dict(collection_row or {}).get("backend") or self.get_vector_backend())),
                        "bucket_count": 0,
                        "vector_dim": 0,
                        "model_id": str((dict(collection_row or {}).get("model_id") or "")),
                        "last_seen": "",
                        "active_collection_version": active_collection_version,
                    },
                )

            for row in profile_rows:
                payload = dict(row)
                version = str(payload.get("version") or "").strip()
                if not version:
                    continue
                bucket = _get_bucket(version)
                bucket["profile_rows"] = int(payload.get("profile_rows") or 0)
                bucket["last_seen"] = max(bucket.get("last_seen") or "", str(payload.get("last_seen") or ""))

            for row in store_rows:
                payload = dict(row)
                version = str(payload.get("version") or "").strip()
                if not version:
                    continue
                bucket = _get_bucket(version)
                bucket["profile_store_rows"] = int(payload.get("profile_store_rows") or 0)
                bucket["last_seen"] = max(bucket.get("last_seen") or "", str(payload.get("last_seen") or ""))

            snapshot_latest_by_version: dict[str, dict] = {}
            for row in snapshot_rows:
                payload = dict(row)
                version = str(payload.get("index_version") or "").strip()
                if not version:
                    continue
                bucket = _get_bucket(version)
                bucket["snapshot_count"] += 1
                if version not in snapshot_latest_by_version:
                    snapshot_latest_by_version[version] = payload
                    bucket["snapshot_status"] = payload.get("status")
                    bucket["bucket_count"] = int(payload.get("bucket_count") or 0)
                    bucket["vector_dim"] = int(payload.get("vector_dim") or 0)
                    bucket["model_id"] = str(payload.get("model_id") or bucket.get("model_id") or "")
                    bucket["last_seen"] = max(
                        bucket.get("last_seen") or "",
                        str(payload.get("activated_at") or payload.get("built_at") or payload.get("created_at") or ""),
                    )

            for row in item_rows:
                payload = dict(row)
                version = str(payload.get("version") or "").strip()
                if not version:
                    continue
                bucket = _get_bucket(version)
                bucket["index_item_rows"] = int(payload.get("index_item_rows") or 0)
                bucket["last_seen"] = max(bucket.get("last_seen") or "", str(payload.get("last_seen") or ""))

            for row in item_store_rows:
                payload = dict(row)
                version = str(payload.get("version") or "").strip()
                if not version:
                    continue
                bucket = _get_bucket(version)
                bucket["index_item_store_rows"] = int(payload.get("index_item_store_rows") or 0)
                bucket["last_seen"] = max(bucket.get("last_seen") or "", str(payload.get("last_seen") or ""))

            version_rows = list(versions.values())
            version_rows.sort(
                key=lambda item: (
                    0 if str(item.get("index_version") or "") == active_collection_version else 1,
                    0 if str(item.get("index_version") or "") == latest_snapshot_version else 1,
                    0 if str(item.get("snapshot_status") or "").lower() == "active" else 1,
                    str(item.get("last_seen") or ""),
                    str(item.get("index_version") or ""),
                ),
                reverse=False,
            )

            protected_versions_resolved: list[str] = []
            for version in (active_collection_version, latest_snapshot_version):
                if version and version not in protected_versions_resolved:
                    protected_versions_resolved.append(version)

            protected_limit = max(resolved_keep_versions, len(protected_versions_resolved))
            for row in version_rows:
                version = str(row.get("index_version") or "").strip()
                if not version or version in protected_versions_resolved:
                    continue
                if len(protected_versions_resolved) >= protected_limit:
                    break
                protected_versions_resolved.append(version)

            for version in protected_input:
                if version not in protected_versions_resolved:
                    protected_versions_resolved.append(version)

            protected_set = {item for item in protected_versions_resolved if item}
            target_rows = [dict(row) for row in version_rows if str(row.get("index_version") or "").strip() not in protected_set]
            target_versions = [str(row.get("index_version") or "").strip() for row in target_rows if str(row.get("index_version") or "").strip()]
            target_version_set = set(target_versions)

            hnsw_indexes = await self.list_vector_hnsw_indexes(
                collection_name=resolved_collection,
                limit=1000,
            ) if cleanup_hnsw else []
            def _fallback_sql_quote(value: Any) -> str:
                return "'" + str(value).replace("'", "''") + "'"

            sql_quote = getattr(self, "_sql_quote", _fallback_sql_quote)
            resolved_profile_sql = sql_quote(resolved_profile_type) if resolved_profile_type else None
            collection_is_scoped = self._is_profile_scoped_collection(resolved_collection)
            indexes_to_drop = [
                row for row in hnsw_indexes
                if any(sql_quote(version) in str(row.get("indexdef") or "") for version in target_version_set)
                and (
                    not resolved_profile_type
                    or collection_is_scoped
                    or (
                        "profile_type" in str(row.get("indexdef") or "")
                        and resolved_profile_sql in str(row.get("indexdef") or "")
                    )
                )
            ]

            summary = {
                "collection_name": resolved_collection,
                "profile_type": resolved_profile_type,
                "dry_run": bool(dry_run),
                "keep_versions": resolved_keep_versions,
                "health_mode": "unified",
                "cleanup_scope": "unified",
                "source_of_truth": "unified_vector_tables",
                "table_family": "unified_vector_tables",
                "legacy_only": False,
                "active_version": active_collection_version or None,
                "latest_snapshot_version": latest_snapshot_version or None,
                "protected_versions": sorted(protected_set),
                "target_versions": target_versions,
                "target_version_keys": [f"{resolved_collection}@{version}" for version in target_versions],
                "hnsw_indexes_to_drop": [row.get("indexname") for row in indexes_to_drop],
                "deleted": {
                    "vector_index_registry": 0,
                    "vector_index_snapshots": 0,
                    "vector_profiles": 0,
                    "vector_profile_store": 0,
                    "vector_index_items": 0,
                    "vector_index_item_store": 0,
                    "hnsw_indexes": 0,
                },
                "version_details": target_rows[:resolved_limit_versions],
                "total_versions": len(version_rows),
                "reason": None if version_rows else "no_vector_versions",
            }
            if dry_run or not target_version_set:
                return summary

            async with self.acquire() as conn:
                async with conn.transaction():
                    for row in indexes_to_drop:
                        index_name = str(row.get("indexname") or "").strip()
                        if not index_name:
                            continue
                        await conn.execute(f"DROP INDEX IF EXISTS {index_name}")
                        summary["deleted"]["hnsw_indexes"] += 1

                    if getattr(self, "supports_sqlite_python", lambda: False)():
                        item_store_sql = """
                            DELETE FROM vector_index_item_store
                            WHERE collection_name = $1
                              AND index_version IN ($2)
                        """
                        item_store_args: list[Any] = [resolved_collection, list(target_version_set)]
                        if resolved_profile_type:
                            item_store_sql += " AND COALESCE(profile_type, '') = $3"
                            item_store_args.append(resolved_profile_type)
                        deleted = await conn.execute(item_store_sql, *item_store_args)
                        summary["deleted"]["vector_index_item_store"] += int(str(deleted).split()[-1])

                    deleted = await conn.execute(
                        """
                        DELETE FROM vector_index_items
                        WHERE collection_name = $1
                          AND index_version IN ($2)
                        """,
                        resolved_collection,
                        list(target_version_set),
                    )
                    summary["deleted"]["vector_index_items"] += int(str(deleted).split()[-1])

                    if getattr(self, "supports_sqlite_python", lambda: False)():
                        profile_store_sql = """
                            DELETE FROM vector_profile_store
                            WHERE collection_name = $1
                              AND version IN ($2)
                        """
                        profile_store_args: list[Any] = [resolved_collection, list(target_version_set)]
                        if resolved_profile_type:
                            profile_store_sql += " AND COALESCE(profile_type, '') = $3"
                            profile_store_args.append(resolved_profile_type)
                        deleted = await conn.execute(profile_store_sql, *profile_store_args)
                        summary["deleted"]["vector_profile_store"] += int(str(deleted).split()[-1])

                    profile_sql = """
                        DELETE FROM vector_profiles
                        WHERE collection_name = $1
                          AND version IN ($2)
                    """
                    profile_args: list[Any] = [resolved_collection, list(target_version_set)]
                    if resolved_profile_type:
                        profile_sql += " AND profile_type = $3"
                        profile_args.append(resolved_profile_type)
                    deleted = await conn.execute(profile_sql, *profile_args)
                    summary["deleted"]["vector_profiles"] += int(str(deleted).split()[-1])

                    snapshot_sql = """
                        DELETE FROM vector_index_snapshots
                        WHERE collection_name = $1
                          AND index_version IN ($2)
                    """
                    snapshot_args: list[Any] = [resolved_collection, list(target_version_set)]
                    if resolved_profile_type:
                        snapshot_sql += " AND profile_type = $3"
                        snapshot_args.append(resolved_profile_type)
                    deleted = await conn.execute(snapshot_sql, *snapshot_args)
                    summary["deleted"]["vector_index_snapshots"] += int(str(deleted).split()[-1])

            return summary

        async def search_vector_profiles_by_embedding(
            self,
            *,
            query_embedding: List[float],
            collection_name: str,
            version: Optional[str] = None,
            profile_type: Optional[str] = None,
            stock_code: Optional[str] = None,
            stock_codes: Optional[List[str]] = None,
            entity_ids: Optional[List[str]] = None,
            exclude_stock_code: Optional[str] = None,
            exclude_entity_id: Optional[str] = None,
            limit: int = 20,
            metric: str = "cosine",
            index_params: Optional[dict] = None,
        ) -> List[dict]:
            if not getattr(self, "supports_sqlite_python", lambda: False)():
                return []
            vector_literal = self._encode_sqlite_python(query_embedding)
            dim = len(list(query_embedding or []))
            if not vector_literal or dim <= 0:
                return []
            distance_sql, similarity_sql = self._sqlite_python_distance_sql("ps.embedding", metric, dim)
            async with self.acquire() as conn:
                sql = f"""
                    SELECT vp.*, {similarity_sql} AS similarity
                    FROM vector_profile_store ps
                    JOIN vector_profiles vp ON vp.id = ps.profile_id
                    WHERE ps.collection_name = $2 AND ps.vector_dim = $3
                """
                params: list[Any] = [vector_literal, str(collection_name or ""), int(dim)]
                idx = 4
                if version:
                    sql += f" AND ps.version = ${idx}"
                    params.append(version)
                    idx += 1
                if profile_type:
                    sql += f" AND ps.profile_type = ${idx}"
                    params.append(profile_type)
                    idx += 1
                if stock_code:
                    sql += f" AND ps.stock_code = ${idx}"
                    params.append(stock_code)
                    idx += 1
                if stock_codes:
                    sql += f" AND ps.stock_code IN (${idx})"
                    params.append([str(item).strip() for item in list(stock_codes or []) if str(item).strip()])
                    idx += 1
                if entity_ids:
                    sql += f" AND ps.entity_id IN (${idx})"
                    params.append([str(item).strip() for item in list(entity_ids or []) if str(item).strip()])
                    idx += 1
                if exclude_stock_code:
                    sql += f" AND COALESCE(ps.stock_code, '') != ${idx}"
                    params.append(str(exclude_stock_code))
                    idx += 1
                if exclude_entity_id:
                    sql += f" AND ps.entity_id != ${idx}"
                    params.append(str(exclude_entity_id))
                    idx += 1
                sql += f" ORDER BY {distance_sql} ASC LIMIT ${idx}"
                params.append(max(1, min(int(limit or 20), 500)))
                rows = await conn.fetch(sql, *params)
            return [{**self._decode_unified_vector_profile(dict(row)), "similarity": round(float(row.get("similarity") or 0.0), 6)} for row in rows]

        async def search_vector_index_items_by_embedding(
            self,
            *,
            query_embedding: List[float],
            collection_name: str,
            index_version: str,
            profile_type: Optional[str] = None,
            stock_code: Optional[str] = None,
            stock_codes: Optional[List[str]] = None,
            entity_ids: Optional[List[str]] = None,
            bucket_ids: Optional[List[str]] = None,
            exclude_stock_code: Optional[str] = None,
            exclude_entity_id: Optional[str] = None,
            limit: int = 80,
            metric: str = "cosine",
            index_params: Optional[dict] = None,
        ) -> List[dict]:
            if not getattr(self, "supports_sqlite_python", lambda: False)():
                return []
            vector_literal = self._encode_sqlite_python(query_embedding)
            dim = len(list(query_embedding or []))
            if not vector_literal or dim <= 0:
                return []
            distance_sql, similarity_sql = self._sqlite_python_distance_sql("iv.embedding", metric, dim)
            async with self.acquire() as conn:
                sql = f"""
                    SELECT i.*, {similarity_sql} AS similarity
                    FROM vector_index_item_store iv
                    JOIN vector_index_items i ON i.id = iv.item_id
                    WHERE iv.collection_name = $2 AND iv.index_version = $3 AND iv.vector_dim = $4
                """
                params: list[Any] = [vector_literal, str(collection_name or ""), str(index_version or "v1"), int(dim)]
                idx = 5
                if profile_type:
                    sql += f" AND iv.profile_type = ${idx}"
                    params.append(profile_type)
                    idx += 1
                if stock_code:
                    sql += f" AND iv.stock_code = ${idx}"
                    params.append(stock_code)
                    idx += 1
                if stock_codes:
                    sql += f" AND iv.stock_code IN (${idx})"
                    params.append([str(item).strip() for item in list(stock_codes or []) if str(item).strip()])
                    idx += 1
                if entity_ids:
                    sql += f" AND iv.entity_id IN (${idx})"
                    params.append([str(item).strip() for item in list(entity_ids or []) if str(item).strip()])
                    idx += 1
                if bucket_ids:
                    sql += f" AND iv.bucket_id IN (${idx})"
                    params.append([str(item).strip() for item in list(bucket_ids or []) if str(item).strip()])
                    idx += 1
                if exclude_stock_code:
                    sql += f" AND COALESCE(iv.stock_code, '') != ${idx}"
                    params.append(str(exclude_stock_code))
                    idx += 1
                if exclude_entity_id:
                    sql += f" AND iv.entity_id != ${idx}"
                    params.append(str(exclude_entity_id))
                    idx += 1
                sql += f" ORDER BY {distance_sql} ASC LIMIT ${idx}"
                params.append(max(1, min(int(limit or 80), 500)))
                rows = await conn.fetch(sql, *params)
            return [{**self._decode_unified_vector_item(dict(row)), "similarity": round(float(row.get("similarity") or 0.0), 6)} for row in rows]
