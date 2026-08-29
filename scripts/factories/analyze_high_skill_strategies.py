#!/usr/bin/env python3
"""
查看高 skill 策略详细信息
"""
import sqlite3
from pathlib import Path

from _paths import DB_PATH

def main():
    conn = sqlite3.connect(str(DB_PATH))

    print("="*80)
    print("高 Skill 策略详细分析")
    print("="*80)

    # 获取高 skill 策略
    cursor = conn.execute("""
        SELECT
            s.id,
            s.name,
            s.status,
            s.created_at,
            COUNT(DISTINCT sig.id) as signal_count,
            AVG(CASE WHEN sfr.actual_return > 0 THEN 1.0 ELSE 0.0 END) as hit_rate,
            AVG(sfr.actual_return) as avg_return,
            MIN(sig.signal_date) as first_signal,
            MAX(sig.signal_date) as last_signal
        FROM strategies s
        JOIN strategy_signals sig ON s.id = sig.strategy_id
        JOIN signal_forward_returns sfr ON sig.id = sfr.signal_id
        WHERE sfr.forward_days = 5
        GROUP BY s.id
        HAVING signal_count >= 3 AND hit_rate >= 0.55
        ORDER BY hit_rate DESC, signal_count DESC
        LIMIT 20
    """)

    results = cursor.fetchall()

    print(f"\n找到 {len(results)} 个高 skill 策略（前 20 个）\n")
    print("-" * 80)

    for i, row in enumerate(results, 1):
        strategy_id, name, status, created_at, signal_count, hit_rate, avg_return, first_signal, last_signal = row

        print(f"\n{i}. 策略 ID: {strategy_id[:30]}...")
        print(f"   名称: {name or '(无名称)'}")
        print(f"   状态: {status}")
        print(f"   创建时间: {created_at}")
        print(f"   信号数: {signal_count}")
        print(f"   命中率: {hit_rate:.1%}")
        print(f"   平均收益: {avg_return:.2%}")
        print(f"   信号时间: {first_signal} ~ {last_signal}")

    # 统计 status 分布
    print("\n" + "="*80)
    print("高 Skill 策略的状态分布")
    print("="*80)

    cursor = conn.execute("""
        SELECT
            s.status,
            COUNT(*) as cnt
        FROM strategies s
        JOIN strategy_signals sig ON s.id = sig.strategy_id
        JOIN signal_forward_returns sfr ON sig.id = sfr.signal_id
        WHERE sfr.forward_days = 5
        GROUP BY s.id
        HAVING COUNT(DISTINCT sig.id) >= 3
            AND AVG(CASE WHEN sfr.actual_return > 0 THEN 1.0 ELSE 0.0 END) >= 0.55
    """)

    # 这个查询需要临时表，让我们用不同方法
    cursor = conn.execute("""
        WITH high_skill AS (
            SELECT s.id, s.status
            FROM strategies s
            JOIN strategy_signals sig ON s.id = sig.strategy_id
            JOIN signal_forward_returns sfr ON sig.id = sfr.signal_id
            WHERE sfr.forward_days = 5
            GROUP BY s.id
            HAVING COUNT(DISTINCT sig.id) >= 3
                AND AVG(CASE WHEN sfr.actual_return > 0 THEN 1.0 ELSE 0.0 END) >= 0.55
        )
        SELECT status, COUNT(*) as cnt
        FROM high_skill
        GROUP BY status
        ORDER BY cnt DESC
    """)

    print("\n状态分布:")
    for row in cursor:
        status, cnt = row
        print(f"  {status:20s}: {cnt:4d}")

    print("\n" + "="*80)
    print("分析完成")
    print("="*80)

    conn.close()

if __name__ == '__main__':
    main()
