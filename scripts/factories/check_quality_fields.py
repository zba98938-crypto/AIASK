#!/usr/bin/env python3
"""
检查策略的详细质量门槛字段
"""
import sqlite3
import json
from pathlib import Path

from _paths import DB_PATH

conn = sqlite3.connect(str(DB_PATH))

print("=" * 80)
print("Quality Gate Field Analysis")
print("=" * 80)

# 检查 strategies 表有哪些质量门槛相关列
cursor = conn.execute("PRAGMA table_info(strategies)")
columns = [row[1] for row in cursor]

quality_columns = [col for col in columns if 'gate' in col.lower() or 'hard' in col.lower() or 'ready' in col.lower()]
print(f"\nQuality gate related columns:")
for col in quality_columns:
    print(f"  - {col}")

# 检查 observe_incubation 策略的 params 中的关键字段
cursor = conn.execute("""
    SELECT id, params
    FROM strategies
    WHERE incubating = 'observe_incubation'
    LIMIT 3
""")

print(f"\nSample strategies (first 3):")
print("-" * 80)

for row in cursor:
    sid = row[0]
    params = json.loads(row[1]) if row[1] else {}

    print(f"\nStrategy: {sid[:40]}...")

    # 检查关键字段
    key_fields = [
        'compiled_dsl',
        'evidence_chain',
        'prediction_contract',
        'confidence_contract',
        'hard_gate_passed',
        'compile_stable_ready',
        'execution_hard_gate_passed',
        'formal_readiness_blockers'
    ]

    for field in key_fields:
        value = params.get(field)
        if value is None:
            print(f"  {field}: (missing)")
        elif isinstance(value, (dict, list)):
            print(f"  {field}: {type(value).__name__} with {len(value)} items")
        else:
            print(f"  {field}: {value}")

print("\n" + "=" * 80)

conn.close()
