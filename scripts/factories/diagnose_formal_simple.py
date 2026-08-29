#!/usr/bin/env python3
"""
诊断为什么 formal=0 的根本原因 - 直接 SQLite 版本
"""
import sqlite3
from pathlib import Path

from _paths import DB_PATH

def main():
    conn = sqlite3.connect(str(DB_PATH))

    print("="*80)
    print("策略工厂 formal=0 根因诊断")
    print("="*80)

    # 首先检查表结构
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='strategies'")
    if not cursor.fetchone():
        print("\n[X] 错误: strategies 表不存在")
        return

    # 检查列
    cursor = conn.execute("PRAGMA table_info(strategies)")
    cols = {row[1] for row in cursor}

    if 'incubating' not in cols:
        print(f"\n[X] 错误: strategies 表没有 incubating 列")
        print(f"   实际列: {', '.join(sorted(cols))}")

        # 检查是否是旧版 status 列
        if 'status' in cols:
            print("\n尝试使用 status 列分析...")
            cursor = conn.execute("""
                SELECT status, COUNT(*) as cnt
                FROM strategies
                GROUP BY status
                ORDER BY cnt DESC
            """)
            print("\n策略 status 分布:")
            for row in cursor:
                print(f"  {row[0]}: {row[1]}")
        return

    # 1. 总体状态分布
    print("\n[1] 策略状态分布")
    print("-" * 80)
    cursor = conn.execute("""
        SELECT incubating, COUNT(*) as cnt
        FROM strategies
        GROUP BY incubating
        ORDER BY cnt DESC
    """)
    total = 0
    observe_count = 0
    formal_count = 0

    for row in cursor:
        incubating_status = row[0] or '(null)'
        count = row[1]
        print(f"  {incubating_status}: {count}")
        total += count

        if incubating_status == 'observe_incubation':
            observe_count = count
        elif incubating_status == 'formal_incubation':
            formal_count = count

    print(f"  总计: {total}")

    # 2. 前向收益统计
    print("\n[2] 前向收益数据")
    print("-" * 80)

    cursor = conn.execute("SELECT COUNT(*) FROM signal_forward_returns")
    forward_count = cursor.fetchone()[0]
    print(f"  signal_forward_returns 记录数: {forward_count}")

    cursor = conn.execute("SELECT COUNT(DISTINCT strategy_id) FROM signal_forward_returns")
    strategies_with_forward = cursor.fetchone()[0]
    print(f"  有前向收益的策略数: {strategies_with_forward}")

    # 3. Skill 分析
    print("\n[3] 真实 skill 分析（≥3 样本）")
    print("-" * 80)

    cursor = conn.execute("""
        SELECT
            s.strategy_id,
            COUNT(DISTINCT sig.signal_id) as signal_count,
            AVG(CASE WHEN sfr.actual_return > 0 THEN 1.0 ELSE 0.0 END) as hit_rate,
            AVG(sfr.actual_return) as avg_return
        FROM strategies s
        JOIN strategy_signals sig ON s.strategy_id = sig.strategy_id
        JOIN signal_forward_returns sfr ON sig.signal_id = sfr.signal_id
        WHERE sfr.forward_days = 5
        GROUP BY s.strategy_id
        HAVING signal_count >= 3
        ORDER BY hit_rate DESC
    """)

    high_skill = []
    medium_skill = []
    low_skill = []

    for row in cursor:
        strategy_id, signal_count, hit_rate, avg_return = row
        if hit_rate >= 0.55:
            high_skill.append((strategy_id, signal_count, hit_rate, avg_return))
        elif hit_rate >= 0.50:
            medium_skill.append((strategy_id, signal_count, hit_rate, avg_return))
        else:
            low_skill.append((strategy_id, signal_count, hit_rate, avg_return))

    total_qualified = len(high_skill) + len(medium_skill) + len(low_skill)
    print(f"  ≥3样本的策略: {total_qualified}")
    print(f"    - hit_rate ≥0.55: {len(high_skill)} (可转 formal)")
    print(f"    - hit_rate ≥0.50: {len(medium_skill)} (边缘)")
    print(f"    - hit_rate <0.50: {len(low_skill)} (不合格)")

    if high_skill:
        print(f"\n  前 5 个高 skill 策略:")
        for i, (sid, cnt, hr, ar) in enumerate(high_skill[:5], 1):
            print(f"    {i}. {sid[:16]}... 样本={cnt} 命中率={hr:.2%} 平均收益={ar:.2%}")

    # 4. 总结
    print("\n[4] formal=0 根因总结")
    print("="*80)

    print(f"\n当前状态:")
    print(f"  - observe_incubation: {observe_count}")
    print(f"  - formal_incubation: {formal_count}")
    print(f"  - 有前向证据的策略: {strategies_with_forward}")
    print(f"  - 真实 skill ≥0.55 的策略: {len(high_skill)}")

    print(f"\n关键瓶颈:")
    if formal_count == 0:
        print("  [X] formal=0 的根本原因:")

        if len(high_skill) > 0:
            print(f"\n  部分好消息: 有 {len(high_skill)} 个策略达到了 skill 要求（≥0.55）")
            print("  → 这些策略被其他条件阻塞（契约、结构性字段等）")
            print("  → 建议: 检查这些策略的 formal_readiness_blockers")
        else:
            print("\n  核心问题: 候选质量不足")
            print(f"     - {total_qualified} 个有足够样本的策略中")
            print(f"     - {len(low_skill)} 个命中率 <50%")
            print(f"     - {len(medium_skill)} 个命中率 50-55%（临界）")
            print(f"     - 0 个命中率 ≥55%（转正标准）")
            print("\n  → 建议: 提升候选生成质量（LLM/因子池/方向匹配）")
    else:
        print(f"  [OK] 已有 {formal_count} 个策略进入 formal")

    print("\n" + "="*80)
    print("诊断完成")
    print("="*80)

    conn.close()

if __name__ == '__main__':
    main()
