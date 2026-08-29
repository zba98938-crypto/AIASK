"""策略运行态告警：分发、确认、收敛。"""

from __future__ import annotations

from typing import Optional


class StrategyRuntimeAlertService:
    BLOCKING_MODES = {'halted', 'manual_stop'}

    @staticmethod
    def _severity_rank(severity: Optional[str]) -> int:
        order = {'critical': 3, 'high': 2, 'medium': 1, 'warning': 1, 'info': 0}
        return order.get(str(severity or 'info').strip().lower(), 0)

    @staticmethod
    def _status_rank(status: Optional[str]) -> int:
        order = {'open': 2, 'acknowledged': 1, 'resolved': 0}
        return order.get(str(status or 'resolved').strip().lower(), 0)

    async def _upsert_alert(
        self,
        db,
        *,
        strategy_id: str,
        account_id: Optional[str],
        alert_key: str,
        category: str,
        severity: str,
        title: str,
        message: str,
        escalation_level: int,
        channels: Optional[list[str]] = None,
        related_event_ids: Optional[list[int]] = None,
        metadata: Optional[dict] = None,
        source: str = 'runtime_alerts',
    ) -> dict:
        existing = await db.get_latest_strategy_runtime_alert(
            strategy_id,
            alert_key=alert_key,
            status='open_or_ack',
        ) if hasattr(db, 'get_latest_strategy_runtime_alert') else None
        payload = {
            'strategy_id': strategy_id,
            'account_id': account_id,
            'alert_key': alert_key,
            'category': category,
            'severity': severity,
            'status': 'acknowledged' if str((existing or {}).get('status') or '').lower() == 'acknowledged' else 'open',
            'title': title,
            'message': message,
            'escalation_level': escalation_level,
            'channels': list(channels or ['dashboard']),
            'related_event_ids': list(related_event_ids or []),
            'metadata': dict(metadata or {}),
            'source': source,
        }
        if (existing or {}).get('alert_id') is not None:
            payload['alert_id'] = existing.get('alert_id')
        return await db.save_strategy_runtime_alert(payload)

    async def _resolve_missing_categories(self, db, *, strategy_id: str, active_categories: set[str], source: str) -> list[dict]:
        resolved = []
        open_alerts = await db.list_strategy_runtime_alerts(
            strategy_id=strategy_id,
            status='open_or_ack',
            limit=100,
        ) if hasattr(db, 'list_strategy_runtime_alerts') else []
        for item in open_alerts:
            category = str(item.get('category') or '').strip()
            if category and category not in active_categories:
                row = await db.resolve_strategy_runtime_alerts(
                    strategy_id=strategy_id,
                    alert_id=item.get('alert_id'),
                    resolution={
                        'resolution': 'condition_cleared',
                        'source': source,
                        'category': category,
                    },
                    source=source,
                ) if hasattr(db, 'resolve_strategy_runtime_alerts') else []
                if row:
                    resolved.extend(row)
        return resolved

    async def dispatch_for_strategy(
        self,
        db,
        strategy: dict,
        *,
        latest_snapshot: Optional[dict] = None,
        runtime_control: Optional[dict] = None,
        open_events: Optional[list[dict]] = None,
        source: str = 'runtime_alerts',
    ) -> dict:
        sid = str((strategy or {}).get('id') or '').strip()
        if not sid:
            return {'strategy_id': sid, 'alerts': [], 'alert_count': 0, 'resolved': [], 'resolved_count': 0}

        snapshot = latest_snapshot
        if snapshot is None and hasattr(db, 'get_latest_strategy_runtime_risk_snapshot'):
            snapshot = await db.get_latest_strategy_runtime_risk_snapshot(sid)
        control = runtime_control
        if control is None and hasattr(db, 'get_strategy_runtime_control'):
            control = await db.get_strategy_runtime_control(sid)
        risk_events = list(open_events or [])
        if not risk_events and hasattr(db, 'list_strategy_runtime_risk_events'):
            risk_events = await db.list_strategy_runtime_risk_events(strategy_id=sid, status='open', limit=100)
        account_id = (snapshot or {}).get('account_id') or (control or {}).get('account_id') or next((item.get('account_id') for item in risk_events if item.get('account_id')), None)

        posture_level = str((snapshot or {}).get('posture_level') or 'safe').strip().lower()
        control_mode = str((snapshot or {}).get('control_mode') or (control or {}).get('control_mode') or 'active').strip().lower()
        recovery_eligible = bool((snapshot or {}).get('recovery_eligible'))
        open_event_count = int((snapshot or {}).get('open_event_count') or len(risk_events))
        related_event_ids = [int(item['id']) for item in risk_events if item.get('id') is not None]
        max_severity = 'info'
        for item in risk_events:
            if self._severity_rank(item.get('severity')) > self._severity_rank(max_severity):
                max_severity = str(item.get('severity') or 'info')

        alerts = []
        active_categories: set[str] = set()

        if posture_level in {'critical', 'guarded', 'recovering'} or open_event_count > 0:
            severity = max_severity if open_event_count > 0 else ('high' if posture_level == 'critical' else 'medium')
            alerts.append(await self._upsert_alert(
                db,
                strategy_id=sid,
                account_id=account_id,
                alert_key=f'posture:{sid}:{posture_level}',
                category='critical_posture',
                severity=severity,
                title='运行风险姿态告警',
                message=f'策略 {sid} 当前风险姿态为 {posture_level}，开放风险事件 {open_event_count} 个。',
                escalation_level=int((snapshot or {}).get('escalation_level') or 0),
                channels=['dashboard', 'runtime_ops'],
                related_event_ids=related_event_ids,
                metadata={
                    'posture_level': posture_level,
                    'control_mode': control_mode,
                    'open_event_count': open_event_count,
                    'recommended_action': (snapshot or {}).get('recommended_action'),
                },
                source=source,
            ))
            active_categories.add('critical_posture')

        if control_mode in self.BLOCKING_MODES:
            alerts.append(await self._upsert_alert(
                db,
                strategy_id=sid,
                account_id=account_id,
                alert_key=f'control:{sid}:{control_mode}',
                category='halted_control',
                severity='critical' if control_mode == 'halted' else 'high',
                title='运行控制已熔断',
                message=f'策略 {sid} 当前控制模式为 {control_mode}，已进入阻断态。',
                escalation_level=max(2, int((snapshot or {}).get('escalation_level') or 0)),
                channels=['dashboard', 'runtime_ops'],
                related_event_ids=related_event_ids,
                metadata={
                    'control_mode': control_mode,
                    'control_status': (control or {}).get('status'),
                    'reason': (control or {}).get('reason'),
                },
                source=source,
            ))
            active_categories.add('halted_control')

        if recovery_eligible:
            alerts.append(await self._upsert_alert(
                db,
                strategy_id=sid,
                account_id=account_id,
                alert_key=f'recovery:{sid}',
                category='recovery_ready',
                severity='info' if control_mode == 'active' else 'medium',
                title='策略满足恢复条件',
                message=f'策略 {sid} 已满足恢复检查条件，可进入人工/自动恢复流程。',
                escalation_level=1,
                channels=['dashboard'],
                related_event_ids=related_event_ids,
                metadata={
                    'posture_level': posture_level,
                    'control_mode': control_mode,
                    'recommended_action': (snapshot or {}).get('recommended_action'),
                },
                source=source,
            ))
            active_categories.add('recovery_ready')

        resolved = await self._resolve_missing_categories(db, strategy_id=sid, active_categories=active_categories, source=source)
        return {
            'strategy_id': sid,
            'alerts': alerts,
            'alert_count': len(alerts),
            'resolved': resolved,
            'resolved_count': len(resolved),
            'snapshot': snapshot,
            'runtime_control': control,
        }

    async def dispatch_batch(self, db, strategies: Optional[list[dict]] = None, *, source: str = 'runtime_alerts') -> dict:
        if strategies is None:
            strategies = []
            for status in ('incubating', 'listed', 'suspended'):
                rows = await db.list_strategies(status, limit=200)
                strategies.extend(rows)

        dispatched = []
        resolved = []
        for strategy in strategies:
            result = await self.dispatch_for_strategy(db, strategy, source=source)
            dispatched.extend(result.get('alerts') or [])
            resolved.extend(result.get('resolved') or [])
        return {
            'scanned': len(strategies),
            'alerts': dispatched,
            'alert_count': len(dispatched),
            'resolved': resolved,
            'resolved_count': len(resolved),
        }

    async def acknowledge_alert(self, db, alert_id: int, *, acknowledged_by: Optional[str] = None, source: str = 'runtime_alerts') -> Optional[dict]:
        if not hasattr(db, 'acknowledge_strategy_runtime_alert'):
            return None
        return await db.acknowledge_strategy_runtime_alert(int(alert_id), acknowledged_by=acknowledged_by, source=source)

    async def resolve_by_strategy(self, db, strategy_id: str, *, category: Optional[str] = None, reason: Optional[str] = None, source: str = 'runtime_alerts') -> list[dict]:
        if not hasattr(db, 'resolve_strategy_runtime_alerts'):
            return []
        return await db.resolve_strategy_runtime_alerts(
            strategy_id=str(strategy_id or '').strip() or None,
            category=(str(category or '').strip() or None),
            resolution={
                'resolution': reason or 'manual_close',
                'source': source,
            },
            source=source,
        )


_runtime_alert_service: Optional[StrategyRuntimeAlertService] = None


def get_strategy_runtime_alert_service() -> StrategyRuntimeAlertService:
    global _runtime_alert_service
    if _runtime_alert_service is None:
        _runtime_alert_service = StrategyRuntimeAlertService()
    return _runtime_alert_service
