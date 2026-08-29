#!/usr/bin/env python3
"""
验证 Schema 迁移结果
"""
import sqlite3
from pathlib import Path

from _paths import DB_PATH

def main():
    conn = sqlite3.connect(str(DB_PATH))

    print("="*80)
    print("Schema 迁移验证")
    print("="*80)

    # 1. incubating 列分布
    print("\n[1] 迁移后 incubating 列分布")
    print("-" * 80)
    cursor = conn.execute("""
        SELECT incubating, COUNT(*) as cnt
        FROM strategies
        GROUP BY incubating
        ORDER BY cnt DESC
    """)

    total_migrated = 0
    null_count = 0

    for row in cursor:
        incubating, cnt = row
        if incubating:
            total_migrated += cnt
            print(f"  {incubating:30s}: {cnt:6d}")
        else:
            null_count = cnt
            print(f"  {'(NULL)':30s}: {cnt:6d}")

    print(f"\n  已迁移记录: {total_migrated}")
    print(f"  未迁移记录: {null_count}")

    # 2. observe_incubation 中的高 skill 策略
    print("\n[2] observe_incubation 中的高 skill 策略")
    print("-" * 80)

    cursor = conn.execute("""
        WITH high_skill AS (
            SELECT s.id
            FROM strategies s
            JOIN strategy_signals sig ON s.id = sig.strategy_id
            JOIN signal_forward_returns sfr ON sig.id = sfr.signal_id
            WHERE sfr.forward_days = 5
            GROUP BY s.id
            HAVING COUNT(DISTINCT sig.id) >= 3
                AND AVG(CASE WHEN sfr.actual_return > 0 THEN 1.0 ELSE 0.0 END) >= 0.55
        )
        SELECT COUNT(*)
        FROM high_skill hs
        JOIN strategies s ON hs.id = s.id
        WHERE s.incubating = 'observe_incubation'
    """)

    high_skill_in_observe = cursor.fetchone()[0]
    print(f"  高 skill 策略数: {high_skill_in_observe}")

    # 3. 对比迁移前后
    print("\n[3] 对比迁移前后（基于原 status）")
    print("-" * 80)

    cursor = conn.execute("""
        SELECT status, incubating, COUNT(*) as cnt
        FROM strategies
        WHERE status IN ('submitted', 'incubating')
        GROUP BY status, incubating
        ORDER BY status, cnt DESC
    """)

    print(f"\n  status → incubating 映射:")
    for row in cursor:
        status, incubating, cnt = row
        print(f"    {status:15s} → {incubating or '(NULL)':30s}: {cnt:6d}")

    # 4. 成功判断
    print("\n" + "="*80)
    print("迁移结果")
    print("="*80)

    if total_migrated > 0:
        print(f"\n  [OK] 迁移成功!")
        print(f"    - 总迁移记录: {total_migrated}")
        print(f"    - observe_incubation 中有高 skill 策略: {high_skill_in_observe}")
        print(f"\n  预期效果:")
        print(f"    - 这些策略现在可以转 formal_incubation")
        print(f"    - 继续运行 Quality Session 观察转正")
    else:
        print(f"\n  [WARN] 迁移可能未执行或已执行过")

    print("\n" + "="*80)

    conn.close()

if __name__ == '__main__':
    main()
