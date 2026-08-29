import os
from pathlib import Path

def _aiask_root() -> Path:
    override = os.getenv("AIASK_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = Path(__file__).resolve()
    for parent in (current.parent, *current.parents):
        if (parent / "packages").is_dir() and (parent / "scripts").is_dir():
            return parent
    return Path.cwd()


def _runtime_home() -> Path:
    override = os.getenv("AIASK_RUNTIME_HOME")
    if override:
        return Path(override).expanduser().resolve()
    localapp = os.getenv("LOCALAPPDATA")
    if localapp:
        return (Path(localapp) / "hermes-aiask").resolve()
    xdg_state = os.getenv("XDG_STATE_HOME")
    if xdg_state:
        return (Path(xdg_state).expanduser() / "hermes-aiask").resolve()
    return (Path.home() / ".aiask").resolve()


def _aiask_db_path() -> str:
    override = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    if override:
        return str(Path(override).expanduser().resolve())
    return str(_runtime_home() / "data" / "db" / "akshare_mcp.sqlite3")
"""Monitor position aging distribution.

Identifies long-held positions that may need attention.
Run periodically to track position lifecycle health.
"""
import sqlite3
import sys
from datetime import datetime, timedelta

def monitor_position_aging(db_path: str = None):
    if not db_path:
        db_path = _aiask_db_path()

    db = sqlite3.connect(db_path)

    print("=== POSITION AGING DISTRIBUTION ===")
    print(f"Report time: {datetime.now().isoformat()}\n")

    # Overall statistics
    total_open = db.execute('SELECT COUNT(*) FROM strategy_trade_positions WHERE status="open"').fetchone()[0]
    print(f"Total open positions: {total_open}\n")

    # Age distribution
    print("Age Distribution (days since opened):")
    age_buckets = db.execute('''
        SELECT
            CASE
                WHEN age_days <= 1 THEN '0-1 day'
                WHEN age_days <= 3 THEN '2-3 days'
                WHEN age_days <= 7 THEN '4-7 days'
                WHEN age_days <= 14 THEN '8-14 days'
                WHEN age_days <= 30 THEN '15-30 days'
                WHEN age_days <= 60 THEN '31-60 days'
                ELSE '60+ days'
            END as age_bucket,
            COUNT(*) as count,
            ROUND(COUNT(*) * 100.0 / ?, 1) as pct
        FROM (
            SELECT julianday('now') - julianday(opened_at) as age_days
            FROM strategy_trade_positions
            WHERE status = 'open'
        )
        GROUP BY age_bucket
        ORDER BY
            CASE age_bucket
                WHEN '0-1 day' THEN 1
                WHEN '2-3 days' THEN 2
                WHEN '4-7 days' THEN 3
                WHEN '8-14 days' THEN 4
                WHEN '15-30 days' THEN 5
                WHEN '31-60 days' THEN 6
                ELSE 7
            END
    ''', (total_open,)).fetchall()

    for bucket, count, pct in age_buckets:
        bar = '█' * int(pct / 2)
        print(f"  {bucket:12} {count:5} ({pct:5.1f}%) {bar}")

    # Stale positions (>30 days)
    stale_threshold = 30
    stale_positions = db.execute('''
        SELECT
            s.id,
            s.name,
            stp.code,
            stp.remaining_shares,
            stp.opened_at,
            julianday('now') - julianday(stp.opened_at) as age_days,
            CASE WHEN EXISTS(
                SELECT 1 FROM strategy_signals ss
                WHERE ss.strategy_id = s.id AND ss.signal = -1
            ) THEN 'YES' ELSE 'NO' END as has_exit_signal
        FROM strategy_trade_positions stp
        JOIN strategies s ON stp.strategy_id = s.id
        WHERE stp.status = 'open'
        AND julianday('now') - julianday(stp.opened_at) > ?
        ORDER BY age_days DESC
        LIMIT 20
    ''', (stale_threshold,)).fetchall()

    if stale_positions:
        print(f"\n=== STALE POSITIONS (>{stale_threshold} days, top 20) ===")
        print(f"{'Age':>5} {'Code':8} {'Shares':>8} {'Exit?':6} {'Strategy'}")
        print("-" * 75)
        for sid, name, code, shares, opened, age, has_exit in stale_positions:
            print(f"{int(age):5}d {code:8} {shares:8} {has_exit:6} {name[:40]}")

    # Strategy-level aggregation
    print("\n=== STRATEGIES WITH OLDEST POSITIONS (top 10) ===")
    old_strategies = db.execute('''
        SELECT
            s.id,
            s.name,
            COUNT(DISTINCT stp.position_id) as position_count,
            ROUND(AVG(julianday('now') - julianday(stp.opened_at)), 1) as avg_age_days,
            MAX(julianday('now') - julianday(stp.opened_at)) as max_age_days,
            CASE WHEN EXISTS(
                SELECT 1 FROM strategy_signals ss
                WHERE ss.strategy_id = s.id AND ss.signal = -1
            ) THEN 'YES' ELSE 'NO' END as has_exit_signal
        FROM strategies s
        JOIN strategy_trade_positions stp ON s.id = stp.strategy_id
        WHERE stp.status = 'open'
        GROUP BY s.id
        ORDER BY avg_age_days DESC
        LIMIT 10
    ''').fetchall()

    for sid, name, pos_count, avg_age, max_age, has_exit in old_strategies:
        print(f"{name[:45]:45} | {pos_count:2} pos | avg {avg_age:5.1f}d | max {int(max_age):3}d | exit:{has_exit}")

    db.close()

if __name__ == '__main__':
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    monitor_position_aging(db_path)
