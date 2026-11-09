"""Post-backtest evidence must not change the alpha that produced it."""
from copy import deepcopy

import pytest

from strategy_factory.application.candidate_contract import (
    apply_resolved_candidate_envelope,
    build_execution_contract_hash,
    build_tested_object_hash,
)
from strategy_factory.application.services.candidate_pipeline import CandidatePipeline
from strategy_factory.application.submitter import StrategySubmitter
from strategy_factory.application.trade_prediction_contract import (
    attach_trade_prediction_context,
    freeze_trade_prediction_contract,
)


def candidate():
    return {
        'name': 'identity candidate', 'strategy_type': 'ma_cross',
        'params': {'short_period': 5, 'long_period': 20},
        'target_symbols': ['000001', '000002'],
        'portfolio_spec': {'construction': 'equal_weight', 'weights': {'000001': .5, '000002': .5}},
        'execution_assumptions': {'commission_rate': .001, 'slippage_bps': 7.},
        'holding_horizon': {'min_days': 1, 'max_days': 20},
    }


@pytest.mark.parametrize('key,value', [
    ('alpha_source', 'technical'), ('candidate_family', 'ma_cross'),
    ('candidate_family_id', 'ma_cross_medium_technical_2'),
    ('candidate_registry_stage', 'validated'), ('candidate_validation_score', 99.),
    ('direction_bias', 'trend_follow_long'), ('generator_mode', 'snapshot'),
    ('holding_period_bucket', 'medium'), ('incubation_budget', {'budget_multiplier': 1.5}),
    ('observe_first_intake', True), ('regime_fit', 'trend_expansion'),
    ('research_protocol_version', 'strategy_factory.research_protocol.v2'),
    ('research_validation_contract_submission_adapter', {'spec_completeness': 'complete'}),
    ('risk_level', 'medium'), ('source_validation_artifact_id', 'audit-123'),
    ('strategy_explanation', {'evidence': {'metrics': {'sharpe_ratio': 9.99}}}),
    ('target_symbol_count', 2), ('validation_profile_name', {'profile': 'trade_rule_validation'}),
])
def test_submission_annotations_do_not_change_tested_alpha(key, value):
    original = candidate()
    annotated = deepcopy(original)
    annotated['params'][key] = value
    assert build_tested_object_hash(annotated) == build_tested_object_hash(original)


def test_identity_survives_real_context_intake_submission_and_report_flow():
    contextual = attach_trade_prediction_context(candidate(), snapshot={'date': '2026-11-09', 'prediction_as_of': '2026-11-06'})
    tested = apply_resolved_candidate_envelope(contextual)
    tested_hash = tested['tested_object_hash']
    # Outcome binding is recorded before the intake metadata is appended.
    tested['backtest_outcome'] = {'tested_object_hash': tested_hash}
    observed = CandidatePipeline._mark_observe_first_candidates([tested])[0]
    assert build_tested_object_hash(observed) == tested_hash
    observed['incubation_budget']['feedback_metrics'] = {'gate_failure_rate': .9}
    metrics = {'sharpe_ratio': .4, 'total_return': .02, 'max_drawdown': .08}
    data = StrategySubmitter()._build_strategy_data('identity-strategy', tested['name'], observed, metrics)
    assert data['params']['strategy_explanation']['evidence']['metrics']['sharpe_ratio'] == .4
    assert build_tested_object_hash(data) == tested_hash == data['params']['tested_object_hash']
    rebuilt = apply_resolved_candidate_envelope({**observed, 'params': data['params']})
    report = StrategySubmitter._candidate_report_params(rebuilt)
    assert report['target_risk_contract']['tested_object_hash'] == tested_hash
    assert report['target_risk_contract']['current_tested_object_hash'] == tested_hash
    for _ in range(3):
        rebuilt = apply_resolved_candidate_envelope(rebuilt)
        assert rebuilt['tested_object_hash'] == build_tested_object_hash(rebuilt) == tested_hash
    changed_metrics = {**metrics, 'sharpe_ratio': -.5}
    other = StrategySubmitter()._build_strategy_data('identity-other', 'renamed', observed, changed_metrics)
    assert build_tested_object_hash(other) == tested_hash


@pytest.mark.parametrize('key,before,after', [
    ('short_period', 5, 8), ('long_period', 20, 40),
    ('prediction_as_of', '2026-11-06', '2026-11-09'),
    ('snapshot_date', '2026-11-09', '2026-11-10'),
    ('signal_threshold', .01, .05),
])
def test_actual_parameters_and_evaluation_context_still_change_identity(key, before, after):
    original = candidate()
    original['params'][key] = before
    changed = deepcopy(original)
    changed['params'][key] = after
    assert build_tested_object_hash(changed) != build_tested_object_hash(original)


def test_dsl_and_factor_changes_still_change_identity():
    original = candidate()
    original['params']['dsl'] = {'entry': 'close > sma(close, 20)', 'exit': 'close < sma(close, 20)'}
    changed = deepcopy(original)
    changed['params']['dsl']['entry'] = 'close > sma(close, 50)'
    assert build_tested_object_hash(changed) != build_tested_object_hash(original)
    original['params']['factor_weights'] = {'value': .5, 'quality': .5}
    changed = deepcopy(original)
    changed['params']['factor_weights'] = {'value': .8, 'quality': .2}
    assert build_tested_object_hash(changed) != build_tested_object_hash(original)


def test_direction_descriptor_changes_the_separate_prediction_identity():
    original = candidate()
    original.update({'id': 'identity-prediction', 'prediction_as_of': '2026-11-06',
                     'target_trading_date': '2026-11-09', 'horizon': 'next_day',
                     'confidence': .7, 'evidence_chain': {'evidences': [{'id': 'frozen-evidence'}]}})
    original['params']['direction_bias'] = 'long_only'
    changed = deepcopy(original)
    changed['params']['direction_bias'] = 'short_only'
    original_prediction = freeze_trade_prediction_contract(original)
    changed_prediction = freeze_trade_prediction_contract(changed)
    assert original_prediction['contract']['direction'] == 'up'
    assert changed_prediction['contract']['direction'] == 'down'
    assert original_prediction['contract_hash'] and changed_prediction['contract_hash']
    assert original_prediction['contract_hash'] != changed_prediction['contract_hash']
    # The current alpha engine consumes ma_cross periods, not this prediction
    # descriptor. Prediction evidence has its own stricter identity boundary.
    assert build_tested_object_hash(changed) == build_tested_object_hash(original)


@pytest.mark.parametrize('field,value', [
    ('target_symbols', ['000001', '000003']),
    ('execution_assumptions', {'commission_rate': .002, 'slippage_bps': 7.}),
    ('portfolio_spec', {'construction': 'equal_weight', 'weights': {'000001': .8, '000002': .2}}),
])
def test_execution_identity_still_binds_targets_costs_and_weights(field, value):
    original = candidate()
    changed = deepcopy(original)
    changed[field] = value
    assert build_execution_contract_hash(changed) != build_execution_contract_hash(original)
