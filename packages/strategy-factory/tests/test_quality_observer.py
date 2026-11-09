"""Observer behaviour against an in-memory database; never the runtime DB."""
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


@pytest.fixture
def observer():
    path = Path(__file__).resolve().parents[3] / 'scripts/factories/monitor_factory_quality.py'
    spec = importlib.util.spec_from_file_location('quality_observer_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE strategy_quality_reports (id INTEGER PRIMARY KEY, strategy_id TEXT, passed INT, summary TEXT, quality_gate TEXT, validation_report TEXT, risk_report TEXT, backtest_metrics TEXT, created_at TEXT, report_type TEXT)')
    yield conn
    conn.close()


def run():
    return dict(id=1, run_id='own', started_at='2026-11-01T22:00:00+08:00', completed_at='2026-11-01T22:01:00+08:00', status='success_observe_only', snapshot_summary='{}', elapsed_seconds=60, execution_mode='observe', engine_version='test', error=None)


def add(db, *, owner='own', gate=None, created='2026-11-01T14:00:30Z'):
    db.execute('INSERT INTO strategy_quality_reports VALUES (NULL, ?, 1, ?, ?, ?, ?, ?, ?, ?)', ('s1', json.dumps({'factory_run_id': owner}) if owner else '{}', json.dumps(gate or {}), '{}', '{}', '{"sharpe_ratio": -1}', created, 'submission'))


def test_advisory_pass_does_not_mean_admitted(observer, db):
    add(db, gate={'admission_stage': 'rejected'})
    q = observer._review_run(db, run())['quality']
    assert q['passed_count'] == 0
    assert q['legacy_passed_count'] == 1
    assert q['samples'][0]['passed'] is False


def test_explicit_incubation_evidence_required(observer, db):
    add(db, gate={'admission_stage': 'incubation', 'admission_evaluations': {'incubation': {'passed': True, 'reasons': []}}})
    assert observer._review_run(db, run())['quality']['passed_count'] == 1


def test_exact_identity_outside_time_window_and_no_foreign_fallback(observer, db):
    add(db, owner='foreign')
    assert observer._review_run(db, run())['quality']['report_count'] == 0
    add(db, created='2026-11-01T01:00:00Z')
    q = observer._review_run(db, run())['quality']
    assert q['report_count'] == 1
    assert q['report_link_mode'] == 'factory_run_id'


def test_unbound_legacy_not_verified(observer, db):
    add(db, owner=None, gate={'admission_stage': 'incubation', 'admission_evaluations': {'incubation': {'passed': True}}})
    q = observer._review_run(db, run())['quality']
    assert q['report_link_mode'] == 'time_window_unverified'
    assert q['passed_count'] == 0


def test_empty_report_renders(observer, db):
    entry = observer._review_run(db, run())
    text = observer._render_day([entry], day='2026-11-01', inventory={}, health={}, cycles={})
    assert '0' in text
    assert '—' in text


def test_health_needs_fresh_complete_live_processes(observer, tmp_path, monkeypatch):
    monkeypatch.setattr(observer, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(observer, '_alive', lambda pid: bool(pid))
    names = ['strategy_factory', 'factor_mining_factory', 'incubation_factory', 'market_event_ingest']
    payload = {'event_type': 'supervisor_heartbeat', 'ts': (observer._now() - timedelta(hours=1)).isoformat(), 'factories': [{'factory': n, 'pid': 1, 'status': 'running'} for n in names]}
    path = tmp_path / 'execution_records.jsonl'
    path.write_text(json.dumps(payload))
    assert observer._supervisor_health()['status'] == 'degraded'
    payload['ts'] = observer._now().isoformat()
    path.write_text(json.dumps(payload))
    assert observer._supervisor_health()['status'] == 'healthy'
    payload['factories'] = []
    path.write_text(json.dumps(payload))
    assert observer._supervisor_health()['status'] == 'degraded'


def test_observe_and_partial_terminal_runs_are_not_dropped(observer, db):
    db.execute('CREATE TABLE strategy_factory_runs (id INTEGER, run_id TEXT, status TEXT, started_at TEXT, completed_at TEXT, elapsed_seconds REAL, execution_mode TEXT, engine_version TEXT, summary TEXT, snapshot_summary TEXT, error TEXT)')
    for index, status in enumerate(['success_observe_only', 'success_no_strategy', 'partial_llm', 'partial_infra'], 1):
        db.execute('INSERT INTO strategy_factory_runs VALUES (?, ?, ?, ?, ?, 60, ?, ?, ?, ?, NULL)', (index, f'run{index}', status, run()['started_at'], run()['completed_at'], 'observe', 'test', '{}', '{}'))
    state = {}
    entries = observer._collect_new_entries(db, state)
    assert len(entries) == 4
    assert state['last_run_rowid'] == 4
    assert observer._collect_new_entries(db, state) == []


def test_nonfinite_is_missing(observer):
    assert observer._num(float('nan')) is None
    assert observer._num('inf') is None


def factory_runs_table(db):
    db.execute('CREATE TABLE strategy_factory_runs (id INTEGER, run_id TEXT, status TEXT, started_at TEXT, completed_at TEXT, elapsed_seconds REAL, execution_mode TEXT, engine_version TEXT, summary TEXT, snapshot_summary TEXT, error TEXT)')


def add_run(db, rowid, run_id, *, status='success', started='2026-11-04T12:00:00+08:00', completed='2026-11-04T12:01:00+08:00'):
    db.execute('INSERT INTO strategy_factory_runs VALUES (?, ?, ?, ?, ?, 60, ?, ?, ?, ?, NULL)', (rowid, run_id, status, started, completed, 'observe', 'test', '{"generated_count": 7}', '{}'))


def test_concurrent_runs_are_collected_when_older_id_finishes_later(observer, db):
    factory_runs_table(db)
    add_run(db, 1, 'slow', status='running', completed=None)
    add_run(db, 2, 'fast')
    state = {}
    assert [x['run_id'] for x in observer._collect_new_entries(db, state)] == ['fast']
    db.execute("UPDATE strategy_factory_runs SET status='success', completed_at='2026-11-04T12:02:00+08:00' WHERE id=1")
    assert [x['run_id'] for x in observer._collect_new_entries(db, state)] == ['slow']
    assert observer._collect_new_entries(db, state) == []


def test_since_scope_excludes_old_runs_and_compares_offsets(observer, db):
    factory_runs_table(db)
    add_run(db, 1, 'old', started='2026-11-04T03:59:59Z')
    add_run(db, 2, 'new', started='2026-11-04T04:00:00Z')
    state = {'since': '2026-11-04T12:00:00+08:00'}
    rows = observer._collect_new_entries(db, state)
    assert [x['run_id'] for x in rows] == ['new']
    assert rows[0]['run_summary']['generated_count'] == 7


def test_every_report_is_recorded_beyond_five_representative_samples(observer, db):
    for i in range(7):
        add(db)
        db.execute('UPDATE strategy_quality_reports SET strategy_id=? WHERE id=?', (f's{i}', i+1))
    quality = observer._review_run(db, run())['quality']
    assert quality['report_count'] == 7
    assert quality['unique_strategy_count'] == 7
    assert len(quality['samples']) == 5
    assert len(quality['candidates']) == 7
    assert quality['goal_completion_verified'] is False


def test_oos_numeric_value_needs_actual_frozen_replay_evidence(observer):
    missing = observer._candidate_evidence({}, {}, {'target_layer_oos_return': .42})
    assert missing['oos_measured'] is False
    assert missing['oos_return'] is None
    measured = observer._candidate_evidence({}, {}, {
        'target_layer_oos_return': 0.0,
        'oos_validation': {'status': 'measured', 'source': 'frozen_candidate_holdout_replay', 'sealed': True, 'excluded_from_selection': True},
    })
    assert measured['oos_measured'] is True
    assert measured['oos_return'] == 0.0
    assert measured['execution_hard_gate_passed'] is None


def test_candidate_ledger_refreshes_later_execution_evidence_without_claiming_quality(observer, db):
    add(db)
    state = {'session_id': 'new-session', 'since': '2026-11-04T12:00:00+08:00', 'entries': [observer._review_run(db, run())]}
    first = observer._current_candidate_ledger(db, state)
    assert first['candidates'][0]['current_execution']['execution_hard_gate_passed'] is None
    db.execute('CREATE TABLE strategy_execution_audit_snapshots (strategy_id TEXT, snapshot_id TEXT, as_of_date TEXT, verdict_status TEXT, verdict_reasons TEXT, execution_hard_gate_passed INT, updated_at TEXT)')
    db.execute("INSERT INTO strategy_execution_audit_snapshots VALUES ('s1','audit1','2026-11-04','passed','[]',1,'2026-11-04T12:05:00+08:00')")
    refreshed = observer._current_candidate_ledger(db, state)
    assert refreshed['execution_hard_gate_passed_count'] == 1
    assert refreshed['candidates'][0]['current_execution']['snapshot_id'] == 'audit1'
    assert refreshed['goal_completion_verified'] is False
    assert refreshed['candidates'][0]['current_forward']['status'] == 'missing'


def test_archives_actual_persisted_candidate_artifacts(observer, db):
    db.execute('CREATE TABLE strategy_factory_run_artifacts (id INTEGER, run_id TEXT, artifact_type TEXT, artifact_version TEXT, payload_json TEXT, payload_hash TEXT, storage_mode TEXT, created_at TEXT)')
    payload = {'candidate_count': 2, 'candidates': [{'params': {'window': 10}}, {'params': {'window': 20}}]}
    db.execute('INSERT INTO strategy_factory_run_artifacts VALUES (1, ?, ?, ?, ?, ?, ?, ?)', ('own', 'candidate_artifact', 'v1', json.dumps(payload), 'digest', 'full', '2026-11-04'))
    archive = observer._archive_run_evidence(db, observer._review_run(db, run()))
    assert archive['persisted_artifacts'][0]['payload_json'] == payload
    assert archive['persisted_artifacts'][0]['payload_hash'] == 'digest'


def test_observer_state_cannot_be_reused_for_a_different_session(observer, tmp_path, monkeypatch):
    monkeypatch.setattr(observer, 'STATE_PATH', tmp_path / 'state.json')
    observer.STATE_PATH.write_text(json.dumps({'session_id': 'old', 'since': '2026-11-01T00:00:00+08:00', 'entries': []}))
    with pytest.raises(RuntimeError, match='observer_state_scope_mismatch'):
        observer._read_state(session_id='new', since='2026-11-04T00:00:00+08:00')


def test_missing_database_is_failure_without_creating_it(observer, tmp_path, monkeypatch):
    monkeypatch.setattr(observer, 'DB_PATH', tmp_path / 'missing.sqlite3')
    monkeypatch.setattr(observer, 'STATE_PATH', tmp_path / 'state.json')
    monkeypatch.setattr(observer, 'PID_PATH', tmp_path / 'observer.pid')
    monkeypatch.setattr(observer, 'LOG_DIR', tmp_path)
    assert observer.run(once=True, interval=5, since='2026-11-04T12:00:00+08:00', session_id='test') == 1
    assert not observer.DB_PATH.exists()
    assert not observer.PID_PATH.exists()
    assert 'database not found' in json.loads(observer.STATE_PATH.read_text())['last_error']


def test_nonfinite_and_boolean_metrics_are_missing(observer):
    assert observer._num(True) is None
    assert observer._num(False) is None


def test_failed_render_does_not_advance_cursor_and_next_poll_retries(observer, tmp_path, monkeypatch):
    path = tmp_path / 'runtime.sqlite3'
    with sqlite3.connect(path) as conn:
        factory_runs_table(conn)
        add_run(conn, 1, 'retry')
        conn.execute('CREATE TABLE strategy_quality_reports (id INTEGER, report_type TEXT, summary TEXT)')
    monkeypatch.setattr(observer, 'DB_PATH', path)
    monkeypatch.setattr(observer, 'STATE_PATH', tmp_path / 'state.json')
    monkeypatch.setattr(observer, 'PID_PATH', tmp_path / 'observer.pid')
    monkeypatch.setattr(observer, 'REPORT_DIR', tmp_path / 'reports')
    monkeypatch.setattr(observer, 'LOG_DIR', tmp_path / 'logs')
    original = observer._collect_new_entries
    def collect(conn, state):
        assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('CREATE TABLE illegal (id INT)')
        state['reviewed_run_ids'] = ['retry']
        return [{'run_id': 'retry'}]
    monkeypatch.setattr(observer, '_collect_new_entries', collect)
    monkeypatch.setattr(observer, '_archive_run_evidence', lambda conn, entry: entry)
    def failed_render(state, conn):
        raise OSError('disk full')
    monkeypatch.setattr(observer, '_render_reports', failed_render)
    assert observer.run(once=True, interval=5) == 1
    saved = json.loads(observer.STATE_PATH.read_text())
    assert saved['reviewed_run_ids'] == []
    assert saved['entries'] == []
    assert 'disk full' in saved['last_error']
    monkeypatch.setattr(observer, '_render_reports', lambda state, conn: None)
    assert observer.run(once=True, interval=5) == 0
    saved = json.loads(observer.STATE_PATH.read_text())
    assert saved['reviewed_run_ids'] == ['retry']
    assert 'last_error' not in saved


def test_upstream_candidate_truncation_is_explicit_recording_gap(observer, db):
    db.execute('CREATE TABLE strategy_factory_run_artifacts (id INTEGER, run_id TEXT, artifact_type TEXT, artifact_version TEXT, payload_json TEXT, payload_hash TEXT, storage_mode TEXT, created_at TEXT)')
    payload = {'candidate_count': 20, 'candidate_briefs': [{'name': 'x'}] * 12}
    db.execute('INSERT INTO strategy_factory_run_artifacts VALUES (1, ?, ?, ?, ?, ?, ?, ?)', ('own', 'candidate_artifact', 'v1', json.dumps(payload), 'digest', 'inline_compact_json', '2026-11-04'))
    archive = observer._archive_run_evidence(db, observer._review_run(db, run()))
    assert archive['recording_gaps'] == [{'reason': 'upstream_candidate_details_truncated', 'candidate_count': 20, 'retained_details': 12}]


@pytest.mark.parametrize('failure', [None, 'hash', 'identity', 'count', 'missing_phase'])
def test_full_archive_hydration_requires_both_verified_phases(observer, db, tmp_path, monkeypatch, failure):
    from strategy_factory.application.candidate_archive import write_candidate_archive

    monkeypatch.setenv('AIASK_FACTORY_CANDIDATE_ARCHIVE_DIR', str(tmp_path))
    db.execute('CREATE TABLE strategy_factory_run_artifacts (id INTEGER, run_id TEXT, artifact_type TEXT, artifact_version TEXT, payload_json TEXT, payload_hash TEXT, storage_mode TEXT, created_at TEXT)')
    candidates = [{'name': str(i), 'params': {'dsl': {'conditions': list(range(30))}}} for i in range(20)]
    compact = {'candidate_count': 20, 'candidate_briefs': candidates[:12]}
    recording = {'complete': True, **{phase: write_candidate_archive('own', phase, {'candidates': candidates}) for phase in ('generated', 'evaluated')}}
    if failure == 'hash':
        recording['evaluated']['sha256'] = 'wrong'
    elif failure == 'identity':
        recording['evaluated'] = write_candidate_archive('foreign', 'evaluated', {'candidates': candidates})
    elif failure == 'count':
        recording['evaluated'] = write_candidate_archive('own', 'evaluated', {'candidates': candidates[:19]})
    elif failure == 'missing_phase':
        recording.pop('evaluated')
    for i, (kind, payload) in enumerate([('candidate_artifact', compact), ('candidate_recording', recording)], 1):
        db.execute('INSERT INTO strategy_factory_run_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (i, 'own', kind, 'v1', json.dumps(payload), 'digest', 'external_gzip_json', '2026-11-04'))
    archive = observer._archive_run_evidence(db, observer._review_run(db, run()))
    full = archive['full_candidate_recording']
    assert full['complete'] is (failure is None)
    if failure is None:
        assert full['phases']['evaluated']['candidates'] == candidates
        assert full['generated_count'] == full['evaluated_count'] == 20
        assert archive['recording_gaps'] == []
    else:
        assert archive['recording_gaps']
        assert any(gap['reason'] == 'upstream_candidate_details_truncated' for gap in archive['recording_gaps'])
