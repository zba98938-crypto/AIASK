#!/usr/bin/env python3
"""
测试 StrategyLifecycleLedger 在质量会话中的集成

验证质量会话能够正确使用 StrategyLifecycleLedger 查询策略状态。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "packages" / "aiask-quant-core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "akshare-mcp" / "src"))

from aiask_quant_core.storage.sqlite import get_db
from aiask_quant_core.strategy_lifecycle_ledger import (
    StrategyLifecycleLedger,
    BusinessLifecycleStage,
)


async def test_quality_session_lifecycle_integration():
    """测试质量会话生命周期集成"""
    print("=" * 80)
    print("测试 StrategyLifecycleLedger 在质量会话中的集成")
    print("=" * 80)

    db = get_db()
    await db.initialize()

    # 初始化生命周期账本
    ledger = StrategyLifecycleLedger(db.connection)

    # 查询所有策略
    cursor = db.connection.execute("SELECT id FROM strategies LIMIT 10")
    strategy_ids = [row[0] for row in cursor.fetchall()]

    if not strategy_ids:
        print("\n[WARN] 数据库中没有策略")
        return

    print(f"\n找到 {len(strategy_ids)} 个策略（示例）")

    # 批量查询快照
    snapshots = ledger.batch_get_snapshots(strategy_ids)

    # 分析状态分布
    business_stats = {}
    physical_stats = {}
    blocker_reasons = {}

    for strategy_id, snapshot in snapshots.items():
        # 统计业务状态
        stage = snapshot.business_stage.value
        business_stats[stage] = business_stats.get(stage, 0) + 1

        # 统计物理状态
        status = snapshot.physical_status
        physical_stats[status] = physical_stats.get(status, 0) + 1

        # 收集阻塞原因
        if snapshot.blocker_reason:
            blocker_reasons[snapshot.blocker_reason] = (
                blocker_reasons.get(snapshot.blocker_reason, 0) + 1
            )

    # 打印统计结果
    print("\n物理状态分布：")
    for status, count in sorted(physical_stats.items()):
        print(f"  {status}: {count}")

    print("\n业务状态分布：")
    for stage, count in sorted(business_stats.items()):
        print(f"  {stage}: {count}")

    if blocker_reasons:
        print("\n阻塞原因 TOP 5：")
        for reason, count in sorted(
            blocker_reasons.items(), key=lambda x: x[1], reverse=True
        )[:5]:
            print(f"  {reason}: {count}")

    # 模拟质量会话报告中的使用场景
    print("\n" + "=" * 80)
    print("质量会话报告集成示例")
    print("=" * 80)

    # 示例 1: 统计准入就绪的策略
    admission_ready_count = sum(
        1
        for snapshot in snapshots.values()
        if snapshot.business_stage
        in {
            BusinessLifecycleStage.ADMITTED_OBSERVE,
            BusinessLifecycleStage.PAPER_SIGNALLED,
            BusinessLifecycleStage.PAPER_ORDERED,
        }
    )
    print(f"\n准入就绪策略数: {admission_ready_count}/{len(snapshots)}")

    # 示例 2: 统计有信号但无订单的策略
    signal_only_count = sum(
        1
        for snapshot in snapshots.values()
        if snapshot.business_stage == BusinessLifecycleStage.PAPER_SIGNALLED
        and snapshot.order_count == 0
    )
    print(f"signal-only backlog: {signal_only_count}/{len(snapshots)}")

    # 示例 3: 统计审计就绪的策略
    audit_ready_count = sum(
        1
        for snapshot in snapshots.values()
        if snapshot.business_stage == BusinessLifecycleStage.AUDIT_READY
    )
    print(f"审计就绪策略数: {audit_ready_count}/{len(snapshots)}")

    # 示例 4: 统计可晋级的策略
    promotion_ready_count = sum(
        1
        for snapshot in snapshots.values()
        if snapshot.business_stage == BusinessLifecycleStage.PROMOTION_READY
    )
    print(f"可晋级策略数: {promotion_ready_count}/{len(snapshots)}")

    # 示例 5: 显示第一个策略的详细信息
    if snapshots:
        first_id = next(iter(snapshots))
        snapshot = snapshots[first_id]
        print(f"\n示例策略详情 ({first_id[:8]}...):")
        print(f"  物理状态: {snapshot.physical_status}")
        print(f"  业务状态: {snapshot.business_stage.value}")
        print(f"  信号数: {snapshot.signal_count}")
        print(f"  订单数: {snapshot.order_count}")
        print(f"  成交数: {snapshot.trade_count}")
        print(f"  开仓数: {snapshot.open_position_count}")
        print(f"  平仓数: {snapshot.closed_position_count}")
        print(f"  前向收益: {snapshot.forward_return_count}")
        print(f"  审计快照: {snapshot.audit_snapshot_count}")
        print(f"  Audit Gate: {snapshot.execution_audit_gate_status or 'N/A'}")
        print(f"  Hard Gate: {'PASS' if snapshot.hard_gate_passed else 'FAIL'}")
        if snapshot.blocker_reason:
            print(f"  阻塞原因: {snapshot.blocker_reason}")

    print("\n[OK] 质量会话生命周期集成测试完成")
    return True


if __name__ == "__main__":
    success = asyncio.run(test_quality_session_lifecycle_integration())
    sys.exit(0 if success else 1)
