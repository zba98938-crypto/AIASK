#!/usr/bin/env python3
"""
检查 paper_orders 表状态
"""
import sqlite3
from pathlib import Path

from _paths import DB_PATH

def main():
    conn = sqlite3.connect(str(DB_PATH))

    print("=" * 80)
    print("Paper Orders 诊断")
    print("=" * 80)

    # 1. 检查表是否存在
    cursor = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='paper_orders'
    """)
    if not cursor.fetchone():
        print("\n❌ paper_orders 表不存在！")
        conn.close()
        return

    print("\n✅ paper_orders 表存在")

    # 2. 总记录数
    cursor = conn.execute("SELECT COUNT(*) FROM paper_orders")
    total = cursor.fetchone()[0]
    print(f"\n[总记录数] {total:,}")

    if total == 0:
        print("❌ paper_orders 表为空！这是 Phase 3e 失败的原因。")
        conn.close()
        return

    # 3. 有 signal_id 的记录
    cursor = conn.execute("""
        SELECT COUNT(*) FROM paper_orders
        WHERE signal_id IS NOT NULL AND signal_id != ''
    """)
    with_signal = cursor.fetchone()[0]
    print(f"[有 signal_id] {with_signal:,} ({with_signal/total*100:.1f}%)")

    if with_signal == 0:
        print("❌ 所有 orders 都没有 signal_id！这是 Phase 3e 失败的原因。")

    # 4. 策略分布
    cursor = conn.execute("""
        SELECT COUNT(DISTINCT strategy_id) FROM paper_orders
        WHERE signal_id IS NOT NULL AND signal_id != ''
    """)
    strategies_with_signal = cursor.fetchone()[0]
    print(f"[有 signal_id 的策略数] {strategies_with_signal:,}")

    # 5. 示例数据
    print(f"\n[示例 paper_orders (前 3 条)]")
    cursor = conn.execute("""
        SELECT id, strategy_id, signal_id, stock_code, direction, status
        FROM paper_orders
        LIMIT 3
    """)
    for row in cursor:
        print(f"  {row[0][:20]}... | {row[1][:20]}... | signal: {row[2] or '(NULL)'} | {row[3]} | {row[4]} | {row[5]}")

    # 6. Phase 3e 的候选集大小
    print(f"\n[Phase 3e 理论候选集]")
    cursor = conn.execute("""
        SELECT COUNT(DISTINCT po.strategy_id)
        FROM paper_orders po
        WHERE po.signal_id IS NOT NULL
        AND po.signal_id != ''
        AND po.strategy_id IN (
            SELECT strategy_id FROM strategies
            WHERE incubating = 'observe_incubation'
        )
    """)
    candidates = cursor.fetchone()[0]
    print(f"  observe + 有 signal_id 的 orders: {candidates:,} 个策略")

    if candidates == 0:
        print("❌ 没有候选策略可以回填！")
        print("   原因：observe 策略的 orders 都没有 signal_id")
    else:
        print(f"✅ 理论上应该回填 {candidates} 个策略")

    conn.close()

    print("\n" + "=" * 80)
    print("诊断完成")
    print("=" * 80)

if __name__ == '__main__':
    main()
