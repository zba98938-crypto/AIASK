"""Unified vector storage mixin for market / quant / strategy derived objects."""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime
from typing import Any, Iterable, List, Optional

logger = logging.getLogger(__name__)


class _VectorUnifiedStorageMixin:
        async def list_vector_dimension_contracts(
            self,
            *,
            collection_name: Optional[str] = None,
            status: Optional[str] = "active",
            limit: int = 200,
        ) -> List[dict]:
            async with self.acquire() as conn:
                sql = "SELECT * FROM vector_dimension_contracts WHERE 1=1"
                params: list[Any] = []
                idx = 1
                if collection_name:
                    sql += f" AND collection_name = ${idx}"
                    params.append(str(collection_name))
                    idx += 1
                if status:
                    sql += f" AND status = ${idx}"
                    params.append(str(status))
                    idx += 1
                sql += f" ORDER BY collection_name, COALESCE(profile_type, ''), vector_dim LIMIT ${idx}"
                params.append(max(1, min(int(limit or 200), 5000)))
                rows = await conn.fetch(sql, *params)
            return [
                {
                    **dict(row),
                    "metadata": self._decode_json_field(dict(row).get("metadata"), {}),
                }
                for row in rows
            ]

        async def get_vector_dimension_contract(
            self,
            *,
            collection_name: str,
            profile_type: Optional[str] = None,
            model_id: Optional[str] = None,
            version: Optional[str] = None,
        ) -> Optional[dict]:
            async with self.acquire() as conn:
                row = await self._fetch_vector_dimension_contract(
                    conn,
                    collection_name=collection_name,
                    profile_type=profile_type,
                    model_id=model_id,
                    version=version,
                )
            if not row:
                return None
            return {
                **dict(row),
                "metadata": self._decode_json_field(dict(row).get("metadata"), {}),
            }

        async def _fetch_vector_dimension_contract(
            self,
            conn,
            *,
            collection_name: str,
            profile_type: Optional[str] = None,
            model_id: Optional[str] = None,
            version: Optional[str] = None,
        ) -> Optional[dict]:
            return await conn.fetchrow(
                """
                SELECT *
                FROM vector_dimension_contracts
                WHERE collection_name = $1
                  AND status = 'active'
                  AND (profile_type IS NULL OR profile_type = $2)
                  AND (model_id IS NULL OR model_id = $3)
                  AND (COALESCE(version_prefix, '') = '' OR $4 LIKE version_prefix || '%')
                ORDER BY
                  CASE WHEN profile_type = $2 THEN 0 ELSE 1 END,
                  CASE WHEN model_id = $3 THEN 0 ELSE 1 END,
                  LENGTH(COALESCE(version_prefix, '')) DESC,
                  id DESC
                LIMIT 1
                """,
                str(collection_name or ""),
                str(profile_type or "") or None,
                str(model_id or "") or None,
                str(version or ""),
            )

        async def _validate_vector_dimension_contract(
            self,
            conn,
            *,
            collection_name: str,
            profile_type: Optional[str],
            model_id: str,
            version: str,
            vector_dim: int,
        ) -> Optional[dict]:
            contract = await self._fetch_vector_dimension_contract(
                conn,
                collection_name=collection_name,
                profile_type=profile_type,
                model_id=model_id,
                version=version,
            )
            if not contract:
                return None
            expected_dim = int(dict(contract).get("vector_dim") or 0)
            if expected_dim > 0 and int(vector_dim or 0) != expected_dim:
                raise ValueError(
                    "vector dimension contract mismatch: "
                    f"{collection_name}/{profile_type or '*'} expected {expected_dim}, got {int(vector_dim or 0)}"
                )
            return dict(contract)

        async def _upsert_vector_graph_for_profile(self, conn, *, row: dict, profile: dict) -> None:
            try:
                collection_name = str(row.get("collection_name") or profile.get("collection_name") or "").strip()
                entity_type = str(row.get("entity_type") or profile.get("entity_type") or "generic").strip()
                entity_id = str(row.get("entity_id") or profile.get("entity_id") or "").strip()
                stock_code = str(row.get("stock_code") or profile.get("stock_code") or "").strip()
                if not collection_name or not entity_id:
                    return
                profile_id = row.get("id")
                entity_node = f"{entity_type}:{entity_id}"
                collection_node = f"collection:{collection_name}"
                metadata = {
                    "collection_name": collection_name,
                    "profile_id": profile_id,
                    "profile_type": row.get("profile_type") or profile.get("profile_type"),
                    "model_id": row.get("model_id") or profile.get("model_id"),
                    "vector_dim": row.get("vector_dim") or profile.get("vector_dim"),
                    "version": row.get("version") or profile.get("version"),
                }
                await conn.execute(
                    """
                    INSERT INTO vector_graph_nodes (node_key, node_type, entity_id, stock_code, label, metadata, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (node_key) DO UPDATE SET
                        node_type = EXCLUDED.node_type,
                        entity_id = EXCLUDED.entity_id,
                        stock_code = EXCLUDED.stock_code,
                        label = EXCLUDED.label,
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    entity_node,
                    entity_type,
                    entity_id,
                    stock_code or None,
                    entity_id,
                    json.dumps(metadata, ensure_ascii=False, default=str),
                )
                await conn.execute(
                    """
                    INSERT INTO vector_graph_nodes (node_key, node_type, entity_id, label, metadata, created_at, updated_at)
                    VALUES ($1, 'collection', $2, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (node_key) DO UPDATE SET
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    collection_node,
                    collection_name,
                    json.dumps({"collection_name": collection_name}, ensure_ascii=False, default=str),
                )
                await conn.execute(
                    """
                    INSERT INTO vector_graph_edges (edge_key, source_node_key, target_node_key, relation_type, weight, metadata, created_at, updated_at)
                    VALUES ($1, $2, $3, 'contains_profile', 1.0, $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (edge_key) DO UPDATE SET
                        weight = EXCLUDED.weight,
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    f"{collection_node}->contains_profile->{entity_node}",
                    collection_node,
                    entity_node,
                    json.dumps(metadata, ensure_ascii=False, default=str),
                )
                if stock_code:
                    stock_node = f"stock:{stock_code}"
                    await conn.execute(
                        """
                        INSERT INTO vector_graph_nodes (node_key, node_type, entity_id, stock_code, label, metadata, created_at, updated_at)
                        VALUES ($1, 'stock', $2, $2, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ON CONFLICT (node_key) DO UPDATE SET
                            stock_code = EXCLUDED.stock_code,
                            metadata = EXCLUDED.metadata,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        stock_node,
                        stock_code,
                        json.dumps({"stock_code": stock_code}, ensure_ascii=False, default=str),
                    )
                    await conn.execute(
                        """
                        INSERT INTO vector_graph_edges (edge_key, source_node_key, target_node_key, relation_type, weight, metadata, created_at, updated_at)
                        VALUES ($1, $2, $3, 'has_vector_profile', 1.0, $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ON CONFLICT (edge_key) DO UPDATE SET
                            metadata = EXCLUDED.metadata,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        f"{stock_node}->has_vector_profile->{entity_node}",
                        stock_node,
                        entity_node,
                        json.dumps(metadata, ensure_ascii=False, default=str),
                    )
            except Exception as exc:
                logger.debug("vector graph upsert skipped: %s", exc)

        async def save_vector_collection(self, payload: dict) -> dict:
            item = dict(payload or {})
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO vector_collections (
                        collection_name, entity_family, backend, metric, model_id, vector_dim,
                        normalization, status, active_version, metadata, created_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (collection_name) DO UPDATE SET
                        entity_family = EXCLUDED.entity_family,
                        backend = EXCLUDED.backend,
                        metric = EXCLUDED.metric,
                        model_id = EXCLUDED.model_id,
                        vector_dim = EXCLUDED.vector_dim,
                        normalization = EXCLUDED.normalization,
                        status = EXCLUDED.status,
                        active_version = COALESCE(EXCLUDED.active_version, vector_collections.active_version),
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING *
                    """,
                    str(item.get("collection_name") or ""),
                    str(item.get("entity_family") or "generic"),
                    str(item.get("backend") or self.get_vector_backend()),
                    str(item.get("metric") or "cosine"),
                    str(item.get("model_id") or "unknown"),
                    int(item.get("vector_dim") or 0),
                    str(item.get("normalization") or "unit"),
                    str(item.get("status") or "active"),
                    item.get("active_version"),
                    json.dumps(item.get("metadata") or {}, ensure_ascii=False, default=str),
                )
            return self._decode_vector_collection(dict(row))

        async def get_vector_collection(self, collection_name: str) -> Optional[dict]:
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM vector_collections WHERE collection_name = $1",
                    str(collection_name or ""),
                )
            return self._decode_vector_collection(dict(row)) if row else None

        async def list_vector_collections(
            self,
            *,
            entity_family: Optional[str] = None,
            status: Optional[str] = None,
            limit: int = 100,
        ) -> List[dict]:
            async with self.acquire() as conn:
                sql = "SELECT * FROM vector_collections WHERE 1=1"
                params: list[Any] = []
                idx = 1
                if entity_family:
                    sql += f" AND entity_family = ${idx}"
                    params.append(entity_family)
                    idx += 1
                if status:
                    sql += f" AND status = ${idx}"
                    params.append(status)
                    idx += 1
                sql += f" ORDER BY collection_name LIMIT ${idx}"
                params.append(max(1, min(int(limit or 100), 5000)))
                rows = await conn.fetch(sql, *params)
            return [self._decode_vector_collection(dict(row)) for row in rows]

        async def save_vector_profile(self, profile: dict) -> dict:
            payload = dict(profile or {})
            try:
                embedding = [float(item) for item in list(payload.get("embedding") or [])]
            except (TypeError, ValueError) as exc:
                raise ValueError("embedding must be a numeric vector") from exc
            if any(not math.isfinite(item) for item in embedding):
                raise ValueError("embedding must contain only finite values")
            metadata = dict(payload.get("metadata") or {})
            collection_name = str(payload.get("collection_name") or "")
            if not collection_name:
                raise ValueError("collection_name is required")
            vector_dim = int(payload.get("vector_dim") or len(embedding))
            model_id = str(payload.get("model_id") or "unknown")
            version = str(payload.get("version") or "v1")
            async with self.acquire() as conn:
                contract = await self._validate_vector_dimension_contract(
                    conn,
                    collection_name=collection_name,
                    profile_type=payload.get("profile_type"),
                    model_id=model_id,
                    version=version,
                    vector_dim=vector_dim,
                )
                if contract:
                    metadata.setdefault("dimension_contract_id", dict(contract).get("id"))
                row = await conn.fetchrow(
                    """
                    INSERT INTO vector_profiles (
                        collection_name, entity_type, entity_id, stock_code, profile_type, model_id,
                        vector_dim, metric, version, signature, status, embedding_json, metadata, created_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (collection_name, entity_type, entity_id, model_id, version) DO UPDATE SET
                        stock_code = EXCLUDED.stock_code,
                        profile_type = EXCLUDED.profile_type,
                        vector_dim = EXCLUDED.vector_dim,
                        metric = EXCLUDED.metric,
                        signature = EXCLUDED.signature,
                        status = EXCLUDED.status,
                        embedding_json = EXCLUDED.embedding_json,
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING *
                    """,
                    collection_name,
                    str(payload.get("entity_type") or "generic"),
                    str(payload.get("entity_id") or ""),
                    payload.get("stock_code"),
                    payload.get("profile_type"),
                    model_id,
                    vector_dim,
                    str(payload.get("metric") or "cosine"),
                    version,
                    payload.get("signature"),
                    str(payload.get("status") or "active"),
                    json.dumps(embedding, ensure_ascii=False, default=str),
                    json.dumps(metadata, ensure_ascii=False, default=str),
                )
                if getattr(self, "supports_sqlite_python", lambda: False)():
                    vector_literal = self._encode_sqlite_python(embedding)
                    if vector_literal:
                        await conn.execute(
                            """
                            INSERT INTO vector_profile_store (
                                profile_id, collection_name, entity_type, entity_id, stock_code, profile_type, model_id,
                                vector_dim, metric, version, embedding, metadata, updated_at
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, CURRENT_TIMESTAMP)
                            ON CONFLICT (profile_id) DO UPDATE SET
                                collection_name = EXCLUDED.collection_name,
                                entity_type = EXCLUDED.entity_type,
                                entity_id = EXCLUDED.entity_id,
                                stock_code = EXCLUDED.stock_code,
                                profile_type = EXCLUDED.profile_type,
                                model_id = EXCLUDED.model_id,
                                vector_dim = EXCLUDED.vector_dim,
                                metric = EXCLUDED.metric,
                                version = EXCLUDED.version,
                                embedding = EXCLUDED.embedding,
                                metadata = EXCLUDED.metadata,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            dict(row).get("id"),
                            collection_name,
                            str(payload.get("entity_type") or "generic"),
                            str(payload.get("entity_id") or ""),
                            payload.get("stock_code"),
                            payload.get("profile_type"),
                            model_id,
                            vector_dim,
                            str(payload.get("metric") or "cosine"),
                            version,
                            vector_literal,
                            json.dumps(metadata, ensure_ascii=False, default=str),
                        )
                await self._upsert_vector_graph_for_profile(conn, row=dict(row), profile=payload)
            return self._decode_unified_vector_profile(dict(row))

        async def list_vector_profiles(
            self,
            *,
            collection_name: Optional[str] = None,
            entity_type: Optional[str] = None,
            entity_id: Optional[str] = None,
            entity_ids: Optional[List[str]] = None,
            stock_code: Optional[str] = None,
            stock_codes: Optional[List[str]] = None,
            profile_type: Optional[str] = None,
            version: Optional[str] = None,
            exclude_stock_code: Optional[str] = None,
            exclude_entity_id: Optional[str] = None,
            limit: int = 100,
        ) -> List[dict]:
            async with self.acquire() as conn:
                sql = "SELECT * FROM vector_profiles WHERE 1=1"
                params: list[Any] = []
                idx = 1
                for field, value in (
                    ("collection_name", collection_name),
                    ("entity_type", entity_type),
                    ("entity_id", entity_id),
                    ("stock_code", stock_code),
                    ("profile_type", profile_type),
                    ("version", version),
                ):
                    if value:
                        sql += f" AND {field} = ${idx}"
                        params.append(value)
                        idx += 1
                resolved_stock_codes = [str(item).strip() for item in list(stock_codes or []) if str(item).strip()]
                if resolved_stock_codes:
                    sql += f" AND stock_code IN (${idx})"
                    params.append(resolved_stock_codes)
                    idx += 1
                resolved_entity_ids = [str(item).strip() for item in list(entity_ids or []) if str(item).strip()]
                if resolved_entity_ids:
                    sql += f" AND entity_id IN (${idx})"
                    params.append(resolved_entity_ids)
                    idx += 1
                if exclude_stock_code:
                    sql += f" AND COALESCE(stock_code, '') != ${idx}"
                    params.append(str(exclude_stock_code))
                    idx += 1
                if exclude_entity_id:
                    sql += f" AND entity_id != ${idx}"
                    params.append(str(exclude_entity_id))
                    idx += 1
                sql += f" ORDER BY updated_at DESC, created_at DESC LIMIT ${idx}"
                params.append(max(1, min(int(limit or 100), 100000)))
                rows = await conn.fetch(sql, *params)
            return [self._decode_unified_vector_profile(dict(row)) for row in rows]
