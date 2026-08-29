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


class _StrategyVectorPlatformSearchMixin:
        async def search_similar(
            self,
            db,
            strategy_id: str,
            profile_type: str = 'behavior',
            limit: int = 5,
            candidate_limit: int = 80,
            index_name: Optional[str] = None,
            index_version: Optional[str] = None,
        ) -> dict:
            started_at = time.perf_counter()
            requested_backend = self.requested_backend_name(db)
            resolved_index_name = str(index_name or 'strategy_behavior')
            active_index = await self.get_active_index(db, resolved_index_name, index_version=index_version)
            rows = await self.ann_search_profiles(
                db,
                strategy_id,
                profile_type=profile_type,
                limit=limit,
                candidate_limit=candidate_limit,
                index_name=index_name,
                index_version=index_version,
            )
            first = rows[0] if rows else {}
            fallback_used = False
            fallback_reason = None
            if rows:
                fallback_used = bool(first.get('search_fallback_used'))
                fallback_reason = first.get('search_fallback_reason')
            if not rows and self.allow_fallback:
                rows = await self.find_similar_profiles(
                    db,
                    strategy_id,
                    profile_type=profile_type,
                    limit=limit,
                    index_name=index_name,
                    index_version=index_version,
                )
                if rows:
                    fallback_used = True
                    fallback_reason = 'ann_empty_result'
            first = rows[0] if rows else {}
            resolved_index_name = str(
                first.get('index_name')
                or (active_index or {}).get('index_name')
                or index_name
                or 'strategy_behavior'
            )
            resolved_index_version = str(
                first.get('index_version')
                or (active_index or {}).get('index_version')
                or index_version
                or ''
            )
            backend_used = str(
                first.get('backend')
                or (active_index or {}).get('backend')
                or requested_backend
            )
            source_of_truth = str(
                first.get('source_of_truth')
                or (active_index or {}).get('source_of_truth')
                or ('legacy_strategy_vector_tables' if rows else '')
            )
            collection_name = str(
                first.get('collection_name')
                or (active_index or {}).get('collection_name')
                or ''
            ).strip() or None
            result_source = str(
                first.get('result_source')
                or ('unified_ann' if source_of_truth == 'unified_vector_tables' and rows else '')
                or ('legacy_ann' if rows else '')
            ).strip() or None
            return {
                'items': rows,
                'count': len(rows),
                'index_name': resolved_index_name,
                'index_version': resolved_index_version,
                'active_index': active_index,
                'collection_name': collection_name,
                'source_of_truth': source_of_truth or None,
                'table_family': first.get('table_family') or (active_index or {}).get('table_family'),
                'result_source': result_source,
                **self._merge_backend_audit(
                    backend_requested=requested_backend,
                    backend_used=backend_used,
                    fallback_reason=fallback_reason,
                    fallback_used=fallback_used,
                    started_at=started_at,
                ),
            }

        async def health_check(
            self,
            db,
            index_name: str = 'strategy_behavior',
            limit_versions: int = 20,
            include_hnsw_indexes: bool = False,
            include_embedding_smoke_check: bool = False,
            force_embedding_smoke_check: bool = False,
        ) -> dict:
            started_at = time.perf_counter()
            embedding_service = (
                await self._ensure_text_embedding_service()
                if include_embedding_smoke_check
                else self._bind_text_embedding_service()
            )
            embedding_status_fn = getattr(embedding_service, 'status', None)
            embedding_status = (
                dict(embedding_status_fn() or {})
                if callable(embedding_status_fn)
                else {
                    'enabled': bool(getattr(embedding_service, 'is_enabled', lambda: False)()),
                    'client_closed': bool(self._embedding_service_is_closed(embedding_service)),
                }
            )
            embedding_smoke = None
            smoke_check = getattr(embedding_service, 'smoke_check', None)
            if include_embedding_smoke_check and callable(smoke_check):
                result = smoke_check(force=force_embedding_smoke_check)
                if asyncio.iscoroutine(result):
                    result = await result
                embedding_smoke = dict(result or {})
            unified_result = await self._build_unified_health(
                db,
                index_name=index_name,
                limit_versions=limit_versions,
                include_hnsw_indexes=include_hnsw_indexes,
            )
            if unified_result:
                active_index = await self.get_active_index(db, index_name=index_name)
                backend_requested = self.requested_backend_name(db)
                backend_used = str((active_index or {}).get('backend') or unified_result.get('backend') or backend_requested)
                audit = self._merge_backend_audit(
                    backend_requested=backend_requested,
                    backend_used=backend_used,
                    started_at=started_at,
                )
                return {
                    **unified_result,
                    'active_index': active_index,
                    'text_embedding': {
                        'service': embedding_status,
                        'smoke_check': embedding_smoke,
                    },
                    **audit,
                }
            if not hasattr(db, 'get_strategy_vector_health'):
                audit = self._merge_backend_audit(
                    backend_requested=self.requested_backend_name(db),
                    backend_used=self.backend_name(db),
                    fallback_reason='health_unsupported',
                    fallback_used=False,
                    started_at=started_at,
                )
                return {
                    'index_name': index_name,
                    'active_index': None,
                    'text_embedding': {
                        'service': embedding_status,
                        'smoke_check': embedding_smoke,
                    },
                    **audit,
                }
            result = await db.get_strategy_vector_health(
                index_name=index_name,
                limit_versions=limit_versions,
                include_hnsw_indexes=include_hnsw_indexes,
            )
            legacy_counts = dict((result or {}).get('counts') or {})
            zero_counts = {
                'profiles': 0,
                'profile_store': 0,
                'index_snapshots': 0,
                'index_items': 0,
                'index_item_store': 0,
            }
            active_index = await self.get_active_index(db, index_name=index_name)
            backend_requested = self.requested_backend_name(db)
            backend_used = str((active_index or {}).get('backend') or result.get('backend') or backend_requested)
            audit = self._merge_backend_audit(
                backend_requested=backend_requested,
                backend_used=backend_used,
                started_at=started_at,
            )
            return {
                **result,
                'health_mode': str(result.get('health_mode') or 'legacy'),
                'source_of_truth': str(result.get('source_of_truth') or 'legacy_strategy_vector_tables'),
                'cleanup_scope': str(result.get('cleanup_scope') or 'legacy'),
                'table_family': str(result.get('table_family') or 'legacy_strategy_vector_tables'),
                'legacy_only': bool(result.get('legacy_only', True)),
                'unified_available': bool(hasattr(db, 'list_vector_collections') and hasattr(db, 'list_vector_index_snapshots')),
                'legacy_available': True,
                'unified_counts': dict(result.get('unified_counts') or zero_counts),
                'legacy_counts': dict(result.get('legacy_counts') or legacy_counts),
                'active_index': active_index,
                'text_embedding': {
                    'service': embedding_status,
                    'smoke_check': embedding_smoke,
                },
                **audit,
            }

        async def _search_unified_profiles(
            self,
            db,
            strategy_id: str,
            *,
            profile_type: str = 'behavior',
            limit: int = 5,
            index_name: Optional[str] = None,
            index_version: Optional[str] = None,
        ) -> List[dict]:
            if not hasattr(db, 'search_vector_collection'):
                return []
            query_profile = await self._load_unified_query_profile(
                db,
                strategy_id,
                profile_type=profile_type,
                preferred_version=index_version,
                index_name=index_name,
            )
            if not query_profile:
                return []
            collection = dict(query_profile.get('_collection') or {})
            query_embedding = self._normalize_embedding(query_profile.get('embedding'))
            if len(query_embedding) == 0:
                return []
            resolved_index_name = str(
                index_name
                or dict(query_profile.get('metadata') or {}).get('index_name')
                or collection.get('index_name')
                or 'strategy_behavior'
            )
            search_result = await db.search_vector_collection(
                collection_name=str(query_profile.get('collection_name') or collection.get('collection_name') or ''),
                query_embedding=query_embedding.tolist(),
                index_version=index_version or collection.get('active_version') or query_profile.get('version'),
                version=index_version or query_profile.get('version'),
                profile_type=profile_type,
                exclude_entity_id=strategy_id,
                limit=max(1, min(int(limit or 5), 20)),
                metric=str(query_profile.get('metric') or collection.get('metric') or 'cosine'),
            )
            items = list((search_result or {}).get('items') or [])
            if not items:
                return []
            backend_used = str((search_result or {}).get('backend_used') or collection.get('backend') or 'sqlite_python')
            backend_family = self._sqlite_python_backend_family(backend_used)
            fallback_reason = str((search_result or {}).get('fallback_reason') or '').strip() or None
            fallback_used = bool((search_result or {}).get('fallback_used') or fallback_reason)
            query_bucket_id = (search_result or {}).get('query_bucket_id')
            candidate_bucket_ids = list((search_result or {}).get('candidate_bucket_ids') or [])
            results: List[dict] = []
            for item in items:
                metadata = dict((item or {}).get('metadata') or {})
                resolved_strategy_id = str(item.get('entity_id') or '').strip()
                if not resolved_strategy_id:
                    continue
                retrieval_mode = self._unified_retrieval_mode(backend_used)
                results.append({
                    'profile_id': item.get('profile_id') or item.get('id') or metadata.get('legacy_profile_id'),
                    'strategy_id': resolved_strategy_id,
                    'profile_type': item.get('profile_type') or profile_type,
                    'vector_method': metadata.get('effective_vector_method') or metadata.get('vector_method'),
                    'metric': item.get('metric') or query_profile.get('metric') or collection.get('metric') or 'cosine',
                    'vector_dim': item.get('vector_dim') or query_profile.get('vector_dim') or collection.get('vector_dim'),
                    'bucket_id': item.get('bucket_id'),
                    'query_bucket_id': query_bucket_id,
                    'candidate_bucket_ids': candidate_bucket_ids,
                    'coarse_score': item.get('coarse_score'),
                    'similarity': round(float(item.get('similarity') or 0.0), 6),
                    'backend': backend_family,
                    'index_name': resolved_index_name,
                    'index_version': str((search_result or {}).get('index_version') or query_profile.get('version') or index_version or ''),
                    'signature': item.get('signature') or metadata.get('signature'),
                    'candidate_count': len(items),
                    'retrieval_mode': retrieval_mode,
                    'result_source': self._unified_result_source(retrieval_mode),
                    'collection_name': query_profile.get('collection_name') or collection.get('collection_name'),
                    'model_id': item.get('model_id') or query_profile.get('model_id') or collection.get('model_id'),
                    'metadata': metadata,
                    'search_fallback_used': fallback_used,
                    'search_fallback_reason': fallback_reason,
                    'source_of_truth': 'unified_vector_tables',
                    'table_family': 'unified_vector_tables',
                    'legacy_only': False,
                })
            results.sort(key=lambda row: (row.get('similarity', 0), row.get('coarse_score', 0)), reverse=True)
            return results[: max(1, min(int(limit or 5), 20))]

        async def ann_search_profiles(
            self,
            db,
            strategy_id: str,
            profile_type: str = 'behavior',
            limit: int = 5,
            candidate_limit: int = 80,
            index_name: Optional[str] = None,
            index_version: Optional[str] = None,
        ) -> List[dict]:
            unified_rows = await self._search_unified_profiles(
                db,
                strategy_id,
                profile_type=profile_type,
                limit=limit,
                index_name=index_name,
                index_version=index_version,
            )
            if unified_rows:
                return unified_rows
            query_profile = await self._load_query_profile(db, strategy_id, profile_type=profile_type, preferred_version=index_version)
            if not query_profile:
                return []
            requested_backend = self.requested_backend_name(db)
            resolved_index_name = self._resolved_index_name(index_name, query_profile)
            snapshot = await self._get_latest_snapshot(db, resolved_index_name, index_version=index_version)
            if snapshot and snapshot.get('index_version') and query_profile.get('index_version') != snapshot.get('index_version'):
                preferred = await self._load_query_profile(db, strategy_id, profile_type=profile_type, preferred_version=str(snapshot.get('index_version')))
                if preferred:
                    query_profile = preferred
            query_embedding = self._normalize_embedding(query_profile.get('embedding'))
            if len(query_embedding) == 0:
                return []
            if not snapshot or not hasattr(db, 'list_strategy_vector_index_items'):
                return []
            sqlite_python_available = getattr(db, 'supports_sqlite_python', lambda: False)() and hasattr(db, 'search_strategy_vector_index_items_by_embedding')
            if requested_backend == 'sqlite_python' and sqlite_python_available:
                search_kwargs = {
                    'query_embedding': query_embedding.tolist(),
                    'index_name': resolved_index_name,
                    'index_version': str(snapshot.get('index_version') or ''),
                    'profile_type': profile_type,
                    'exclude_strategy_id': strategy_id,
                    'limit': max(1, min(int(candidate_limit or 80), 500)),
                    'metric': str(query_profile.get('metric') or 'cosine'),
                }
                index_params = dict(snapshot.get('index_params') or {})
                if index_params:
                    search_kwargs['index_params'] = index_params
                try:
                    pg_rows = await db.search_strategy_vector_index_items_by_embedding(**search_kwargs)
                except TypeError as exc:
                    if 'index_params' not in str(exc):
                        raise
                    search_kwargs.pop('index_params', None)
                    pg_rows = await db.search_strategy_vector_index_items_by_embedding(**search_kwargs)
                if pg_rows:
                    results = []
                    candidate_count = 0
                    for item in pg_rows:
                        if item.get('strategy_id') == strategy_id:
                            continue
                        candidate_count += 1
                        results.append({
                            'profile_id': item.get('profile_id'),
                            'strategy_id': item.get('strategy_id'),
                            'profile_type': item.get('profile_type'),
                            'vector_method': item.get('vector_method'),
                            'metric': item.get('metric') or str(query_profile.get('metric') or 'cosine'),
                            'vector_dim': item.get('vector_dim'),
                            'bucket_id': item.get('bucket_id'),
                            'query_bucket_id': None,
                            'coarse_score': item.get('coarse_score'),
                            'similarity': round(float(item.get('similarity') or 0.0), 6),
                            'backend': 'sqlite_python',
                            'index_name': resolved_index_name,
                            'index_version': snapshot.get('index_version'),
                            'signature': dict(item.get('metadata') or {}).get('signature'),
                            'candidate_count': max(candidate_count, len(pg_rows)),
                            'retrieval_mode': 'sqlite_python_ann',
                            'result_source': 'legacy_ann',
                            'metadata': item.get('metadata') or {},
                            'source_of_truth': 'legacy_strategy_vector_tables',
                            'table_family': 'legacy_strategy_vector_tables',
                            'legacy_only': True,
                        })
                    results.sort(key=lambda row: (row.get('similarity', 0), row.get('coarse_score', 0)), reverse=True)
                    return results[: max(1, min(int(limit or 5), 20))]
                if not self.allow_fallback:
                    return []
            elif requested_backend == 'sqlite_python' and not self.allow_fallback:
                return []
            primary_bucket, candidate_buckets = self._resolve_query_bucket(snapshot, query_embedding)
            if not candidate_buckets:
                return []
            items = await db.list_strategy_vector_index_items(
                index_name=resolved_index_name,
                index_version=str(snapshot.get('index_version') or ''),
                bucket_ids=candidate_buckets,
                limit=max(1, min(int(candidate_limit or 80), 500)),
            )
            if not items:
                return []
            metric = str(query_profile.get('metric') or 'cosine')
            results = []
            candidate_count = 0
            for item in items:
                embedding = self._normalize_embedding(item.get('embedding'))
                if len(embedding) != len(query_embedding) or len(embedding) == 0:
                    continue
                if item.get('strategy_id') == strategy_id:
                    continue
                candidate_count += 1
                similarity = self.engine.calculate_similarity(query_embedding, embedding, metric)
                results.append({
                    'profile_id': item.get('profile_id'),
                    'strategy_id': item.get('strategy_id'),
                    'profile_type': item.get('profile_type'),
                    'vector_method': item.get('vector_method'),
                    'metric': item.get('metric') or metric,
                    'vector_dim': item.get('vector_dim'),
                    'bucket_id': item.get('bucket_id'),
                    'query_bucket_id': primary_bucket,
                    'coarse_score': item.get('coarse_score'),
                    'similarity': round(float(similarity), 6),
                    'backend': snapshot.get('backend') or self.engine.backend,
                    'index_name': resolved_index_name,
                    'index_version': snapshot.get('index_version'),
                    'signature': dict(item.get('metadata') or {}).get('signature'),
                    'candidate_count': max(candidate_count, len(items) - 1),
                    'retrieval_mode': 'persisted_ann',
                    'result_source': 'legacy_ann',
                    'metadata': item.get('metadata') or {},
                    'source_of_truth': 'legacy_strategy_vector_tables',
                    'table_family': 'legacy_strategy_vector_tables',
                    'legacy_only': True,
                })
            results.sort(key=lambda row: (row.get('similarity', 0), row.get('coarse_score', 0)), reverse=True)
            return results[: max(1, min(int(limit or 5), 20))]

        async def find_similar_profiles(
            self,
            db,
            strategy_id: str,
            profile_type: str = 'behavior',
            limit: int = 5,
            index_name: Optional[str] = None,
            index_version: Optional[str] = None,
        ) -> List[dict]:
            ann_rows = await self.ann_search_profiles(
                db,
                strategy_id,
                profile_type=profile_type,
                limit=limit,
                index_name=index_name,
                index_version=index_version,
            )
            if ann_rows:
                return ann_rows
            query_profile = await self._load_query_profile(db, strategy_id, profile_type=profile_type, preferred_version=index_version)
            if not query_profile:
                return []
            requested_backend = self.requested_backend_name(db)
            query_embedding = self._normalize_embedding(query_profile.get('embedding'))
            if len(query_embedding) == 0:
                return []
            resolved_index_name = self._resolved_index_name(index_name, query_profile)
            sqlite_python_available = getattr(db, 'supports_sqlite_python', lambda: False)() and hasattr(db, 'search_strategy_vector_profiles_by_embedding')
            if requested_backend == 'sqlite_python' and sqlite_python_available:
                pg_rows = await db.search_strategy_vector_profiles_by_embedding(
                    query_embedding=query_embedding.tolist(),
                    profile_type=profile_type,
                    index_name=resolved_index_name,
                    index_version=index_version or query_profile.get('index_version'),
                    exclude_strategy_id=strategy_id,
                    limit=max(1, min(int(limit or 5), 50)),
                    metric=str(query_profile.get('metric') or 'cosine'),
                    index_params=self._hnsw_index_params(),
                )
                if pg_rows:
                    results = []
                    candidate_count = 0
                    for item in pg_rows:
                        if item.get('strategy_id') == strategy_id:
                            continue
                        candidate_count += 1
                        results.append({
                            **item,
                            'similarity': round(float(item.get('similarity') or 0.0), 6),
                            'retrieval_mode': 'sqlite_python_exact',
                            'result_source': 'legacy_profile_exact',
                            'candidate_count': max(candidate_count, len(pg_rows)),
                            'index_name': self._resolved_index_name(index_name, item),
                            'query_bucket_id': None,
                            'bucket_id': None,
                            'backend': 'sqlite_python',
                            'source_of_truth': 'legacy_strategy_vector_tables',
                            'table_family': 'legacy_strategy_vector_tables',
                            'legacy_only': True,
                        })
                    results.sort(key=lambda row: row.get('similarity', 0), reverse=True)
                    return results[: max(1, min(int(limit or 5), 20))]
                if not self.allow_fallback:
                    return []
            elif requested_backend == 'sqlite_python' and not self.allow_fallback:
                return []
            profiles = await db.list_strategy_vector_profiles(
                profile_type=profile_type,
                index_name=resolved_index_name,
                index_version=index_version or query_profile.get('index_version'),
                limit=2000,
            )
            results = []
            candidate_count = 0
            for item in profiles:
                if item.get('strategy_id') == strategy_id:
                    continue
                candidate = self._normalize_embedding(item.get('embedding'))
                if len(candidate) != len(query_embedding) or len(candidate) == 0:
                    continue
                candidate_count += 1
                similarity = self.engine.calculate_similarity(query_embedding, candidate, query_profile.get('metric') or 'cosine')
                results.append({
                    **item,
                    'similarity': round(float(similarity), 6),
                    'retrieval_mode': 'full_scan',
                    'result_source': 'legacy_full_scan',
                    'candidate_count': candidate_count,
                    'index_name': self._resolved_index_name(index_name, item),
                    'query_bucket_id': None,
                    'bucket_id': None,
                    'source_of_truth': 'legacy_strategy_vector_tables',
                    'table_family': 'legacy_strategy_vector_tables',
                    'legacy_only': True,
                })
            results.sort(key=lambda row: row.get('similarity', 0), reverse=True)
            return results[: max(1, min(int(limit or 5), 20))]
