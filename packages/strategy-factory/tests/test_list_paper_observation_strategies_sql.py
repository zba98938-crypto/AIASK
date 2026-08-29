"""DEV-V1 P1: list_paper_observation_strategies SQL 边界集成测试.

直接对临时 SQLite 数据库执行目标 SQL,验证:
  1. JOIN ON a.strategy_id = s.id 正确(s.strategy_id 不存在,误用立即报错)
  2. 反 EXISTS 子句排除已升 candidate / listed 的策略
  3. 排除 stage='warmup' 历史孤儿账户
  4. ORDER BY datetime(bound_at) DESC 生效
  5. LIMIT 参数化生效
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest


# 与 _strategy_crud_core.py::list_paper_observation_strategies 保持同步的 SQL,
# 只把 $1 改为 ? 以便直接 sqlite3.execute 测试。
_SQL_PAPER_OBS = """
SELECT s.id, s.strategy_type, a.bound_at AS paper_bound_at
FROM strategies s
JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
WHERE s.status = 'submitted'
  AND a.stage = 'paper'
  AND a.status = 'active'
  AND NOT EXISTS (
      SELECT 1 FROM strategy_incubation_accounts a2
      WHERE a2.strategy_id = s.id
        AND a2.stage IN ('candidate', 'listed')
        AND a2.status = 'active'
  )
ORDER BY datetime(a.bound_at) DESC
LIMIT ?
"""

_SQL_PAPER_BACKLOG = """
SELECT COUNT(*) AS backlog_count,
       MAX(datetime(a.bound_at)) AS latest_bound_at
FROM strategies s
JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
WHERE s.status = 'submitted'
  AND a.stage = 'paper'
  AND a.status = 'active'
  AND NOT EXISTS (
      SELECT 1 FROM strategy_incubation_accounts a2
      WHERE a2.strategy_id = s.id
        AND a2.stage IN ('candidate', 'listed')
      AND a2.status = 'active'
  )
"""

_SQL_PAPER_ACTIVE = """
SELECT s.id, s.strategy_type, a.stage AS observation_stage
FROM strategies s
JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
LEFT JOIN paper_accounts pa ON pa.id = a.account_id
WHERE s.status = 'submitted'
  AND a.status = 'active'
  AND a.stage IN ('paper', 'warmup', 'observe')
  AND COALESCE(pa.status, 'active') = 'active'
  AND COALESCE(pa.account_type, 'incubation') = 'incubation'
  AND NOT EXISTS (
      SELECT 1 FROM strategy_incubation_accounts a2
      WHERE a2.strategy_id = s.id
        AND a2.stage IN ('candidate', 'graduation_ready', 'listed', 'promoted')
        AND a2.status = 'active'
  )
ORDER BY
  CASE a.stage
      WHEN 'paper' THEN 0
      WHEN 'warmup' THEN 1
      WHEN 'observe' THEN 2
      ELSE 9
  END,
  datetime(COALESCE(a.updated_at, a.bound_at)) DESC
LIMIT ?
"""

_SQL_PAPER_ACTIVE_COUNT = """
SELECT COUNT(*) AS active_count,
       MAX(datetime(COALESCE(a.updated_at, a.bound_at))) AS latest_active_at
FROM strategies s
JOIN strategy_incubation_accounts a ON a.strategy_id = s.id
LEFT JOIN paper_accounts pa ON pa.id = a.account_id
WHERE s.status = 'submitted'
  AND a.status = 'active'
  AND a.stage IN ('paper', 'warmup', 'observe')
  AND COALESCE(pa.status, 'active') = 'active'
  AND COALESCE(pa.account_type, 'incubation') = 'incubation'
  AND NOT EXISTS (
      SELECT 1 FROM strategy_incubation_accounts a2
      WHERE a2.strategy_id = s.id
        AND a2.stage IN ('candidate', 'graduation_ready', 'listed', 'promoted')
        AND a2.status = 'active'
  )
"""

_SQL_SUBMITTED_FOR_DEDUP = """
SELECT s.id
FROM strategies s
WHERE s.status = 'submitted'
  AND NOT EXISTS (
      SELECT 1
      FROM strategy_incubation_accounts a
      LEFT JOIN paper_accounts pa ON pa.id = a.account_id
      WHERE a.strategy_id = s.id
        AND a.status = 'active'
        AND a.stage IN ('paper', 'warmup', 'observe')
        AND COALESCE(pa.status, 'active') = 'active'
        AND COALESCE(pa.account_type, 'incubation') = 'incubation'
  )
ORDER BY datetime(s.updated_at) DESC
LIMIT ? OFFSET ?
"""

_SQL_LATEST_PAPER_RECOGNIZED = """
SELECT event_type, created_at
FROM strategy_domain_events
WHERE event_type = 'incubation_factory.paper_observation_recognized'
ORDER BY datetime(created_at) DESC
LIMIT 1
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
        CREATE TABLE strategy_domain_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            created_at TEXT
        );
        CREATE TABLE paper_accounts (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            name TEXT,
            initial_capital REAL,
            current_capital REAL,
            total_value REAL,
            risk_rules TEXT,
            strategy_id TEXT,
            account_type TEXT,
            incubation_stage TEXT,
            promotion_candidate INTEGER,
            archived_reason TEXT,
            status TEXT,
            created_at TEXT,
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


def _run(con, limit: int = 50):
    cur = con.execute(_SQL_PAPER_OBS, (limit,))
    return cur.fetchall()


def test_select_only_paper_active_submitted(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s2', 'n2', 'value_factor',        'rejected',  '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s3', 'n3', 'ma_cross',            'submitted', '{}', '{}', '', '');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc1', 'paper',  'active', '', '{}', '2026-05-26 10:00:00', ''),
            (NULL, 's2', 'acc2', 'paper',  'active', '', '{}', '2026-05-26 11:00:00', ''),
            (NULL, 's3', 'acc3', 'warmup', 'active', '', '{}', '2026-05-21 09:00:00', '');
        """
    )
    rows = _run(con)
    ids = [r[0] for r in rows]
    # s2 status=rejected 排除,s3 stage=warmup 排除
    assert ids == ['s1']


def test_exclude_strategies_promoted_to_candidate(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc_paper', 'paper',     'active', '', '{}', '2026-05-26 10:00:00', ''),
            (NULL, 's1', 'acc_cand',  'candidate', 'active', '', '{}', '2026-05-26 12:00:00', '');
        """
    )
    rows = _run(con)
    # 反 EXISTS 排除了已升 candidate
    assert rows == []


def test_exclude_strategies_already_listed(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc_paper',  'paper',  'active', '', '{}', '2026-05-26 10:00:00', ''),
            (NULL, 's1', 'acc_listed', 'listed', 'active', '', '{}', '2026-05-26 12:00:00', '');
        """
    )
    rows = _run(con)
    assert rows == []


def test_inactive_paper_account_not_selected(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc_paper', 'paper', 'inactive', '', '{}', '2026-05-26 10:00:00', '');
        """
    )
    rows = _run(con)
    assert rows == []


def test_order_by_bound_at_desc(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s2', 'n2', 'value_factor',        'submitted', '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s3', 'n3', 'sector_rotation',     'submitted', '{}', '{}', '', '');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc1', 'paper', 'active', '', '{}', '2026-05-26 10:00:00', ''),
            (NULL, 's2', 'acc2', 'paper', 'active', '', '{}', '2026-05-26 14:00:00', ''),
            (NULL, 's3', 'acc3', 'paper', 'active', '', '{}', '2026-05-26 12:00:00', '');
        """
    )
    rows = _run(con)
    ids = [r[0] for r in rows]
    assert ids == ['s2', 's3', 's1']  # 按 bound_at DESC


def test_limit_respected(temp_db):
    con, _ = temp_db
    for i in range(20):
        con.execute(
            "INSERT INTO strategies VALUES (?, ?, 'volatility_breakout', 'submitted', '{}', '{}', '', '')",
            (f's{i}', f'n{i}'),
        )
        con.execute(
            "INSERT INTO strategy_incubation_accounts VALUES (NULL, ?, ?, 'paper', 'active', '', '{}', ?, '')",
            (f's{i}', f'acc{i}', f'2026-05-26 {10+i:02d}:00:00'),
        )
    con.commit()
    rows = _run(con, limit=5)
    assert len(rows) == 5


def test_join_uses_s_id_not_strategy_id(temp_db):
    """关键:strategies 表只有 id 列,如果 SQL 误用 s.strategy_id 会立即 OperationalError."""
    con, _ = temp_db
    cur = con.execute("PRAGMA table_info(strategies)")
    cols = {r[1] for r in cur.fetchall()}
    assert "id" in cols
    assert "strategy_id" not in cols, (
        "strategies 表如果出现 strategy_id 列,P1 SQL 的 JOIN 条件必须重新核对"
    )


def test_strategy_with_no_account_excluded(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        """
    )
    # 没有 incubation_account 关联,JOIN 后空
    rows = _run(con)
    assert rows == []


def test_active_observation_includes_submitted_warmup_after_initial_paper_recognition(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s2', 'n2', 'value_factor',        'submitted', '{}', '{}', '', '');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc1', 'warmup', 'active', '', '{}', '2026-05-26 10:00:00', '2026-05-26 11:00:00'),
            (NULL, 's2', 'acc2', 'listed', 'active', '', '{}', '2026-05-26 12:00:00', '2026-05-26 13:00:00');
        INSERT INTO paper_accounts VALUES
            ('acc1', 'u1', 'p1', 100000, 100000, 100000, '{}', 's1', 'incubation', 'warmup', 0, '', 'active', '2026-05-26 10:00:00', '2026-05-26 11:00:00'),
            ('acc2', 'u1', 'p2', 100000, 100000, 100000, '{}', 's2', 'incubation', 'listed', 1, '', 'active', '2026-05-26 12:00:00', '2026-05-26 13:00:00');
        """
    )

    rows = con.execute(_SQL_PAPER_ACTIVE, (50,)).fetchall()
    ids = [r[0] for r in rows]

    assert ids == ["s1"]


def test_submitted_dedup_query_excludes_active_observe_backlog_only(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '2026-05-26 10:00:00');
        INSERT INTO strategies VALUES ('s2', 'n2', 'value_factor',        'submitted', '{}', '{}', '', '2026-05-26 11:00:00');
        INSERT INTO strategies VALUES ('s3', 'n3', 'sector_rotation',     'submitted', '{}', '{}', '', '2026-05-26 12:00:00');
        INSERT INTO strategies VALUES ('s4', 'n4', 'ma_cross',            'submitted', '{}', '{}', '', '2026-05-26 13:00:00');
        INSERT INTO strategies VALUES ('s5', 'n5', 'trend_follow',        'rejected',  '{}', '{}', '', '2026-05-26 14:00:00');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc1', 'warmup',    'active', '', '{}', '2026-05-26 10:00:00', '2026-05-26 10:30:00'),
            (NULL, 's2', 'acc2', 'paper',     'active', '', '{}', '2026-05-26 11:00:00', '2026-05-26 11:30:00'),
            (NULL, 's3', 'acc3', 'observe',   'active', '', '{}', '2026-05-26 12:00:00', '2026-05-26 12:30:00'),
            (NULL, 's4', 'acc4', 'candidate', 'active', '', '{}', '2026-05-26 13:00:00', '2026-05-26 13:30:00');
        INSERT INTO paper_accounts VALUES
            ('acc1', 'u1', 'p1', 100000, 100000, 100000, '{}', 's1', 'incubation', 'warmup', 0, '', 'active', '2026-05-26 10:00:00', '2026-05-26 10:30:00'),
            ('acc2', 'u1', 'p2', 100000, 100000, 100000, '{}', 's2', 'incubation', 'paper', 0, '', 'active', '2026-05-26 11:00:00', '2026-05-26 11:30:00'),
            ('acc3', 'u1', 'p3', 100000, 100000, 100000, '{}', 's3', 'incubation', 'observe', 0, '', 'active', '2026-05-26 12:00:00', '2026-05-26 12:30:00'),
            ('acc4', 'u1', 'p4', 100000, 100000, 100000, '{}', 's4', 'incubation', 'candidate', 0, '', 'active', '2026-05-26 13:00:00', '2026-05-26 13:30:00');
        """
    )

    rows = con.execute(_SQL_SUBMITTED_FOR_DEDUP, (50, 0)).fetchall()
    ids = [r[0] for r in rows]

    assert ids == ["s4"]


def test_paper_observation_backlog_status_counts_submitted_active_paper_only(temp_db):
    con, _ = temp_db
    con.executescript(
        """
        INSERT INTO strategies VALUES ('s1', 'n1', 'volatility_breakout', 'submitted', '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s2', 'n2', 'value_factor',        'submitted', '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s3', 'n3', 'sector_rotation',     'submitted', '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s4', 'n4', 'ma_cross',            'rejected',  '{}', '{}', '', '');
        INSERT INTO strategies VALUES ('s5', 'n5', 'trend_follow',        'submitted', '{}', '{}', '', '');
        INSERT INTO strategy_incubation_accounts VALUES
            (NULL, 's1', 'acc1', 'paper',     'active', '', '{}', '2026-05-26 10:00:00', ''),
            (NULL, 's2', 'acc2', 'paper',     'active', '', '{}', '2026-05-26 14:00:00', ''),
            (NULL, 's2', 'acc2c','candidate', 'active', '', '{}', '2026-05-26 15:00:00', ''),
            (NULL, 's3', 'acc3', 'paper',     'inactive', '', '{}', '2026-05-26 16:00:00', ''),
            (NULL, 's4', 'acc4', 'paper',     'active', '', '{}', '2026-05-26 17:00:00', ''),
            (NULL, 's5', 'acc5', 'warmup',    'active', '', '{}', '2026-05-26 18:00:00', '2026-05-26 19:00:00');
        INSERT INTO strategy_domain_events(event_type, created_at) VALUES
            ('incubation_factory.paper_observation_recognized', '2026-05-26 09:00:00'),
            ('incubation_factory.paper_observation_recognized', '2026-05-26 18:00:00'),
            ('incubation_factory.heartbeat', '2026-05-26 19:00:00');
        INSERT INTO paper_accounts VALUES
            ('acc1', 'u1', 'p1', 100000, 100000, 100000, '{}', 's1', 'incubation', 'paper', 0, '', 'active', '2026-05-26 10:00:00', '2026-05-26 10:00:00'),
            ('acc2', 'u1', 'p2', 100000, 100000, 100000, '{}', 's2', 'incubation', 'paper', 0, '', 'active', '2026-05-26 14:00:00', '2026-05-26 14:00:00'),
            ('acc3', 'u1', 'p3', 100000, 100000, 100000, '{}', 's3', 'incubation', 'paper', 0, '', 'inactive', '2026-05-26 16:00:00', '2026-05-26 16:00:00'),
            ('acc4', 'u1', 'p4', 100000, 100000, 100000, '{}', 's4', 'incubation', 'paper', 0, '', 'active', '2026-05-26 17:00:00', '2026-05-26 17:00:00'),
            ('acc5', 'u1', 'p5', 100000, 100000, 100000, '{}', 's5', 'incubation', 'warmup', 0, '', 'active', '2026-05-26 18:00:00', '2026-05-26 19:00:00');
        """
    )

    backlog = dict(zip(["backlog_count", "latest_bound_at"], con.execute(_SQL_PAPER_BACKLOG).fetchone()))
    active = dict(zip(["active_count", "latest_active_at"], con.execute(_SQL_PAPER_ACTIVE_COUNT).fetchone()))
    recognized = dict(
        zip(["event_type", "created_at"], con.execute(_SQL_LATEST_PAPER_RECOGNIZED).fetchone())
    )

    assert backlog["backlog_count"] == 1
    assert backlog["latest_bound_at"] == "2026-05-26 10:00:00"
    assert active["active_count"] == 2
    assert active["latest_active_at"] == "2026-05-26 19:00:00"
    assert recognized["event_type"] == "incubation_factory.paper_observation_recognized"
    assert recognized["created_at"] == "2026-05-26 18:00:00"
