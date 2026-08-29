"""SQLite 适配器 — 策略向量 Mixin (vector archive / sqlite_python ANN / index management / health check)"""

import json
import logging
from datetime import timezone
from typing import List, Optional

logger = logging.getLogger(__name__)


class StrategyVectorMixin:
    """向量存档 / sqlite_python ANN / HNSW 索引管理 / 健康检查"""

    # ------------------------------------------------------------------
    # vector profiles
    # ------------------------------------------------------------------

    def _decode_vector_profile(self, row: dict) -> dict:
        result = dict(row)
        result["embedding"] = self._decode_json_field(result.get("embedding"), [])
        result["metadata"] = self._decode_json_field(result.get("metadata"), {})
        return result

    async def save_strategy_vector_profile(self, profile: dict) -> dict:
        payload = dict(profile or {})
        embedding = payload.get("embedding") or []
        index_name = self._resolve_vector_index_name(payload)
        metadata_json = json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str)
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_vector_profiles
                    (strategy_id, index_name, profile_type, vector_method, metric, vector_dim, embedding, signature,
                     backend, index_version, metadata, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING *
                """,
                payload.get("strategy_id"),
                index_name,
                str(payload.get("profile_type") or "behavior"),
                str(payload.get("vector_method") or "price_volume"),
                str(payload.get("metric") or "cosine"),
                int(payload.get("vector_dim") or len(embedding)),
                json.dumps(embedding, ensure_ascii=False, default=str),
                payload.get("signature"),
                str(payload.get("backend") or getattr(self, 'get_vector_backend', lambda: 'index')()),
                payload.get("index_version"),
                metadata_json,
            )
            if getattr(self, 'supports_sqlite_python', lambda: False)():
                vector_literal = self._encode_sqlite_python(embedding)
                if vector_literal:
                    await conn.execute(
                        """
                        INSERT INTO strategy_vector_profile_store
                            (profile_id, strategy_id, index_name, index_version, profile_type, vector_method, metric,
                             vector_dim, embedding, metadata, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, CURRENT_TIMESTAMP)
                        ON CONFLICT (profile_id) DO UPDATE SET
                            strategy_id = EXCLUDED.strategy_id,
                            index_name = EXCLUDED.index_name,
                            index_version = EXCLUDED.index_version,
                            profile_type = EXCLUDED.profile_type,
                            vector_method = EXCLUDED.vector_method,
                            metric = EXCLUDED.metric,
                            vector_dim = EXCLUDED.vector_dim,
                            embedding = EXCLUDED.embedding,
                            metadata = EXCLUDED.metadata,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        dict(row).get('id'),
                        payload.get('strategy_id'),
                        index_name,
                        payload.get('index_version'),
                        str(payload.get('profile_type') or 'behavior'),
                        str(payload.get('vector_method') or 'price_volume'),
                        str(payload.get('metric') or 'cosine'),
                        int(payload.get('vector_dim') or len(embedding)),
                        vector_literal,
                        metadata_json,
                    )
        return self._decode_vector_profile(dict(row))

    async def list_strategy_vector_profiles(
        self,
        strategy_id: Optional[str] = None,
        profile_type: Optional[str] = None,
        index_name: Optional[str] = None,
        index_version: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT id, strategy_id, index_name, profile_type, vector_method, metric, vector_dim, embedding, signature, backend, index_version, metadata, created_at, updated_at FROM strategy_vector_profiles WHERE 1=1"
            params: list = []
            idx = 1
            if strategy_id:
                sql += f" AND strategy_id = ${idx}"
                params.append(strategy_id)
                idx += 1
            if index_name:
                sql += f" AND index_name = ${idx}"
                params.append(index_name)
                idx += 1
            if profile_type:
                sql += f" AND profile_type = ${idx}"
                params.append(profile_type)
                idx += 1
            if index_version:
                sql += f" AND index_version = ${idx}"
                params.append(index_version)
                idx += 1
            sql += f" ORDER BY updated_at DESC, created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 20), 5000)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_vector_profile(dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # vector index registry
    # ------------------------------------------------------------------

    def _decode_vector_index(self, row: dict) -> dict:
        result = dict(row)
        result["metadata"] = self._decode_json_field(result.get("metadata"), {})
        return result

    async def save_vector_index_registry(self, entry: dict) -> dict:
        payload = dict(entry or {})
        built_at = self._coerce_timestamp(payload.get("built_at"))
        activated_at = self._coerce_timestamp(payload.get("activated_at"))
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO vector_index_registry
                    (index_name, backend, status, profile_type, vector_method, metric, sample_count,
                     index_version, metadata, built_at, activated_at, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, CURRENT_TIMESTAMP)
                ON CONFLICT (index_name, index_version) DO UPDATE SET
                    backend = EXCLUDED.backend,
                    status = EXCLUDED.status,
                    profile_type = EXCLUDED.profile_type,
                    vector_method = EXCLUDED.vector_method,
                    metric = EXCLUDED.metric,
                    sample_count = EXCLUDED.sample_count,
                    metadata = EXCLUDED.metadata,
                    built_at = EXCLUDED.built_at,
                    activated_at = EXCLUDED.activated_at
                RETURNING *
                """,
                str(payload.get("index_name") or "default"),
                str(payload.get("backend") or "index"),
                str(payload.get("status") or "building"),
                payload.get("profile_type"),
                payload.get("vector_method"),
                str(payload.get("metric") or "cosine"),
                int(payload.get("sample_count") or 0),
                str(payload.get("index_version") or "v1"),
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
                built_at,
                activated_at,
            )
        return self._decode_vector_index(dict(row))

    async def list_vector_index_registry(
        self,
        index_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM vector_index_registry WHERE 1=1"
            params: list = []
            idx = 1
            if index_name:
                sql += f" AND index_name = ${idx}"
                params.append(index_name)
                idx += 1
            if status:
                sql += f" AND status = ${idx}"
                params.append(status)
                idx += 1
            sql += f" ORDER BY created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 20), 5000)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_vector_index(dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # vector index snapshots
    # ------------------------------------------------------------------

    def _decode_vector_index_snapshot(self, row: dict) -> dict:
        result = dict(row)
        result.pop("rn", None)
        result["centroids"] = self._decode_json_field(result.get("centroids"), [])
        result["index_params"] = self._decode_json_field(result.get("index_params"), {})
        result["metrics"] = self._decode_json_field(result.get("metrics"), {})
        result["metadata"] = self._decode_json_field(result.get("metadata"), {})
        return result

    async def save_strategy_vector_index_snapshot(self, snapshot: dict) -> dict:
        payload = dict(snapshot or {})
        built_at = self._coerce_timestamp(payload.get("built_at"))
        activated_at = self._coerce_timestamp(payload.get("activated_at"))
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_vector_index_snapshots
                    (index_name, index_version, status, profile_type, vector_method, metric, backend,
                     profile_count, bucket_count, vector_dim, centroids, index_params, metrics, metadata, task_run_id, source,
                     built_at, activated_at, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, CURRENT_TIMESTAMP)
                RETURNING *
                """,
                str(payload.get("index_name") or "strategy_behavior"),
                str(payload.get("index_version") or "v1"),
                str(payload.get("status") or "building"),
                payload.get("profile_type"),
                payload.get("vector_method"),
                str(payload.get("metric") or "cosine"),
                str(payload.get("backend") or "index"),
                int(payload.get("profile_count") or 0),
                int(payload.get("bucket_count") or 0),
                int(payload.get("vector_dim") or 0),
                json.dumps(payload.get("centroids") or [], ensure_ascii=False, default=str),
                json.dumps(payload.get("index_params") or {}, ensure_ascii=False, default=str),
                json.dumps(payload.get("metrics") or {}, ensure_ascii=False, default=str),
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
                payload.get("task_run_id"),
                str(payload.get("source") or "system"),
                built_at,
                activated_at,
            )
        return self._decode_vector_index_snapshot(dict(row))

    async def get_latest_strategy_vector_index_snapshot(self, index_name: str = 'strategy_behavior') -> Optional[dict]:
        rows = await self.list_strategy_vector_index_snapshots(index_name=index_name, limit=1)
        return rows[0] if rows else None

    async def list_strategy_vector_index_snapshots(
        self,
        index_name: Optional[str] = None,
        index_version: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        latest_only: bool = False,
    ) -> List[dict]:
        async with self.acquire() as conn:
            where_parts = ["1=1"]
            params: list = []
            idx = 1
            if index_name:
                where_parts.append(f"index_name = ${idx}")
                params.append(index_name)
                idx += 1
            if index_version:
                where_parts.append(f"index_version = ${idx}")
                params.append(index_version)
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
                                   PARTITION BY index_name, index_version
                                   ORDER BY {order_sql}
                               ) AS rn
                        FROM strategy_vector_index_snapshots
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
                    FROM strategy_vector_index_snapshots
                    WHERE {where_sql}
                    ORDER BY {order_sql}
                    LIMIT ${idx}
                """
            params.append(max(1, min(int(limit or 20), 5000)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_vector_index_snapshot(dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # vector index items (with sqlite_python store sync)
    # ------------------------------------------------------------------

    def _decode_vector_index_item(self, row: dict) -> dict:
        result = dict(row)
        result["embedding"] = self._decode_json_field(result.get("embedding"), [])
        result["metadata"] = self._decode_json_field(result.get("metadata"), {})
        return result

    async def replace_strategy_vector_index_items(self, index_name: str, index_version: str, items: List[dict]) -> dict:
        resolved_index_name = str(index_name or 'strategy_behavior')
        resolved_index_version = str(index_version or 'v1')
        payloads = [dict(item or {}) for item in items or []]
        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM strategy_vector_index_items WHERE index_name = $1 AND index_version = $2",
                    resolved_index_name,
                    resolved_index_version,
                )
                item_rows = []
                for payload in payloads:
                    item_rows.append((
                        resolved_index_name,
                        resolved_index_version,
                        payload.get("profile_id"),
                        payload.get("strategy_id"),
                        payload.get("profile_type"),
                        payload.get("vector_method"),
                        str(payload.get("metric") or 'cosine'),
                        int(payload.get("vector_dim") or len(payload.get("embedding") or [])),
                        payload.get("bucket_id"),
                        float(payload.get("coarse_score") or 0.0),
                        json.dumps(payload.get("embedding") or [], ensure_ascii=False, default=str),
                        json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
                    ))
                if item_rows:
                    await conn.executemany(
                        """
                        INSERT INTO strategy_vector_index_items
                            (index_name, index_version, profile_id, strategy_id, profile_type, vector_method, metric,
                             vector_dim, bucket_id, coarse_score, embedding, metadata, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, CURRENT_TIMESTAMP)
                        ON CONFLICT (index_name, index_version, profile_id) DO UPDATE SET
                            strategy_id = EXCLUDED.strategy_id,
                            profile_type = EXCLUDED.profile_type,
                            vector_method = EXCLUDED.vector_method,
                            metric = EXCLUDED.metric,
                            vector_dim = EXCLUDED.vector_dim,
                            bucket_id = EXCLUDED.bucket_id,
                            coarse_score = EXCLUDED.coarse_score,
                            embedding = EXCLUDED.embedding,
                            metadata = EXCLUDED.metadata
                        """,
                        item_rows,
                    )
                if getattr(self, 'supports_sqlite_python', lambda: False)() and payloads:
                    mapping_rows = await conn.fetch(
                        """
                        SELECT id, profile_id
                        FROM strategy_vector_index_items
                        WHERE index_name = $1
                          AND index_version = $2
                        """,
                        resolved_index_name,
                        resolved_index_version,
                    )
                    row_ids = {
                        str(dict(row).get('profile_id')): dict(row).get('id')
                        for row in mapping_rows
                        if dict(row).get('profile_id') is not None
                    }
                    store_rows = []
                    for payload in payloads:
                        vector_literal = self._encode_sqlite_python(payload.get('embedding') or [])
                        row_id = row_ids.get(str(payload.get('profile_id')))
                        if not vector_literal or row_id is None:
                            continue
                        store_rows.append((
                            row_id,
                            resolved_index_name,
                            resolved_index_version,
                            payload.get('strategy_id'),
                            payload.get('profile_id'),
                            payload.get('profile_type'),
                            payload.get('vector_method'),
                            str(payload.get('metric') or 'cosine'),
                            int(payload.get('vector_dim') or len(payload.get('embedding') or [])),
                            vector_literal,
                            json.dumps(payload.get('metadata') or {}, ensure_ascii=False, default=str),
                        ))
                    if store_rows:
                        await conn.executemany(
                            """
                            INSERT INTO strategy_vector_index_item_store
                                (item_id, index_name, index_version, strategy_id, profile_id, profile_type, vector_method,
                                 metric, vector_dim, embedding, metadata, updated_at)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, CURRENT_TIMESTAMP)
                            ON CONFLICT (item_id) DO UPDATE SET
                                index_name = EXCLUDED.index_name,
                                index_version = EXCLUDED.index_version,
                                strategy_id = EXCLUDED.strategy_id,
                                profile_id = EXCLUDED.profile_id,
                                profile_type = EXCLUDED.profile_type,
                                vector_method = EXCLUDED.vector_method,
                                metric = EXCLUDED.metric,
                                vector_dim = EXCLUDED.vector_dim,
                                embedding = EXCLUDED.embedding,
                                metadata = EXCLUDED.metadata,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            store_rows,
                        )
        return {"index_name": resolved_index_name, "index_version": resolved_index_version, "count": len(item_rows)}

    async def list_strategy_vector_index_items(
        self,
        index_name: Optional[str] = None,
        index_version: Optional[str] = None,
        bucket_ids: Optional[List[str]] = None,
        strategy_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_vector_index_items WHERE 1=1"
            params: list = []
            idx = 1
            if index_name:
                sql += f" AND index_name = ${idx}"
                params.append(index_name)
                idx += 1
            if index_version:
                sql += f" AND index_version = ${idx}"
                params.append(index_version)
                idx += 1
            if bucket_ids:
                sql += f" AND bucket_id IN (${idx})"
                params.append([str(item) for item in bucket_ids])
                idx += 1
            if strategy_id:
                sql += f" AND strategy_id = ${idx}"
                params.append(strategy_id)
                idx += 1
            sql += f" ORDER BY coarse_score DESC, created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 200), 5000)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_vector_index_item(dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # sqlite_python ANN search
    # ------------------------------------------------------------------

    async def search_strategy_vector_profiles_by_embedding(
        self,
        query_embedding: List[float],
        profile_type: Optional[str] = None,
        index_name: Optional[str] = None,
        index_version: Optional[str] = None,
        exclude_strategy_id: Optional[str] = None,
        limit: int = 20,
        metric: str = 'cosine',
        index_params: Optional[dict] = None,
    ) -> List[dict]:
        if not getattr(self, 'supports_sqlite_python', lambda: False)():
            return []
        vector_literal = self._encode_sqlite_python(query_embedding)
        dim = len(list(query_embedding or []))
        if not vector_literal or dim <= 0:
            return []
        distance_sql, similarity_sql = self._sqlite_python_distance_sql('pv.embedding', metric, dim)
        async with self.acquire() as conn:
            sql = f"""
                SELECT p.id, p.strategy_id, p.index_name, p.profile_type, p.vector_method, p.metric, p.vector_dim,
                       p.embedding, p.signature, p.backend, p.index_version, p.metadata, p.created_at, p.updated_at,
                       {similarity_sql} AS similarity
                FROM strategy_vector_profile_store pv
                JOIN strategy_vector_profiles p ON p.id = pv.profile_id
                WHERE pv.vector_dim = $2
            """
            params: list = [vector_literal, int(dim)]
            idx = 3
            if index_name:
                sql += f" AND pv.index_name = ${idx}"
                params.append(index_name)
                idx += 1
            if index_version:
                sql += f" AND pv.index_version = ${idx}"
                params.append(index_version)
                idx += 1
            if profile_type:
                sql += f" AND pv.profile_type = ${idx}"
                params.append(profile_type)
                idx += 1
            if exclude_strategy_id:
                sql += f" AND pv.strategy_id <> ${idx}"
                params.append(exclude_strategy_id)
                idx += 1
            sql += f" ORDER BY {distance_sql} ASC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 20), 500)))
            rows = await conn.fetch(sql, *params)
        return [{**self._decode_vector_profile(dict(row)), 'similarity': round(float(row.get('similarity') or 0.0), 6)} for row in rows]

    async def search_strategy_vector_index_items_by_embedding(
        self,
        query_embedding: List[float],
        index_name: str,
        index_version: str,
        profile_type: Optional[str] = None,
        exclude_strategy_id: Optional[str] = None,
        limit: int = 80,
        metric: str = 'cosine',
        index_params: Optional[dict] = None,
    ) -> List[dict]:
        if not getattr(self, 'supports_sqlite_python', lambda: False)():
            return []
        vector_literal = self._encode_sqlite_python(query_embedding)
        dim = len(list(query_embedding or []))
        if not vector_literal or dim <= 0:
            return []
        distance_sql, similarity_sql = self._sqlite_python_distance_sql('iv.embedding', metric, dim)
        async with self.acquire() as conn:
            sql = f"""
                SELECT i.id, i.index_name, i.index_version, i.profile_id, i.strategy_id, i.profile_type, i.vector_method,
                       i.metric, i.vector_dim, i.bucket_id, i.coarse_score, i.embedding, i.metadata, i.created_at,
                       {similarity_sql} AS similarity
                FROM strategy_vector_index_item_store iv
                JOIN strategy_vector_index_items i ON i.id = iv.item_id
                WHERE iv.index_name = $2 AND iv.index_version = $3 AND iv.vector_dim = $4
            """
            params: list = [vector_literal, str(index_name or 'strategy_behavior'), str(index_version or 'v1'), int(dim)]
            idx = 5
            if profile_type:
                sql += f" AND iv.profile_type = ${idx}"
                params.append(profile_type)
                idx += 1
            if exclude_strategy_id:
                sql += f" AND iv.strategy_id <> ${idx}"
                params.append(exclude_strategy_id)
                idx += 1
            sql += f" ORDER BY {distance_sql} ASC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 80), 500)))
            rows = await conn.fetch(sql, *params)
        return [{**self._decode_vector_index_item(dict(row)), 'similarity': round(float(row.get('similarity') or 0.0), 6)} for row in rows]

    # ------------------------------------------------------------------
    # HNSW index management
    # ------------------------------------------------------------------

    async def ensure_strategy_vector_index_item_sqlite_python_index(
        self,
        index_name: str,
        index_version: str,
        vector_dim: int,
        metric: str = 'cosine',
        index_params: Optional[dict] = None,
    ) -> Optional[str]:
        return None

    async def ensure_strategy_vector_profile_sqlite_python_index(
        self,
        index_name: str,
        index_version: str,
        vector_dim: int,
        profile_type: Optional[str] = None,
        metric: str = 'cosine',
        index_params: Optional[dict] = None,
    ) -> Optional[str]:
        return None

    async def list_strategy_vector_hnsw_indexes(
        self,
        index_name: Optional[str] = None,
        index_version: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict]:
        return []

    # ------------------------------------------------------------------
    # health check
    # ------------------------------------------------------------------

    async def get_strategy_vector_health(
        self,
        index_name: str = 'strategy_behavior',
        limit_versions: int = 20,
        include_hnsw_indexes: bool = False,
    ) -> dict:
        async with self.acquire() as conn:
            table_flags = {
                'strategy_vector_profiles': bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1)", 'strategy_vector_profiles')),
                'strategy_vector_profile_store': bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1)", 'strategy_vector_profile_store')),
                'strategy_vector_index_snapshots': bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1)", 'strategy_vector_index_snapshots')),
                'strategy_vector_index_items': bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1)", 'strategy_vector_index_items')),
                'strategy_vector_index_item_store': bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1)", 'strategy_vector_index_item_store')),
                'vector_dimension_contracts': bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1)", 'vector_dimension_contracts')),
                'market_doc_chunks_fts': bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1)", 'market_doc_chunks_fts')),
                'vector_graph_nodes': bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1)", 'vector_graph_nodes')),
                'vector_graph_edges': bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1)", 'vector_graph_edges')),
            }
            counts = {
                'profiles': int(await conn.fetchval("SELECT COUNT(*) FROM strategy_vector_profiles WHERE index_name = $1", index_name) or 0) if table_flags['strategy_vector_profiles'] else 0,
                'profile_store': int(await conn.fetchval("SELECT COUNT(*) FROM strategy_vector_profile_store WHERE index_name = $1", index_name) or 0) if table_flags['strategy_vector_profile_store'] else 0,
                'index_snapshots': int(await conn.fetchval("SELECT COUNT(*) FROM strategy_vector_index_snapshots WHERE index_name = $1", index_name) or 0) if table_flags['strategy_vector_index_snapshots'] else 0,
                'index_items': int(await conn.fetchval("SELECT COUNT(*) FROM strategy_vector_index_items WHERE index_name = $1", index_name) or 0) if table_flags['strategy_vector_index_items'] else 0,
                'index_item_store': int(await conn.fetchval("SELECT COUNT(*) FROM strategy_vector_index_item_store WHERE index_name = $1", index_name) or 0) if table_flags['strategy_vector_index_item_store'] else 0,
                'dimension_contracts': int(await conn.fetchval("SELECT COUNT(*) FROM vector_dimension_contracts WHERE status = 'active'") or 0) if table_flags['vector_dimension_contracts'] else 0,
                'graph_nodes': int(await conn.fetchval("SELECT COUNT(*) FROM vector_graph_nodes") or 0) if table_flags['vector_graph_nodes'] else 0,
                'graph_edges': int(await conn.fetchval("SELECT COUNT(*) FROM vector_graph_edges") or 0) if table_flags['vector_graph_edges'] else 0,
            }
            dimension_contracts = [
                dict(row)
                for row in (
                    await conn.fetch(
                        """
                        SELECT collection_name, entity_family, profile_type, model_id, vector_dim, version_prefix, metric, status
                        FROM vector_dimension_contracts
                        WHERE status = 'active'
                        ORDER BY collection_name, COALESCE(profile_type, ''), vector_dim
                        LIMIT 200
                        """
                    )
                    if table_flags['vector_dimension_contracts']
                    else []
                )
            ]
            coverage_rows = [
                dict(row)
                for row in await conn.fetch(
                    """
                    SELECT
                        vc.collection_name,
                        vc.vector_dim AS declared_dim,
                        vc.active_version,
                        COUNT(vp.id) AS profile_count,
                        COUNT(DISTINCT vp.vector_dim) AS distinct_profile_dims,
                        MIN(vp.vector_dim) AS min_profile_dim,
                        MAX(vp.vector_dim) AS max_profile_dim
                    FROM vector_collections vc
                    LEFT JOIN vector_profiles vp ON vp.collection_name = vc.collection_name
                    GROUP BY vc.collection_name, vc.vector_dim, vc.active_version
                    ORDER BY vc.collection_name
                    """
                )
            ]
            graph_counts = {
                'nodes': counts.get('graph_nodes', 0),
                'edges': counts.get('graph_edges', 0),
            }
            version_rows = []
            if table_flags['strategy_vector_index_snapshots']:
                version_rows.extend(await conn.fetch(
                    """
                    SELECT index_version,
                           MAX(created_at) AS last_seen,
                           NULL AS registry_status,
                           NULL AS registry_backend,
                           NULL AS sample_count,
                           MAX(status) AS snapshot_status,
                           MAX(backend) AS snapshot_backend,
                           MAX(profile_count) AS profile_count,
                           MAX(bucket_count) AS bucket_count,
                           MAX(vector_dim) AS vector_dim,
                           0 AS profile_rows,
                           0 AS profile_store_rows,
                           0 AS index_item_rows,
                           0 AS index_item_store_rows
                    FROM strategy_vector_index_snapshots
                    WHERE index_name = $1
                    GROUP BY index_version
                    LIMIT $2
                    """,
                    index_name,
                    max(1, min(int(limit_versions or 20), 200)),
                ))
            latest_snapshot = await conn.fetchrow(
                """
                SELECT index_name, index_version, status, profile_count, bucket_count, vector_dim, backend, built_at, activated_at, created_at
                FROM strategy_vector_index_snapshots
                WHERE index_name = $1
                ORDER BY COALESCE(activated_at, built_at, created_at) DESC
                LIMIT 1
                """,
                index_name,
            ) if table_flags['strategy_vector_index_snapshots'] else None
        hnsw_indexes = await self.list_strategy_vector_hnsw_indexes(index_name=index_name, limit=500) if include_hnsw_indexes else []
        latest_snapshot_dict = dict(latest_snapshot) if latest_snapshot else None
        latest_snapshot_version = str((latest_snapshot_dict or {}).get('index_version') or '')
        versions = [dict(row) for row in version_rows]

        def _version_sort_key(item: dict) -> tuple:
            last_seen = self._coerce_timestamp(item.get('last_seen'))
            if last_seen and last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            item['last_seen'] = last_seen
            version = str(item.get('index_version') or '')
            priority = 3
            if latest_snapshot_version and version == latest_snapshot_version:
                priority = 0
            elif str(item.get('snapshot_status') or '').lower() == 'active':
                priority = 1
            elif str(item.get('registry_status') or '').lower() == 'active':
                priority = 2
            return (priority, -(last_seen.astimezone(timezone.utc).timestamp() if last_seen else 0.0), version)

        versions.sort(key=_version_sort_key)
        quality_flags = []
        if not table_flags.get('vector_dimension_contracts'):
            quality_flags.append('dimension_contracts_missing')
        if not table_flags.get('market_doc_chunks_fts'):
            quality_flags.append('fts5_missing')
        for row in coverage_rows:
            distinct_dims = int(row.get('distinct_profile_dims') or 0)
            min_dim = int(row.get('min_profile_dim') or 0)
            max_dim = int(row.get('max_profile_dim') or 0)
            declared_dim = int(row.get('declared_dim') or 0)
            if distinct_dims > 1:
                quality_flags.append(f"mixed_dims:{row.get('collection_name')}")
            elif min_dim and declared_dim and min_dim != declared_dim:
                quality_flags.append(f"declared_dim_mismatch:{row.get('collection_name')}:{declared_dim}->{min_dim}")
        return {
            'index_name': index_name,
            'backend': getattr(self, 'get_vector_backend', lambda: 'index')(),
            'sqlite_python_enabled': bool(getattr(self, 'supports_sqlite_python', lambda: False)()),
            'sqlite_python_extension': None,
            'tables': table_flags,
            'counts': counts,
            'latest_snapshot': latest_snapshot_dict,
            'versions': versions,
            'hnsw_indexes': hnsw_indexes,
            'hnsw_index_count': len(hnsw_indexes),
            'dimension_contracts': dimension_contracts,
            'fts5_enabled': bool(table_flags.get('market_doc_chunks_fts')),
            'graph_nodes': graph_counts.get('nodes', 0),
            'graph_edges': graph_counts.get('edges', 0),
            'coverage_by_collection': coverage_rows,
            'quality_flags': quality_flags,
            'recommended_cleanup_versions': [item.get('index_version') for item in versions[1:] if item.get('index_version')],
        }

    # ------------------------------------------------------------------
    # cleanup
    # ------------------------------------------------------------------

    async def cleanup_strategy_vector_history(
        self,
        index_name: str = 'strategy_behavior',
        keep_versions: int = 1,
        dry_run: bool = True,
        cleanup_hnsw: bool = True,
        limit_versions: int = 200,
        protect_versions: Optional[List[str]] = None,
    ) -> dict:
        health = await self.get_strategy_vector_health(index_name=index_name, limit_versions=limit_versions, include_hnsw_indexes=cleanup_hnsw)
        versions = [item for item in list(health.get('versions') or []) if item.get('index_version')]
        latest_snapshot_version = str((health.get('latest_snapshot') or {}).get('index_version') or '').strip()
        keep_total = max(0, int(keep_versions or 0))
        protected: List[str] = []
        if latest_snapshot_version:
            protected.append(latest_snapshot_version)
        protected_limit = max(keep_total, 1 if latest_snapshot_version else 0)
        for item in versions:
            version = str(item.get('index_version') or '').strip()
            if not version or version in protected:
                continue
            if len(protected) >= protected_limit:
                break
            protected.append(version)
        protected.extend(str(item) for item in list(protect_versions or []) if str(item).strip())
        protected_set = {item for item in protected if item}
        target_versions = [item for item in versions if str(item.get('index_version')) not in protected_set]
        index_rows = list(health.get('hnsw_indexes') or []) if cleanup_hnsw else []
        indexes_to_drop = [
            row for row in index_rows
            if any(self._sql_quote(item.get('index_version')) in str(row.get('indexdef') or '') for item in target_versions)
        ]
        summary = {
            'index_name': index_name,
            'dry_run': bool(dry_run),
            'keep_versions': max(0, int(keep_versions or 0)),
            'protected_versions': sorted(protected_set),
            'target_versions': [item.get('index_version') for item in target_versions],
            'hnsw_indexes_to_drop': [row.get('indexname') for row in indexes_to_drop],
            'deleted': {
                'vector_index_registry': 0,
                'vector_index_snapshots': 0,
                'vector_profiles': 0,
                'vector_profile_store': 0,
                'vector_index_items': 0,
                'vector_index_item_store': 0,
                'hnsw_indexes': 0,
            },
            'version_details': target_versions,
        }
        if dry_run or not target_versions:
            return summary
        async with self.acquire() as conn:
            for item in target_versions:
                version = str(item.get('index_version') or '')
                if not version:
                    continue
                if cleanup_hnsw:
                    for row in indexes_to_drop:
                        if self._sql_quote(version) not in str(row.get('indexdef') or ''):
                            continue
                        await conn.execute(f"DROP INDEX IF EXISTS {row['indexname']}")
                        summary['deleted']['hnsw_indexes'] += 1
                if health.get('tables', {}).get('strategy_vector_index_item_store'):
                    summary['deleted']['vector_index_item_store'] += int((await conn.execute(
                        "DELETE FROM strategy_vector_index_item_store WHERE index_name = $1 AND index_version = $2",
                        index_name,
                        version,
                    )).split()[-1])
                summary['deleted']['vector_index_items'] += int((await conn.execute(
                    "DELETE FROM strategy_vector_index_items WHERE index_name = $1 AND index_version = $2",
                    index_name,
                    version,
                )).split()[-1])
                if health.get('tables', {}).get('strategy_vector_profile_store'):
                    summary['deleted']['vector_profile_store'] += int((await conn.execute(
                        "DELETE FROM strategy_vector_profile_store WHERE index_name = $1 AND index_version = $2",
                        index_name,
                        version,
                    )).split()[-1])
                summary['deleted']['vector_profiles'] += int((await conn.execute(
                    "DELETE FROM strategy_vector_profiles WHERE index_name = $1 AND index_version = $2",
                    index_name,
                    version,
                )).split()[-1])
                summary['deleted']['vector_index_snapshots'] += int((await conn.execute(
                    "DELETE FROM strategy_vector_index_snapshots WHERE index_name = $1 AND index_version = $2",
                    index_name,
                    version,
                )).split()[-1])
                summary['deleted']['vector_index_registry'] += int((await conn.execute(
                    "DELETE FROM vector_index_registry WHERE index_name = $1 AND index_version = $2",
                    index_name,
                    version,
                )).split()[-1])
        return summary
