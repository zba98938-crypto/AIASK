#!/usr/bin/env python3
"""
策略工厂完整监控仪表板

监控候选质量、转正进度、关键指标
"""
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timedelta

from _paths import DB_PATH

def get_strategy_stats(conn):
    """策略状态统计"""
    cursor = conn.execute("""
        SELECT status, COUNT(*) as cnt
        FROM strategies
        GROUP BY status
        ORDER BY cnt DESC
    """)

    stats = {}
    total = 0
    for row in cursor:
        status, cnt = row
        stats[status] = cnt
        total += cnt

    return stats, total

def get_signal_stats(conn):
    """信号和前向收益统计"""
    # 总信号数
    cursor = conn.execute("SELECT COUNT(*) FROM strategy_signals")
    total_signals = cursor.fetchone()[0]

    # 有信号的策略数
    cursor = conn.execute("SELECT COUNT(DISTINCT strategy_id) FROM strategy_signals")
    strategies_with_signals = cursor.fetchone()[0]

    # 前向收益记录数
    cursor = conn.execute("SELECT COUNT(*) FROM signal_forward_returns")
    total_forward = cursor.fetchone()[0]

    # 有前向收益的策略数（通过 JOIN）
    cursor = conn.execute("""
        SELECT COUNT(DISTINCT sig.strategy_id)
        FROM strategy_signals sig
        JOIN signal_forward_returns sfr ON sig.id = sfr.signal_id
    """)
    strategies_with_forward = cursor.fetchone()[0]

    return {
        'total_signals': total_signals,
        'strategies_with_signals': strategies_with_signals,
        'total_forward': total_forward,
        'strategies_with_forward': strategies_with_forward,
    }

def get_skill_distribution(conn):
    """真实 skill 分布"""
    cursor = conn.execute("""
        SELECT
            s.id as strategy_id,
            COUNT(DISTINCT sig.id) as signal_count,
            AVG(CASE WHEN sfr.actual_return > 0 THEN 1.0 ELSE 0.0 END) as hit_rate,
            AVG(sfr.actual_return) as avg_return
        FROM strategies s
        JOIN strategy_signals sig ON s.id = sig.strategy_id
        JOIN signal_forward_returns sfr ON sig.id = sfr.signal_id
        WHERE sfr.forward_days = 5
        GROUP BY s.id
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

    return {
        'high_skill': high_skill,
        'medium_skill': medium_skill,
        'low_skill': low_skill,
        'total_qualified': len(high_skill) + len(medium_skill) + len(low_skill),
    }

def get_paper_trading_stats(conn):
    """纸面交易统计"""
    # 账户数
    cursor = conn.execute("SELECT COUNT(*) FROM paper_accounts")
    total_accounts = cursor.fetchone()[0]

    # 订单数
    cursor = conn.execute("SELECT COUNT(*) FROM paper_orders")
    total_orders = cursor.fetchone()[0]

    # 成交数
    cursor = conn.execute("SELECT COUNT(*) FROM paper_trades")
    total_trades = cursor.fetchone()[0]

    # 持仓数
    cursor = conn.execute("SELECT COUNT(*) FROM paper_positions WHERE quantity > 0")
    open_positions = cursor.fetchone()[0]

    return {
        'total_accounts': total_accounts,
        'total_orders': total_orders,
        'total_trades': total_trades,
        'open_positions': open_positions,
    }

def get_recent_activity(conn, days=7):
    """最近活动统计"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    # 最近新增策略
    cursor = conn.execute("""
        SELECT COUNT(*)
        FROM strategies
        WHERE created_at >= ?
    """, (cutoff,))
    new_strategies = cursor.fetchone()[0]

    # 最近新增信号
    cursor = conn.execute("""
        SELECT COUNT(*)
        FROM strategy_signals
        WHERE signal_date >= ?
    """, (cutoff,))
    new_signals = cursor.fetchone()[0]

    return {
        'days': days,
        'new_strategies': new_strategies,
        'new_signals': new_signals,
    }

def main():
    """主流程"""
    print("="*80)
    print("策略工厂监控仪表板")
    print("="*80)
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not DB_PATH.exists():
        print(f"\n[ERROR] 数据库不存在: {DB_PATH}")
        return 1

    conn = sqlite3.connect(str(DB_PATH))

    try:
        # 1. 策略状态
        print("\n[1] 策略状态分布")
        print("-" * 80)
        stats, total = get_strategy_stats(conn)

        print(f"  总策略数: {total}")
        print(f"\n  状态分布:")
        for status, cnt in sorted(stats.items(), key=lambda x: x[1], reverse=True):
            pct = cnt / total * 100 if total > 0 else 0
            print(f"    {status:20s}: {cnt:6d} ({pct:5.1f}%)")

        # 关键指标
        incubating = stats.get('incubating', 0)
        listed = stats.get('listed', 0)
        print(f"\n  关键指标:")
        print(f"    孵化中 (incubating): {incubating}")
        print(f"    已上线 (listed):     {listed}")

        # 2. 信号统计
        print("\n[2] 信号与前向收益")
        print("-" * 80)
        signal_stats = get_signal_stats(conn)

        print(f"  总信号数: {signal_stats['total_signals']}")
        print(f"  有信号的策略: {signal_stats['strategies_with_signals']}")
        print(f"  前向收益记录: {signal_stats['total_forward']}")
        print(f"  有前向证据的策略: {signal_stats['strategies_with_forward']}")

        # 3. Skill 分布
        print("\n[3] 真实 skill 分析（5日前向，≥3样本）")
        print("-" * 80)
        skill = get_skill_distribution(conn)

        print(f"  合格策略数 (≥3样本): {skill['total_qualified']}")
        print(f"    高 skill (≥0.55):   {len(skill['high_skill'])} [可转 formal]")
        print(f"    中 skill (0.50-0.55): {len(skill['medium_skill'])} [边缘]")
        print(f"    低 skill (<0.50):    {len(skill['low_skill'])} [不合格]")

        if skill['high_skill']:
            print(f"\n  前 3 个高 skill 策略:")
            for i, (sid, cnt, hr, ar) in enumerate(skill['high_skill'][:3], 1):
                print(f"    {i}. {sid[:20]}... 样本={cnt} 命中率={hr:.1%} 收益={ar:.2%}")

        # 4. 纸面交易
        print("\n[4] 纸面交易统计")
        print("-" * 80)
        paper = get_paper_trading_stats(conn)

        print(f"  纸面账户: {paper['total_accounts']}")
        print(f"  纸面订单: {paper['total_orders']}")
        print(f"  纸面成交: {paper['total_trades']}")
        print(f"  当前持仓: {paper['open_positions']}")

        # 5. 最近活动
        print("\n[5] 最近 7 天活动")
        print("-" * 80)
        activity = get_recent_activity(conn, days=7)

        print(f"  新增策略: {activity['new_strategies']}")
        print(f"  新增信号: {activity['new_signals']}")

        # 6. 健康状态判断
        print("\n[6] 系统健康状态")
        print("-" * 80)

        health = "HEALTHY"
        issues = []

        # 检查是否有 formal
        formal_count = 0  # 旧 schema 没有 formal_incubation
        if formal_count == 0 and skill['total_qualified'] > 0:
            if len(skill['high_skill']) > 0:
                health = "PENDING_EVIDENCE"
                issues.append(f"有 {len(skill['high_skill'])} 个高 skill 策略但未转 formal")
            else:
                health = "DEGRADED"
                issues.append("候选质量不足，没有高 skill 策略")

        # 检查信号生成
        if signal_stats['total_signals'] == 0:
            health = "BLOCKED"
            issues.append("信号生成为 0")

        print(f"  状态: {health}")
        if issues:
            print(f"\n  问题:")
            for issue in issues:
                print(f"    - {issue}")

        print("\n" + "="*80)
        print("监控完成")
        print("="*80)

        # 建议
        print("\n下一步建议:")
        if health == "BLOCKED":
            print("  1. 检查 Signal Tracker 是否运行")
            print("  2. 检查数据源连接")
        elif health == "DEGRADED":
            print("  1. 继续运行 Quality Session 积累样本")
            print("  2. 检查候选生成质量（方向门/LLM/IC排序）")
        elif health == "PENDING_EVIDENCE":
            print("  1. 检查 Schema 是否已迁移（status → incubating）")
            print("  2. 检查语义契约是否启用")
            print("  3. 等待样本成熟（2-4周）")
        else:
            print("  系统运行正常，继续观察")

        return 0

    except Exception as e:
        print(f"\n[ERROR] 监控失败: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        conn.close()

if __name__ == '__main__':
    sys.exit(main())
