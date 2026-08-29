"""策略领域事件投影：基于事件流回放当前聚合状态。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4


class StrategyDomainProjectionService:
    @staticmethod
    def _parse_time(value: Any) -> datetime:
        def _utc(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        if isinstance(value, datetime):
            return _utc(value)
        text = str(value or '').strip()
        if not text:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            return _utc(datetime.fromisoformat(text.replace('Z', '+00:00')))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    async def project_strategy(self, db, strategy_id: str, limit: int = 200) -> dict:
        strategy = await db.get_strategy(strategy_id)
        if not strategy:
            raise ValueError(f'strategy not found: {strategy_id}')

        status_events = await db.list_strategy_status_events(strategy_id, limit=limit) if hasattr(db, 'list_strategy_status_events') else []
        domain_events = await db.list_strategy_domain_events(strategy_id=strategy_id, limit=limit) if hasattr(db, 'list_strategy_domain_events') else []
        runtime_control = await db.get_strategy_runtime_control(strategy_id) if hasattr(db, 'get_strategy_runtime_control') else None
        latest_review = await db.get_latest_strategy_promotion_review(strategy_id) if hasattr(db, 'get_latest_strategy_promotion_review') else None
        latest_metric = await db.get_latest_strategy_incubation_metric(strategy_id) if hasattr(db, 'get_latest_strategy_incubation_metric') else None
        open_risks = await db.list_strategy_runtime_risk_events(strategy_id=strategy_id, status='open', limit=100) if hasattr(db, 'list_strategy_runtime_risk_events') else []
        task_runs = await db.list_strategy_task_runs(strategy_id=strategy_id, limit=50) if hasattr(db, 'list_strategy_task_runs') else []
        experiments = await db.list_strategy_generation_experiments(strategy_id=strategy_id, limit=50) if hasattr(db, 'list_strategy_generation_experiments') else []

        merged_timeline = []
        for item in status_events:
            merged_timeline.append({
                'timestamp': item.get('created_at'),
                'event_type': item.get('event_type') or 'status_change',
                'source': item.get('actor_id') or 'status_event',
                'summary': f"{item.get('from_status') or '初始'} → {item.get('to_status') or '-'}",
            })
        for item in domain_events:
            merged_timeline.append({
                'timestamp': item.get('created_at'),
                'event_type': item.get('event_type'),
                'source': item.get('source') or 'domain_event',
                'summary': str(item.get('payload') or {})[:120],
            })
        merged_timeline.sort(key=lambda row: self._parse_time(row.get('timestamp')), reverse=True)

        latest_status_event = status_events[0] if status_events else None
        current_status = (latest_status_event or {}).get('to_status') or strategy.get('status')
        ai_cycle_count = len([item for item in task_runs if item.get('task_name') == 'strategy_ai_cycle'])
        runtime_cycle_count = len([item for item in task_runs if item.get('task_name') == 'strategy_runtime_cycle'])

        phases = {
            'submitted': bool(strategy.get('status') in {'submitted', 'incubating', 'listed', 'deprecated', 'suspended', 'archived'}),
            'incubated': bool(latest_metric),
            'listed': current_status == 'listed',
            'runtime_guarded': bool(runtime_control and runtime_control.get('control_mode') != 'active'),
            'ai_evolved': ai_cycle_count > 0 or len(experiments) > 0,
            'promotion_reviewed': latest_review is not None,
        }

        return {
            'strategy_id': strategy_id,
            'current_status': current_status,
            'aggregate_version': len(status_events) + len(domain_events),
            'status_event_count': len(status_events),
            'domain_event_count': len(domain_events),
            'open_risk_count': len(open_risks),
            'runtime_control_mode': (runtime_control or {}).get('control_mode') or 'active',
            'runtime_control_status': (runtime_control or {}).get('status'),
            'latest_promotion_status': (latest_review or {}).get('status'),
            'latest_promotion_recommendation': (latest_review or {}).get('recommendation'),
            'latest_incubation_decision': (latest_metric or {}).get('decision'),
            'ai_cycle_count': ai_cycle_count,
            'runtime_cycle_count': runtime_cycle_count,
            'last_status_event_at': (latest_status_event or {}).get('created_at'),
            'last_domain_event_at': (domain_events[0] if domain_events else {}).get('created_at'),
            'phases': phases,
            'timeline': merged_timeline[:20],
        }

    async def rebuild_projection(
        self,
        db,
        strategy_id: str,
        *,
        limit: int = 200,
        source: str = 'manual',
        persist: bool = True,
    ) -> dict:
        task_run = await db.save_strategy_task_run({
            'strategy_id': strategy_id,
            'task_name': 'strategy_projection_rebuild',
            'task_scope': source,
            'task_key': strategy_id,
            'status': 'running',
            'trace_id': uuid4().hex[:12],
            'payload': {'strategy_id': strategy_id, 'limit': limit},
        }) if hasattr(db, 'save_strategy_task_run') else {'id': None, 'trace_id': None}
        try:
            projection = await self.project_strategy(db, strategy_id, limit=limit)
            snapshot = None
            if persist and hasattr(db, 'save_strategy_projection_snapshot'):
                snapshot = await db.save_strategy_projection_snapshot({
                    'strategy_id': strategy_id,
                    'projection_type': 'strategy_state',
                    'aggregate_version': projection.get('aggregate_version'),
                    'current_status': projection.get('current_status'),
                    'runtime_control_mode': projection.get('runtime_control_mode'),
                    'timeline_count': len(projection.get('timeline') or []),
                    'projection': projection,
                    'metadata': {
                        'status_event_count': projection.get('status_event_count'),
                        'domain_event_count': projection.get('domain_event_count'),
                        'open_risk_count': projection.get('open_risk_count'),
                    },
                    'task_run_id': task_run.get('id'),
                    'source': source,
                    'rebuilt_at': datetime.now(timezone.utc),
                })
            result = {
                'task_run_id': task_run.get('id'),
                'strategy_id': strategy_id,
                'projection': projection,
                'snapshot': snapshot,
            }
            if task_run.get('id') is not None and hasattr(db, 'update_strategy_task_run'):
                await db.update_strategy_task_run(task_run['id'], status='completed', result=result)
            if hasattr(db, 'save_strategy_domain_event'):
                await db.save_strategy_domain_event({
                    'strategy_id': strategy_id,
                    'aggregate_type': 'strategy_projection',
                    'aggregate_id': strategy_id,
                    'event_type': 'projection.rebuilt',
                    'source': source,
                    'severity': 'info',
                    'correlation_id': task_run.get('trace_id'),
                    'payload': {
                        'task_run_id': task_run.get('id'),
                        'aggregate_version': projection.get('aggregate_version'),
                        'current_status': projection.get('current_status'),
                    },
                })
            return result
        except Exception as exc:
            if task_run.get('id') is not None and hasattr(db, 'update_strategy_task_run'):
                await db.update_strategy_task_run(task_run['id'], status='failed', error=str(exc), result={'strategy_id': strategy_id})
            raise

    async def rebuild_batch(
        self,
        db,
        *,
        statuses: Optional[list[str]] = None,
        limit: int = 200,
        source: str = 'manual',
    ) -> dict:
        statuses = list(statuses or ['incubating', 'listed', 'suspended', 'deprecated'])
        task_run = await db.save_strategy_task_run({
            'task_name': 'strategy_projection_rebuild_batch',
            'task_scope': source,
            'task_key': ','.join(statuses),
            'status': 'running',
            'trace_id': uuid4().hex[:12],
            'payload': {'statuses': statuses, 'limit': limit},
        }) if hasattr(db, 'save_strategy_task_run') else {'id': None, 'trace_id': None}
        try:
            strategies = []
            for status in statuses:
                strategies.extend(await db.list_strategies(status, limit=limit))
            seen = set()
            items = []
            for strategy in strategies:
                sid = strategy.get('id')
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                rebuilt = await self.rebuild_projection(db, sid, limit=200, source=source, persist=True)
                items.append({
                    'strategy_id': sid,
                    'aggregate_version': (rebuilt.get('projection') or {}).get('aggregate_version'),
                    'snapshot_id': ((rebuilt.get('snapshot') or {}).get('id')),
                })
            result = {
                'task_run_id': task_run.get('id'),
                'count': len(items),
                'items': items,
            }
            if task_run.get('id') is not None and hasattr(db, 'update_strategy_task_run'):
                await db.update_strategy_task_run(task_run['id'], status='completed', result=result)
            if hasattr(db, 'save_strategy_domain_event'):
                await db.save_strategy_domain_event({
                    'strategy_id': None,
                    'aggregate_type': 'strategy_projection_batch',
                    'aggregate_id': str(task_run.get('id') or 'batch'),
                    'event_type': 'projection.batch_rebuilt',
                    'source': source,
                    'severity': 'info',
                    'correlation_id': task_run.get('trace_id'),
                    'payload': {'count': len(items), 'statuses': statuses},
                })
            return result
        except Exception as exc:
            if task_run.get('id') is not None and hasattr(db, 'update_strategy_task_run'):
                await db.update_strategy_task_run(task_run['id'], status='failed', error=str(exc), result={'statuses': statuses})
            raise


_domain_projection_service: Optional[StrategyDomainProjectionService] = None


def get_strategy_domain_projection_service() -> StrategyDomainProjectionService:
    global _domain_projection_service
    if _domain_projection_service is None:
        _domain_projection_service = StrategyDomainProjectionService()
    return _domain_projection_service
