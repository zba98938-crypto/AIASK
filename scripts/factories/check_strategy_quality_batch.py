#!/usr/bin/env python3
"""Read-only quality screen for a bounded factory batch, not a trading approval.

This audit deliberately separates report counts, strict evaluation, unique tested
objects and forward/live acceptance. An old advisory passed flag never qualifies.
The screen is a research shortlist; independent replay and forward evidence still
have to be reviewed before any shortlist member counts toward the human goal.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def screen(summary: dict, gate: dict, metrics: dict) -> dict:
    reasons = []
    stage = gate.get('admission_stage') or summary.get('admission_stage')
    incubation = (gate.get('admission_evaluations') or {}).get('incubation') or {}
    if stage not in ('incubation', 'live') or incubation.get('passed') is not True:
        reasons.append('strict_incubation_evaluation_not_passed')
    if incubation.get('reasons') or gate.get('hard_fail_reasons'):
        reasons.append('admission_blockers_present')
    if gate.get('diagnostic_only') or summary.get('diagnostic_only'):
        reasons.append('diagnostic_only')
    # Conservative research-screen thresholds; not a claim of live profitability.
    values = {
        'post_cost_sharpe': number(metrics.get('post_cost_sharpe')),
        'total_return': number(metrics.get('total_return')),
        'trade_count': number(metrics.get('trade_count', metrics.get('trades_count'))),
        'max_drawdown': number(metrics.get('max_drawdown')),
        'parameter_perturbation_trade_stability': number(metrics.get('parameter_perturbation_trade_stability')),
    }
    checks = {
        'post_cost_sharpe': lambda x: x >= 0.35,
        'total_return': lambda x: x > 0,
        'trade_count': lambda x: x >= 30,
        'max_drawdown': lambda x: 0 <= x <= 0.25,
        'parameter_perturbation_trade_stability': lambda x: 0.5 <= x <= 1,
    }
    for key, predicate in checks.items():
        value = values[key]
        if value is None:
            reasons.append('missing_' + key)
        elif not predicate(value):
            reasons.append('weak_' + key)
    identity = summary.get('tested_object_hash') or (gate.get('multiple_testing_registry') or {}).get('tested_object_hash')
    if not identity:
        reasons.append('tested_object_identity_missing')
    if not summary.get('factory_run_id'):
        reasons.append('factory_run_binding_missing')
    return {
        'strategy_id': summary.get('strategy_id'),
        'factory_run_id': summary.get('factory_run_id'),
        'name': summary.get('name'),
        'tested_object_hash': identity,
        'admission_stage': stage,
        'submission_lane': summary.get('submission_lane'),
        'metrics': values,
        'screen_passed': not reasons,
        'screen_reasons': reasons,
    }


def collect(conn: sqlite3.Connection, since: str) -> dict:
    parsed = datetime.fromisoformat(since)
    if parsed.tzinfo is None:
        raise ValueError('--since must include an explicit UTC offset')
    rows = conn.execute(
        "SELECT summary,quality_gate,backtest_metrics FROM strategy_quality_reports "
        "WHERE report_type='submission' AND julianday(created_at)>=julianday(?) ORDER BY id",
        (since,),
    )
    reports = []
    for raw_summary, raw_gate, raw_metrics in rows:
        summary, gate, metrics = (json.loads(x or '{}') for x in (raw_summary, raw_gate, raw_metrics))
        reports.append(screen(summary, gate, metrics))
    qualified = {}
    for report in reports:
        if report['screen_passed']:
            qualified.setdefault(report['tested_object_hash'], report)
    blockers = Counter(reason for r in reports for reason in r['screen_reasons'])
    return {
        'audit_version': 1,
        'since': since,
        'screen_policy': {'min_post_cost_sharpe': 0.35, 'total_return_positive': True,
                          'min_trade_count': 30, 'max_drawdown': 0.25,
                          'min_parameter_perturbation_trade_stability': 0.5,
                          'requires_strict_incubation_evaluation': True},
        'target_unique_high_quality_strategies': 30,
        'report_count': len(reports),
        'stage_counts': dict(Counter(str(r['admission_stage']) for r in reports)),
        'screen_passed_reports': sum(r['screen_passed'] for r in reports),
        'screen_passed_unique_tested_objects': len(qualified),
        'requires_independent_replay_and_forward_review': True,
        'goal_completion_verified': False,
        'top_screen_blockers': dict(blockers.most_common(15)),
        'shortlist': list(qualified.values()),
        'reports': reports,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--db', type=Path, default=Path.home() / '.aiask/data/db/akshare_mcp.sqlite3')
    parser.add_argument('--since', required=True, help='Inclusive report creation time with explicit UTC offset')
    parser.add_argument('--output', type=Path, help='Optional new JSON evidence file (existing files are never overwritten)')
    args = parser.parse_args()
    with sqlite3.connect(args.db.expanduser().resolve().as_uri() + '?mode=ro', uri=True) as conn:
        payload = collect(conn, args.since)
    if args.output:
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({k: v for k, v in payload.items() if k not in ('reports', 'shortlist')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
