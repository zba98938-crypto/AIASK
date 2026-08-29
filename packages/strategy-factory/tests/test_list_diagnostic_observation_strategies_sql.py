from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest


_SQL_DIAGNOSTIC_OBS = """
SELECT s.id, s.strategy_type, a.bound_at AS diagnostic_bound_at
FROM strategies s
JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
WHERE s.status IN ('diagnostic', 'submitted')
  AND a.stage = 'diagnostic'
  AND a.status = 'active'
  AND NOT EXISTS (
      SELECT 1 FROM strategy_incubation_accounts a2
      WHERE a2.strategy_id = s.id
        AND a2.stage IN ('paper', 'candidate', 'listed', 'incubating', 'promoted')
        AND a2.status = 'active'
  )
ORDER BY datetime(a.bound_at) DESC
LIMIT ?
"""


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        path = Path(f.name)
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE strategies (
            id TEXT PRIMARY KEY,
            name TEXT,
            strategy_type TEXT,
            status TEXT,
            params TEXT,
            metadata TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE strategy_incubation_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT,
            account_id TEXT,
            stage TEXT,
            status TEXT,
            source_run_id TEXT,
            metadata TEXT,
            bound_at TEXT,
            updated_at TEXT
        );
        """
    )
    yield con, path
    con.close()
    try:
        path.unlink()
    except Exception:
        pass


def _run(con, limit: int = 5):
    return con.execute(_SQL_DIAGNOSTIC_OBS, (limit,)).fetchall()


def test_select_only_active_diagnostic_statuses(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s2', 'n2', 'value_factor',        'diagnostic','{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s3', 'n3', 'momentum',            'submitted', '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s4', 'n4', 'rsi',                 'rejected',  '{}', '{}', '', '');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc1', 'diagnostic', 'active', '', '{}', '2026-05-29 10:00:00', ''),
            (NULL, 's2', 'acc2', 'diagnostic', 'active', '', '{}', '2026-05-29 11:00:00', ''),
            (NULL, 's3', 'acc3', 'paper',      'active', '', '{}', '2026-05-29 12:00:00', ''),
            (NULL, 's4', 'acc4', 'diagnostic', 'active', '', '{}', '2026-05-29 13:00:00', '');
        """
    )

    assert [row[0] for row in _run(con)] == ["s2", "s1"]


@pytest.mark.parametrize("stage", ["paper", "candidate", "listed", "incubating", "promoted"])
def test_exclude_strategies_with_active_formal_or_observation_account(temp_db, stage):
    con, _ = temp_db
    con.executescript(
        f"""
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{{}}', '{{}}', '', '');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc_diag', 'diagnostic', 'active', '', '{{}}', '2026-05-29 10:00:00', ''),
            (NULL, 's1', 'acc_other', '{stage}', 'active', '', '{{}}', '2026-05-29 12:00:00', '');
        """
    )

    assert _run(con) == []


def test_inactive_promoted_account_does_not_exclude(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc_diag', 'diagnostic', 'active',   '', '{}', '2026-05-29 10:00:00', ''),
            (NULL, 's1', 'acc_old',  'candidate',  'inactive', '', '{}', '2026-05-29 12:00:00', '');
        """
    )

    assert [row[0] for row in _run(con)] == ["s1"]


def test_order_by_bound_at_desc_and_limit(temp_db):
    con, _ = temp_db
    for idx, hour in enumerate([10, 12, 11], start=1):
        con.execute(
            "INSERT INTO strategies VALUES (?, ?, 'momentum', 'submitted', '{}', '{}', '', '')",
            (f"s{idx}", f"n{idx}"),
        )
        con.execute(
            "INSERT INTO strategy_incubation_accounts VALUES (NULL, ?, ?, 'diagnostic', 'active', '', '{}', ?, '')",
            (f"s{idx}", f"acc{idx}", f"2026-05-29 {hour:02d}:00:00"),
        )
    con.commit()

    rows = _run(con, limit=2)

    assert [row[0] for row in rows] == ["s2", "s3"]


def test_strategy_without_diagnostic_account_excluded(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        """
    )

    assert _run(con) == []
