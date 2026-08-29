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
"""Monitor signal-position alignment health.

Identifies strategies with positions but no exit signals,
which indicates potential issues in signal generation continuity.
"""
import sqlite3
import sys
from datetime import datetime

def monitor_signal_position_alignment(db_path: str = None):
    if not db_path:
        db_path = _aiask_db_path()

    db = sqlite3.connect(db_path)

    print("=== SIGNAL-POSITION ALIGNMENT HEALTH CHECK ===")
    print(f"Report time: {datetime.now().isoformat()}\n")

    # Overall metrics
    total_strategies_with_positions = db.execute('''
        SELECT COUNT(DISTINCT strategy_id)
        FROM strategy_trade_positions
        WHERE status = 'open'
    ''').fetchone()[0]

    with_exit_signals = db.execute('''
        SELECT COUNT(DISTINCT stp.strategy_id)
        FROM strategy_trade_positions stp
        WHERE stp.status = 'open'
        AND stp.strategy_id IN (
            SELECT DISTINCT strategy_id FROM strategy_signals WHERE signal = -1
        )
    ''').fetchone()[0]

    without_exit_signals = total_strategies_with_positions - with_exit_signals
    coverage_pct = (with_exit_signals / total_strategies_with_positions * 100) if total_strategies_with_positions > 0 else 0

    print(f"Strategies with open positions: {total_strategies_with_positions}")
    print(f"  [OK] With exit signals:    {with_exit_signals} ({coverage_pct:.1f}%)")
    print(f"  [!!] Without exit signals: {without_exit_signals} ({100-coverage_pct:.1f}%)")

    # Health status
    print(f"\n=== HEALTH STATUS ===")
    if coverage_pct >= 80:
        status = "HEALTHY"
        emoji = "[OK]"
    elif coverage_pct >= 50:
        status = "MODERATE"
        emoji = "[WARN]"
    else:
        status = "NEEDS ATTENTION"
        emoji = "[FAIL]"
    print(f"{emoji} {status} - Exit signal coverage: {coverage_pct:.1f}%")

    # Breakdown by status
    print(f"\n=== BREAKDOWN BY STRATEGY STATUS ===")
    status_breakdown = db.execute('''
        SELECT
            s.status,
            COUNT(DISTINCT s.id) as strategy_count,
            SUM(CASE WHEN EXISTS(
                SELECT 1 FROM strategy_signals ss
                WHERE ss.strategy_id = s.id AND ss.signal = -1
            ) THEN 1 ELSE 0 END) as with_exit_signal
        FROM strategies s
        JOIN strategy_trade_positions stp ON s.id = stp.strategy_id
        WHERE stp.status = 'open'
        GROUP BY s.status
        ORDER BY strategy_count DESC
    ''').fetchall()

    for status, count, with_exit in status_breakdown:
        pct = (with_exit / count * 100) if count > 0 else 0
        print(f"  {status:20} {count:5} strategies | {with_exit:5} with exit ({pct:5.1f}%)")

    # Signal recency analysis
    print(f"\n=== SIGNAL GENERATION RECENCY ===")
    signal_recency = db.execute('''
        SELECT
            CASE
                WHEN days_since_last_signal <= 1 THEN 'Today'
                WHEN days_since_last_signal <= 3 THEN '2-3 days ago'
                WHEN days_since_last_signal <= 7 THEN '4-7 days ago'
                WHEN days_since_last_signal <= 14 THEN '8-14 days ago'
                WHEN days_since_last_signal <= 30 THEN '15-30 days ago'
                ELSE '30+ days ago'
            END as recency_bucket,
            COUNT(*) as strategy_count
        FROM (
            SELECT
                stp.strategy_id,
                julianday('now') - MAX(julianday(ss.signal_date)) as days_since_last_signal
            FROM strategy_trade_positions stp
            LEFT JOIN strategy_signals ss ON stp.strategy_id = ss.strategy_id
            WHERE stp.status = 'open'
            GROUP BY stp.strategy_id
        )
        GROUP BY recency_bucket
        ORDER BY
            CASE recency_bucket
                WHEN 'Today' THEN 1
                WHEN '2-3 days ago' THEN 2
                WHEN '4-7 days ago' THEN 3
                WHEN '8-14 days ago' THEN 4
                WHEN '15-30 days ago' THEN 5
                ELSE 6
            END
    ''').fetchall()

    for bucket, count in signal_recency:
        bar = '█' * (count // 50)
        print(f"  {bucket:15} {count:5} {bar}")

    # Sample strategies without exit signals
    print(f"\n=== SAMPLE: STRATEGIES WITHOUT EXIT SIGNALS (10 random) ===")
    samples = db.execute('''
        SELECT
            s.id,
            s.name,
            s.strategy_type,
            s.status,
            COUNT(DISTINCT stp.position_id) as position_count,
            MAX(ss.signal_date) as last_signal_date,
            MAX(CASE WHEN ss.signal = 1 THEN ss.signal_date END) as last_entry_signal
        FROM strategies s
        JOIN strategy_trade_positions stp ON s.id = stp.strategy_id
        LEFT JOIN strategy_signals ss ON s.id = ss.strategy_id
        WHERE stp.status = 'open'
        AND s.id NOT IN (
            SELECT DISTINCT strategy_id FROM strategy_signals WHERE signal = -1
        )
        GROUP BY s.id
        ORDER BY RANDOM()
        LIMIT 10
    ''').fetchall()

    for sid, name, stype, status, pos_count, last_signal, last_entry in samples:
        print(f"\n  {name[:50]}")
        print(f"    ID: {sid} | Type: {stype} | Status: {status}")
        print(f"    Positions: {pos_count} | Last signal: {last_signal or 'Never'}")
        print(f"    Last entry signal: {last_entry or 'Never'}")

    # Recommendations
    print(f"\n=== RECOMMENDATIONS ===")
    if without_exit_signals > 500:
        print(f"  [WARN] {without_exit_signals} strategies need exit signal generation")
        print(f"    Consider reviewing strategy DSL exit conditions")
        print(f"    Check if Phase 3 signal generation is running correctly")
    if coverage_pct < 50:
        print(f"  [FAIL] Low coverage indicates systematic signal generation issues")
        print(f"    Priority: Review incubation_parts/runtime.py signal generation")
    else:
        print(f"  [OK] Signal coverage is within acceptable range")
        print(f"    Continue monitoring; address outliers in next review")

    db.close()

if __name__ == '__main__':
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    monitor_signal_position_alignment(db_path)
