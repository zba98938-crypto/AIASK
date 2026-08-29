"""策略运行时控制面：人工/自动熔断、节流、恢复。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class StrategyRuntimeControlService:
    BLOCKING_MODES = {'halted', 'manual_stop'}

    @classmethod
    def is_blocking_mode(cls, mode: Optional[str]) -> bool:
        return str(mode or 'active').strip().lower() in cls.BLOCKING_MODES

    async def _latest_suspend_source(self, db, strategy_id: str) -> Optional[str]:
        if not hasattr(db, 'list_strategy_status_events'):
            return None
        rows = await db.list_strategy_status_events(strategy_id, to_status='suspended', limit=1)
        if not rows:
            return None
        return rows[0].get('from_status') or None

    async def set_control(
        self,
        db,
        strategy: dict,
        control_mode: str = 'active',
        *,
        source: str = 'manual',
        reason: Optional[str] = None,
        trigger_event_type: Optional[str] = None,
        action_summary: Optional[dict] = None,
        metadata: Optional[dict] = None,
        apply_runtime_changes: bool = True,
    ) -> dict:
        from .strategy_lifecycle_shared import (
            update_status as _update_status,
            validate_transition as _validate_transition,
        )

        sid = str(strategy.get('id') or '').strip()
        if not sid:
            raise ValueError('strategy id is required')

        mode = str(control_mode or 'active').strip().lower() or 'active'
        trace_metadata = dict(metadata or {})
        existing = await db.get_strategy_runtime_control(sid) if hasattr(db, 'get_strategy_runtime_control') else None
        existing_mode = str((existing or {}).get('control_mode') or 'active').strip().lower()
        priority = {'active': 0, 'throttled': 1, 'halted': 2, 'manual_stop': 3}
        if mode != 'active' and priority.get(existing_mode, 0) > priority.get(mode, 0):
            mode = existing_mode
        account = await db.get_strategy_incubation_account(sid) if hasattr(db, 'get_strategy_incubation_account') else None
        account_id = (account or {}).get('account_id')
        latest_metric = await db.get_latest_strategy_incubation_metric(sid) if hasattr(db, 'get_latest_strategy_incubation_metric') else None
        now = datetime.now(timezone.utc)

        row = await db.save_strategy_runtime_control({
            'strategy_id': sid,
            'account_id': account_id,
            'control_mode': mode,
            'status': 'released' if mode == 'active' else 'engaged',
            'source': source,
            'trigger_event_type': trigger_event_type,
            'reason': reason,
            'action_summary': action_summary or {},
            'metadata': {
                **dict((existing or {}).get('metadata') or {}),
                **trace_metadata,
            },
            'activated_at': (existing or {}).get('activated_at') if mode == 'active' and existing else now,
            'released_at': now if mode == 'active' else None,
        }) if hasattr(db, 'save_strategy_runtime_control') else {
            'strategy_id': sid,
            'account_id': account_id,
            'control_mode': mode,
            'status': 'released' if mode == 'active' else 'engaged',
            'source': source,
            'trigger_event_type': trigger_event_type,
            'reason': reason,
            'action_summary': action_summary or {},
            'metadata': metadata or {},
            'activated_at': now,
            'released_at': now if mode == 'active' else None,
        }

        transition = None
        current_status = str(strategy.get('status') or '')
        if apply_runtime_changes:
            if mode in self.BLOCKING_MODES:
                if account_id and hasattr(db, 'update_paper_account_status'):
                    await db.update_paper_account_status(account_id, 'frozen', promotion_candidate=False)
                if _validate_transition(current_status, 'suspended'):
                    await _update_status(
                        db,
                        sid,
                        'suspended',
                        actor_id=source,
                        reason='runtime_control_engaged',
                        metadata={
                            'control_mode': mode,
                            'trigger_event_type': trigger_event_type,
                            'reason': reason,
                            **trace_metadata,
                        },
                    )
                    transition = {'from': current_status, 'to': 'suspended'}
            elif mode == 'throttled':
                if account_id and hasattr(db, 'update_paper_account_status'):
                    await db.update_paper_account_status(account_id, 'guarded', promotion_candidate=False)
            else:
                if account_id and hasattr(db, 'update_paper_account_status'):
                    await db.update_paper_account_status(
                        account_id,
                        'active',
                        promotion_candidate=bool((latest_metric or {}).get('decision') == 'promote'),
                    )
                if current_status == 'suspended':
                    recover_to = await self._latest_suspend_source(db, sid) or 'listed'
                    if _validate_transition(current_status, recover_to):
                        await _update_status(
                            db,
                        sid,
                        recover_to,
                        actor_id=source,
                        reason='runtime_control_released',
                        metadata={
                            'control_mode': mode,
                            'trigger_event_type': trigger_event_type,
                            'reason': reason,
                            **trace_metadata,
                        },
                    )
                        transition = {'from': current_status, 'to': recover_to}

        if hasattr(db, 'save_strategy_domain_event'):
            await db.save_strategy_domain_event({
                'strategy_id': sid,
                'aggregate_type': 'strategy_runtime_control',
                'aggregate_id': sid,
                'event_type': 'runtime_control.changed',
                'source': source,
                'severity': 'warning' if mode in self.BLOCKING_MODES else ('info' if mode == 'active' else 'medium'),
                'correlation_id': trace_metadata.get('correlation_id') or account_id,
                'payload': {
                    'previous_mode': (existing or {}).get('control_mode'),
                    'control_mode': mode,
                    'trigger_event_type': trigger_event_type,
                    'reason': reason,
                    'action_summary': action_summary or {},
                    'transition': transition,
                    'trace': trace_metadata,
                },
            })

        return {**row, 'transition': transition}


_runtime_control_service: Optional[StrategyRuntimeControlService] = None


def get_strategy_runtime_control_service() -> StrategyRuntimeControlService:
    global _runtime_control_service
    if _runtime_control_service is None:
        _runtime_control_service = StrategyRuntimeControlService()
    return _runtime_control_service
