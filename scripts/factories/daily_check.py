#!/usr/bin/env python3
"""
每日快速检查 - 一键查看关键指标
"""
import sqlite3
from pathlib import Path
from datetime import datetime

from _paths import DB_PATH

def main():
    conn = sqlite3.connect(str(DB_PATH))

    print("="*80)
    print(f"策略工厂每日快速检查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)

    # 1. 核心指标
    print("\n[核心指标]")
    print("-" * 80)

    # observe 数量
    cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE incubating = 'observe_incubation'")
    observe_count = cursor.fetchone()[0]

    # formal 数量
    cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE incubating = 'formal_incubation'")
    formal_count = cursor.fetchone()[0]

    # production 数量
    cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE incubating = 'production'")
    production_count = cursor.fetchone()[0]

    print(f"  observe_incubation:    {observe_count:6d}")
    if formal_count > 0:
        print(f"  formal_incubation:     {formal_count:6d}  [!] 开始转正")
    else:
        print(f"  formal_incubation:     {formal_count:6d}  [*] 等待转正")
    print(f"  production:            {production_count:6d}")

    # 2. 高 skill 策略
    print("\n[高 Skill 策略]")
    print("-" * 80)

    cursor = conn.execute("""
        WITH high_skill AS (
            SELECT s.id, s.incubating
            FROM strategies s
            JOIN strategy_signals sig ON s.id = sig.strategy_id
            JOIN signal_forward_returns sfr ON sig.id = sfr.signal_id
            WHERE sfr.forward_days = 5
            GROUP BY s.id
            HAVING COUNT(DISTINCT sig.id) >= 3
                AND AVG(CASE WHEN sfr.actual_return > 0 THEN 1.0 ELSE 0.0 END) >= 0.55
        )
        SELECT incubating, COUNT(*) as cnt
        FROM high_skill
        GROUP BY incubating
        ORDER BY cnt DESC
    """)

    high_skill_dist = {}
    for row in cursor:
        incubating, cnt = row
        high_skill_dist[incubating or '(NULL)'] = cnt
        print(f"  {incubating or '(NULL)':25s}: {cnt:4d}")

    total_high_skill = sum(high_skill_dist.values())
    print(f"  {'总计':25s}: {total_high_skill:4d}")

    # 3. 最近活动（24小时）
    print("\n[最近 24 小时]")
    print("-" * 80)

    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE created_at >= ?", (cutoff,))
    new_strategies = cursor.fetchone()[0]

    cursor = conn.execute("SELECT COUNT(*) FROM strategy_signals WHERE signal_date >= ?", (cutoff,))
    new_signals = cursor.fetchone()[0]

    print(f"  新增策略: {new_strategies}")
    print(f"  新增信号: {new_signals}")

    # 4. 健康状态
    print("\n[系统状态]")
    print("-" * 80)

    if formal_count > 0:
        status = "[OK] HEALTHY - formal 转正进行中"
    elif high_skill_dist.get('observe_incubation', 0) > 500:
        status = "[READY] - 等待 formal 转正"
    else:
        status = "[WARN] DEGRADED - 候选质量不足"

    print(f"  {status}")

    # 5. 下一步建议
    print("\n[建议]")
    print("-" * 80)

    if formal_count == 0:
        print("  - 继续运行 Quality Session")
        print("  - 等待样本成熟（2-4天）")
        print("  - 明天同一时间再检查")
    elif formal_count < 50:
        print("  - formal 转正已开始，继续观察")
        print("  - 目标：50+ formal 策略")
    elif formal_count >= 50 and production_count < 5:
        print("  - 准备启动 Promotion Factory")
        print("  - 目标：5+ production 策略")
    else:
        print("  - 系统运行正常")
        print("  - 准备接入 Execution Factory")

    print("\n" + "="*80)

    conn.close()

if __name__ == '__main__':
    main()
