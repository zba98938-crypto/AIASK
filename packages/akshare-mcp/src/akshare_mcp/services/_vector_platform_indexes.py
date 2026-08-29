"""策略向量平台：统一画像、持久化索引和 ANN-like 相似检索。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

from .text_embedding import get_strategy_text_embedding_service
from .vector_search import VectorSearchEngine

logger = logging.getLogger(__name__)


class _StrategyVectorPlatformIndexesMixin:
        def _build_ann_layout(self, profiles: List[dict]) -> tuple[dict, List[dict]]:
            valid: List[tuple[dict, np.ndarray]] = []
            skipped: List[dict] = []
            for profile in profiles:
                embedding = self._normalize_embedding(profile.get('embedding'))
                if len(embedding) == 0:
                    skipped.append({'profile_id': profile.get('id'), 'strategy_id': profile.get('strategy_id'), 'reason': 'empty_embedding'})
                    continue
                valid.append((profile, embedding))
            if not valid:
                return {
                    'profile_count': 0,
                    'bucket_count': 0,
                    'vector_dim': 0,
                    'centroids': [],
                    'metadata': {'skipped_profiles': skipped},
                }, []

            dim_counts: dict[int, int] = {}
            for _, embedding in valid:
                dim_counts[len(embedding)] = dim_counts.get(len(embedding), 0) + 1
            dominant_dim = max(dim_counts.items(), key=lambda item: (item[1], item[0]))[0]
            selected = [(profile, embedding) for profile, embedding in valid if len(embedding) == dominant_dim]
            for profile, embedding in valid:
                if len(embedding) != dominant_dim:
                    skipped.append({'profile_id': profile.get('id'), 'strategy_id': profile.get('strategy_id'), 'reason': f'dim_mismatch:{len(embedding)}'})
            if not selected:
                return {
                    'profile_count': 0,
                    'bucket_count': 0,
                    'vector_dim': dominant_dim,
                    'centroids': [],
                    'metadata': {'skipped_profiles': skipped},
                }, []

            ordered = sorted(selected, key=lambda item: f"{item[0].get('strategy_id') or ''}:{item[0].get('id') or 0}")
            vectors = np.vstack([embedding for _, embedding in ordered])
            bucket_count = self._resolve_bucket_count(len(ordered))
            if bucket_count == 1:
                centroids = [self._normalize_embedding(np.mean(vectors, axis=0).tolist())]
            else:
                initial = np.linspace(0, len(ordered) - 1, num=bucket_count, dtype=int)
                centroids = [vectors[idx].copy() for idx in initial.tolist()]
                for _ in range(12):
                    assignments: List[List[int]] = [[] for _ in range(bucket_count)]
                    for row_idx, vector in enumerate(vectors):
                        sims = [self.engine.calculate_similarity(vector, centroid, 'cosine') for centroid in centroids]
                        best_idx = int(max(range(bucket_count), key=lambda idx: sims[idx]))
                        assignments[best_idx].append(row_idx)
                    updated: List[np.ndarray] = []
                    for centroid_idx, members in enumerate(assignments):
                        if not members:
                            updated.append(centroids[centroid_idx])
                            continue
                        new_centroid = self._normalize_embedding(np.mean(vectors[members], axis=0).tolist())
                        updated.append(new_centroid if len(new_centroid) else centroids[centroid_idx])
                    max_shift = max(
                        float(np.linalg.norm(updated[idx] - centroids[idx]))
                        for idx in range(bucket_count)
                    ) if bucket_count else 0.0
                    centroids = updated
                    if max_shift <= 1e-4:
                        break

            bucket_members: List[List[int]] = [[] for _ in range(bucket_count)]
            assignments_meta: List[tuple[int, float]] = []
            for row_idx, vector in enumerate(vectors):
                sims = [self.engine.calculate_similarity(vector, centroid, 'cosine') for centroid in centroids]
                best_idx = int(max(range(bucket_count), key=lambda idx: sims[idx]))
                best_score = float(sims[best_idx])
                bucket_members[best_idx].append(row_idx)
                assignments_meta.append((best_idx, best_score))

            centroid_rows = []
            for centroid_idx, centroid in enumerate(centroids):
                neighbors = []
                if bucket_count > 1:
                    sims = []
                    for other_idx, other_centroid in enumerate(centroids):
                        if other_idx == centroid_idx:
                            continue
                        sims.append((other_idx, float(self.engine.calculate_similarity(centroid, other_centroid, 'cosine'))))
                    sims.sort(key=lambda item: item[1], reverse=True)
                    neighbors = [self._bucket_label(item[0]) for item in sims[: min(2, len(sims))]]
                centroid_rows.append({
                    'bucket_id': self._bucket_label(centroid_idx),
                    'centroid': np.round(centroid, 8).tolist(),
                    'size': len(bucket_members[centroid_idx]),
                    'neighbors': neighbors,
                    'mean_similarity': round(
                        float(np.mean([assignments_meta[row_idx][1] for row_idx in bucket_members[centroid_idx]]) if bucket_members[centroid_idx] else 0.0),
                        6,
                    ),
                })

            items: List[dict] = []
            for row_idx, (profile, vector) in enumerate(ordered):
                bucket_idx, coarse_score = assignments_meta[row_idx]
                items.append({
                    'profile_id': profile.get('id'),
                    'strategy_id': profile.get('strategy_id'),
                    'profile_type': profile.get('profile_type') or 'behavior',
                    'vector_method': profile.get('vector_method') or 'price_volume',
                    'metric': profile.get('metric') or 'cosine',
                    'vector_dim': len(vector),
                    'bucket_id': self._bucket_label(bucket_idx),
                    'coarse_score': round(float(coarse_score), 6),
                    'embedding': np.round(vector, 8).tolist(),
                    'metadata': {
                        'signature': profile.get('signature'),
                        'backend': profile.get('backend') or self.engine.backend,
                        'source_profile_id': profile.get('id'),
                    },
                })

            return {
                'profile_count': len(items),
                'bucket_count': bucket_count,
                'vector_dim': dominant_dim,
                'centroids': centroid_rows,
                'metadata': {
                    'skipped_profiles': skipped,
                    'dominant_dim': dominant_dim,
                    'cluster_sizes': {self._bucket_label(idx): len(members) for idx, members in enumerate(bucket_members)},
                },
            }, items

        async def build_persisted_ann_index(
            self,
            db,
            index_name: str = 'strategy_behavior',
            index_version: str = 'v1',
            profile_type: str = 'behavior',
            task_run_id: Optional[int] = None,
            source: str = 'vector_governance',
            limit_profiles: int = 5000,
        ) -> dict:
            started_at = time.perf_counter()
            if not hasattr(db, 'list_strategy_vector_profiles'):
                return {'snapshot': None, 'items_count': 0, 'bucket_count': 0, 'unified_snapshot': None}
            profiles = await db.list_strategy_vector_profiles(
                profile_type=profile_type,
                index_version=index_version,
                limit=max(1, min(int(limit_profiles or 5000), 10000)),
            )
            layout, items = self._build_ann_layout(profiles)
            hnsw_index_params = self._hnsw_index_params()
            index_metrics = {
                'build_time_ms': round((time.perf_counter() - started_at) * 1000, 3),
                'profile_count': int(layout.get('profile_count') or len(items)),
                'index_item_count': len(items),
                'bucket_count': int(layout.get('bucket_count') or 0),
            }
            now = datetime.now(timezone.utc).isoformat()
            snapshot = None
            if hasattr(db, 'save_strategy_vector_index_snapshot'):
                snapshot = await db.save_strategy_vector_index_snapshot({
                    'index_name': index_name,
                    'index_version': index_version,
                    'status': 'building' if items else 'empty',
                    'profile_type': profile_type,
                    'vector_method': str((profiles[0] if profiles else {}).get('vector_method') or 'price_volume'),
                    'metric': str((profiles[0] if profiles else {}).get('metric') or 'cosine'),
                    'backend': self.backend_name(db),
                    'profile_count': int(layout.get('profile_count') or len(items)),
                    'bucket_count': int(layout.get('bucket_count') or 0),
                    'vector_dim': int(layout.get('vector_dim') or 0),
                    'centroids': layout.get('centroids') or [],
                    'index_params': hnsw_index_params,
                    'metrics': index_metrics,
                    'metadata': {
                        **dict(layout.get('metadata') or {}),
                        'index_name': index_name,
                        'index_version': index_version,
                        'source': source,
                        'task_run_id': task_run_id,
                    },
                    'task_run_id': task_run_id,
                    'source': source,
                    'built_at': now,
                    'activated_at': None,
                })
            try:
                if hasattr(db, 'replace_strategy_vector_index_items'):
                    await db.replace_strategy_vector_index_items(index_name, index_version, items)
                if items and getattr(db, 'supports_sqlite_python', lambda: False)() and hasattr(db, 'ensure_strategy_vector_index_item_sqlite_python_index'):
                    try:
                        ensure_kwargs = {
                            'index_name': index_name,
                            'index_version': index_version,
                            'vector_dim': int(layout.get('vector_dim') or 0),
                            'metric': str((profiles[0] if profiles else {}).get('metric') or 'cosine'),
                        }
                        if hnsw_index_params:
                            ensure_kwargs['index_params'] = hnsw_index_params
                        try:
                            await db.ensure_strategy_vector_index_item_sqlite_python_index(**ensure_kwargs)
                        except TypeError as exc:
                            if 'index_params' not in str(exc):
                                raise
                            ensure_kwargs.pop('index_params', None)
                            await db.ensure_strategy_vector_index_item_sqlite_python_index(**ensure_kwargs)
                    except Exception as exc:
                        logger.warning('StrategyVectorPlatform.build_persisted_ann_index failed to create sqlite_python index: %s', exc)
                if hasattr(db, 'save_strategy_vector_index_snapshot'):
                    snapshot = await db.save_strategy_vector_index_snapshot({
                        'index_name': index_name,
                        'index_version': index_version,
                        'status': 'active' if items else 'empty',
                        'profile_type': profile_type,
                        'vector_method': str((profiles[0] if profiles else {}).get('vector_method') or 'price_volume'),
                        'metric': str((profiles[0] if profiles else {}).get('metric') or 'cosine'),
                        'backend': self.backend_name(db),
                        'profile_count': int(layout.get('profile_count') or len(items)),
                        'bucket_count': int(layout.get('bucket_count') or 0),
                        'vector_dim': int(layout.get('vector_dim') or 0),
                        'centroids': layout.get('centroids') or [],
                        'index_params': hnsw_index_params,
                        'metrics': {
                            **index_metrics,
                            'build_time_ms': round((time.perf_counter() - started_at) * 1000, 3),
                        },
                        'metadata': {
                            **dict(layout.get('metadata') or {}),
                            'index_name': index_name,
                            'index_version': index_version,
                            'source': source,
                            'task_run_id': task_run_id,
                        },
                        'task_run_id': task_run_id,
                        'source': source,
                        'built_at': now,
                        'activated_at': datetime.now(timezone.utc).isoformat(),
                    })
            except Exception as exc:
                if hasattr(db, 'save_strategy_vector_index_snapshot'):
                    snapshot = await db.save_strategy_vector_index_snapshot({
                        'index_name': index_name,
                        'index_version': index_version,
                        'status': 'failed',
                        'profile_type': profile_type,
                        'vector_method': str((profiles[0] if profiles else {}).get('vector_method') or 'price_volume'),
                        'metric': str((profiles[0] if profiles else {}).get('metric') or 'cosine'),
                        'backend': self.backend_name(db),
                        'profile_count': int(layout.get('profile_count') or len(items)),
                        'bucket_count': int(layout.get('bucket_count') or 0),
                        'vector_dim': int(layout.get('vector_dim') or 0),
                        'centroids': layout.get('centroids') or [],
                        'index_params': hnsw_index_params,
                        'metrics': {
                            **index_metrics,
                            'build_time_ms': round((time.perf_counter() - started_at) * 1000, 3),
                        },
                        'metadata': {
                            **dict(layout.get('metadata') or {}),
                            'index_name': index_name,
                            'index_version': index_version,
                            'source': source,
                            'task_run_id': task_run_id,
                            'error': str(exc),
                        },
                        'task_run_id': task_run_id,
                        'source': source,
                        'built_at': now,
                        'activated_at': None,
                    })
                raise
            unified_snapshot = None
            unified_collection_name = None
            if hasattr(db, 'save_vector_index_snapshot') and hasattr(db, 'list_vector_profiles'):
                try:
                    from .unified_vector_governance import build_vector_collection_snapshot

                    primary_collection, _ = await self._select_primary_unified_collection(
                        db,
                        index_name=index_name,
                        profile_type=profile_type,
                        version=index_version,
                    )
                    unified_collection_name = str((primary_collection or {}).get('collection_name') or '').strip() or None
                    if unified_collection_name:
                        unified_snapshot = await build_vector_collection_snapshot(
                            db,
                            collection_name=unified_collection_name,
                            version=index_version,
                            index_version=index_version,
                            profile_type=profile_type,
                            activate=True,
                            source=source,
                        )
                except Exception as exc:
                    logger.warning('StrategyVectorPlatform.build_persisted_ann_index unified snapshot build failed: %s', exc)
            return {
                'snapshot': snapshot,
                'items_count': len(items),
                'bucket_count': int(layout.get('bucket_count') or 0),
                'profile_count': int(layout.get('profile_count') or len(items)),
                'unified_snapshot': unified_snapshot,
                'unified_collection_name': unified_collection_name,
                'degraded': bool((unified_snapshot or {}).get('degraded')),
                'quality_flags': list((unified_snapshot or {}).get('quality_flags') or []),
                'qa': (unified_snapshot or {}).get('qa'),
            }

        async def _get_latest_snapshot(self, db, index_name: str, index_version: Optional[str] = None) -> Optional[dict]:
            if not hasattr(db, 'list_strategy_vector_index_snapshots'):
                return None
            if index_version:
                rows = await db.list_strategy_vector_index_snapshots(index_name=index_name, index_version=index_version, limit=1)
                return rows[0] if rows else None
            if hasattr(db, 'get_latest_strategy_vector_index_snapshot'):
                return await db.get_latest_strategy_vector_index_snapshot(index_name=index_name)
            rows = await db.list_strategy_vector_index_snapshots(index_name=index_name, limit=1)
            return rows[0] if rows else None

        @staticmethod
        def _snapshot_bucket_map(snapshot: Optional[dict]) -> dict[str, dict]:
            centroids = list((snapshot or {}).get('centroids') or [])
            return {str(item.get('bucket_id')): dict(item) for item in centroids if item.get('bucket_id')}

        def _resolve_query_bucket(self, snapshot: dict, query_embedding: np.ndarray) -> tuple[Optional[str], List[str]]:
            bucket_map = self._snapshot_bucket_map(snapshot)
            if not bucket_map:
                return None, []
            scored = []
            for bucket_id, item in bucket_map.items():
                centroid = self._normalize_embedding(item.get('centroid'))
                if len(centroid) != len(query_embedding) or len(centroid) == 0:
                    continue
                scored.append((bucket_id, float(self.engine.calculate_similarity(query_embedding, centroid, 'cosine'))))
            if not scored:
                return None, []
            scored.sort(key=lambda item: item[1], reverse=True)
            primary = scored[0][0]
            bucket_rows = [primary]
            neighbors = bucket_map.get(primary, {}).get('neighbors') or []
            for item in neighbors[:2]:
                if item not in bucket_rows:
                    bucket_rows.append(item)
            return primary, bucket_rows

        async def archive_profile(
            self,
            db,
            strategy: dict,
            profile_type: str = 'behavior',
            vector_method: Optional[str] = None,
            metric: str = 'cosine',
            index_name: str = 'strategy_behavior',
            index_version: str = 'v1',
        ) -> Optional[dict]:
            return await self.build_strategy_profile(
                db,
                strategy,
                profile_type=profile_type,
                vector_method=vector_method,
                metric=metric,
                index_name=index_name,
                index_version=index_version,
            )

        async def get_active_index(self, db, index_name: str = 'strategy_behavior', index_version: Optional[str] = None) -> Optional[dict]:
            collections = await self._list_unified_strategy_collections(db, index_name=index_name)
            if collections and hasattr(db, 'list_vector_index_snapshots'):
                resolved_index_version = str(index_version or '').strip() or None
                for collection in collections:
                    if resolved_index_version:
                        snapshots = await db.list_vector_index_snapshots(
                            collection_name=collection.get('collection_name'),
                            index_version=resolved_index_version,
                            latest_only=True,
                            limit=1,
                        )
                    else:
                        active_version = str(collection.get('active_version') or '').strip() or None
                        snapshots = await db.list_vector_index_snapshots(
                            collection_name=collection.get('collection_name'),
                            index_version=active_version,
                            latest_only=True,
                            limit=1,
                        ) if active_version else []
                        if not snapshots:
                            snapshots = await db.list_vector_index_snapshots(
                                collection_name=collection.get('collection_name'),
                                latest_only=True,
                                limit=1,
                            )
                    if snapshots:
                        snapshot = dict(snapshots[0] or {})
                        mapped = self._map_unified_snapshot_row(snapshot, dict(collection), index_name=index_name)
                        return {
                            'index_name': mapped.get('index_name') or str(index_name or 'strategy_behavior'),
                            'index_version': mapped.get('index_version') or str(index_version or ''),
                            'backend': mapped.get('backend') or self.backend_name(db),
                            'status': mapped.get('status') or 'active',
                            'source': mapped.get('source') or 'unified_snapshot',
                            'collection_name': mapped.get('collection_name'),
                            'model_id': mapped.get('model_id'),
                            'vector_dim': mapped.get('vector_dim'),
                            'source_of_truth': 'unified_vector_tables',
                            'table_family': 'unified_vector_tables',
                            'legacy_only': False,
                        }
            snapshot = await self._get_latest_snapshot(db, index_name, index_version=index_version)
            if snapshot:
                return {
                    'index_name': str(snapshot.get('index_name') or index_name),
                    'index_version': str(snapshot.get('index_version') or index_version or ''),
                    'backend': str(snapshot.get('backend') or self.backend_name(db)),
                    'status': str(snapshot.get('status') or 'active'),
                    'source': 'snapshot',
                    'source_of_truth': 'legacy_strategy_vector_tables',
                    'table_family': 'legacy_strategy_vector_tables',
                    'legacy_only': True,
                }
            if hasattr(db, 'list_vector_index_registry'):
                rows = await db.list_vector_index_registry(index_name=index_name, status='active', limit=20)
                if index_version:
                    rows = [row for row in rows if str(row.get('index_version') or '') == str(index_version)]
                if rows:
                    row = rows[0]
                    return {
                        'index_name': str(row.get('index_name') or index_name),
                        'index_version': str(row.get('index_version') or index_version or ''),
                        'backend': str(row.get('backend') or self.backend_name(db)),
                        'status': str(row.get('status') or 'active'),
                        'source': 'registry',
                        'source_of_truth': 'legacy_strategy_vector_tables',
                        'table_family': 'legacy_strategy_vector_tables',
                        'legacy_only': True,
                    }
            return None

        async def _build_unified_health(
            self,
            db,
            *,
            index_name: str,
            limit_versions: int,
            include_hnsw_indexes: bool,
        ) -> Optional[dict]:
            collections = await self._list_unified_strategy_collections(db, index_name=index_name)
            if not collections or not hasattr(db, 'list_vector_index_snapshots'):
                return None
            zero_counts = {
                'profiles': 0,
                'profile_store': 0,
                'index_snapshots': 0,
                'index_items': 0,
                'index_item_store': 0,
            }
            legacy_counts = dict(zero_counts)
            if hasattr(db, 'get_strategy_vector_health'):
                try:
                    legacy_health = await db.get_strategy_vector_health(
                        index_name=index_name,
                        limit_versions=1,
                        include_hnsw_indexes=False,
                    )
                    legacy_counts.update(dict((legacy_health or {}).get('counts') or {}))
                except Exception:
                    legacy_counts = dict(zero_counts)
            counts = {
                'profiles': 0,
                'profile_store': 0,
                'index_snapshots': 0,
                'index_items': 0,
                'index_item_store': 0,
            }
            versions: List[dict] = []
            latest_snapshot: Optional[dict] = None
            for collection in collections:
                collection_name = collection.get('collection_name')
                try:
                    profiles = await db.list_vector_profiles(
                        collection_name=collection_name,
                        entity_type='strategy',
                        limit=5000,
                    ) if hasattr(db, 'list_vector_profiles') else []
                except Exception:
                    profiles = []
                counts['profiles'] += len(profiles)
                if getattr(db, 'supports_sqlite_python', lambda: False)():
                    counts['profile_store'] += len(profiles)
                snapshots = await db.list_vector_index_snapshots(
                    collection_name=collection_name,
                    latest_only=False,
                    limit=max(20, min(int(limit_versions or 20) * 4, 500)),
                )
                counts['index_snapshots'] += len(snapshots)
                for snapshot in snapshots:
                    mapped = self._map_unified_snapshot_row(dict(snapshot or {}), dict(collection), index_name=index_name)
                    try:
                        items = await db.list_vector_index_items(
                            collection_name=collection_name,
                            index_version=mapped.get('index_version'),
                            profile_type=mapped.get('profile_type'),
                            limit=5000,
                        ) if hasattr(db, 'list_vector_index_items') else []
                    except Exception:
                        items = []
                    item_count = len(items)
                    counts['index_items'] += item_count
                    if getattr(db, 'supports_sqlite_python', lambda: False)():
                        counts['index_item_store'] += item_count
                    versions.append({
                        'collection_name': mapped.get('collection_name'),
                        'index_version': mapped.get('index_version'),
                        'registry_status': mapped.get('status'),
                        'registry_backend': mapped.get('backend'),
                        'snapshot_status': mapped.get('status'),
                        'snapshot_backend': mapped.get('backend'),
                        'profile_count': mapped.get('profile_count'),
                        'bucket_count': mapped.get('bucket_count'),
                        'vector_dim': mapped.get('vector_dim'),
                        'model_id': mapped.get('model_id'),
                        'profile_rows': sum(
                            1
                            for row in profiles
                            if str((row or {}).get('version') or '') == str(mapped.get('index_version') or '')
                        ),
                        'profile_store_rows': sum(
                            1
                            for row in profiles
                            if str((row or {}).get('version') or '') == str(mapped.get('index_version') or '')
                        ) if getattr(db, 'supports_sqlite_python', lambda: False)() else 0,
                        'index_item_rows': item_count,
                        'index_item_store_rows': item_count if getattr(db, 'supports_sqlite_python', lambda: False)() else 0,
                        'last_seen': str(mapped.get('activated_at') or mapped.get('built_at') or ''),
                    })
                    candidate_latest_key = str(mapped.get('activated_at') or mapped.get('built_at') or '')
                    current_latest_key = str((latest_snapshot or {}).get('activated_at') or (latest_snapshot or {}).get('built_at') or '')
                    if candidate_latest_key >= current_latest_key:
                        latest_snapshot = dict(mapped)
            versions.sort(
                key=lambda row: (
                    str(row.get('last_seen') or ''),
                    str(row.get('collection_name') or ''),
                    str(row.get('index_version') or ''),
                ),
                reverse=True,
            )
            hnsw_indexes: List[dict] = []
            if include_hnsw_indexes and hasattr(db, 'list_vector_hnsw_indexes'):
                seen_index_names: set[str] = set()
                for collection in collections:
                    try:
                        rows = await db.list_vector_hnsw_indexes(
                            collection_name=collection.get('collection_name'),
                            limit=500,
                        )
                    except Exception:
                        rows = []
                    for row in list(rows or []):
                        item = dict(row or {})
                        index_key = str(item.get('indexname') or '')
                        if index_key and index_key in seen_index_names:
                            continue
                        if index_key:
                            seen_index_names.add(index_key)
                        hnsw_indexes.append(item)
            return {
                'index_name': index_name,
                'backend': self._sqlite_python_backend_family(getattr(db, 'get_vector_backend', lambda: 'sqlite_python')()),
                'sqlite_python_enabled': getattr(db, 'supports_sqlite_python', lambda: False)(),
                'tables': {
                    'vector_collections': True,
                    'vector_profiles': True,
                    'vector_profile_store': getattr(db, 'supports_sqlite_python', lambda: False)(),
                    'vector_index_snapshots': True,
                    'vector_index_items': True,
                    'vector_index_item_store': getattr(db, 'supports_sqlite_python', lambda: False)(),
                },
                'counts': counts,
                'unified_counts': dict(counts),
                'legacy_counts': legacy_counts,
                'source_of_truth': 'unified_vector_tables',
                'cleanup_scope': 'unified',
                'table_family': 'unified_vector_tables',
                'legacy_only': False,
                'unified_available': True,
                'legacy_available': hasattr(db, 'get_strategy_vector_health'),
                'collection_count': len(collections),
                'collections': [
                    {
                        'collection_name': item.get('collection_name'),
                        'active_version': item.get('active_version'),
                        'metric': item.get('metric'),
                        'model_id': item.get('model_id'),
                        'vector_dim': item.get('vector_dim'),
                        'status': item.get('status'),
                    }
                    for item in collections
                ],
                'latest_snapshot': latest_snapshot,
                'versions': versions[: max(1, min(int(limit_versions or 20), 200))],
                'hnsw_indexes': hnsw_indexes,
                'hnsw_index_count': len(hnsw_indexes),
                'recommended_cleanup_versions': [row.get('index_version') for row in versions[1:] if row.get('index_version')],
                'health_mode': 'unified',
            }
