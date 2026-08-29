#!/usr/bin/env python3
"""
诊断 Signal Evidence 保存状态
"""
import sqlite3
from pathlib import Path

from _paths import DB_PATH

def main():
    conn = sqlite3.connect(str(DB_PATH))

    print("=" * 80)
    print("Signal Evidence 诊断")
    print("=" * 80)

    # 1. 策略总数
    cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE incubating = 'observe_incubation'")
    observe_count = cursor.fetchone()[0]
    print(f"\n[1] observe_incubation 策略数: {observe_count}")

    # 2. 有交易的策略
    cursor = conn.execute("""
        SELECT COUNT(DISTINCT strategy_id)
        FROM paper_trades
        WHERE strategy_id IN (
            SELECT strategy_id FROM strategies WHERE incubating = 'observe_incubation'
        )
    """)
    strategies_with_trades = cursor.fetchone()[0]
    print(f"\n[2] 有 paper_trades 的策略: {strategies_with_trades}")

    # 3. 有 signal evidence 的策略
    cursor = conn.execute("""
        SELECT COUNT(DISTINCT strategy_id)
        FROM strategy_signals
        WHERE strategy_id IN (
            SELECT strategy_id FROM strategies WHERE incubating = 'observe_incubation'
        )
    """)
    strategies_with_signals = cursor.fetchone()[0]
    print(f"\n[3] 有 strategy_signals 的策略: {strategies_with_signals}")

    # 4. Gap 计算
    gap = strategies_with_trades - strategies_with_signals
    print(f"\n[4] Signal Evidence Gap: {gap}")
    print(f"    (有交易但无信号记录)")

    # 5. 检查 backfill 候选
    print(f"\n[5] 为什么 Phase 3e 没有回填?")
    if gap == 0:
        print("    ✓ 没有 gap,不需要回填")
    elif gap > 0:
        print(f"    ✗ 有 {gap} 个策略需要回填")
        print("    可能原因:")
        print("      - backfill 方法调用失败")
        print("      - 数据库方法不存在")
        print("      - Trade → Signal 映射逻辑有问题")

    # 6. 检查方法是否存在
    print(f"\n[6] 检查数据库方法")
    try:
        # 检查是否有 backfill_strategy_signal_evidence_native 方法的实现
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor]
        print(f"    数据库表数: {len(tables)}")
        print(f"    strategy_signals 表: {'✓' if 'strategy_signals' in tables else '✗'}")
        print(f"    paper_trades 表: {'✓' if 'paper_trades' in tables else '✗'}")
    except Exception as e:
        print(f"    ✗ 错误: {e}")

    # 7. 示例查询
    if gap > 0:
        print(f"\n[7] 前 5 个需要回填的策略")
        cursor = conn.execute("""
            SELECT DISTINCT pt.strategy_id
            FROM paper_trades pt
            WHERE pt.strategy_id IN (
                SELECT strategy_id FROM strategies WHERE incubating = 'observe_incubation'
            )
            AND pt.strategy_id NOT IN (
                SELECT DISTINCT strategy_id FROM strategy_signals
            )
            LIMIT 5
        """)
        for i, row in enumerate(cursor, 1):
            print(f"    {i}. {row[0][:16]}...")

    conn.close()

    print("\n" + "=" * 80)
    print("诊断完成")
    print("=" * 80)

if __name__ == '__main__':
    main()
