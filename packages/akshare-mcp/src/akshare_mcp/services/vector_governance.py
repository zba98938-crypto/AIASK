"""策略向量索引治理：索引注册表校准、重建与版本切换。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


def _empty_cleanup_deleted() -> dict[str, int]:
    return {
        'vector_index_registry': 0,
        'vector_index_snapshots': 0,
        'vector_profiles': 0,
        'vector_profile_store': 0,
        'vector_index_items': 0,
        'vector_index_item_store': 0,
        'hnsw_indexes': 0,
    }


def _merge_cleanup_deleted(target: dict[str, int], payload: dict | None) -> dict[str, int]:
    merged = dict(target or _empty_cleanup_deleted())
    for key, value in dict(payload or {}).items():
        merged[key] = int(merged.get(key) or 0) + int(value or 0)
    return merged


def _unique_strings(values) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(values or []):
        normalized = str(item or '').strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


class StrategyVectorGovernanceService:
    async def _record_domain_event(self, db, *, event_type: str, source: str, payload: dict, correlation_id: str | None = None):
        if hasattr(db, 'save_strategy_domain_event'):
            await db.save_strategy_domain_event({
                'strategy_id': None,
                'aggregate_type': 'vector_index',
                'aggregate_id': payload.get('index_name') or payload.get('index_version') or 'strategy_behavior',
                'event_type': event_type,
                'source': source,
                'severity': 'info',
                'correlation_id': correlation_id,
                'payload': payload,
            })

    async def reconcile_registry(
        self,
        db,
        index_name: Optional[str] = None,
        profile_type: Optional[str] = None,
        limit_profiles: int = 2000,
    ) -> dict:
        from .vector_platform import get_strategy_vector_platform

        platform = get_strategy_vector_platform()
        resolved_limit = max(1, min(int(limit_profiles or 2000), 5000))
        if hasattr(platform, 'list_profiles'):
            rows = await platform.list_profiles(
                db,
                profile_type=profile_type,
                index_name=index_name,
                limit=resolved_limit,
            )
        elif hasattr(db, 'list_strategy_vector_profiles'):
            rows = await db.list_strategy_vector_profiles(
                profile_type=profile_type,
                index_name=index_name,
                limit=resolved_limit,
            )
        else:
            rows = []
        grouped: dict[tuple[str, str], dict] = {}
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            meta = dict(row.get('metadata') or {})
            resolved_index_name = str(index_name or meta.get('index_name') or 'strategy_behavior')
            version = str(row.get('index_version') or meta.get('index_version') or 'v1')
            key = (resolved_index_name, version)
            bucket = grouped.setdefault(key, {
                'index_name': resolved_index_name,
                'backend': str(row.get('backend') or 'index'),
                'status': 'active',
                'profile_type': str(row.get('profile_type') or 'behavior'),
                'vector_method': str(row.get('vector_method') or 'price_volume'),
                'metric': str(row.get('metric') or 'cosine'),
                'sample_count': 0,
                'index_version': version,
                'metadata': {
                    'strategy_ids': [],
                    'profile_ids': [],
                    'reconciled_at': now,
                },
                'built_at': now,
                'activated_at': now,
            })
            bucket['sample_count'] += 1
            if row.get('strategy_id') and row.get('strategy_id') not in bucket['metadata']['strategy_ids']:
                bucket['metadata']['strategy_ids'].append(row.get('strategy_id'))
            if row.get('id') is not None:
                bucket['metadata']['profile_ids'].append(row.get('id'))

        if hasattr(platform, 'list_index_snapshots'):
            snapshots = await platform.list_index_snapshots(
                db,
                index_name=index_name,
                limit=5000,
            )
        elif hasattr(db, 'list_strategy_vector_index_snapshots'):
            snapshots = await db.list_strategy_vector_index_snapshots(
                index_name=index_name,
                limit=5000,
            )
        else:
            snapshots = []
        latest_snapshots: dict[tuple[str, str], dict] = {}
        for snapshot in snapshots:
            key = (str(snapshot.get('index_name') or 'strategy_behavior'), str(snapshot.get('index_version') or 'v1'))
            if key not in latest_snapshots:
                latest_snapshots[key] = dict(snapshot)
        for snapshot in latest_snapshots.values():
            key = (str(snapshot.get('index_name') or 'strategy_behavior'), str(snapshot.get('index_version') or 'v1'))
            if key not in grouped:
                continue
            grouped[key]['metadata'].update({
                'ann_snapshot_id': snapshot.get('id'),
                'bucket_count': snapshot.get('bucket_count'),
                'vector_dim': snapshot.get('vector_dim'),
                'index_snapshot_status': snapshot.get('status'),
                'collection_name': snapshot.get('collection_name'),
                'model_id': snapshot.get('model_id'),
            })
            if snapshot.get('backend'):
                grouped[key]['backend'] = str(snapshot.get('backend') or grouped[key]['backend'])

        updated = []
        for item in grouped.values():
            updated.append(await db.save_vector_index_registry(item))

        existing = await db.list_vector_index_registry(index_name=index_name, limit=5000) if hasattr(db, 'list_vector_index_registry') else []
        active_keys = set(grouped.keys())
        stale = []
        for item in existing:
            key = (str(item.get('index_name') or 'strategy_behavior'), str(item.get('index_version') or 'v1'))
            if key in active_keys:
                continue
            if item.get('status') == 'stale':
                continue
            stale_item = await db.save_vector_index_registry({
                **item,
                'status': 'stale',
                'metadata': {
                    **dict(item.get('metadata') or {}),
                    'reconciled_at': now,
                    'stale_reason': 'profile_group_missing',
                },
            })
            stale.append(stale_item)

        if updated or stale:
            await self._record_domain_event(
                db,
                event_type='vector_index.registry_reconciled',
                source='vector_governance',
                payload={
                    'index_name': index_name or 'all',
                    'profile_type': profile_type,
                    'registry_updated': len(updated),
                    'stale_marked': len(stale),
                },
            )

        return {
            'registry_updated': len(updated),
            'stale_marked': len(stale),
            'active_indexes': len(updated),
            'items': updated,
            'stale_items': stale,
        }

    async def cleanup_unified_history(
        self,
        db,
        *,
        index_name: str = 'strategy_behavior',
        keep_versions: int = 1,
        dry_run: bool = True,
        cleanup_hnsw: bool = True,
        limit_versions: int = 200,
        protect_versions: Optional[list[str]] = None,
    ) -> dict:
        from .vector_platform import get_strategy_vector_platform

        platform = get_strategy_vector_platform()
        collections = await platform._list_unified_strategy_collections(db, index_name=index_name)
        if not collections:
            return {
                'index_name': index_name,
                'requested_scope': 'unified',
                'cleanup_scope': 'unified',
                'health_mode': 'unified',
                'source_of_truth': 'unified_vector_tables',
                'table_family': 'unified_vector_tables',
                'legacy_only': False,
                'dry_run': bool(dry_run),
                'keep_versions': max(0, int(keep_versions or 0)),
                'collection_count': 0,
                'collections': [],
                'protected_versions': [],
                'target_versions': [],
                'target_version_keys': [],
                'hnsw_indexes_to_drop': [],
                'deleted': _empty_cleanup_deleted(),
                'version_details': [],
                'reason': 'no_unified_collections',
            }
        if not hasattr(db, 'cleanup_vector_collection_history'):
            return {
                'index_name': index_name,
                'requested_scope': 'unified',
                'cleanup_scope': 'unified',
                'health_mode': 'unified',
                'source_of_truth': 'unified_vector_tables',
                'table_family': 'unified_vector_tables',
                'legacy_only': False,
                'dry_run': bool(dry_run),
                'keep_versions': max(0, int(keep_versions or 0)),
                'collection_count': len(collections),
                'collections': [
                    {'collection_name': item.get('collection_name'), 'active_version': item.get('active_version')}
                    for item in collections
                ],
                'protected_versions': [],
                'target_versions': [],
                'target_version_keys': [],
                'hnsw_indexes_to_drop': [],
                'deleted': _empty_cleanup_deleted(),
                'version_details': [],
                'reason': 'unified_cleanup_unsupported',
            }

        collection_summaries: list[dict] = []
        deleted = _empty_cleanup_deleted()
        protected_versions: list[str] = []
        target_versions: list[str] = []
        target_version_keys: list[str] = []
        hnsw_indexes_to_drop: list[str] = []
        version_details: list[dict] = []

        for collection in collections:
            collection_name = str(collection.get('collection_name') or '').strip()
            profile_type = str(dict(collection.get('metadata') or {}).get('profile_type') or '').strip() or None
            summary = await db.cleanup_vector_collection_history(
                collection_name=collection_name,
                keep_versions=keep_versions,
                dry_run=dry_run,
                cleanup_hnsw=cleanup_hnsw,
                limit_versions=limit_versions,
                protect_versions=protect_versions,
                profile_type=profile_type,
            )
            normalized_summary = {
                **dict(summary or {}),
                'index_name': index_name,
                'collection_name': collection_name,
                'source_of_truth': 'unified_vector_tables',
                'cleanup_scope': 'unified',
                'health_mode': 'unified',
                'table_family': 'unified_vector_tables',
                'legacy_only': False,
            }
            collection_summaries.append(normalized_summary)
            deleted = _merge_cleanup_deleted(deleted, normalized_summary.get('deleted'))
            protected_versions.extend(normalized_summary.get('protected_versions') or [])
            target_versions.extend(normalized_summary.get('target_versions') or [])
            target_version_keys.extend(
                normalized_summary.get('target_version_keys')
                or [f'{collection_name}@{version}' for version in normalized_summary.get('target_versions') or []]
            )
            hnsw_indexes_to_drop.extend(normalized_summary.get('hnsw_indexes_to_drop') or [])
            version_details.extend(
                [
                    {
                        'collection_name': collection_name,
                        **dict(item or {}),
                    }
                    for item in normalized_summary.get('version_details') or []
                ]
            )

        return {
            'index_name': index_name,
            'requested_scope': 'unified',
            'cleanup_scope': 'unified',
            'health_mode': 'unified',
            'source_of_truth': 'unified_vector_tables',
            'table_family': 'unified_vector_tables',
            'legacy_only': False,
            'dry_run': bool(dry_run),
            'keep_versions': max(0, int(keep_versions or 0)),
            'collection_count': len(collection_summaries),
            'collections': [
                {
                    'collection_name': item.get('collection_name'),
                    'active_version': item.get('active_version'),
                    'latest_snapshot_version': item.get('latest_snapshot_version'),
                    'reason': item.get('reason'),
                }
                for item in collection_summaries
            ],
            'target_versions': _unique_strings(target_versions),
            'target_version_keys': _unique_strings(target_version_keys),
            'protected_versions': _unique_strings(protected_versions),
            'hnsw_indexes_to_drop': _unique_strings(hnsw_indexes_to_drop),
            'deleted': deleted,
            'version_details': version_details[: max(1, min(int(limit_versions or 200), 1000))],
            'scopes': {'unified': collection_summaries},
            'reason': None if collection_summaries else 'no_unified_collections',
        }

    async def rebuild_index(
        self,
        db,
        index_name: str = 'strategy_behavior',
        index_version: Optional[str] = None,
        statuses: Optional[list[str]] = None,
        limit: int = 200,
        profile_type: str = 'behavior',
        vector_method: Optional[str] = None,
    ) -> dict:
        statuses = list(statuses or ['incubating', 'listed'])
        resolved_version = str(index_version or f"auto_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
        correlation_id = uuid4().hex[:12]
        from .vector_platform import get_strategy_vector_platform

        platform = get_strategy_vector_platform()
        resolved_vector_method = (
            platform.ensure_vector_method_available(vector_method)
            if hasattr(platform, 'ensure_vector_method_available')
            else str(vector_method or 'price_volume')
        )
        task_run = await db.save_strategy_task_run({
            'task_name': 'strategy_vector_rebuild',
            'task_scope': index_name,
            'task_key': resolved_version,
            'status': 'running',
            'trace_id': correlation_id,
            'payload': {
                'index_name': index_name,
                'index_version': resolved_version,
                'statuses': statuses,
                'limit': limit,
                'profile_type': profile_type,
                'vector_method': resolved_vector_method,
            },
        }) if hasattr(db, 'save_strategy_task_run') else {'id': None}

        try:
            seen = set()
            strategies = []
            for status in statuses:
                rows = await db.list_strategies(status, limit=max(1, min(int(limit or 200), 1000)))
                for row in rows:
                    sid = row.get('id')
                    if sid and sid not in seen:
                        seen.add(sid)
                        strategies.append(row)

            build_result = await platform.build_profiles_for_strategies(
                db,
                strategies,
                profile_type=profile_type,
                vector_method=resolved_vector_method,
                index_name=index_name,
                index_version=resolved_version,
            )
            degraded_count = int(build_result.get('degraded_count') or 0)
            if degraded_count > 0:
                result = {
                    'task_run_id': task_run.get('id'),
                    'index_name': index_name,
                    'index_version': resolved_version,
                    'strategy_count': len(strategies),
                    'built_profiles': int(build_result.get('count') or 0),
                    'degraded_profiles': degraded_count,
                    'failed_profiles': int(build_result.get('failed_count') or 0),
                    'status': 'degraded',
                    'degraded': True,
                    'quality_flags': list(build_result.get('quality_flags') or ['unified_write_failed']),
                    'reason': 'unified_profile_write_degraded',
                    'degraded_items': list(build_result.get('degraded_items') or []),
                }
                if task_run.get('id') is not None and hasattr(db, 'update_strategy_task_run'):
                    await db.update_strategy_task_run(
                        task_run['id'],
                        status='completed',
                        result=result,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                await self._record_domain_event(
                    db,
                    event_type='vector_index.rebuild_degraded',
                    source='vector_governance',
                    correlation_id=correlation_id,
                    payload=result,
                )
                return result
            persist_result = await platform.build_persisted_ann_index(
                db,
                index_name=index_name,
                index_version=resolved_version,
                profile_type=profile_type,
                task_run_id=task_run.get('id'),
                source='vector_governance',
                limit_profiles=max(1, min(int(limit or 200), 5000)),
            )

            existing = await db.list_vector_index_registry(index_name=index_name, limit=5000) if hasattr(db, 'list_vector_index_registry') else []
            for item in existing:
                version = str(item.get('index_version') or 'v1')
                if version == resolved_version:
                    continue
                await db.save_vector_index_registry({
                    **item,
                    'status': 'stale',
                    'metadata': {
                        **dict(item.get('metadata') or {}),
                        'replaced_by': resolved_version,
                        'stale_reason': 'rebuild_replaced',
                    },
                })

            if hasattr(db, 'list_strategy_vector_index_snapshots') and hasattr(db, 'save_strategy_vector_index_snapshot'):
                snapshots = await db.list_strategy_vector_index_snapshots(index_name=index_name, limit=5000, latest_only=True)
                for snapshot in snapshots:
                    version = str(snapshot.get('index_version') or 'v1')
                    if version == resolved_version or snapshot.get('status') == 'stale':
                        continue
                    await db.save_strategy_vector_index_snapshot({
                        **snapshot,
                        'status': 'stale',
                        'metadata': {
                            **dict(snapshot.get('metadata') or {}),
                            'replaced_by': resolved_version,
                            'stale_reason': 'rebuild_replaced',
                        },
                    })

            reconcile = await self.reconcile_registry(db, index_name=index_name, profile_type=profile_type)
            active_registry = await db.save_vector_index_registry({
                'index_name': index_name,
                'backend': platform.backend_name(db) if hasattr(platform, 'backend_name') else getattr(getattr(platform, 'engine', None), 'backend', 'index'),
                'status': 'active',
                'profile_type': profile_type,
                'vector_method': resolved_vector_method,
                'metric': 'cosine',
                'sample_count': int(build_result.get('count') or 0),
                'index_version': resolved_version,
                'metadata': {
                    'profile_ids': [item.get('id') for item in build_result.get('items') or [] if item.get('id') is not None],
                    'ann_snapshot_id': ((persist_result.get('snapshot') or {}).get('id')),
                    'bucket_count': persist_result.get('bucket_count'),
                    'persisted_items': persist_result.get('items_count'),
                    'unified_collection_name': persist_result.get('unified_collection_name'),
                    'unified_snapshot_id': ((persist_result.get('unified_snapshot') or {}).get('snapshot') or {}).get('id')
                    if isinstance(persist_result.get('unified_snapshot'), dict)
                    else None,
                    'task_run_id': task_run.get('id'),
                },
                'built_at': datetime.now(timezone.utc).isoformat(),
                'activated_at': datetime.now(timezone.utc).isoformat(),
            })
            result = {
                'task_run_id': task_run.get('id'),
                'index_name': index_name,
                'index_version': resolved_version,
                'strategy_count': len(strategies),
                'built_profiles': int(build_result.get('count') or 0),
                'persisted_snapshot_id': (persist_result.get('snapshot') or {}).get('id'),
                'persisted_items': int(persist_result.get('items_count') or 0),
                'bucket_count': int(persist_result.get('bucket_count') or 0),
                'active_registry': active_registry,
                'reconcile': reconcile,
                'degraded': bool(persist_result.get('degraded')),
                'quality_flags': list(persist_result.get('quality_flags') or []),
                'qa': persist_result.get('qa'),
            }
            if task_run.get('id') is not None and hasattr(db, 'update_strategy_task_run'):
                await db.update_strategy_task_run(task_run['id'], status='completed', result=result, completed_at=datetime.now(timezone.utc).isoformat())
            await self._record_domain_event(
                db,
                event_type='vector_index.rebuilt',
                source='vector_governance',
                correlation_id=correlation_id,
                payload=result,
            )
            return result
        except Exception as exc:
            logger.warning('StrategyVectorGovernanceService.rebuild_index failed: %s', exc)
            if task_run.get('id') is not None and hasattr(db, 'update_strategy_task_run'):
                await db.update_strategy_task_run(task_run['id'], status='failed', error=str(exc), result={'index_name': index_name, 'index_version': resolved_version}, completed_at=datetime.now(timezone.utc).isoformat())
            raise


_vector_governance_service: Optional[StrategyVectorGovernanceService] = None


def get_strategy_vector_governance_service() -> StrategyVectorGovernanceService:
    global _vector_governance_service
    if _vector_governance_service is None:
        _vector_governance_service = StrategyVectorGovernanceService()
    return _vector_governance_service
