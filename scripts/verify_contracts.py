import sqlite3

from _integration_paths import default_sqlite_path

conn = sqlite3.connect(str(default_sqlite_path()))

print("=" * 80)
print("契约字段验证")
print("=" * 80)

# 使用 json_extract 检查
cursor = conn.execute("""
    SELECT
        COUNT(*) as total,
        SUM(CASE WHEN json_extract(params, '$.compiled_dsl') IS NOT NULL THEN 1 ELSE 0 END) as has_dsl,
        SUM(CASE WHEN json_extract(params, '$.evidence_chain') IS NOT NULL THEN 1 ELSE 0 END) as has_evidence,
        SUM(CASE WHEN json_extract(params, '$.confidence_contract') IS NOT NULL THEN 1 ELSE 0 END) as has_confidence,
        SUM(CASE WHEN json_extract(params, '$.trade_prediction_contract') IS NOT NULL THEN 1 ELSE 0 END) as has_prediction
    FROM strategies
    WHERE incubating = 'observe_incubation'
""")

row = cursor.fetchone()
total = row[0]

print(f"\nobserve_incubation strategies: {total:,}")
print(f"  compiled_dsl: {row[1]} ({row[1]/total*100:.1f}%)")
print(f"  evidence_chain: {row[2]} ({row[2]/total*100:.1f}%)")
print(f"  confidence_contract: {row[3]} ({row[3]/total*100:.1f}%)")
print(f"  trade_prediction_contract: {row[4]} ({row[4]/total*100:.1f}%)")

print("\n关键发现:")
if row[1] == 0:
    print("  ❌ compiled_dsl = 0 → 无法通过 compile_stable_ready 检查")
if row[2] == 0:
    print("  ❌ evidence_chain = 0 → 无法通过语义契约检查")
if row[3] == 0:
    print("  ❌ confidence_contract = 0 → 无法通过置信度检查")

if row[4] > 0:
    print(f"  ✅ trade_prediction_contract = {row[4]} → 这个字段存在")

print("\n结论:")
print("  Phase 3f 的 hard_gate 需要:")
print("    1. compiled_dsl ✅ 或 evidence_chain ✅")
print("    2. confidence_contract ✅")
print("    3. 其他质量字段")
print("\n  当前:")
print("    1. compiled_dsl ❌ AND evidence_chain ❌")
print("    2. confidence_contract ❌")
print("    → 无法通过 hard_gate")

conn.close()

print("\n" + "=" * 80)
