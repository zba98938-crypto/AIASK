"""Research shortlist is distinct from an approved high-quality batch."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


@pytest.fixture
def audit():
    path = Path(__file__).resolve().parents[3] / 'scripts/factories/check_strategy_quality_batch.py'
    spec = importlib.util.spec_from_file_location('batch_screen_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate():
    return (
        {'strategy_id': 's1', 'factory_run_id': 'r1', 'tested_object_hash': 'identity'},
        {'admission_stage': 'incubation', 'admission_evaluations': {'incubation': {'passed': True, 'reasons': []}}},
        {'post_cost_sharpe': 0.6, 'total_return': 0.1, 'trade_count': 40, 'max_drawdown': 0.1, 'parameter_perturbation_trade_stability': 0.8},
    )


def test_advisory_pass_rejected_is_not_shortlisted(audit):
    s, g, m = candidate()
    g.update(passed=True, admission_stage='rejected')
    assert audit.screen(s, g, m)['screen_passed'] is False


@pytest.mark.parametrize('key,value,reason', [
    ('post_cost_sharpe', -1.0, 'weak_post_cost_sharpe'),
    ('trade_count', 0, 'weak_trade_count'),
    ('total_return', None, 'missing_total_return'),
    ('max_drawdown', float('nan'), 'missing_max_drawdown'),
    ('parameter_perturbation_trade_stability', None, 'missing_parameter_perturbation_trade_stability'),
])
def test_unmeasurable_and_weak_have_different_reasons(audit, key, value, reason):
    s, g, m = candidate()
    m[key] = value
    result = audit.screen(s, g, m)
    assert result['screen_passed'] is False
    assert reason in result['screen_reasons']


def test_positive_shortlist_deduplicates_tested_objects_not_reports(audit):
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE strategy_quality_reports (id INTEGER PRIMARY KEY, summary TEXT, quality_gate TEXT, backtest_metrics TEXT, report_type TEXT, created_at TEXT)')
        s, g, m = candidate()
        for sid in ('s1', 's2'):
            s['strategy_id'] = sid
            conn.execute('INSERT INTO strategy_quality_reports VALUES (NULL, ?, ?, ?, ?, ?)', (json.dumps(s), json.dumps(g), json.dumps(m), 'submission', '2026-11-01 14:06:00'))
        result = audit.collect(conn, '2026-11-01T22:00:00+08:00')
        assert result['report_count'] == 2
        assert result['screen_passed_reports'] == 2
        assert result['screen_passed_unique_tested_objects'] == 1
        assert result['requires_independent_replay_and_forward_review'] is True
        assert result['goal_completion_verified'] is False
    finally:
        conn.close()


def test_timezone_required_for_batch_boundary(audit):
    with sqlite3.connect(':memory:') as conn, pytest.raises(ValueError, match='UTC offset'):
        audit.collect(conn, '2026-11-01T22:00:00')
