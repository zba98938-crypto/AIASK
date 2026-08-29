#!/usr/bin/env python3
"""测试 StrategyLifecycleLedger 一致性

验证统一状态查询与当前多表拼接查询的结果是否一致。
"""

import sys
from pathlib import Path
import asyncio

# Windows 控制台编码修复
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "packages" / "aiask-quant-core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "akshare-mcp" / "src"))

from aiask_quant_core.strategy_lifecycle_ledger import StrategyLifecycleLedger, BusinessLifecycleStage
from aiask_quant_core.storage.sqlite import get_db


async def init_db():
    """初始化数据库连接"""
    db = get_db()
    await db.initialize()
    return db


def test_ledger_basic():
    """测试基本功能"""
    db = asyncio.run(init_db())
    # SQLiteAdapter.connection 是底层 sqlite3.Connection
    conn = db.connection
    if conn is None:
        print("❌ 数据库连接失败")
        return
    ledger = StrategyLifecycleLedger(conn)

    # 查询所有策略
    cursor = conn.execute("SELECT id FROM strategies LIMIT 10")
    strategy_ids = [row[0] for row in cursor.fetchall()]

    if not strategy_ids:
        print("⚠️  数据库中没有策略,跳过测试")
        return

    print(f"✅ 找到 {len(strategy_ids)} 个策略")

    # 单个查询测试
    for strategy_id in strategy_ids[:3]:
        try:
            snapshot = ledger.get_snapshot(strategy_id)
            print(f"\n策略 {strategy_id}:")
            print(f"  物理状态: {snapshot.physical_status}")
            print(f"  业务状态: {snapshot.business_stage.value}")
            print(f"  信号数: {snapshot.signal_count}")
            print(f"  订单数: {snapshot.order_count}")
            print(f"  成交数: {snapshot.trade_count}")
            print(f"  持仓数: open={snapshot.open_position_count}, closed={snapshot.closed_position_count}")
            print(f"  前向收益数: {snapshot.forward_return_count}")
            print(f"  审计快照数: {snapshot.audit_snapshot_count}")
            print(f"  Hard Gate: {'✅ PASSED' if snapshot.hard_gate_passed else '❌ NOT PASSED'}")
            if snapshot.blocker_reason:
                print(f"  ⚠️  Blocker: {snapshot.blocker_reason}")
        except Exception as e:
            print(f"❌ 查询策略 {strategy_id} 失败: {e}")

    # 批量查询测试
    print(f"\n批量查询 {len(strategy_ids)} 个策略...")
    snapshots = ledger.batch_get_snapshots(strategy_ids)
    print(f"✅ 批量查询成功,返回 {len(snapshots)} 个快照")

    # 统计业务状态分布
    stage_counts = {}
    for snapshot in snapshots.values():
        stage = snapshot.business_stage.value
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    print("\n业务状态分布:")
    for stage, count in sorted(stage_counts.items(), key=lambda x: -x[1]):
        print(f"  {stage}: {count}")


def test_ledger_consistency():
    """测试与现有查询的一致性"""
    db = asyncio.run(init_db())
    conn = db.connection
    if conn is None:
        print("❌ 数据库连接失败")
        return
    ledger = StrategyLifecycleLedger(conn)

    # 查询所有 incubating 策略
    cursor = conn.execute(
        "SELECT id FROM strategies WHERE status = 'incubating' LIMIT 20"
    )
    incubating_ids = [row[0] for row in cursor.fetchall()]

    if not incubating_ids:
        print("⚠️  没有 incubating 策略,跳过一致性测试")
        return

    print(f"\n一致性测试: 检查 {len(incubating_ids)} 个 incubating 策略")

    inconsistencies = []
    for strategy_id in incubating_ids:
        snapshot = ledger.get_snapshot(strategy_id)

        # 验证信号计数
        cursor = conn.execute(
            "SELECT COUNT(*) FROM strategy_signals WHERE strategy_id = ? AND COALESCE(signal, 0) != 0",
            (strategy_id,)
        )
        expected_signal_count = cursor.fetchone()[0]

        if snapshot.signal_count != expected_signal_count:
            inconsistencies.append(
                f"策略 {strategy_id} 信号计数不一致: ledger={snapshot.signal_count}, direct={expected_signal_count}"
            )

    if inconsistencies:
        print("❌ 发现不一致:")
        for msg in inconsistencies:
            print(f"  {msg}")
    else:
        print("✅ 所有查询结果一致")


def main():
    print("=" * 80)
    print("StrategyLifecycleLedger 一致性测试")
    print("=" * 80)

    try:
        test_ledger_basic()
        test_ledger_consistency()
        print("\n" + "=" * 80)
        print("✅ 测试完成")
        print("=" * 80)
        return 0
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
