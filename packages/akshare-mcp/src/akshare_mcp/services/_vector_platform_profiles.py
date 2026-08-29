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


class _StrategyVectorPlatformProfilesMixin:
        async def build_strategy_profile(
            self,
            db,
            strategy: dict,
            profile_type: str = 'behavior',
            vector_method: Optional[str] = None,
            metric: str = 'cosine',
            index_name: str = 'strategy_behavior',
            index_version: str = 'v1',
        ) -> Optional[dict]:
            try:
                from strategy_factory.api import build_strategy_panels
                started_at = time.perf_counter()

                resolved_vector_method = self.ensure_vector_method_available(vector_method)
                panels = await build_strategy_panels(
                    strategy.get('strategy_type') or '',
                    strategy.get('params') or {},
                    db,
                    sample_size=4,
                )
                series = panels.get('strategy_returns')
                if series is None or len(series) < 30:
                    return None

                embedding, embedding_meta, effective_vector_method = await self._build_embedding(
                    strategy=strategy,
                    panels=panels,
                    vector_method=resolved_vector_method,
                    profile_type=profile_type,
                    index_name=index_name,
                    index_version=index_version,
                )
                if embedding is None or len(embedding) == 0:
                    return None
                vector_dim = int(len(embedding))
                model_id = self._resolve_strategy_model_id(
                    vector_method=effective_vector_method,
                    vector_dim=vector_dim,
                    embedding_meta=embedding_meta,
                )
                collection_name = self._strategy_collection_name(
                    index_name=index_name,
                    model_id=model_id,
                    vector_dim=vector_dim,
                    metric=metric,
                )
                backend_audit = self._build_backend_audit(db, started_at)
                trace_metadata = dict(strategy.get('_closure_trace') or {})

                profile = await db.save_strategy_vector_profile({
                    'strategy_id': strategy.get('id'),
                    'profile_type': profile_type,
                    'vector_method': effective_vector_method,
                    'metric': metric,
                    'vector_dim': vector_dim,
                    'embedding': embedding.tolist(),
                    'signature': self._signature(strategy, profile_type, effective_vector_method),
                    'backend': self.backend_name(db),
                    'index_name': index_name,
                    'index_version': index_version,
                    'metadata': {
                        'strategy_type': strategy.get('strategy_type'),
                        'index_name': index_name,
                        'index_version': index_version,
                        'profile_type': profile_type,
                        'requested_vector_method': resolved_vector_method,
                        'effective_vector_method': effective_vector_method,
                        'audit': backend_audit,
                        'unified_collection_name': collection_name,
                        'model_id': model_id,
                        'trace': trace_metadata,
                        **embedding_meta,
                    },
                })
                unified_profile = None
                unified_write_error = None
                if hasattr(db, 'save_vector_collection') and hasattr(db, 'save_vector_profile'):
                    try:
                        await db.save_vector_collection({
                            'collection_name': collection_name,
                            'entity_family': 'strategy_behavior',
                            'backend': self.backend_name(db),
                            'metric': metric,
                            'model_id': model_id,
                            'vector_dim': vector_dim,
                            'normalization': 'unit',
                            'status': 'active',
                            'metadata': {
                                'domain': 'strategy',
                                'index_name': index_name,
                                'profile_type': profile_type,
                                'vector_method': effective_vector_method,
                            },
                        })
                        unified_profile = await db.save_vector_profile({
                            'collection_name': collection_name,
                            'entity_type': 'strategy',
                            'entity_id': str(strategy.get('id') or ''),
                            'profile_type': profile_type,
                            'model_id': model_id,
                            'vector_dim': vector_dim,
                            'metric': metric,
                            'version': index_version,
                            'signature': self._signature(strategy, profile_type, effective_vector_method),
                            'status': 'active',
                            'embedding': embedding.tolist(),
                            'metadata': {
                                'strategy_type': strategy.get('strategy_type'),
                                'index_name': index_name,
                                'index_version': index_version,
                                'profile_type': profile_type,
                                'requested_vector_method': resolved_vector_method,
                                'effective_vector_method': effective_vector_method,
                                'legacy_profile_id': profile.get('id'),
                                'audit': backend_audit,
                                **embedding_meta,
                            },
                        })
                        if (
                            getattr(db, 'supports_sqlite_python', lambda: False)()
                            and hasattr(db, 'ensure_vector_profile_sqlite_python_index')
                        ):
                            await db.ensure_vector_profile_sqlite_python_index(
                                collection_name=collection_name,
                                version=index_version,
                                vector_dim=vector_dim,
                                profile_type=profile_type,
                                metric=metric,
                            )
                    except Exception as exc:
                        unified_write_error = exc
                        logger.warning(
                            'StrategyVectorPlatform.build_strategy_profile unified dual-write failed for %s: %s',
                            strategy.get('id'),
                            exc,
                        )
                degraded = unified_write_error is not None
                quality_flags = ['unified_write_failed'] if degraded else []
                await db.save_vector_index_registry({
                    'index_name': index_name,
                    'backend': self.backend_name(db),
                    'status': 'degraded' if degraded else 'active',
                    'profile_type': profile_type,
                    'vector_method': effective_vector_method,
                    'metric': metric,
                    'sample_count': 1,
                    'index_version': index_version,
                    'metadata': {
                        'last_strategy_id': strategy.get('id'),
                        'requested_vector_method': resolved_vector_method,
                        'effective_vector_method': effective_vector_method,
                        'unified_collection_name': collection_name,
                        'unified_profile_id': (unified_profile or {}).get('id'),
                        'model_id': model_id,
                        'audit': backend_audit,
                        'degraded': degraded,
                        'quality_flags': quality_flags,
                        'unified_write_error': (
                            f'{type(unified_write_error).__name__}: {unified_write_error}'
                            if unified_write_error is not None
                            else None
                        ),
                    },
                })
                return {
                    **dict(profile or {}),
                    'unified_collection_name': collection_name,
                    'unified_profile_id': (unified_profile or {}).get('id'),
                    'model_id': model_id,
                    'status': 'degraded' if degraded else 'active',
                    'degraded': degraded,
                    'quality_flags': quality_flags,
                    'quality': {
                        'status': 'degraded' if degraded else 'passed',
                        'checks': {
                            'legacy_write': {'status': 'passed'},
                            'unified_write': {
                                'status': 'degraded' if degraded else 'passed',
                                'error': (
                                    f'{type(unified_write_error).__name__}: {unified_write_error}'
                                    if unified_write_error is not None
                                    else None
                                ),
                            },
                        },
                    },
                }
            except Exception as exc:
                logger.warning('StrategyVectorPlatform.build_strategy_profile failed for %s: %s', strategy.get('id'), exc)
                return None

        async def build_profiles_for_strategies(
            self,
            db,
            strategies: List[dict],
            profile_type: str = 'behavior',
            vector_method: Optional[str] = None,
            index_name: str = 'strategy_behavior',
            index_version: str = 'v1',
        ) -> dict:
            resolved_vector_method = self.ensure_vector_method_available(vector_method)
            sem = asyncio.Semaphore(max(1, min(4, len(list(strategies or [])) or 1)))

            async def _build_one(strategy: dict) -> Optional[dict]:
                async with sem:
                    return await self.build_strategy_profile(
                        db,
                        strategy,
                        profile_type=profile_type,
                        vector_method=resolved_vector_method,
                        index_name=index_name,
                        index_version=index_version,
                    )

            built_profiles = await asyncio.gather(*[_build_one(strategy) for strategy in list(strategies or [])])
            items = [profile for profile in built_profiles if profile and not profile.get('degraded')]
            degraded_items = [profile for profile in built_profiles if profile and profile.get('degraded')]
            failed_count = max(0, len(list(strategies or [])) - len(items) - len(degraded_items))
            if items:
                await db.save_vector_index_registry({
                    'index_name': index_name,
                    'backend': self.backend_name(db),
                    'status': 'degraded' if degraded_items else 'active',
                    'profile_type': profile_type,
                    'vector_method': resolved_vector_method,
                    'metric': 'cosine',
                    'sample_count': len(items),
                    'index_version': index_version,
                    'metadata': {
                        'profile_ids': [item.get('id') for item in items if item.get('id') is not None],
                        'degraded_profile_ids': [item.get('id') for item in degraded_items if item.get('id') is not None],
                        'degraded_count': len(degraded_items),
                        'failed_count': failed_count,
                    },
                })
                if getattr(db, 'supports_sqlite_python', lambda: False)() and hasattr(db, 'ensure_strategy_vector_profile_sqlite_python_index'):
                    dims = sorted({int(item.get('vector_dim') or len(item.get('embedding') or [])) for item in items if int(item.get('vector_dim') or len(item.get('embedding') or [])) > 0})
                    metric = str((items[0] if items else {}).get('metric') or 'cosine')
                    for dim in dims:
                        try:
                            await db.ensure_strategy_vector_profile_sqlite_python_index(
                                index_name=index_name,
                                index_version=index_version,
                                vector_dim=dim,
                                profile_type=profile_type,
                                metric=metric,
                            )
                        except Exception as exc:
                            logger.warning('StrategyVectorPlatform.build_profiles_for_strategies failed to create profile sqlite_python index: %s', exc)
            return {
                'count': len(items),
                'items': items,
                'degraded_count': len(degraded_items),
                'degraded_items': degraded_items,
                'failed_count': failed_count,
                'quality_flags': ['unified_write_failed'] if degraded_items else [],
                'degraded': bool(degraded_items),
            }

        async def _list_unified_strategy_collections(
            self,
            db,
            *,
            index_name: Optional[str] = None,
        ) -> List[dict]:
            if not hasattr(db, 'list_vector_collections'):
                return []
            try:
                rows = await db.list_vector_collections(entity_family='strategy_behavior', limit=200)
            except Exception:
                return []
            resolved_index_name = str(index_name or '').strip()
            filtered: List[dict] = []
            for row in list(rows or []):
                item = dict(row or {})
                collection_name = str(item.get('collection_name') or '').strip()
                logical_index_name = self._strategy_index_name_from_collection(collection_name, item)
                if resolved_index_name and logical_index_name != resolved_index_name:
                    continue
                filtered.append(
                    {
                        **item,
                        'collection_name': collection_name,
                        'index_name': logical_index_name,
                    }
                )
            filtered.sort(key=self._collection_sort_key)
            return filtered

        async def _load_unified_query_profile(
            self,
            db,
            strategy_id: str,
            *,
            profile_type: str = 'behavior',
            preferred_version: Optional[str] = None,
            index_name: Optional[str] = None,
        ) -> Optional[dict]:
            if not hasattr(db, 'list_vector_profiles'):
                return None
            collections = await self._list_unified_strategy_collections(db, index_name=index_name)
            for collection in collections:
                version_candidates: List[Optional[str]] = []
                explicit_version = str(preferred_version or '').strip() or None
                active_version = str(collection.get('active_version') or '').strip() or None
                if explicit_version:
                    version_candidates.append(explicit_version)
                if active_version and active_version not in version_candidates:
                    version_candidates.append(active_version)
                version_candidates.append(None)
                seen_versions: set[Optional[str]] = set()
                for version in version_candidates:
                    if version in seen_versions:
                        continue
                    seen_versions.add(version)
                    try:
                        rows = await db.list_vector_profiles(
                            collection_name=collection.get('collection_name'),
                            entity_type='strategy',
                            entity_id=strategy_id,
                            profile_type=profile_type,
                            version=version,
                            limit=5,
                        )
                    except Exception:
                        rows = []
                    if rows:
                        return {
                            **dict(rows[0] or {}),
                            'collection_name': collection.get('collection_name'),
                            '_collection': dict(collection),
                        }
            return None

        async def _select_primary_unified_collection(
            self,
            db,
            *,
            index_name: str,
            profile_type: str,
            version: Optional[str],
        ) -> tuple[Optional[dict], int]:
            if not hasattr(db, 'list_vector_profiles'):
                return None, 0
            collections = await self._list_unified_strategy_collections(db, index_name=index_name)
            best_collection: Optional[dict] = None
            best_count = 0
            for collection in collections:
                try:
                    rows = await db.list_vector_profiles(
                        collection_name=collection.get('collection_name'),
                        entity_type='strategy',
                        profile_type=profile_type,
                        version=version,
                        limit=5000,
                    )
                except Exception:
                    rows = []
                count = len(list(rows or []))
                if count > best_count:
                    best_collection = dict(collection)
                    best_count = count
            return best_collection, best_count

        def _map_unified_profile_row(
            self,
            row: dict,
            collection: dict,
            *,
            index_name: Optional[str] = None,
        ) -> dict:
            metadata = dict((row or {}).get('metadata') or {})
            resolved_index_name = str(
                index_name
                or metadata.get('index_name')
                or collection.get('index_name')
                or self._strategy_index_name_from_collection(collection.get('collection_name'), collection)
            )
            return {
                'id': row.get('id'),
                'profile_id': row.get('id'),
                'strategy_id': row.get('entity_id'),
                'profile_type': row.get('profile_type') or metadata.get('profile_type'),
                'vector_method': metadata.get('effective_vector_method') or metadata.get('vector_method'),
                'metric': row.get('metric') or collection.get('metric') or 'cosine',
                'vector_dim': row.get('vector_dim'),
                'embedding': row.get('embedding'),
                'signature': row.get('signature') or metadata.get('signature'),
                'backend': self._sqlite_python_backend_family(collection.get('backend')),
                'index_name': resolved_index_name,
                'index_version': str(row.get('version') or metadata.get('index_version') or ''),
                'collection_name': collection.get('collection_name') or row.get('collection_name'),
                'model_id': row.get('model_id') or collection.get('model_id'),
                'metadata': metadata,
                'source': 'unified_profile',
                'source_of_truth': 'unified_vector_tables',
                'table_family': 'unified_vector_tables',
                'legacy_only': False,
            }

        def _map_unified_snapshot_row(
            self,
            snapshot: dict,
            collection: dict,
            *,
            index_name: Optional[str] = None,
        ) -> dict:
            resolved_index_name = str(
                index_name
                or collection.get('index_name')
                or self._strategy_index_name_from_collection(collection.get('collection_name'), collection)
            )
            metadata = dict((snapshot or {}).get('metadata') or {})
            return {
                'id': snapshot.get('id'),
                'index_name': resolved_index_name,
                'index_version': str(snapshot.get('index_version') or ''),
                'collection_name': collection.get('collection_name'),
                'status': snapshot.get('status'),
                'backend': self._sqlite_python_backend_family(collection.get('backend') or metadata.get('backend_used')),
                'profile_type': snapshot.get('profile_type'),
                'vector_method': metadata.get('vector_method'),
                'metric': snapshot.get('metric') or collection.get('metric') or 'cosine',
                'vector_dim': snapshot.get('vector_dim') or collection.get('vector_dim'),
                'profile_count': int(snapshot.get('sample_count') or snapshot.get('profile_count') or 0),
                'bucket_count': int(snapshot.get('bucket_count') or 0),
                'model_id': snapshot.get('model_id') or collection.get('model_id'),
                'built_at': snapshot.get('built_at'),
                'activated_at': snapshot.get('activated_at'),
                'metadata': metadata,
                'source': 'unified_snapshot',
                'source_of_truth': 'unified_vector_tables',
                'table_family': 'unified_vector_tables',
                'legacy_only': False,
            }

        def _map_legacy_profile_row(
            self,
            row: dict,
            *,
            index_name: Optional[str] = None,
        ) -> dict:
            payload = dict(row or {})
            metadata = dict(payload.get('metadata') or {})
            resolved_index_name = str(index_name or payload.get('index_name') or metadata.get('index_name') or 'strategy_behavior')
            resolved_index_version = str(payload.get('index_version') or metadata.get('index_version') or '')
            return {
                **payload,
                'index_name': resolved_index_name,
                'index_version': resolved_index_version,
                'source': payload.get('source') or 'legacy_profile',
                'source_of_truth': 'legacy_strategy_vector_tables',
                'table_family': 'legacy_strategy_vector_tables',
                'legacy_only': True,
            }

        def _map_legacy_snapshot_row(
            self,
            row: dict,
            *,
            index_name: Optional[str] = None,
        ) -> dict:
            payload = dict(row or {})
            metadata = dict(payload.get('metadata') or {})
            resolved_index_name = str(index_name or payload.get('index_name') or metadata.get('index_name') or 'strategy_behavior')
            return {
                **payload,
                'index_name': resolved_index_name,
                'source': payload.get('source') or 'legacy_snapshot',
                'source_of_truth': 'legacy_strategy_vector_tables',
                'table_family': 'legacy_strategy_vector_tables',
                'legacy_only': True,
            }

        async def list_profiles(
            self,
            db,
            *,
            strategy_id: Optional[str] = None,
            profile_type: Optional[str] = None,
            index_name: Optional[str] = None,
            index_version: Optional[str] = None,
            limit: int = 20,
        ) -> List[dict]:
            resolved_limit = max(1, min(int(limit or 20), 200))
            unified_rows: List[dict] = []
            if hasattr(db, 'list_vector_profiles'):
                collections = await self._list_unified_strategy_collections(db, index_name=index_name)
                for collection in collections:
                    try:
                        rows = await db.list_vector_profiles(
                            collection_name=collection.get('collection_name'),
                            entity_type='strategy',
                            entity_id=strategy_id,
                            profile_type=profile_type,
                            version=index_version,
                            limit=resolved_limit,
                        )
                    except Exception:
                        rows = []
                    for row in rows:
                        unified_rows.append(self._map_unified_profile_row(dict(row or {}), dict(collection), index_name=index_name))
            if unified_rows:
                return unified_rows[:resolved_limit]
            if not hasattr(db, 'list_strategy_vector_profiles'):
                return []
            rows = await db.list_strategy_vector_profiles(
                strategy_id=strategy_id,
                profile_type=profile_type,
                index_name=index_name,
                index_version=index_version,
                limit=resolved_limit,
            )
            return [
                self._map_legacy_profile_row(dict(row or {}), index_name=index_name)
                for row in list(rows or [])[:resolved_limit]
            ]

        async def list_index_snapshots(
            self,
            db,
            *,
            index_name: Optional[str] = None,
            index_version: Optional[str] = None,
            status: Optional[str] = None,
            limit: int = 20,
        ) -> List[dict]:
            resolved_limit = max(1, min(int(limit or 20), 200))
            unified_rows: List[dict] = []
            if hasattr(db, 'list_vector_index_snapshots'):
                collections = await self._list_unified_strategy_collections(db, index_name=index_name)
                for collection in collections:
                    try:
                        rows = await db.list_vector_index_snapshots(
                            collection_name=collection.get('collection_name'),
                            index_version=index_version,
                            status=status,
                            latest_only=False,
                            limit=resolved_limit,
                        )
                    except Exception:
                        rows = []
                    for row in rows:
                        unified_rows.append(self._map_unified_snapshot_row(dict(row or {}), dict(collection), index_name=index_name))
            if unified_rows:
                unified_rows.sort(
                    key=lambda row: (
                        str(row.get('activated_at') or row.get('built_at') or ''),
                        str(row.get('index_version') or ''),
                        int(row.get('id') or 0),
                    ),
                    reverse=True,
                )
                return unified_rows[:resolved_limit]
            if not hasattr(db, 'list_strategy_vector_index_snapshots'):
                return []
            rows = await db.list_strategy_vector_index_snapshots(
                index_name=index_name,
                index_version=index_version,
                status=status,
                limit=resolved_limit,
            )
            return [
                self._map_legacy_snapshot_row(dict(row or {}), index_name=index_name)
                for row in list(rows or [])[:resolved_limit]
            ]

        async def _load_query_profile(
            self,
            db,
            strategy_id: str,
            profile_type: str = 'behavior',
            preferred_version: Optional[str] = None,
        ) -> Optional[dict]:
            if not hasattr(db, 'list_strategy_vector_profiles'):
                return None
            rows = await db.list_strategy_vector_profiles(
                strategy_id=strategy_id,
                profile_type=profile_type,
                index_version=preferred_version,
                limit=20,
            )
            if rows:
                return rows[0]
            if preferred_version:
                fallback = await db.list_strategy_vector_profiles(
                    strategy_id=strategy_id,
                    profile_type=profile_type,
                    limit=20,
                )
                if fallback:
                    return fallback[0]
            return None
