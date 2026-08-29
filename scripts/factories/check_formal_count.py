#!/usr/bin/env python3
"""
简单检查 formal_incubation 数量
"""
import sqlite3
from pathlib import Path

from _paths import DB_PATH

conn = sqlite3.connect(str(DB_PATH))

cursor = conn.execute("""
    SELECT incubating, COUNT(*) as cnt
    FROM strategies
    GROUP BY incubating
    ORDER BY cnt DESC
""")

print("=" * 80)
print("策略状态分布")
print("=" * 80)

formal_count = 0
observe_count = 0

for row in cursor:
    status = row[0] or '(null)'
    count = row[1]
    print(f"  {status}: {count}")

    if status == 'formal_incubation':
        formal_count = count
    elif status == 'observe_incubation':
        observe_count = count

print("\n" + "=" * 80)
print(f"关键结果:")
print(f"  observe_incubation: {observe_count}")
print(f"  formal_incubation: {formal_count}")

if formal_count > 0:
    print(f"\n✅ SUCCESS! {formal_count} 个策略已转正到 formal_incubation")
else:
    print(f"\n❌ formal_incubation 仍然为 0")

print("=" * 80)

conn.close()
