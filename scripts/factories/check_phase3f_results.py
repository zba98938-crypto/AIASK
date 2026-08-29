#!/usr/bin/env python3
"""
检查 Phase 3f 质量门槛结果
"""
import sqlite3
from pathlib import Path

from _paths import DB_PATH

conn = sqlite3.connect(str(DB_PATH))

print("=" * 80)
print("Phase 3f Quality Gate Results")
print("=" * 80)

# 1. 检查 execution_audit_snapshot 表
cursor = conn.execute("""
    SELECT
        COUNT(*) as total,
        SUM(CASE WHEN hard_gate_passed = 1 THEN 1 ELSE 0 END) as passed,
        SUM(CASE WHEN hard_gate_passed = 0 THEN 1 ELSE 0 END) as failed
    FROM execution_audit_snapshot
    WHERE incubating = 'observe_incubation'
""")

row = cursor.fetchone()
if row:
    total, passed, failed = row
    print(f"\nExecution Audit Snapshot:")
    print(f"  Total: {total}")
    print(f"  hard_gate_passed=1: {passed}")
    print(f"  hard_gate_passed=0: {failed}")

# 2. 检查失败原因
cursor = conn.execute("""
    SELECT
        execution_audit_gate_status,
        COUNT(*) as cnt
    FROM execution_audit_snapshot
    WHERE incubating = 'observe_incubation'
    GROUP BY execution_audit_gate_status
    ORDER BY cnt DESC
""")

print(f"\nGate Status Distribution:")
for row in cursor:
    status = row[0] or '(null)'
    count = row[1]
    print(f"  {status}: {count}")

# 3. 检查一个失败样本的详细信息
cursor = conn.execute("""
    SELECT
        strategy_id,
        hard_gate_passed,
        execution_audit_gate_status,
        execution_audit_gate_blockers
    FROM execution_audit_snapshot
    WHERE incubating = 'observe_incubation'
    LIMIT 5
""")

print(f"\nSample strategies (first 5):")
for row in cursor:
    sid, passed, status, blockers = row
    print(f"  {sid[:30]}...")
    print(f"    hard_gate_passed: {passed}")
    print(f"    gate_status: {status}")
    print(f"    blockers: {blockers}")
    print()

print("=" * 80)

conn.close()
