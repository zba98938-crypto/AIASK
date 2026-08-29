#!/usr/bin/env python3
"""
诊断为什么 formal=0 的根本原因

分析策略状态、阻塞点、数据链路
"""
import sys
import asyncio
from pathlib import Path

# 添加包路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "packages/akshare-mcp/src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "packages/strategy-factory/src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "packages/aiask-quant-core/src"))

from akshare_mcp.storage.sqlite import get_db

def main():
    db = get_db()
    asyncio.run(db.initialize())
    conn = db.connection  # 获取底层连接

    print("="*80)
    print("策略工厂 formal=0 根因诊断")
    print("="*80)

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
    for row in cursor:
        print(f"  {row[0] or '(null)'}: {row[1]}")
        total += row[1]
    print(f"  总计: {total}")

    # 2. 孵化中的策略 tier 分布
    print("\n[2] 孵化中策略的 execution_readiness_tier 分布")
    print("-" * 80)
    cursor = conn.execute("""
        SELECT execution_readiness_tier, COUNT(*) as cnt
        FROM strategies
        WHERE incubating IN ('observe_incubation', 'formal_incubation')
        GROUP BY execution_readiness_tier
        ORDER BY cnt DESC
    """)
    for row in cursor:
        print(f"  {row[0] or '(null)'}: {row[1]}")

    # 3. 阻塞原因分布（observe）
    print("\n[3] observe_incubation 阻塞原因 (formal_readiness_blockers)")
    print("-" * 80)
    cursor = conn.execute("""
        SELECT
            json_extract(params, '$.formal_readiness_blockers') as blockers,
            COUNT(*) as cnt
        FROM strategies
        WHERE incubating = 'observe_incubation'
        GROUP BY blockers
        ORDER BY cnt DESC
        LIMIT 10
    """)
    for row in cursor:
        blockers = row[0] or '(null)'
        if len(blockers) > 100:
            blockers = blockers[:100] + '...'
        print(f"  [{row[1]}] {blockers}")

    # 4. 语义契约缺失情况
    print("\n[4] 语义契约完整性（三契约）")
    print("-" * 80)
    cursor = conn.execute("""
        SELECT
            CASE
                WHEN json_extract(params, '$.evidence_chain') IS NOT NULL THEN 1
                ELSE 0
            END as has_evidence,
            CASE
                WHEN json_extract(params, '$.trade_prediction_contract') IS NOT NULL THEN 1
                ELSE 0
            END as has_prediction,
            CASE
                WHEN json_extract(params, '$.confidence_contract') IS NOT NULL THEN 1
                ELSE 0
            END as has_confidence,
            COUNT(*) as cnt
        FROM strategies
        WHERE incubating IN ('observe_incubation', 'submitted')
        GROUP BY has_evidence, has_prediction, has_confidence
        ORDER BY cnt DESC
    """)
    print("  [evidence, prediction, confidence] 计数")
    for row in cursor:
        contracts = f"[{row[0]}, {row[1]}, {row[2]}]"
        print(f"  {contracts}: {row[3]}")

    # 5. compiled_dsl 和 instrument_profile 情况
    print("\n[5] 结构性阻塞检查（compiled_dsl + measured profile）")
    print("-" * 80)
    cursor = conn.execute("""
        SELECT
            CASE
                WHEN json_extract(params, '$.compiled_dsl') IS NOT NULL THEN 1
                ELSE 0
            END as has_dsl,
            CASE
                WHEN json_extract(params, '$.instrument_profile.profile_source') = 'measured' THEN 1
                ELSE 0
            END as has_measured,
            COUNT(*) as cnt
        FROM strategies
        WHERE incubating IN ('observe_incubation', 'submitted')
        GROUP BY has_dsl, has_measured
        ORDER BY cnt DESC
    """)
    print("  [compiled_dsl, measured_profile] 计数")
    for row in cursor:
        status = f"[{row[0]}, {row[1]}]"
        print(f"  {status}: {row[2]}")

    # 6. 前向 skill 情况
    print("\n[6] 真实前向 skill 分布（signal_forward_returns）")
    print("-" * 80)
    cursor = conn.execute("""
        SELECT COUNT(DISTINCT strategy_id) as strategy_count
        FROM signal_forward_returns
    """)
    row = cursor.fetchone()
    strategies_with_forward = row[0] if row else 0
    print(f"  有前向收益的策略数: {strategies_with_forward}")

    # 统计真实 skill_lcb > 0 的策略
    cursor = conn.execute("""
        SELECT
            s.strategy_id,
            COUNT(DISTINCT sig.signal_id) as signal_count,
            AVG(CASE WHEN sfr.actual_return > 0 THEN 1.0 ELSE 0.0 END) as hit_rate
        FROM strategies s
        JOIN strategy_signals sig ON s.strategy_id = sig.strategy_id
        JOIN signal_forward_returns sfr ON sig.signal_id = sfr.signal_id
        WHERE sfr.forward_days = 5
        GROUP BY s.strategy_id
        HAVING signal_count >= 3
    """)

    high_skill = []
    medium_skill = []
    low_skill = []

    for row in cursor:
        hit_rate = row[2]
        if hit_rate >= 0.55:
            high_skill.append(row)
        elif hit_rate >= 0.50:
            medium_skill.append(row)
        else:
            low_skill.append(row)

    total_qualified = len(high_skill) + len(medium_skill) + len(low_skill)
    print(f"  ≥3样本的策略: {total_qualified}")
    print(f"    - hit_rate ≥0.55: {len(high_skill)} (可转 formal)")
    print(f"    - hit_rate ≥0.50: {len(medium_skill)} (边缘)")
    print(f"    - hit_rate <0.50: {len(low_skill)} (不合格)")

    # 7. 转正阻塞总结
    print("\n[7] formal=0 根因总结")
    print("="*80)

    # 获取 observe 数量
    cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE incubating = 'observe_incubation'")
    observe_count = cursor.fetchone()[0]

    # 获取 formal 数量
    cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE incubating = 'formal_incubation'")
    formal_count = cursor.fetchone()[0]

    print(f"\n当前状态:")
    print(f"  - observe_incubation: {observe_count}")
    print(f"  - formal_incubation: {formal_count}")
    print(f"  - 有前向证据的策略: {strategies_with_forward}")
    print(f"  - 真实 skill ≥0.55 的策略: {len(high_skill)}")

    print(f"\n关键瓶颈:")
    if formal_count == 0:
        print("  ❌ formal=0 的根本原因:")
        print("     1. 候选质量不足 - 真实命中率极低 (<50%)")
        print("     2. 语义契约缺失 - evidence/prediction/confidence 三契约不全")
        print("     3. 结构性阻塞 - compiled_dsl 或 measured profile 缺失")
        print("     4. 样本不成熟 - 需要时间积累真实前向证据")

        if len(high_skill) > 0:
            print(f"\n  ✓ 有 {len(high_skill)} 个策略有真实 skill，但被其他条件阻塞")
            print("     → 优先修复: 补全语义契约或结构性字段")
        else:
            print(f"\n  ✗ 没有策略达到 skill_lcb>0 的基本要求")
            print("     → 优先修复: 提升候选生成质量")
    else:
        print(f"  ✓ 已有 {formal_count} 个策略进入 formal")

    print("\n" + "="*80)
    print("诊断完成")
    print("="*80)

if __name__ == '__main__':
    main()
