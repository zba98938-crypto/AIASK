#!/usr/bin/env python3
"""
诊断为什么高 skill 策略卡在 submitted 状态
"""
import sqlite3
from pathlib import Path

from _paths import DB_PATH

def main():
    conn = sqlite3.connect(str(DB_PATH))

    print("="*80)
    print("Submitted 队列阻塞诊断")
    print("="*80)

    # 1. 总体 submitted 策略
    cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE status = 'submitted'")
    total_submitted = cursor.fetchone()[0]
    print(f"\n总 submitted 策略: {total_submitted}")

    # 2. 有真实 skill 的 submitted 策略
    cursor = conn.execute("""
        SELECT COUNT(DISTINCT s.id)
        FROM strategies s
        JOIN strategy_signals sig ON s.id = sig.strategy_id
        JOIN signal_forward_returns sfr ON sig.id = sfr.signal_id
        WHERE s.status = 'submitted'
            AND sfr.forward_days = 5
        GROUP BY s.id
        HAVING COUNT(DISTINCT sig.id) >= 3
            AND AVG(CASE WHEN sfr.actual_return > 0 THEN 1.0 ELSE 0.0 END) >= 0.55
    """)

    # 上面的查询不对，让我用子查询
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
        WHERE s.status = 'submitted'
    """)
    high_skill_submitted = cursor.fetchone()[0]

    print(f"有高 skill 的 submitted 策略: {high_skill_submitted}")
    print(f"占比: {high_skill_submitted / total_submitted * 100:.1f}%")

    # 3. 检查是否有阻塞字段
    print("\n" + "-"*80)
    print("检查 submitted 策略的参数")
    print("-"*80)

    cursor = conn.execute("""
        SELECT
            s.id,
            s.created_at,
            LENGTH(s.params) as params_size
        FROM strategies s
        WHERE s.status = 'submitted'
        ORDER BY s.created_at DESC
        LIMIT 5
    """)

    print("\n最新的 5 个 submitted 策略:")
    for row in cursor:
        strategy_id, created_at, params_size = row
        print(f"  {strategy_id[:30]}... 创建:{created_at} params:{params_size}字节")

    # 4. 对比 incubating 策略
    print("\n" + "-"*80)
    print("对比 incubating 策略")
    print("-"*80)

    cursor = conn.execute("""
        SELECT COUNT(*) FROM strategies WHERE status = 'incubating'
    """)
    total_incubating = cursor.fetchone()[0]

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
        WHERE s.status = 'incubating'
    """)
    high_skill_incubating = cursor.fetchone()[0]

    print(f"\n总 incubating 策略: {total_incubating}")
    print(f"有高 skill 的 incubating 策略: {high_skill_incubating}")
    print(f"占比: {high_skill_incubating / total_incubating * 100:.1f}%")

    # 5. 结论
    print("\n" + "="*80)
    print("诊断结论")
    print("="*80)

    print(f"\n关键发现:")
    print(f"  1. submitted 状态下有 {high_skill_submitted} 个高 skill 策略")
    print(f"  2. incubating 状态下有 {high_skill_incubating} 个高 skill 策略")
    print(f"  3. submitted → incubating 的转换可能未触发")

    print(f"\n可能原因:")
    print(f"  - Incubation Factory 未处理 submitted 队列")
    print(f"  - Quality Gate 阻塞")
    print(f"  - 需要手动触发孵化流程")

    print(f"\n建议:")
    print(f"  Schema 迁移应该将 submitted 映射到 observe_incubation")
    print(f"  这样 {high_skill_submitted} 个高质量策略也能进入转正流程")

    print("\n" + "="*80)

    conn.close()

if __name__ == '__main__':
    main()
