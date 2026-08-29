"""策略模拟盘晋级评审与自动应用流水线。"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class StrategyPromotionPipelineService:
    @staticmethod
    def _normalized_status(value: object) -> str:
        return str(value or '').strip().lower()

    @classmethod
    def _signal_snapshot_status(cls, overview: dict) -> str:
        return cls._normalized_status(dict(overview.get('signal_quality_snapshot') or {}).get('status'))

    @classmethod
    def _execution_snapshot_status(cls, overview: dict) -> str:
        return cls._normalized_status(dict(overview.get('execution_quality_snapshot') or {}).get('status'))

    @classmethod
    def _trace_evidence_gap_codes(cls, overview: dict) -> list[str]:
        payload = dict(overview.get('prediction_trace_ledger') or {})
        codes: list[str] = []
        for item in list(payload.get('evidence_gap_codes') or []):
            token = cls._normalized_status(item)
            if token and token not in codes:
                codes.append(token)
        return codes

    @classmethod
    def _objective_profile(cls, overview: dict) -> str:
        return cls._normalized_status(overview.get('objective_profile'))

    @classmethod
    def _precision_readiness(cls, overview: dict) -> str:
        return cls._normalized_status(overview.get('precision_readiness'))

    @classmethod
    def _position_cycle_evidence(cls, overview: dict) -> dict:
        return dict(overview.get('position_cycle_evidence') or {})

    @classmethod
    def _event_prefilter_summary(cls, overview: dict) -> dict:
        return dict(overview.get('event_prefilter_summary') or {})

    @classmethod
    def _hard_gate_reasons(cls, overview: dict) -> list[str]:
        payload = dict(overview.get('hard_gate_result') or {})
        reasons: list[str] = []
        for item in list(payload.get('reasons') or overview.get('blockers') or []):
            token = str(item or '').strip()
            if token and token not in reasons:
                reasons.append(token)
        return reasons

    @classmethod
    def _primary_trace_gap_codes(cls, overview: dict) -> list[str]:
        critical = {
            'missing_actual_fill',
            'missing_position_round_trip',
            'missing_pnl_audit',
            'missing_pnl_audit_summary',
        }
        return [code for code in cls._trace_evidence_gap_codes(overview) if code in critical]

    @classmethod
    def _resolve_review_outcome(cls, overview: dict) -> tuple[str, str, list[str]]:
        objective_profile = cls._objective_profile(overview)
        signal_status = cls._signal_snapshot_status(overview)
        execution_status = cls._execution_snapshot_status(overview)
        hard_gate_reasons = cls._hard_gate_reasons(overview)
        critical_trace_gaps = cls._primary_trace_gap_codes(overview)
        position_cycle_evidence = cls._position_cycle_evidence(overview)
        position_cycle_status = cls._normalized_status(position_cycle_evidence.get('status'))
        precision_readiness = cls._precision_readiness(overview)
        cost_robustness_summary = dict(overview.get('cost_robustness_summary') or {})
        trade_density_summary = dict(overview.get('trade_density_summary') or {})
        event_prefilter_summary = cls._event_prefilter_summary(overview)
        blockers: list[str] = []
        blockers.extend(hard_gate_reasons)
        blockers.extend(code for code in critical_trace_gaps if code not in blockers)
        if signal_status and signal_status != 'strong' and 'signal_quality_snapshot_not_strong' not in blockers:
            blockers.append('signal_quality_snapshot_not_strong')
        if execution_status in {'weak', 'insufficient_evidence', 'missing'} and (
            f'execution_quality_snapshot:{execution_status}' not in blockers
        ):
            blockers.append(f'execution_quality_snapshot:{execution_status}')
        if objective_profile == 'high_precision':
            if precision_readiness not in {'candidate', 'strong'}:
                blockers.append(f'high_precision_precision_readiness:{precision_readiness or "missing"}')
            if position_cycle_status in {'', 'weak', 'insufficient_evidence', 'observe'}:
                blockers.append(
                    f'high_precision_cycle_evidence:{position_cycle_status or "missing"}'
                )
            if trade_density_summary and not bool(trade_density_summary.get('passed')):
                blockers.append('high_precision_trade_density_not_ready')
            if cost_robustness_summary.get('required') and not bool(cost_robustness_summary.get('passed')):
                blockers.append('high_precision_cost_fragility')
            if event_prefilter_summary.get('required') and not bool(event_prefilter_summary.get('passed')):
                blockers.append('high_precision_event_prefilter_not_ready')
            if overview.get('adverse_regime_avoidance') is False:
                blockers.append('high_precision_adverse_regime_not_avoided')
        if overview.get('deprecation_risk'):
            return 'rejected', 'deprecate', blockers
        if (
            bool(overview.get('promotion_ready'))
            and signal_status == 'strong'
            and execution_status in {'strong', 'passed'}
            and not blockers
        ):
            return 'approved', 'promote', blockers
        return 'watch', 'observe', blockers

    @classmethod
    def _score(cls, overview: dict, metric: Optional[dict]) -> float:
        objective_profile = cls._objective_profile(overview)
        signal_status = cls._signal_snapshot_status(overview)
        execution_status = cls._execution_snapshot_status(overview)
        hard_gate_reasons = cls._hard_gate_reasons(overview)
        trace_gap_codes = cls._trace_evidence_gap_codes(overview)
        precision_readiness = cls._precision_readiness(overview)
        position_cycle_evidence = cls._position_cycle_evidence(overview)
        position_cycle_status = cls._normalized_status(position_cycle_evidence.get('status'))
        event_prefilter_summary = cls._event_prefilter_summary(overview)
        score = 0.2
        if signal_status == 'strong':
            score += 0.24
        elif signal_status == 'candidate':
            score += 0.10
        elif signal_status == 'weak':
            score -= 0.18
        elif signal_status == 'insufficient_evidence':
            score -= 0.14
        if execution_status in {'strong', 'passed'}:
            score += 0.24
        elif execution_status == 'candidate':
            score += 0.08
        elif execution_status == 'weak':
            score -= 0.22
        elif execution_status == 'insufficient_evidence':
            score -= 0.18
        if overview.get('promotion_ready'):
            score += 0.12
        if overview.get('execution_hard_gate_passed'):
            score += 0.08
        elif str(overview.get('execution_audit_gate_status') or '') == 'failed_metrics':
            score -= 0.18
        if overview.get('deprecation_risk'):
            score -= 0.35
        score -= min(len(hard_gate_reasons), 5) * 0.08
        score -= min(len(trace_gap_codes), 5) * 0.05
        if objective_profile == 'high_precision':
            if precision_readiness == 'strong':
                score += 0.12
            elif precision_readiness == 'candidate':
                score += 0.06
            else:
                score -= 0.08
            if position_cycle_status == 'strong':
                score += 0.10
            elif position_cycle_status == 'candidate':
                score += 0.05
            else:
                score -= 0.08
            regime_consistency = float(position_cycle_evidence.get('regime_consistency') or 0)
            cost_robustness = float(position_cycle_evidence.get('cost_robustness') or 0)
            score += max(min(regime_consistency, 1.0), 0.0) * 0.06
            score += max(min(cost_robustness, 1.0), 0.0) * 0.05
            if dict(overview.get('trade_density_summary') or {}) and not bool(
                dict(overview.get('trade_density_summary') or {}).get('passed')
            ):
                score -= 0.10
            if dict(overview.get('cost_robustness_summary') or {}).get('required') and not bool(
                dict(overview.get('cost_robustness_summary') or {}).get('passed')
            ):
                score -= 0.12
            if event_prefilter_summary.get('required') and not bool(event_prefilter_summary.get('passed')):
                score -= 0.10
            if overview.get('adverse_regime_avoidance') is False:
                score -= 0.08
        if metric:
            sharpe = float(metric.get('sharpe_ratio') or 0)
            hit_rate = float(metric.get('hit_rate_5d') or 0)
            forward_sharpe = float(metric.get('forward_sharpe_5d') or 0)
            score += min(max(sharpe, -1.0), 2.0) * 0.08
            score += hit_rate * 0.12
            score += max(forward_sharpe, -1.0) * 0.08
        score -= min(len(overview.get('blockers') or []), 5) * 0.05
        score -= min(len(overview.get('risk_flags') or []), 5) * 0.05
        return round(max(0.0, min(score, 1.0)), 4)

    async def review(
        self,
        db,
        strategy: dict,
        *,
        source: str = 'manual',
        auto_apply: bool = False,
        metadata: Optional[dict] = None,
    ) -> dict:
        from .strategy_lifecycle_shared import (
            build_incubation_overview as _build_incubation_overview,
            update_status as _update_status,
            validate_transition as _validate_transition,
        )

        trace_metadata = dict(metadata or {})
        overview = await _build_incubation_overview(db, strategy)
        sid = strategy['id']
        metric = await db.get_latest_strategy_incubation_metric(sid) if hasattr(db, 'get_latest_strategy_incubation_metric') else None
        account = await db.get_strategy_incubation_account(sid) if hasattr(db, 'get_strategy_incubation_account') else None
        blockers = list(overview.get('blockers') or [])
        risk_flags = list(overview.get('risk_flags') or [])
        score = self._score(overview, metric)
        status, recommendation, snapshot_blockers = self._resolve_review_outcome(overview)
        blockers = list(dict.fromkeys([*blockers, *snapshot_blockers]))

        review = await db.save_strategy_promotion_review({
            'strategy_id': sid,
            'account_id': (account or {}).get('account_id'),
            'review_source': source,
            'stage': (metric or {}).get('stage') or (account or {}).get('stage') or 'incubating',
            'status': status,
            'recommendation': recommendation,
            'score': score,
            'blockers': blockers,
            'risk_flags': risk_flags,
            'summary': {
                'objective_profile': overview.get('objective_profile'),
                'precision_readiness': overview.get('precision_readiness'),
                'promotion_ready': bool(overview.get('promotion_ready')),
                'deprecation_risk': bool(overview.get('deprecation_risk')),
                'validation_grade': overview.get('validation_grade'),
                'strict_incubation_ready': overview.get('strict_incubation_ready'),
                'live_candidate_ready': overview.get('live_candidate_ready'),
                'strict_live_alignment_status': overview.get('strict_live_alignment_status'),
                'signal_stage_without_execution_gate': overview.get('signal_stage_without_execution_gate'),
                'execution_audit_gate_status': overview.get('execution_audit_gate_status'),
                'execution_hard_gate_passed': bool(overview.get('execution_hard_gate_passed')),
                'promotion_gate_status': overview.get('promotion_gate_status'),
                'total_signals': overview.get('total_signals'),
                'observed_forward_days': overview.get('observed_forward_days') or [],
                'signal_quality_snapshot': dict(overview.get('signal_quality_snapshot') or {}),
                'execution_quality_snapshot': dict(overview.get('execution_quality_snapshot') or {}),
                'position_cycle_evidence': dict(overview.get('position_cycle_evidence') or {}),
                'regime_validation_summary': dict(overview.get('regime_validation_summary') or {}),
                'cost_robustness_summary': dict(overview.get('cost_robustness_summary') or {}),
                'trade_density_summary': dict(overview.get('trade_density_summary') or {}),
                'event_prefilter_summary': dict(overview.get('event_prefilter_summary') or {}),
                'event_anchor_summary': dict(overview.get('event_anchor_summary') or {}),
                'backtest_metrics_contract_status': overview.get('backtest_metrics_contract_status'),
                'regime_consistency': overview.get('regime_consistency'),
                'payoff_asymmetry': overview.get('payoff_asymmetry'),
                'adverse_regime_avoidance': overview.get('adverse_regime_avoidance'),
                'prediction_trace_ledger': dict(overview.get('prediction_trace_ledger') or {}),
                'evidence_gap_codes': self._trace_evidence_gap_codes(overview),
                'hard_gate_reasons': self._hard_gate_reasons(overview),
            },
            'metadata': {
                'overview': overview,
                'metric': metric or {},
                **trace_metadata,
            },
        }) if hasattr(db, 'save_strategy_promotion_review') else {
            'strategy_id': sid,
            'status': status,
            'recommendation': recommendation,
            'score': score,
            'blockers': blockers,
            'risk_flags': risk_flags,
            'summary': overview,
            'metadata': {'metric': metric or {}},
        }

        if hasattr(db, 'save_strategy_domain_event'):
            await db.save_strategy_domain_event({
                'strategy_id': sid,
                'aggregate_type': 'strategy_promotion_review',
                'aggregate_id': str(review.get('id') or sid),
                'event_type': 'promotion.reviewed',
                'source': source,
                'severity': 'warning' if status == 'rejected' else 'info',
                'correlation_id': trace_metadata.get('correlation_id') or (account or {}).get('account_id'),
                'payload': {
                    'status': status,
                    'recommendation': recommendation,
                    'score': score,
                    'blockers': blockers,
                    'risk_flags': risk_flags,
                    'trace': trace_metadata,
                },
            })

        applied_transition = None
        if auto_apply:
            current_status = str(strategy.get('status') or '')
            if recommendation == 'promote' and _validate_transition(current_status, 'listed'):
                await _update_status(
                    db,
                    sid,
                    'listed',
                    actor_id=source,
                    reason='promotion_pipeline_approved',
                    metadata={'promotion_review_id': review.get('id'), 'score': score, **trace_metadata},
                )
                applied_transition = {'from': current_status, 'to': 'listed'}
                if account and hasattr(db, 'save_strategy_incubation_account'):
                    await db.save_strategy_incubation_account(
                        sid,
                        account['account_id'],
                        stage='listed',
                        status='active',
                        source_run_id=(account or {}).get('source_run_id'),
                        metadata={
                            **dict((account or {}).get('metadata') or {}),
                            'promotion_review_id': review.get('id'),
                            **trace_metadata,
                        },
                    )
            elif recommendation == 'deprecate' and _validate_transition(current_status, 'deprecated'):
                await _update_status(
                    db,
                    sid,
                    'deprecated',
                    actor_id=source,
                    reason='promotion_pipeline_rejected',
                    metadata={'promotion_review_id': review.get('id'), 'score': score, **trace_metadata},
                )
                applied_transition = {'from': current_status, 'to': 'deprecated'}
                if account and hasattr(db, 'save_strategy_incubation_account'):
                    await db.save_strategy_incubation_account(
                        sid,
                        account['account_id'],
                        stage='failed',
                        status='retired',
                        source_run_id=(account or {}).get('source_run_id'),
                        metadata={
                            **dict((account or {}).get('metadata') or {}),
                            'promotion_review_id': review.get('id'),
                            **trace_metadata,
                        },
                    )

        if applied_transition and hasattr(db, 'save_strategy_domain_event'):
            await db.save_strategy_domain_event({
                'strategy_id': sid,
                'aggregate_type': 'strategy_promotion_review',
                'aggregate_id': str(review.get('id') or sid),
                'event_type': 'promotion.applied',
                'source': source,
                'severity': 'info',
                'correlation_id': trace_metadata.get('correlation_id') or (account or {}).get('account_id'),
                'payload': {
                    'transition': applied_transition,
                    'status': status,
                    'recommendation': recommendation,
                    'score': score,
                    'trace': trace_metadata,
                },
            })

        try:
            from .strategy_lifecycle_shared import build_closure_review as _build_closure_review

            await _build_closure_review(
                db,
                {
                    **dict(strategy or {}),
                    'status': (applied_transition or {}).get('to') or strategy.get('status'),
                },
                as_of=(overview or {}).get('as_of'),
                correlation_id=trace_metadata.get('correlation_id'),
                force_recompute=True,
            )
        except Exception as exc:
            logger.warning(
                "StrategyPromotionPipelineService: closure review refresh failed for %s: %s",
                sid,
                exc,
            )

        return {
            'review': review,
            'overview': overview,
            'applied_transition': applied_transition,
        }


_promotion_pipeline_service: Optional[StrategyPromotionPipelineService] = None


def get_strategy_promotion_pipeline_service() -> StrategyPromotionPipelineService:
    global _promotion_pipeline_service
    if _promotion_pipeline_service is None:
        _promotion_pipeline_service = StrategyPromotionPipelineService()
    return _promotion_pipeline_service
