#!/usr/bin/env python3
import sqlite3
from pathlib import Path

from _integration_paths import default_sqlite_path

DB_PATH = default_sqlite_path()
conn = sqlite3.connect(str(DB_PATH))

print("=" * 80)
print("快速状态检查")
print("=" * 80)

# incubating 状态分布
cursor = conn.execute("""
    SELECT incubating, COUNT(*) as cnt
    FROM strategies
    WHERE incubating IS NOT NULL
    GROUP BY incubating
    ORDER BY cnt DESC
""")

print("\n[incubating 状态]")
for row in cursor:
    print(f"  {row[0]}: {row[1]}")

# formal 专项
cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE incubating = 'formal_incubation'")
formal_count = cursor.fetchone()[0]
print(f"\n[formal_incubation 专项]")
print(f"  数量: {formal_count}")

if formal_count > 0:
    print("  🎉 首批转正成功！")
else:
    print("  ⏳ 等待转正...")

conn.close()
