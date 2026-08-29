"""策略运行时风控：告警、熔断、恢复。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class StrategyRuntimeRiskService:
    def __init__(self):
        self.critical_drawdown = 0.30
        self.recovery_drawdown = 0.18
        self.daily_loss_limit = -0.08
        self.over_exposure_limit = 0.95
        self.alpha_decay_limit = 0.35
        self.drift_limit = 0.45

    async def _set_account_status(self, db, account_id: str, status: str, promotion_candidate: Optional[bool] = None):
        if hasattr(db, 'update_paper_account_status'):
            await db.update_paper_account_status(account_id, status, promotion_candidate=promotion_candidate)
            return
        async with db.acquire() as conn:
            if promotion_candidate is None:
                await conn.execute(
                    "UPDATE paper_accounts SET status=$1, updated_at=CURRENT_TIMESTAMP WHERE id=$2",
                    status,
                    account_id,
                )
            else:
                await conn.execute(
                    "UPDATE paper_accounts SET status=$1, promotion_candidate=$2, updated_at=CURRENT_TIMESTAMP WHERE id=$3",
                    status,
                    bool(promotion_candidate),
                    account_id,
                )

    async def _latest_suspend_source(self, db, strategy_id: str) -> Optional[str]:
        if not hasattr(db, 'list_strategy_status_events'):
            return None
        rows = await db.list_strategy_status_events(strategy_id, to_status='suspended', limit=1)
        if not rows:
            return None
        return rows[0].get('from_status') or None

    async def _suspend_strategy(self, db, strategy: dict, metric: dict):
        # Fix #10/#14: 从 strategy_lifecycle_shared 导入，避免循环依赖
        from .strategy_lifecycle_shared import update_status as _update_status, validate_transition as _validate_transition

        current = str(strategy.get('status') or '')
        if _validate_transition(current, 'suspended'):
            await _update_status(
                db,
                strategy['id'],
                'suspended',
                actor_id='runtime_risk',
                reason='runtime_circuit_breaker',
                metadata={'metric': metric},
            )

    async def _recover_strategy(self, db, strategy: dict, metric: dict):
        # Fix #10/#14: 从 strategy_lifecycle_shared 导入，避免循环依赖
        from .strategy_lifecycle_shared import update_status as _update_status, validate_transition as _validate_transition

        # Fix #14: 恢复目标应考虑孵化中策略，而非一律默认 'listed'
        recover_to = await self._latest_suspend_source(db, strategy['id'])
        if not recover_to:
            # 根据策略当前上下文决定恢复目标
            recover_to = 'incubating' if strategy.get('status') == 'suspended' and self._was_incubating(strategy) else 'listed'
        current = str(strategy.get('status') or '')
        if _validate_transition(current, recover_to):
            await _update_status(
                db,
                strategy['id'],
                recover_to,
                actor_id='runtime_risk',
                reason='runtime_recovered',
                metadata={'metric': metric},
            )
            return recover_to
        return None

    @staticmethod
    def _was_incubating(strategy: dict) -> bool:
        """判断策略是否曾处于孵化状态（通过 metadata/tags 线索推断）"""
        tags = {str(t).strip().lower() for t in (strategy.get('tags') or [])}
        if 'incubating' in tags or 'factory' in tags:
            return True
        return False

    async def _record_domain_event(self, db, strategy_id: Optional[str], event_type: str, payload: dict, *, source: str = 'runtime_risk', severity: str = 'info', correlation_id: Optional[str] = None):
        if hasattr(db, 'save_strategy_domain_event'):
            await db.save_strategy_domain_event({
                'strategy_id': strategy_id,
                'aggregate_type': 'strategy',
                'aggregate_id': strategy_id,
                'event_type': event_type,
                'source': source,
                'severity': severity,
                'correlation_id': correlation_id,
                'payload': payload,
            })

    async def _execute_risk_actions(self, db, strategy_id: str, account_id: Optional[str], trigger: str, metric: dict) -> list[dict]:
        if not account_id:
            return []
        try:
            from .risk_executor import get_risk_executor
            actions = await get_risk_executor().enforce(account_id)
        except Exception as exc:
            logger.warning('StrategyRuntimeRiskService._execute_risk_actions failed for %s/%s: %s', strategy_id, account_id, exc)
            return []
        action_rows = [item.to_dict() for item in actions]
        if action_rows:
            await self._record_domain_event(
                db,
                strategy_id,
                'runtime_risk.actions_executed',
                {
                    'account_id': account_id,
                    'trigger': trigger,
                    'metric': metric,
                    'actions': action_rows,
                },
                severity='warning',
                correlation_id=account_id,
            )
        return action_rows

    @staticmethod
    def _needs_emit(open_types: set[str], event_type: str) -> bool:
        return event_type not in open_types

    @staticmethod
    def _severity_rank(severity: str) -> int:
        order = {'critical': 3, 'high': 2, 'medium': 1, 'warning': 1, 'info': 0}
        return order.get(str(severity or 'info').lower(), 0)

    def _posture(self, *, open_events: list[dict], control_mode: str, recovery_eligible: bool, latest_metric: dict) -> tuple[str, int, str]:
        critical_open_count = len([item for item in open_events if self._severity_rank(item.get('severity')) >= 3])
        warning_open_count = len([item for item in open_events if self._severity_rank(item.get('severity')) >= 1])
        max_drawdown = abs(float((latest_metric or {}).get('max_drawdown') or 0))
        daily_return = float((latest_metric or {}).get('daily_return') or 0)
        if str(control_mode or 'active') in {'halted', 'manual_stop'} or critical_open_count > 0:
            return 'critical', 3, 'manual_recovery_review' if recovery_eligible else 'halt_and_review'
        if recovery_eligible:
            return 'recovering', 1, 'recovery_check'
        if warning_open_count >= 2 or daily_return <= self.daily_loss_limit or max_drawdown >= self.recovery_drawdown:
            return 'guarded', 2, 'throttle_and_observe'
        return 'safe', 0, 'monitor'

    async def _save_snapshot(
        self,
        db,
        *,
        strategy: dict,
        account_id: Optional[str],
        open_events: list[dict],
        control_mode: str,
        latest_metric: dict,
        source: str,
        task_run_id: Optional[int] = None,
    ) -> Optional[dict]:
        if not hasattr(db, 'save_strategy_runtime_risk_snapshot'):
            return None
        critical_open_count = len([item for item in open_events if self._severity_rank(item.get('severity')) >= 3])
        warning_open_count = len([item for item in open_events if self._severity_rank(item.get('severity')) >= 1])
        recovery_eligible = (
            str(strategy.get('status') or '') == 'suspended'
            and abs(float((latest_metric or {}).get('max_drawdown') or 0)) < self.recovery_drawdown
            and float((latest_metric or {}).get('daily_return') or 0) > -0.03
            and float((latest_metric or {}).get('drift_score') or 0) < 0.25
            and critical_open_count == 0
        )
        posture_level, escalation_level, recommended_action = self._posture(
            open_events=open_events,
            control_mode=control_mode,
            recovery_eligible=recovery_eligible,
            latest_metric=latest_metric,
        )
        snapshot = await db.save_strategy_runtime_risk_snapshot({
            'strategy_id': strategy.get('id'),
            'account_id': account_id,
            'posture_level': posture_level,
            'escalation_level': escalation_level,
            'control_mode': control_mode,
            'open_event_count': len(open_events),
            'critical_open_count': critical_open_count,
            'warning_open_count': warning_open_count,
            'recommended_action': recommended_action,
            'recovery_eligible': recovery_eligible,
            'blockers': [item.get('event_type') for item in open_events if item.get('event_type')],
            'summary': {
                'strategy_status': strategy.get('status'),
                'latest_metric': {
                    'daily_return': latest_metric.get('daily_return'),
                    'max_drawdown': latest_metric.get('max_drawdown'),
                    'exposure_rate': latest_metric.get('exposure_rate'),
                    'alpha_decay': latest_metric.get('alpha_decay'),
                    'drift_score': latest_metric.get('drift_score'),
                },
                'open_events': [
                    {
                        'id': item.get('id'),
                        'event_type': item.get('event_type'),
                        'severity': item.get('severity'),
                        'status': item.get('status'),
                    }
                    for item in open_events[:10]
                ],
            },
            'metadata': {
                'thresholds': {
                    'critical_drawdown': self.critical_drawdown,
                    'daily_loss_limit': self.daily_loss_limit,
                    'over_exposure_limit': self.over_exposure_limit,
                    'alpha_decay_limit': self.alpha_decay_limit,
                    'drift_limit': self.drift_limit,
                },
            },
            'task_run_id': task_run_id,
            'source': source,
            'evaluated_at': datetime.now(timezone.utc).isoformat(),
        })
        await self._record_domain_event(
            db,
            strategy.get('id'),
            'runtime_risk.snapshot_recorded',
            {
                'posture_level': posture_level,
                'escalation_level': escalation_level,
                'control_mode': control_mode,
                'recommended_action': recommended_action,
                'recovery_eligible': recovery_eligible,
                'open_event_count': len(open_events),
            },
            source=source,
            severity='warning' if posture_level in {'critical', 'guarded'} else 'info',
            correlation_id=account_id,
        )
        return snapshot

    async def _attempt_auto_recovery(self, db, strategy: dict, metric: dict, open_events: list[dict], account_id: Optional[str], control_service) -> Optional[dict]:
        sid = str(strategy.get('id') or '')
        if str(strategy.get('status') or '') != 'suspended':
            return None
        if abs(float(metric.get('max_drawdown') or 0)) >= self.recovery_drawdown:
            return None
        if float(metric.get('daily_return') or 0) <= -0.03:
            return None
        if float(metric.get('drift_score') or 0) >= 0.25:
            return None
        critical_open = [item for item in open_events if self._severity_rank(item.get('severity')) >= 3]
        if critical_open and hasattr(db, 'resolve_strategy_runtime_risk_event'):
            for item in critical_open:
                await db.resolve_strategy_runtime_risk_event(item['id'], {
                    'resolution': 'auto_recovered',
                    'recovery_gate': 'metric_safe_zone',
                })
        recovered_to = await self._recover_strategy(db, strategy, metric)
        if not recovered_to:
            return None
        if account_id:
            await self._set_account_status(db, account_id, 'active', promotion_candidate=bool(metric.get('decision') == 'promote'))
        await self._record_domain_event(db, sid, 'runtime_risk.recovered', {'account_id': account_id, 'to_status': recovered_to, 'metric': metric}, severity='info', correlation_id=account_id)
        await control_service.set_control(
            db,
            {**strategy, 'status': recovered_to},
            control_mode='active',
            source='runtime_risk',
            reason='recovery_guard_released',
            trigger_event_type='recovered',
            action_summary={'to_status': recovered_to},
            metadata={'metric': metric},
            apply_runtime_changes=False,
        )
        return {'strategy_id': sid, 'to_status': recovered_to}

    async def attempt_recovery(self, db, strategy: dict, *, source: str = 'manual_recovery') -> dict:
        sid = str(strategy.get('id') or '')
        metric = await db.get_latest_strategy_incubation_metric(sid) if hasattr(db, 'get_latest_strategy_incubation_metric') else None
        if not metric:
            return {'strategy_id': sid, 'eligible': False, 'reason': 'latest_metric_missing'}
        account = await db.get_strategy_incubation_account(sid) if hasattr(db, 'get_strategy_incubation_account') else None
        account_id = (account or {}).get('account_id')
        open_events = await db.list_strategy_runtime_risk_events(strategy_id=sid, status='open', limit=100) if hasattr(db, 'list_strategy_runtime_risk_events') else []
        from .runtime_control import get_strategy_runtime_control_service
        from .runtime_alerts import get_strategy_runtime_alert_service
        control_service = get_strategy_runtime_control_service()
        alert_service = get_strategy_runtime_alert_service()
        recovered = await self._attempt_auto_recovery(db, strategy, metric, open_events, account_id, control_service)
        latest_control = await db.get_strategy_runtime_control(sid) if hasattr(db, 'get_strategy_runtime_control') else {'control_mode': 'active'}
        remaining_open = await db.list_strategy_runtime_risk_events(strategy_id=sid, status='open', limit=100) if hasattr(db, 'list_strategy_runtime_risk_events') else []
        snapshot = await self._save_snapshot(
            db,
            strategy={**strategy, 'status': recovered.get('to_status') if recovered else strategy.get('status')},
            account_id=account_id,
            open_events=remaining_open,
            control_mode=str((latest_control or {}).get('control_mode') or 'active'),
            latest_metric=metric,
            source=source,
        )
        alert_result = await alert_service.dispatch_for_strategy(
            db,
            {**strategy, 'status': recovered.get('to_status') if recovered else strategy.get('status')},
            latest_snapshot=snapshot,
            runtime_control=latest_control,
            open_events=remaining_open,
            source=source,
        )
        if not recovered:
            latest_snapshot = await db.get_latest_strategy_runtime_risk_snapshot(sid) if hasattr(db, 'get_latest_strategy_runtime_risk_snapshot') else snapshot
            return {
                'strategy_id': sid,
                'eligible': bool((latest_snapshot or {}).get('recovery_eligible')),
                'reason': 'recovery_conditions_not_met',
                'snapshot': latest_snapshot,
                'alerts': alert_result.get('alerts') or [],
                'resolved_alerts': alert_result.get('resolved') or [],
            }
        return {
            'strategy_id': sid,
            'eligible': True,
            'recovered': True,
            'recovery': recovered,
            'snapshot': snapshot,
            'alerts': alert_result.get('alerts') or [],
            'resolved_alerts': alert_result.get('resolved') or [],
        }

    async def scan(self, db, strategies: Optional[list[dict]] = None, enforce_actions: bool = True) -> dict:
        if strategies is None:
            strategies = []
            for status in ('incubating', 'listed', 'suspended'):
                rows = await db.list_strategies(status, limit=200)
                strategies.extend(rows)

        emitted = []
        suspended = []
        recovered = []
        action_items = []
        snapshots = []
        alert_items = []
        resolved_alert_items = []
        executed_accounts: set[str] = set()

        from .runtime_control import get_strategy_runtime_control_service
        from .runtime_alerts import get_strategy_runtime_alert_service
        control_service = get_strategy_runtime_control_service()
        alert_service = get_strategy_runtime_alert_service()

        for strategy in strategies:
            sid = strategy.get('id')
            metric = await db.get_latest_strategy_incubation_metric(sid) if hasattr(db, 'get_latest_strategy_incubation_metric') else None
            if not metric:
                continue
            account = await db.get_strategy_incubation_account(sid) if hasattr(db, 'get_strategy_incubation_account') else None
            account_id = (account or {}).get('account_id')
            open_events = await db.list_strategy_runtime_risk_events(strategy_id=sid, status='open', limit=100) if hasattr(db, 'list_strategy_runtime_risk_events') else []
            open_types = {item.get('event_type') for item in open_events}

            daily_return = float(metric.get('daily_return') or 0)
            max_drawdown = abs(float(metric.get('max_drawdown') or 0))
            exposure_rate = float(metric.get('exposure_rate') or 0)
            alpha_decay = float(metric.get('alpha_decay') or 0)
            drift_score = float(metric.get('drift_score') or 0)

            if max_drawdown >= self.critical_drawdown and self._needs_emit(open_types, 'drawdown_breach'):
                event = await db.save_strategy_runtime_risk_event({
                    'strategy_id': sid,
                    'account_id': account_id,
                    'severity': 'critical',
                    'event_type': 'drawdown_breach',
                    'action': 'suspend_strategy',
                    'title': '策略回撤触发熔断',
                    'reason': f'最大回撤 {max_drawdown:.1%} ≥ {self.critical_drawdown:.0%}',
                    'payload': {'metric': metric},
                })
                emitted.append(event)
                if account_id:
                    await self._set_account_status(db, account_id, 'frozen', promotion_candidate=False)
                await self._suspend_strategy(db, strategy, metric)
                suspended.append({'strategy_id': sid, 'reason': event.get('reason')})
                await self._record_domain_event(db, sid, 'runtime_risk.drawdown_breach', {'account_id': account_id, 'reason': event.get('reason'), 'metric': metric}, severity='critical', correlation_id=account_id)
                await control_service.set_control(
                    db,
                    strategy,
                    control_mode='halted',
                    source='runtime_risk',
                    reason=event.get('reason'),
                    trigger_event_type='drawdown_breach',
                    action_summary={'event_id': event.get('id'), 'severity': event.get('severity')},
                    metadata={'metric': metric},
                    apply_runtime_changes=False,
                )
                if enforce_actions and account_id not in executed_accounts:
                    action_items.extend(await self._execute_risk_actions(db, sid, account_id, 'drawdown_breach', metric))
                    executed_accounts.add(account_id)

            if daily_return <= self.daily_loss_limit and self._needs_emit(open_types, 'daily_loss_spike'):
                event = await db.save_strategy_runtime_risk_event({
                    'strategy_id': sid,
                    'account_id': account_id,
                    'severity': 'high',
                    'event_type': 'daily_loss_spike',
                    'action': 'freeze_account',
                    'title': '单日大幅亏损',
                    'reason': f'日收益 {daily_return:.1%} ≤ {self.daily_loss_limit:.1%}',
                    'payload': {'metric': metric},
                })
                emitted.append(event)
                if account_id:
                    await self._set_account_status(db, account_id, 'frozen', promotion_candidate=False)
                await self._record_domain_event(db, sid, 'runtime_risk.daily_loss_spike', {'account_id': account_id, 'metric': metric}, severity='warning', correlation_id=account_id)
                await control_service.set_control(
                    db,
                    strategy,
                    control_mode='throttled',
                    source='runtime_risk',
                    reason=f"日收益 {daily_return:.1%} ≤ {self.daily_loss_limit:.1%}",
                    trigger_event_type='daily_loss_spike',
                    action_summary={'severity': 'high'},
                    metadata={'metric': metric},
                    apply_runtime_changes=False,
                )
                if enforce_actions and account_id not in executed_accounts:
                    action_items.extend(await self._execute_risk_actions(db, sid, account_id, 'daily_loss_spike', metric))
                    executed_accounts.add(account_id)

            if exposure_rate >= self.over_exposure_limit and self._needs_emit(open_types, 'over_exposure'):
                event = await db.save_strategy_runtime_risk_event({
                    'strategy_id': sid,
                    'account_id': account_id,
                    'severity': 'medium',
                    'event_type': 'over_exposure',
                    'action': 'reduce_position',
                    'title': '仓位暴露过高',
                    'reason': f'暴露率 {exposure_rate:.1%} ≥ {self.over_exposure_limit:.0%}',
                    'payload': {'metric': metric},
                })
                emitted.append(event)
                await self._record_domain_event(db, sid, 'runtime_risk.over_exposure', {'account_id': account_id, 'metric': metric}, severity='warning', correlation_id=account_id)
                await control_service.set_control(
                    db,
                    strategy,
                    control_mode='throttled',
                    source='runtime_risk',
                    reason=f"暴露率 {exposure_rate:.1%} ≥ {self.over_exposure_limit:.0%}",
                    trigger_event_type='over_exposure',
                    action_summary={'severity': 'medium'},
                    metadata={'metric': metric},
                    apply_runtime_changes=False,
                )
                if enforce_actions and account_id not in executed_accounts:
                    action_items.extend(await self._execute_risk_actions(db, sid, account_id, 'over_exposure', metric))
                    executed_accounts.add(account_id)

            if alpha_decay >= self.alpha_decay_limit and self._needs_emit(open_types, 'alpha_decay'):
                event = await db.save_strategy_runtime_risk_event({
                    'strategy_id': sid,
                    'account_id': account_id,
                    'severity': 'medium',
                    'event_type': 'alpha_decay',
                    'action': 'review_required',
                    'title': 'Alpha 衰减告警',
                    'reason': f'alpha_decay {alpha_decay:.2f} ≥ {self.alpha_decay_limit:.2f}',
                    'payload': {'metric': metric},
                })
                emitted.append(event)
                await self._record_domain_event(db, sid, 'runtime_risk.alpha_decay', {'account_id': account_id, 'metric': metric}, severity='info', correlation_id=account_id)

            if drift_score >= self.drift_limit and self._needs_emit(open_types, 'behavior_drift'):
                event = await db.save_strategy_runtime_risk_event({
                    'strategy_id': sid,
                    'account_id': account_id,
                    'severity': 'medium',
                    'event_type': 'behavior_drift',
                    'action': 'review_required',
                    'title': '策略行为漂移',
                    'reason': f'drift_score {drift_score:.2f} ≥ {self.drift_limit:.2f}',
                    'payload': {'metric': metric},
                })
                emitted.append(event)
                await self._record_domain_event(db, sid, 'runtime_risk.behavior_drift', {'account_id': account_id, 'metric': metric}, severity='info', correlation_id=account_id)

            composite_cluster = daily_return <= self.daily_loss_limit and exposure_rate >= self.over_exposure_limit
            if composite_cluster and self._needs_emit(open_types, 'liquidity_stress'):
                event = await db.save_strategy_runtime_risk_event({
                    'strategy_id': sid,
                    'account_id': account_id,
                    'severity': 'critical',
                    'event_type': 'liquidity_stress',
                    'action': 'halt_and_liquidate',
                    'title': '损失与暴露复合熔断',
                    'reason': f'日收益 {daily_return:.1%} 且暴露率 {exposure_rate:.1%} 触发复合熔断',
                    'payload': {'metric': metric},
                })
                emitted.append(event)
                if account_id:
                    await self._set_account_status(db, account_id, 'frozen', promotion_candidate=False)
                await self._suspend_strategy(db, strategy, metric)
                suspended.append({'strategy_id': sid, 'reason': event.get('reason')})
                await control_service.set_control(
                    db,
                    strategy,
                    control_mode='halted',
                    source='runtime_risk',
                    reason=event.get('reason'),
                    trigger_event_type='liquidity_stress',
                    action_summary={'event_id': event.get('id'), 'composite': True},
                    metadata={'metric': metric},
                    apply_runtime_changes=False,
                )
                if enforce_actions and account_id not in executed_accounts:
                    action_items.extend(await self._execute_risk_actions(db, sid, account_id, 'liquidity_stress', metric))
                    executed_accounts.add(account_id)

            composite_model_breakdown = alpha_decay >= self.alpha_decay_limit and drift_score >= self.drift_limit
            if composite_model_breakdown and self._needs_emit(open_types, 'model_breakdown'):
                event = await db.save_strategy_runtime_risk_event({
                    'strategy_id': sid,
                    'account_id': account_id,
                    'severity': 'high',
                    'event_type': 'model_breakdown',
                    'action': 'force_review',
                    'title': '模型失稳',
                    'reason': f'alpha_decay {alpha_decay:.2f} 与 drift_score {drift_score:.2f} 同时超阈',
                    'payload': {'metric': metric},
                })
                emitted.append(event)
                await control_service.set_control(
                    db,
                    strategy,
                    control_mode='throttled',
                    source='runtime_risk',
                    reason=event.get('reason'),
                    trigger_event_type='model_breakdown',
                    action_summary={'event_id': event.get('id'), 'composite': True},
                    metadata={'metric': metric},
                    apply_runtime_changes=False,
                )
                await self._record_domain_event(db, sid, 'runtime_risk.model_breakdown', {'account_id': account_id, 'metric': metric}, severity='warning', correlation_id=account_id)

            latest_open_events = await db.list_strategy_runtime_risk_events(strategy_id=sid, status='open', limit=100) if hasattr(db, 'list_strategy_runtime_risk_events') else []
            auto_recovered = await self._attempt_auto_recovery(db, strategy, metric, latest_open_events, account_id, control_service)
            if auto_recovered:
                recovered.append(auto_recovered)

            latest_control = await db.get_strategy_runtime_control(sid) if hasattr(db, 'get_strategy_runtime_control') else {'control_mode': 'active'}
            latest_open_events = await db.list_strategy_runtime_risk_events(strategy_id=sid, status='open', limit=100) if hasattr(db, 'list_strategy_runtime_risk_events') else []
            snapshot = await self._save_snapshot(
                db,
                strategy={**strategy, 'status': auto_recovered.get('to_status') if auto_recovered else strategy.get('status')},
                account_id=account_id,
                open_events=latest_open_events,
                control_mode=str((latest_control or {}).get('control_mode') or 'active'),
                latest_metric=metric,
                source='runtime_risk',
            )
            if snapshot:
                snapshots.append(snapshot)
            alert_result = await alert_service.dispatch_for_strategy(
                db,
                {**strategy, 'status': auto_recovered.get('to_status') if auto_recovered else strategy.get('status')},
                latest_snapshot=snapshot,
                runtime_control=latest_control,
                open_events=latest_open_events,
                source='runtime_risk',
            )
            alert_items.extend(alert_result.get('alerts') or [])
            resolved_alert_items.extend(alert_result.get('resolved') or [])

        return {
            'scanned': len(strategies),
            'events': emitted,
            'event_count': len(emitted),
            'actions': action_items,
            'action_count': len(action_items),
            'suspended': suspended,
            'recovered': recovered,
            'snapshots': snapshots,
            'snapshot_count': len(snapshots),
            'alerts': alert_items,
            'alert_count': len(alert_items),
            'resolved_alerts': resolved_alert_items,
            'resolved_alert_count': len(resolved_alert_items),
        }


_runtime_risk_service: Optional[StrategyRuntimeRiskService] = None


def get_strategy_runtime_risk_service() -> StrategyRuntimeRiskService:
    global _runtime_risk_service
    if _runtime_risk_service is None:
        _runtime_risk_service = StrategyRuntimeRiskService()
    return _runtime_risk_service
