import sqlite3
import json

from _paths import DB_PATH

conn = sqlite3.connect(str(DB_PATH))

print("=" * 80)
print("Phase 3f Quality Gate 诊断")
print("=" * 80)

# 1. 检查 evidence 的 proxy_only 分布
print("\n[1] Evidence proxy_only 分布")
print("-" * 80)
cursor = conn.execute("""
    SELECT
        json_extract(payload, '$.proxy_only') as proxy_only,
        json_extract(payload, '$.build_mode') as build_mode,
        COUNT(*) as cnt
    FROM strategy_signal_evidence
    GROUP BY proxy_only, build_mode
    ORDER BY cnt DESC
""")
for row in cursor:
    proxy = row[0] if row[0] is not None else '(null)'
    mode = row[1] if row[1] is not None else '(null)'
    print(f"  proxy_only={proxy}, build_mode={mode}: {row[2]:,}")

# 2. 检查策略的 compile_stable 状态
print("\n[2] observe 策略的语义契约状态")
print("-" * 80)
cursor = conn.execute("""
    SELECT
        COUNT(*) as total,
        SUM(CASE WHEN json_extract(params, '$.compiled_dsl') IS NOT NULL THEN 1 ELSE 0 END) as has_dsl,
        SUM(CASE WHEN json_extract(params, '$.evidence_chain') IS NOT NULL THEN 1 ELSE 0 END) as has_evidence_chain,
        SUM(CASE WHEN json_extract(params, '$.trade_prediction_contract') IS NOT NULL THEN 1 ELSE 0 END) as has_prediction,
        SUM(CASE WHEN json_extract(params, '$.confidence_contract') IS NOT NULL THEN 1 ELSE 0 END) as has_confidence
    FROM strategies
    WHERE incubating = 'observe_incubation'
""")
row = cursor.fetchone()
total = row[0]
print(f"  总数: {total:,}")
print(f"  有 compiled_dsl: {row[1]:,} ({row[1]/total*100:.1f}%)")
print(f"  有 evidence_chain: {row[2]:,} ({row[2]/total*100:.1f}%)")
print(f"  有 trade_prediction_contract: {row[3]:,} ({row[3]/total*100:.1f}%)")
print(f"  有 confidence_contract: {row[4]:,} ({row[4]/total*100:.1f}%)")

# 3. 检查有 evidence 的策略中，语义契约的状态
print("\n[3] 有 evidence 的策略中，语义契约完整性")
print("-" * 80)
cursor = conn.execute("""
    SELECT
        COUNT(DISTINCT s.id) as total,
        SUM(CASE WHEN json_extract(s.params, '$.compiled_dsl') IS NOT NULL THEN 1 ELSE 0 END) as has_dsl,
        SUM(CASE WHEN json_extract(s.params, '$.evidence_chain') IS NOT NULL THEN 1 ELSE 0 END) as has_evidence_chain
    FROM strategies s
    WHERE s.incubating = 'observe_incubation'
    AND EXISTS (
        SELECT 1 FROM strategy_signal_evidence e
        WHERE e.strategy_id = s.id
    )
""")
row = cursor.fetchone()
if row and row[0] > 0:
    total = row[0]
    print(f"  有 evidence 的策略: {total:,}")
    print(f"  其中有 compiled_dsl: {row[1]:,} ({row[1]/total*100:.1f}%)")
    print(f"  其中有 evidence_chain: {row[2]:,} ({row[2]/total*100:.1f}%)")
else:
    print("  没有策略同时有 evidence 和 observe 状态")

# 4. 检查 evidence 的置信度分布
print("\n[4] Evidence 置信度分布")
print("-" * 80)
cursor = conn.execute("""
    SELECT
        COUNT(*) as total,
        SUM(CASE WHEN raw_confidence IS NOT NULL THEN 1 ELSE 0 END) as has_raw_conf,
        SUM(CASE WHEN calibrated_confidence IS NOT NULL THEN 1 ELSE 0 END) as has_calib_conf
    FROM strategy_signal_evidence
""")
row = cursor.fetchone()
total = row[0]
print(f"  总 evidence: {total:,}")
print(f"  有 raw_confidence: {row[1]:,} ({row[1]/total*100:.1f}%)")
print(f"  有 calibrated_confidence: {row[2]:,} ({row[2]/total*100:.1f}%)")

# 5. 示例：查看一条 evidence 的完整 payload
print("\n[5] Evidence 示例（查看质量字段）")
print("-" * 80)
cursor = conn.execute("""
    SELECT
        signal_id,
        strategy_id,
        source_type,
        proxy_only,
        raw_confidence,
        calibrated_confidence,
        payload
    FROM strategy_signal_evidence
    LIMIT 1
""")
row = cursor.fetchone()
if row:
    print(f"  signal_id: {row[0]}")
    print(f"  strategy_id: {row[1][:40]}...")
    print(f"  source_type: {row[2]}")
    print(f"  proxy_only: {row[3]}")
    print(f"  raw_confidence: {row[4]}")
    print(f"  calibrated_confidence: {row[5]}")
    if row[6]:
        try:
            payload = json.loads(row[6])
            print(f"\n  payload 关键字段:")
            print(f"    build_mode: {payload.get('build_mode')}")
            print(f"    semantic_contract_status: {payload.get('semantic_contract_status')}")
            print(f"    semantic_contract_missing_fields: {payload.get('semantic_contract_missing_fields')}")
        except:
            print(f"  payload: (无法解析)")

conn.close()

print("\n" + "=" * 80)
print("诊断完成")
print("=" * 80)
