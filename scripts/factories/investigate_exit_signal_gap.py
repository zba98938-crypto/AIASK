#!/usr/bin/env python3
"""
Exit Signal 断链调查

问题：12,585 个 exit signal 只转成了 45 个 exit order（0.36% 转化率）
目标：找出为什么 exit signal 没有转成 exit order
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "packages" / "aiask-quant-core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "akshare-mcp" / "src"))

from aiask_quant_core.storage.sqlite import get_db


async def investigate_exit_signal_gap():
    """调查 exit signal 断链"""
    print("=" * 80)
    print("Exit Signal 断链调查")
    print("=" * 80)

    db = get_db()
    await db.initialize()

    # 1. 统计 exit signal
    cursor = db.connection.execute(
        """
        SELECT COUNT(*)
        FROM strategy_signals
        WHERE signal = -1
        """
    )
    exit_signal_count = cursor.fetchone()[0]
    print(f"\n[1] Exit Signal 数量: {exit_signal_count}")

    # 2. 统计有 exit signal 但无 exit order 的策略
    cursor = db.connection.execute(
        """
        SELECT COUNT(DISTINCT strategy_id)
        FROM strategy_signals
        WHERE signal = -1
          AND strategy_id NOT IN (
              SELECT DISTINCT strategy_id
              FROM paper_orders
              WHERE direction IN ('sell', 'exit', 'short')
          )
        """
    )
    strategies_with_signal_no_order = cursor.fetchone()[0]
    print(f"[2] 有 exit signal 但无 exit order 的策略: {strategies_with_signal_no_order}")

    # 3. 查看示例策略
    cursor = db.connection.execute(
        """
        SELECT
            ss.strategy_id,
            s.status,
            COUNT(DISTINCT ss.id) as signal_count,
            COUNT(DISTINCT po.id) as order_count
        FROM strategy_signals ss
        LEFT JOIN strategies s ON s.id = ss.strategy_id
        LEFT JOIN paper_orders po ON po.strategy_id = ss.strategy_id AND po.direction IN ('sell', 'exit', 'short')
        WHERE ss.signal = -1
        GROUP BY ss.strategy_id, s.status
        HAVING COUNT(DISTINCT po.id) = 0
        LIMIT 10
        """
    )

    print(f"\n[3] 示例策略（有 exit signal 但无 exit order）:")
    examples = cursor.fetchall()
    for row in examples[:5]:
        strategy_id = row[0]
        status = row[1]
        signal_count = row[2]
        print(f"  - {strategy_id[:8]}...: status={status}, exit_signals={signal_count}")

    # 4. 检查这些策略是否有 open position
    strategies_with_open = 0
    if examples:
        strategy_ids = [row[0] for row in examples[:10]]
        placeholders = ",".join("?" * len(strategy_ids))

        cursor = db.connection.execute(
            f"""
            SELECT
                strategy_id,
                COUNT(*) as position_count,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_count
            FROM strategy_trade_positions
            WHERE strategy_id IN ({placeholders})
            GROUP BY strategy_id
            """,
            strategy_ids,
        )

        print(f"\n[4] 这些策略的持仓情况:")
        position_map = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        for sid in strategy_ids[:5]:
            if sid in position_map:
                total, open_count = position_map[sid]
                print(f"  - {sid[:8]}...: {open_count}/{total} open positions")
                if open_count > 0:
                    strategies_with_open += 1
            else:
                print(f"  - {sid[:8]}...: 无持仓")

        print(f"\n  有 open position 的策略: {strategies_with_open}/{len(strategy_ids[:5])}")

    # 5. 检查 SignalTracker 是否在执行宇宙中
    cursor = db.connection.execute(
        """
        SELECT COUNT(DISTINCT s.id)
        FROM strategies s
        LEFT JOIN strategy_incubation_accounts sia ON sia.strategy_id = s.id
        WHERE s.status IN ('incubating', 'listed')
          AND (sia.status = 'active' OR sia.status IS NULL)
        """
    )
    execution_universe_count = cursor.fetchone()[0]
    print(f"\n[5] SignalTracker 执行宇宙大小: {execution_universe_count}")

    # 6. 检查有 exit signal 的策略是否在执行宇宙中
    cursor = db.connection.execute(
        """
        SELECT COUNT(DISTINCT ss.strategy_id)
        FROM strategy_signals ss
        JOIN strategies s ON s.id = ss.strategy_id
        LEFT JOIN strategy_incubation_accounts sia ON sia.strategy_id = ss.strategy_id
        WHERE ss.signal = -1
          AND s.status IN ('incubating', 'listed')
          AND (sia.status = 'active' OR sia.status IS NULL)
        """
    )
    exit_signal_in_universe = cursor.fetchone()[0]
    print(
        f"[6] 有 exit signal 且在执行宇宙中的策略: {exit_signal_in_universe}"
    )

    # 7. 可能的原因分析
    print("\n" + "=" * 80)
    print("根因分析")
    print("=" * 80)

    reasons = []

    # 原因 1: 策略不在执行宇宙中
    if strategies_with_signal_no_order > exit_signal_in_universe:
        not_in_universe = strategies_with_signal_no_order - exit_signal_in_universe
        reasons.append(
            f"策略不在执行宇宙中: {not_in_universe} 个策略（status 不是 incubating/listed）"
        )

    # 原因 2: 没有 open position 可以平仓
    if strategies_with_open == 0:
        reasons.append(
            "示例策略都没有 open position（exit signal 无持仓可平）"
        )

    # 原因 3: SignalTracker 未处理 exit signal
    reasons.append(
        "SignalTracker 可能只处理 entry signal，不处理 exit signal"
    )

    # 原因 4: exit order 逻辑可能在别处
    reasons.append(
        "exit order 可能不由 SignalTracker 生成，而是由 stale close policy 或其他组件生成"
    )

    print("\n可能的根因:")
    for i, reason in enumerate(reasons, 1):
        print(f"{i}. {reason}")

    print("\n建议:")
    print("1. 检查 SignalTracker 代码，确认是否处理 exit signal")
    print("2. 检查 Incubation Factory 是否有 exit order 生成逻辑")
    print("3. 启用 stale_paper_position_closure（强制平仓老旧持仓）")
    print("4. 确认策略是否在执行宇宙中且有 open position")

    return True


if __name__ == "__main__":
    success = asyncio.run(investigate_exit_signal_gap())
    sys.exit(0 if success else 1)
