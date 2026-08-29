"""策略模拟盘孵化编排：阶段推进、自动评审和快照留痕。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from aiask_quant_core.incubation_stages import incubation_stage_from_pipeline

logger = logging.getLogger(__name__)


class StrategyIncubationPipelineService:
    @staticmethod
    def _consecutive(metrics: list[dict], predicate) -> int:
        streak = 0
        for item in metrics:
            if predicate(item):
                streak += 1
            else:
                break
        return streak

    @staticmethod
    def _readiness_score(*, latest_metric: Optional[dict], overview: dict, open_risk_count: int, runtime_control: Optional[dict], observed_days: int, promote_streak: int, trade_days: int) -> float:
        signal_quality = dict(overview.get('signal_quality') or {})
        primary_skill_lcb = float(signal_quality.get('primary_skill_lcb') or 0.0)
        secondary_skill_lcb = float(signal_quality.get('secondary_skill_lcb') or 0.0)
        recent_primary_skill_lcb = float(signal_quality.get('recent_primary_skill_lcb') or 0.0)
        coverage_ratio = float(signal_quality.get('coverage_ratio') or 0.0)
        stability_gap = float(signal_quality.get('stability_gap') or 0.0)
        primary_effective_n = int(signal_quality.get('primary_effective_n') or 0)
        secondary_effective_n = int(signal_quality.get('secondary_effective_n') or 0)

        # `priority_score` 只用于同 stage 内排序，不能覆盖 hard gate。
        # `readiness_score` 继续作为兼容别名暴露，因此这里主动弱化 legacy NAV/Sharpe/观察天数信号。
        score = 0.15
        score += max(min(primary_skill_lcb, 0.20), -0.20) * 1.1
        score += max(min(secondary_skill_lcb, 0.18), -0.18) * 0.7
        score += max(min(recent_primary_skill_lcb, 0.18), -0.18) * 0.8
        score += min(max(coverage_ratio, 0.0), 1.0) * 0.18
        score += min(primary_effective_n, 60) / 60 * 0.16
        score += min(secondary_effective_n, 30) / 30 * 0.08
        score -= min(max(stability_gap, 0.0), 0.2) * 0.8
        if latest_metric:
            score += min(max(float(latest_metric.get('nav') or 1.0) - 1.0, -0.08), 0.12) * 0.12
            score += min(max(float(latest_metric.get('sharpe_ratio') or 0.0), -1.0), 2.0) * 0.015
            score += min(max(float(latest_metric.get('forward_sharpe_5d') or 0.0), -1.0), 1.5) * 0.015
            score -= min(max(float(latest_metric.get('max_drawdown') or 0.0), 0.0), 0.5) * 0.12
        score += min(observed_days, 20) * 0.001
        score += min(promote_streak, 5) * 0.004
        score += min(trade_days, 10) * 0.002
        if overview.get('promotion_ready'):
            score += 0.08
        if overview.get('deprecation_risk'):
            score -= 0.12
        score -= min(len(overview.get('blockers') or []), 5) * 0.06
        score -= min(len(overview.get('risk_flags') or []), 5) * 0.05
        score -= min(open_risk_count, 3) * 0.08
        control_mode = str((runtime_control or {}).get('control_mode') or 'active')
        if control_mode not in {'active', 'throttled'}:
            score -= 0.18
        return round(max(0.0, min(score, 1.0)), 4)

    async def _evaluate_promotion_gate(self, db, strategy_id: str, *, signal_quality: dict, observed_days: int) -> dict:
        """INVERT-DESIGN P3 改动B：对前向收益序列跑 PromotionGate（DSR）。

        n_trials 取观察池规模（同期 listed+incubating 策略数）做选择偏差校正；
        取不到则回退 observed_days+1（保守，至少 1）。默认 OFF（toggle）时直接返回 enabled=False。
        软降级：任何异常都不阻断主流程。
        """
        try:
            from .incubation_factory.promotion_gate import (
                PromotionGate,
                _promotion_gate_enabled,
                fetch_forward_return_series,
            )
        except Exception as exc:
            logger.debug('PromotionGate import failed for %s: %s', strategy_id, exc)
            return {'enabled': False, 'reason': 'import_failed'}

        if not _promotion_gate_enabled():
            return {'enabled': False}

        primary_horizon = int(signal_quality.get('primary_horizon') or 5)
        try:
            series = await fetch_forward_return_series(
                db, strategy_id, horizon_days=primary_horizon
            )
        except Exception as exc:
            logger.debug('PromotionGate fetch series failed for %s: %s', strategy_id, exc)
            return {'enabled': True, 'eligible': False, 'passed': False, 'reasons': ['fetch_series_failed']}

        n_trials = 1
        lister = getattr(db, 'count_strategies_by_status', None) or getattr(db, 'list_strategies', None)
        try:
            if callable(getattr(db, 'list_strategies', None)):
                pool = await db.list_strategies(status='incubating', limit=1000)
                n_trials = max(1, len(pool or []))
        except Exception:
            n_trials = max(1, int(observed_days) + 1)

        verdict = PromotionGate().evaluate(series, n_trials=n_trials)
        audit = verdict.to_dict()
        audit['enabled'] = True
        audit['n_trials'] = n_trials
        audit['primary_horizon'] = primary_horizon
        return audit

    async def _record_domain_event(self, db, *, strategy_id: str, event_type: str, payload: dict, source: str, correlation_id: Optional[str] = None, severity: str = 'info'):
        if hasattr(db, 'save_strategy_domain_event'):
            await db.save_strategy_domain_event({
                'strategy_id': strategy_id,
                'aggregate_type': 'strategy_incubation_pipeline',
                'aggregate_id': strategy_id,
                'event_type': event_type,
                'source': source,
                'severity': severity,
                'correlation_id': correlation_id,
                'payload': payload,
            })

    async def _derive_snapshot(self, db, strategy: dict, *, task_run_id: Optional[int], source: str, auto_apply_review: bool) -> dict:
        # Fix #10: 从 strategy_lifecycle_shared 导入，避免循环依赖
        from .governance_monitor import GovernanceMonitor
        from .governance_persistence import persist_governance_report_snapshot
        from .strategy_lifecycle_shared import (
            build_incubation_overview as _build_incubation_overview,
            resolve_incubation_pipeline_stage as _resolve_incubation_pipeline_stage,
        )

        sid = str(strategy['id'])
        trace_metadata = dict(strategy.get('_closure_trace') or {})
        overview = await _build_incubation_overview(db, strategy)
        account = await db.get_strategy_incubation_account(sid) if hasattr(db, 'get_strategy_incubation_account') else None
        if not account and str(strategy.get('status') or '') in {'incubating', 'listed'}:
            try:
                from .incubation import get_strategy_incubation_service
                ensured = await get_strategy_incubation_service().ensure_account(db, strategy, stage='warmup', source_run_id=source)
                account = ensured.get('binding') or await db.get_strategy_incubation_account(sid)
            except Exception as exc:
                logger.warning('StrategyIncubationPipelineService.ensure_account failed for %s: %s', sid, exc)
        latest_metric = await db.get_latest_strategy_incubation_metric(sid) if hasattr(db, 'get_latest_strategy_incubation_metric') else None
        metrics = await db.list_strategy_incubation_metrics(sid, limit=30) if hasattr(db, 'list_strategy_incubation_metrics') else ([] if latest_metric is None else [latest_metric])
        runtime_control = await db.get_strategy_runtime_control(sid) if hasattr(db, 'get_strategy_runtime_control') else None
        open_risks = await db.list_strategy_runtime_risk_events(strategy_id=sid, status='open', limit=20) if hasattr(db, 'list_strategy_runtime_risk_events') else []

        observed_days = len(metrics)
        trade_days = len([item for item in metrics if int(item.get('total_orders') or 0) > 0 or int(item.get('total_trades') or 0) > 0])
        promote_streak = self._consecutive(metrics, lambda row: str(row.get('decision') or '') == 'promote')
        observe_streak = self._consecutive(metrics, lambda row: str(row.get('decision') or '') in {'observe', 'promote'})
        halt_streak = self._consecutive(metrics, lambda row: str(row.get('decision') or '') == 'halt')
        open_risk_count = len(open_risks)
        control_mode = str((runtime_control or {}).get('control_mode') or 'active')
        is_control_blocking = control_mode not in {'active', 'throttled'}
        latest_decision = str((latest_metric or {}).get('decision') or 'observe')
        signal_quality = dict(overview.get('signal_quality') or {})
        governance_monitor = GovernanceMonitor()
        raw_drawdown = latest_metric.get('max_drawdown') if isinstance(latest_metric, dict) else None
        max_drawdown_pct = None
        try:
            if raw_drawdown is not None:
                max_drawdown_pct = float(raw_drawdown)
                if max_drawdown_pct <= 1:
                    max_drawdown_pct *= 100.0
        except Exception:
            max_drawdown_pct = None
        governance_report = governance_monitor.run_full_check(
            target_type="strategy",
            target_id=sid,
            include_factor_decay=False,
            include_crowding=False,
            include_model_drift=False,
            posture_level=str(overview.get('posture_level') or 'safe'),
            control_mode=control_mode,
            open_alert_count=open_risk_count,
            recovery_eligible=bool((runtime_control or {}).get('recovery_eligible')),
            max_drawdown_pct=max_drawdown_pct,
            days_since_last_trade=None,
        )
        governance_snapshot = await persist_governance_report_snapshot(
            governance_report,
            scope_type="strategy",
            scope_id=sid,
        )
        governance_status = str(governance_report.overall_status or 'unknown')
        governance_issues = list(governance_report.issues or [])

        if str(strategy.get('status') or '') == 'listed':
            pipeline_stage = 'promoted'
            pipeline_status = 'listed'
            next_action = 'listed_monitoring'
        else:
            pipeline_stage = _resolve_incubation_pipeline_stage(
                signal_quality,
                open_risk_count=open_risk_count,
                execution_audit_gate_status=overview.get('execution_audit_gate_status'),
                holding_bucket=overview.get('holding_period_bucket'),
            )
            if pipeline_stage == 'failed':
                pipeline_status = 'blocked'
                next_action = 'manual_intervention'
            elif pipeline_stage == 'warmup':
                pipeline_status = 'collecting'
                next_action = 'collect_more_samples'
            elif pipeline_stage == 'graduation_ready' and not is_control_blocking:
                pipeline_status = 'ready_for_review'
                next_action = 'run_promotion_review'
            elif pipeline_stage == 'graduation_ready':
                pipeline_status = 'candidate'
                next_action = 'release_runtime_block'
            elif pipeline_stage == 'candidate':
                pipeline_status = 'candidate'
                next_action = 'accumulate_confirmation'
            else:
                pipeline_status = 'observing'
                next_action = 'stabilize_signal_quality' if latest_decision == 'halt' or halt_streak > 0 else 'continue_observation'

        # === INVERT-DESIGN P3 改动B: Layer 3 晋升门（前向序列 + DSR）===
        # graduation_ready 的策略在晋升前，用 PromotionGate 对其累积的**前向收益序列**
        # 跑 Deflated Sharpe Ratio（喂 n_trials=观察池规模做选择偏差校正）。
        # 默认 OFF（toggle）；ON 时 DSR 未过 → 降级 graduation_ready→candidate，
        # 不直接 listed，体现"严出、用前向真实数据、稀少晋升"。
        promotion_gate_audit: Optional[dict] = None
        gate_reasons_extra: list[str] = []
        if pipeline_stage == 'graduation_ready':
            promotion_gate_audit = await self._evaluate_promotion_gate(
                db, sid, signal_quality=signal_quality, observed_days=observed_days
            )
            if promotion_gate_audit and promotion_gate_audit.get('enabled'):
                if promotion_gate_audit.get('eligible') and not promotion_gate_audit.get('passed'):
                    pipeline_stage = 'candidate'
                    pipeline_status = 'candidate'
                    next_action = 'accumulate_forward_evidence_for_dsr'
                    for reason in promotion_gate_audit.get('reasons') or []:
                        tag = f"promotion_dsr:{reason}"
                        if tag not in gate_reasons_extra:
                            gate_reasons_extra.append(tag)

        gate_status = pipeline_stage
        gate_reasons = []
        if pipeline_stage == 'failed':
            gate_reasons = list(overview.get('gate_blockers') or overview.get('blockers') or [])
        elif pipeline_stage == 'observe' and overview.get('execution_audit_gate_status') not in {None, '', 'passed'}:
            gate_reasons = [f"execution_audit_gate:{overview.get('execution_audit_gate_status')}"]
        if overview.get('risk_hard_gate_status') not in {None, '', 'passed'}:
            gate_reasons.extend(
                item
                for item in (
                    overview.get('risk_hard_gate_reasons')
                    or [f"risk_hard_gate:{overview.get('risk_hard_gate_status')}"]
                )
                if item not in gate_reasons
            )
        if governance_status == 'critical':
            governance_blockers = [f"governance:{item}" for item in governance_issues] or ['governance:critical']
            gate_reasons.extend(item for item in governance_blockers if item not in gate_reasons)
            if pipeline_stage == 'graduation_ready':
                pipeline_stage = 'candidate'
                pipeline_status = 'candidate'
                next_action = 'resolve_governance_critical'
            elif pipeline_stage == 'promoted':
                pipeline_status = 'blocked'
                next_action = 'resolve_governance_critical'
        # P3 改动B：把晋升门（DSR）的降级原因并入 gate_reasons。
        gate_reasons.extend(item for item in gate_reasons_extra if item not in gate_reasons)
        priority_score = self._readiness_score(
            latest_metric=latest_metric,
            overview=overview,
            open_risk_count=open_risk_count,
            runtime_control=runtime_control,
            observed_days=observed_days,
            promote_streak=promote_streak,
            trade_days=trade_days,
        )

        snapshot = {
            'strategy_id': sid,
            'account_id': (account or {}).get('account_id'),
            'pipeline_stage': pipeline_stage,
            'pipeline_status': pipeline_status,
            'observed_days': observed_days,
            'promote_streak': promote_streak,
            'halt_streak': halt_streak,
            'latest_decision': latest_decision,
            'readiness_score': priority_score,
            'priority_score': priority_score,
            'gate_status': gate_status,
            'gate_reasons': gate_reasons,
            'hard_gate_result': {
                'pipeline_stage': pipeline_stage,
                'signal_stage_without_execution_gate': overview.get('signal_stage_without_execution_gate'),
                'execution_audit_gate_status': overview.get('execution_audit_gate_status'),
                'execution_hard_gate_passed': bool(overview.get('execution_hard_gate_passed')),
                'risk_hard_gate_status': overview.get('risk_hard_gate_status'),
                'governance_status': governance_status,
                'passed': pipeline_stage in {'candidate', 'graduation_ready', 'promoted'} and overview.get('risk_hard_gate_status') in {None, '', 'passed'} and governance_status != 'critical',
                'reasons': list(gate_reasons),
            },
            'next_action': next_action,
            'auto_review': bool(auto_apply_review and pipeline_stage == 'graduation_ready'),
            'auto_promoted': False,
            'promotion_gate': promotion_gate_audit,
            'blockers': list(overview.get('blockers') or []),
            'risk_flags': list(overview.get('risk_flags') or []),
            'summary': {
                'strategy_status': strategy.get('status'),
                'promotion_ready': bool(overview.get('promotion_ready')),
                'deprecation_risk': bool(overview.get('deprecation_risk')),
                'observed_forward_days': overview.get('observed_forward_days') or [],
                'missing_forward_days': overview.get('missing_forward_days') or [],
                'total_signals': overview.get('total_signals'),
                'primary_horizon': signal_quality.get('primary_horizon'),
                'secondary_horizon': signal_quality.get('secondary_horizon'),
                'primary_effective_n': signal_quality.get('primary_effective_n'),
                'secondary_effective_n': signal_quality.get('secondary_effective_n'),
                'primary_skill_lcb': signal_quality.get('primary_skill_lcb'),
                'recent_primary_skill_lcb': signal_quality.get('recent_primary_skill_lcb'),
                'coverage_ratio': signal_quality.get('coverage_ratio'),
                'stability_gap': signal_quality.get('stability_gap'),
                'trade_days': trade_days,
                'observe_streak': observe_streak,
                'open_risk_count': open_risk_count,
                'runtime_control_mode': control_mode,
                'governance_report_id': governance_snapshot.get('id'),
                'governance_status': governance_status,
                'governance_issues': governance_issues,
                'hard_gate_result': {
                    'pipeline_stage': pipeline_stage,
                    'execution_audit_gate_status': overview.get('execution_audit_gate_status'),
                    'risk_hard_gate_status': overview.get('risk_hard_gate_status'),
                    'governance_status': governance_status,
                    'passed': pipeline_stage in {'candidate', 'graduation_ready', 'promoted'} and overview.get('risk_hard_gate_status') in {None, '', 'passed'} and governance_status != 'critical',
                    'reasons': list(gate_reasons),
                },
            },
            'metadata': {
                'latest_metric': latest_metric or {},
                'overview': overview,
                'runtime_control': runtime_control or {},
                'open_risk_ids': [item.get('id') for item in open_risks if item.get('id') is not None],
                'governance_report_id': governance_snapshot.get('id'),
                'governance_status': governance_status,
                'governance_issues': governance_issues,
                'governance_report': governance_report.to_dict(),
                'trace': trace_metadata,
            },
            'task_run_id': task_run_id,
            'source': source,
            'evaluated_at': datetime.now(timezone.utc).isoformat(),
        }
        return snapshot

    async def _apply_account_state(self, db, snapshot: dict, account: Optional[dict]):
        if not account:
            return
        account_id = account.get('account_id')
        strategy_id = snapshot.get('strategy_id')
        pipeline_stage = snapshot.get('pipeline_stage') or 'warmup'
        stage = incubation_stage_from_pipeline(pipeline_stage)
        status = 'active'
        if pipeline_stage == 'graduation_ready':
            status = 'ready'
        elif pipeline_stage == 'promoted':
            status = 'promoted'
        elif pipeline_stage == 'failed':
            status = 'retired'
        if hasattr(db, 'save_strategy_incubation_account'):
            await db.save_strategy_incubation_account(
                strategy_id,
                account_id,
                stage=stage,
                status=status,
                source_run_id=str(snapshot.get('task_run_id') or ''),
                metadata={
                    **dict((account or {}).get('metadata') or {}),
                    'pipeline_status': snapshot.get('pipeline_status'),
                    'readiness_score': snapshot.get('readiness_score'),
                    'next_action': snapshot.get('next_action'),
                },
            )
        if hasattr(db, 'update_paper_account_status') and account_id:
            await db.update_paper_account_status(
                account_id,
                'active' if status in {'active', 'ready', 'promoted'} else 'archived',
                stage=stage,
                promotion_candidate=bool(stage in {'candidate', 'graduation_ready', 'promoted'}),
            )

    async def run_strategy(
        self,
        db,
        strategy: dict,
        *,
        source: str = 'manual',
        auto_apply_review: bool = False,
        task_run_id: Optional[int] = None,
    ) -> dict:
        trace_metadata = dict(strategy.get('_closure_trace') or {})
        correlation_id = str(trace_metadata.get('correlation_id') or uuid4().hex[:12])
        owns_task_run = task_run_id is None and hasattr(db, 'save_strategy_task_run')
        task_run = {'id': task_run_id, 'trace_id': correlation_id}
        if owns_task_run:
            task_run = await db.save_strategy_task_run({
                'strategy_id': strategy.get('id'),
                'task_name': 'strategy_incubation_pipeline',
                'task_scope': source,
                'task_key': str(strategy.get('id')),
                'status': 'running',
                'trace_id': correlation_id,
                'payload': {
                    'source': source,
                    'auto_apply_review': bool(auto_apply_review),
                    **trace_metadata,
                },
            })

        try:
            snapshot = await self._derive_snapshot(
                db,
                strategy,
                task_run_id=task_run.get('id'),
                source=source,
                auto_apply_review=auto_apply_review,
            )
            latest_before = await db.get_latest_strategy_incubation_pipeline_snapshot(strategy['id']) if hasattr(db, 'get_latest_strategy_incubation_pipeline_snapshot') else None
            account = await db.get_strategy_incubation_account(strategy['id']) if hasattr(db, 'get_strategy_incubation_account') else None
            await self._apply_account_state(db, snapshot, account)
            persisted = await db.save_strategy_incubation_pipeline_snapshot(snapshot) if hasattr(db, 'save_strategy_incubation_pipeline_snapshot') else snapshot

            applied_review = None
            if auto_apply_review and snapshot.get('pipeline_stage') == 'graduation_ready':
                from .promotion_pipeline import get_strategy_promotion_pipeline_service
                applied_review = await get_strategy_promotion_pipeline_service().review(
                    db,
                    strategy,
                    source=f'{source}_incubation_pipeline',
                    auto_apply=True,
                )
                if applied_review.get('applied_transition'):
                    persisted = await db.save_strategy_incubation_pipeline_snapshot({
                        **persisted,
                        'pipeline_stage': 'promoted',
                        'pipeline_status': 'promoted',
                        'next_action': 'listed_monitoring',
                        'auto_promoted': True,
                        'metadata': {
                            **dict(persisted.get('metadata') or {}),
                            'promotion_review': applied_review.get('review') or {},
                            'applied_transition': applied_review.get('applied_transition') or {},
                        },
                        'summary': {
                            **dict(persisted.get('summary') or {}),
                            'strategy_status': 'listed',
                        },
                    }) if hasattr(db, 'save_strategy_incubation_pipeline_snapshot') else {**persisted, 'auto_promoted': True}
                    snapshot = {**snapshot, 'pipeline_stage': 'promoted', 'pipeline_status': 'promoted', 'auto_promoted': True}

            if latest_before and latest_before.get('pipeline_stage') != persisted.get('pipeline_stage'):
                await self._record_domain_event(
                    db,
                    strategy_id=str(strategy['id']),
                    event_type='incubation.stage_transitioned',
                    payload={
                        'from_stage': latest_before.get('pipeline_stage'),
                        'to_stage': persisted.get('pipeline_stage'),
                        'pipeline_status': persisted.get('pipeline_status'),
                        'trace': trace_metadata,
                    },
                    source=source,
                    correlation_id=task_run.get('trace_id'),
                )

            await self._record_domain_event(
                db,
                strategy_id=str(strategy['id']),
                event_type='incubation.pipeline_evaluated',
                payload={
                    'pipeline_stage': persisted.get('pipeline_stage'),
                    'pipeline_status': persisted.get('pipeline_status'),
                    'readiness_score': persisted.get('readiness_score'),
                    'next_action': persisted.get('next_action'),
                    'auto_review': persisted.get('auto_review'),
                    'auto_promoted': persisted.get('auto_promoted'),
                    'trace': trace_metadata,
                },
                source=source,
                correlation_id=task_run.get('trace_id'),
                severity='warning' if persisted.get('pipeline_stage') == 'failed' else 'info',
            )

            result = {
                'strategy_id': strategy.get('id'),
                'snapshot': persisted,
                'promotion_review': applied_review,
                'auto_promoted': bool((persisted or {}).get('auto_promoted')),
                'task_run_id': task_run.get('id'),
            }
            if owns_task_run and hasattr(db, 'update_strategy_task_run'):
                await db.update_strategy_task_run(task_run['id'], status='completed', result=result, completed_at=datetime.now(timezone.utc).isoformat())
            return result
        except Exception as exc:
            logger.warning('StrategyIncubationPipelineService.run_strategy failed for %s: %s', strategy.get('id'), exc)
            if owns_task_run and task_run.get('id') is not None and hasattr(db, 'update_strategy_task_run'):
                await db.update_strategy_task_run(task_run['id'], status='failed', error=str(exc), completed_at=datetime.now(timezone.utc).isoformat())
            raise

    async def run_batch(
        self,
        db,
        *,
        statuses: Optional[list[str]] = None,
        limit: int = 200,
        source: str = 'runtime_cycle',
        auto_apply_review: bool = True,
        strategies: Optional[list[dict]] = None,
    ) -> dict:
        statuses = list(statuses or ['incubating'])
        provided_strategies = strategies is not None
        correlation_id = uuid4().hex[:12]
        batch_run = await db.save_strategy_task_run({
            'strategy_id': None,
            'task_name': 'strategy_incubation_pipeline_batch',
            'task_scope': source,
            'task_key': correlation_id,
            'status': 'running',
            'trace_id': correlation_id,
            'payload': {
                'statuses': statuses,
                'limit': limit,
                'auto_apply_review': bool(auto_apply_review),
                'provided_strategies': bool(provided_strategies),
                'strategy_count': len(strategies or []) if provided_strategies else None,
            },
        }) if hasattr(db, 'save_strategy_task_run') else {'id': None, 'trace_id': correlation_id}

        try:
            seen = set()
            selected_strategies = []
            if provided_strategies:
                rows_iterable = list(strategies or [])
                for row in rows_iterable:
                    sid = row.get('id') if isinstance(row, dict) else None
                    if sid and sid not in seen:
                        seen.add(sid)
                        selected_strategies.append(row)
            else:
                for status in statuses:
                    rows = await db.list_strategies(status, limit=max(1, min(int(limit or 200), 1000)))
                    for row in rows:
                        sid = row.get('id')
                        if sid and sid not in seen:
                            seen.add(sid)
                            selected_strategies.append(row)
            items = []
            auto_promoted = 0
            stage_counts: dict[str, int] = {}
            for strategy in selected_strategies:
                item = await self.run_strategy(
                    db,
                    strategy,
                    source=source,
                    auto_apply_review=auto_apply_review,
                    task_run_id=batch_run.get('id'),
                )
                items.append(item)
                stage = str(((item.get('snapshot') or {}).get('pipeline_stage') or 'unknown'))
                stage_counts[stage] = stage_counts.get(stage, 0) + 1
                auto_promoted += 1 if item.get('auto_promoted') else 0
            result = {
                'task_run_id': batch_run.get('id'),
                'count': len(items),
                'auto_promoted': auto_promoted,
                'stage_counts': stage_counts,
                'items': items,
            }
            if batch_run.get('id') is not None and hasattr(db, 'update_strategy_task_run'):
                await db.update_strategy_task_run(batch_run['id'], status='completed', result=result, completed_at=datetime.now(timezone.utc).isoformat())
            if hasattr(db, 'save_strategy_domain_event'):
                await db.save_strategy_domain_event({
                    'strategy_id': None,
                    'aggregate_type': 'incubation_pipeline',
                    'aggregate_id': str(batch_run.get('id') or correlation_id),
                    'event_type': 'incubation.pipeline_batch_completed',
                    'source': source,
                    'severity': 'info',
                    'correlation_id': batch_run.get('trace_id'),
                    'payload': result,
                })
            return result
        except Exception as exc:
            logger.warning('StrategyIncubationPipelineService.run_batch failed: %s', exc)
            if batch_run.get('id') is not None and hasattr(db, 'update_strategy_task_run'):
                await db.update_strategy_task_run(batch_run['id'], status='failed', error=str(exc), completed_at=datetime.now(timezone.utc).isoformat())
            raise


_incubation_pipeline_service: Optional[StrategyIncubationPipelineService] = None


def get_strategy_incubation_pipeline_service() -> StrategyIncubationPipelineService:
    global _incubation_pipeline_service
    if _incubation_pipeline_service is None:
        _incubation_pipeline_service = StrategyIncubationPipelineService()
    return _incubation_pipeline_service
