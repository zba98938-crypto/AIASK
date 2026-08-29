import sqlite3

from _integration_paths import default_sqlite_path

conn = sqlite3.connect(str(default_sqlite_path()))

# Check table existence
cursor = conn.execute("""
    SELECT name FROM sqlite_master
    WHERE type='table' AND name='strategy_signal_evidence'
""")
exists = cursor.fetchone()
print(f"strategy_signal_evidence exists: {bool(exists)}")

if exists:
    # Total records
    total = conn.execute("SELECT COUNT(*) FROM strategy_signal_evidence").fetchone()[0]
    print(f"Total evidence records: {total:,}")

    # Unique signals
    signals = conn.execute("SELECT COUNT(DISTINCT signal_id) FROM strategy_signal_evidence").fetchone()[0]
    print(f"Unique signal_ids: {signals:,}")

    # Sample
    print("\nSample (first 3):")
    cursor = conn.execute("SELECT signal_id, strategy_id FROM strategy_signal_evidence LIMIT 3")
    for row in cursor:
        print(f"  signal: {row[0][:30]}... strategy: {row[1][:30]}...")

conn.close()
