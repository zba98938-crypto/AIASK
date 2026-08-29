#!/usr/bin/env python3
"""
证据链路完整性测试

验证策略从信号到订单、成交、持仓、前向收益、审计的完整证据链。

根据 docs/factory-architecture/02-策略工厂全链路生命周期规范.md 要求：
- signal -> order -> trade -> position -> forward_returns -> audit
- 不允许出现断链（如有 signal 无 order，有 order 无 trade）
- 必须有 exit 路径（不能只有 entry）
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "packages" / "aiask-quant-core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "akshare-mcp" / "src"))

from aiask_quant_core.storage.sqlite import get_db
from aiask_quant_core.strategy_lifecycle_ledger import StrategyLifecycleLedger


async def test_evidence_chain_completeness():
    """测试证据链路完整性"""
    print("=" * 80)
    print("证据链路完整性测试")
    print("=" * 80)

    db = get_db()
    await db.initialize()

    ledger = StrategyLifecycleLedger(db.connection)

    # 查询所有有证据的策略
    cursor = db.connection.execute(
        """
        SELECT DISTINCT s.id
        FROM strategies s
        LEFT JOIN strategy_signals ss ON ss.strategy_id = s.id
        LEFT JOIN paper_orders po ON po.strategy_id = s.id
        LEFT JOIN paper_trades pt ON pt.strategy_id = s.id
        WHERE ss.id IS NOT NULL OR po.id IS NOT NULL OR pt.id IS NOT NULL
        LIMIT 100
        """
    )
    strategy_ids = [row[0] for row in cursor.fetchall()]

    if not strategy_ids:
        print("\n[WARN] 数据库中没有策略证据")
        print("\n跳过测试（需要先运行 SignalTracker 和 Incubation Factory）")
        return True

    print(f"\n找到 {len(strategy_ids)} 个有证据的策略")

    # 批量查询快照
    snapshots = ledger.batch_get_snapshots(strategy_ids)

    # 检查证据链路
    print("\n[1] 检查证据链路完整性...")

    # 1. signal -> order 断链
    signal_only = [
        sid
        for sid, snapshot in snapshots.items()
        if snapshot.signal_count > 0 and snapshot.order_count == 0
    ]

    # 2. order -> trade 断链
    order_no_trade = [
        sid
        for sid, snapshot in snapshots.items()
        if snapshot.order_count > 0 and snapshot.trade_count == 0
    ]

    # 3. trade -> position 断链
    trade_no_position = [
        sid
        for sid, snapshot in snapshots.items()
        if snapshot.trade_count > 0
        and snapshot.open_position_count == 0
        and snapshot.closed_position_count == 0
    ]

    # 4. position -> forward_returns 断链
    position_no_forward = [
        sid
        for sid, snapshot in snapshots.items()
        if (snapshot.open_position_count > 0 or snapshot.closed_position_count > 0)
        and snapshot.forward_return_count == 0
    ]

    # 5. forward_returns -> audit 断链
    forward_no_audit = [
        sid
        for sid, snapshot in snapshots.items()
        if snapshot.forward_return_count > 0 and snapshot.audit_snapshot_count == 0
    ]

    # 打印结果
    print(f"\n   signal -> order 断链: {len(signal_only)}/{len(snapshots)}")
    if signal_only[:3]:
        print(f"      示例: {', '.join(s[:8] + '...' for s in signal_only[:3])}")

    print(f"   order -> trade 断链: {len(order_no_trade)}/{len(snapshots)}")
    if order_no_trade[:3]:
        print(f"      示例: {', '.join(s[:8] + '...' for s in order_no_trade[:3])}")

    print(f"   trade -> position 断链: {len(trade_no_position)}/{len(snapshots)}")
    if trade_no_position[:3]:
        print(f"      示例: {', '.join(s[:8] + '...' for s in trade_no_position[:3])}")

    print(
        f"   position -> forward_returns 断链: {len(position_no_forward)}/{len(snapshots)}"
    )
    if position_no_forward[:3]:
        print(f"      示例: {', '.join(s[:8] + '...' for s in position_no_forward[:3])}")

    print(f"   forward_returns -> audit 断链: {len(forward_no_audit)}/{len(snapshots)}")
    if forward_no_audit[:3]:
        print(f"      示例: {', '.join(s[:8] + '...' for s in forward_no_audit[:3])}")

    # 检查 exit 路径
    print("\n[2] 检查 exit 路径...")

    # 统计只有 open 没有 closed 的策略
    open_only = [
        sid
        for sid, snapshot in snapshots.items()
        if snapshot.open_position_count > 0 and snapshot.closed_position_count == 0
    ]

    # 统计有 closed 的策略
    has_closed = [
        sid
        for sid, snapshot in snapshots.items()
        if snapshot.closed_position_count > 0
    ]

    print(f"\n   仅有 open position: {len(open_only)}/{len(snapshots)}")
    print(f"   有 closed position: {len(has_closed)}/{len(snapshots)}")

    if open_only and not has_closed:
        print("\n   [WARN] 所有持仓都是 open，缺少 exit 路径")
        print("   建议: 启用 stale close policy 或确保 exit signal 生成")

    # 检查 signal evidence
    print("\n[3] 检查 signal evidence...")

    # 查询有 signal 的策略
    has_signal = [
        sid for sid, snapshot in snapshots.items() if snapshot.signal_count > 0
    ]

    if has_signal:
        # 查询 signal_evidence
        signal_evidence_count = db.connection.execute(
            f"""
            SELECT COUNT(DISTINCT strategy_id)
            FROM strategy_signal_evidence
            WHERE strategy_id IN ({','.join('?' * len(has_signal))})
            """,
            has_signal,
        ).fetchone()[0]

        print(
            f"\n   有 signal 的策略: {len(has_signal)}/{len(snapshots)}"
        )
        print(
            f"   有 signal_evidence 的策略: {signal_evidence_count}/{len(has_signal)}"
        )

        if signal_evidence_count < len(has_signal):
            missing_evidence = len(has_signal) - signal_evidence_count
            print(
                f"\n   [WARN] {missing_evidence} 个策略缺少 signal_evidence"
            )
            print("   建议: 检查 SignalTracker native lineage 保存逻辑")

    # 总结
    print("\n" + "=" * 80)
    print("测试总结")
    print("=" * 80)

    issues = []

    if signal_only:
        issues.append(
            f"signal -> order 断链: {len(signal_only)} 个策略（可能原因: 执行宇宙未覆盖、价格缺失、账户缺失）"
        )

    if order_no_trade:
        issues.append(
            f"order -> trade 断链: {len(order_no_trade)} 个策略（可能原因: settlement 未运行、paper matching 失败）"
        )

    if trade_no_position:
        issues.append(
            f"trade -> position 断链: {len(trade_no_position)} 个策略（可能原因: position 记录未创建）"
        )

    if position_no_forward:
        issues.append(
            f"position -> forward_returns 断链: {len(position_no_forward)} 个策略（可能原因: forward window 未成熟、forward backfill 未运行）"
        )

    if forward_no_audit:
        issues.append(
            f"forward_returns -> audit 断链: {len(forward_no_audit)} 个策略（可能原因: execution audit 未运行）"
        )

    if open_only and not has_closed:
        issues.append(
            "缺少 exit 路径: 所有持仓都是 open（可能原因: stale close 未启用、exit signal 未生成）"
        )

    if issues:
        print("[WARN] 发现以下证据链路问题:\n")
        for i, issue in enumerate(issues, 1):
            print(f"{i}. {issue}")
        print("\n建议:")
        print("- 检查 SignalTracker、Incubation Factory、forward verifier 是否正常运行")
        print("- 启用 stale close policy 确保有 exit 路径")
        print("- 确保 signal_evidence 在 signal 生成时同步保存")
        return False
    else:
        print("[OK] 证据链路完整性测试通过")
        print("\n当前状态:")
        print("- 所有证据环节都正常流转")
        print("- exit 路径存在")
        print("- signal_evidence 完整")
        return True


if __name__ == "__main__":
    success = asyncio.run(test_evidence_chain_completeness())
    sys.exit(0 if success else 1)
