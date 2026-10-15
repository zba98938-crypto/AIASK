import sqlite3
import json

from _integration_paths import default_sqlite_path
import logging

logger = logging.getLogger(__name__)

conn = sqlite3.connect(str(default_sqlite_path()))

# 检查所有策略中是否有这些字段
cursor = conn.execute("SELECT id, params FROM strategies LIMIT 100")

found_fields = {
    'compiled_dsl': 0,
    'evidence_chain': 0,
    'confidence_contract': 0,
    'trade_prediction_contract': 0
}

total = 0
for row in cursor:
    total += 1
    if row[1]:
        try:
            params = json.loads(row[1])
            for field in found_fields:
                if field in params and params[field]:
                    found_fields[field] += 1
        except BaseException as _swallowed:
            logger.warning("scripts/check_contract_fields.py:28: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

print("Checked first 100 strategies:")
for field, count in found_fields.items():
    print(f"  {field}: {count}/{total} ({count/total*100:.1f}%)")

# 检查是否在 params 的其他位置
print("\nChecking nested structures...")
cursor = conn.execute("SELECT id, params FROM strategies WHERE incubating = 'observe_incubation' LIMIT 1")
row = cursor.fetchone()
if row and row[1]:
    params = json.loads(row[1])
    print(f"Sample params has {len(params)} top-level keys")

    # 检查是否有嵌套的语义契约
    if 'candidate_lineage_contract' in params:
        print("  Has candidate_lineage_contract")
        lineage = params['candidate_lineage_contract']
        if isinstance(lineage, dict):
            print(f"    Keys: {list(lineage.keys())[:10]}")

conn.close()
