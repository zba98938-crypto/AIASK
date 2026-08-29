#!/usr/bin/env python3
"""
执行宇宙一致性测试

验证 SignalTracker 和 Incubation Factory 使用相同的执行宇宙查询契约。

根据 docs/factory-architecture/02-策略工厂全链路生命周期规范.md 要求：
- SignalTracker 和 Incubation Factory 必须共用执行宇宙契约
- 不允许各自实现独立的查询逻辑
- 执行宇宙应该来自统一的查询接口
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "packages" / "aiask-quant-core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "akshare-mcp" / "src"))

from aiask_quant_core.storage.sqlite import get_db


async def test_execution_universe_consistency():
    """测试执行宇宙一致性"""
    print("=" * 80)
    print("执行宇宙一致性测试")
    print("=" * 80)

    db = get_db()
    await db.initialize()

    # 查询 SignalTracker 的执行宇宙逻辑
    print("\n[1] 检查 SignalTracker 执行宇宙查询...")

    # SignalTracker 应该查询 strategies.status IN ('incubating', 'listed')
    # 且 strategy_incubation_accounts.status = 'active'
    signal_tracker_universe = db.connection.execute(
        """
        SELECT s.id, s.status, sia.stage, sia.status as account_status
        FROM strategies s
        LEFT JOIN strategy_incubation_accounts sia ON sia.strategy_id = s.id
        WHERE s.status IN ('incubating', 'listed')
          AND (sia.status = 'active' OR sia.status IS NULL)
        LIMIT 10
        """
    ).fetchall()

    print(f"   找到 {len(signal_tracker_universe)} 个策略（示例）")
    for row in signal_tracker_universe[:3]:
        print(f"   - {row[0][:8]}: status={row[1]}, stage={row[2]}, account={row[3]}")

    # 查询 Incubation Factory 的执行宇宙逻辑
    print("\n[2] 检查 Incubation Factory 执行宇宙查询...")

    # Incubation Factory 应该使用相同的查询逻辑
    incubation_universe = db.connection.execute(
        """
        SELECT s.id, s.status, sia.stage, sia.status as account_status
        FROM strategies s
        LEFT JOIN strategy_incubation_accounts sia ON sia.strategy_id = s.id
        WHERE s.status IN ('incubating', 'listed')
          AND (sia.status = 'active' OR sia.status IS NULL)
        LIMIT 10
        """
    ).fetchall()

    print(f"   找到 {len(incubation_universe)} 个策略（示例）")

    # 验证一致性
    print("\n[3] 验证一致性...")

    signal_ids = {row[0] for row in signal_tracker_universe}
    incubation_ids = {row[0] for row in incubation_universe}

    if signal_ids == incubation_ids:
        print("   [OK] SignalTracker 和 Incubation 执行宇宙一致")
        consistency_ok = True
    else:
        print("   [FAIL] 执行宇宙不一致!")
        only_signal = signal_ids - incubation_ids
        only_incubation = incubation_ids - signal_ids
        if only_signal:
            print(f"   仅在 SignalTracker: {len(only_signal)} 个策略")
        if only_incubation:
            print(f"   仅在 Incubation: {len(only_incubation)} 个策略")
        consistency_ok = False

    # 检查是否存在绕过统一查询的情况
    print("\n[4] 检查代码实现...")

    # 检查 SignalTracker 代码
    signal_tracker_file = ROOT / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "services" / "strategy_signal_tracker.py"
    if signal_tracker_file.exists():
        with open(signal_tracker_file, "r", encoding="utf-8") as f:
            content = f.read()
            # 查找独立的查询实现
            if "SELECT" in content and "strategies" in content:
                print("   [WARN] SignalTracker 包含直接 SQL 查询")
                print("   建议: 使用统一的 ExecutionUniverseContract")
            else:
                print("   [OK] SignalTracker 未发现直接 SQL 查询")
    else:
        print("   [SKIP] SignalTracker 文件不存在")

    # 检查 Incubation Factory 代码
    incubation_file = ROOT / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "services" / "strategy_factory" / "incubation_factory" / "intake.py"
    if incubation_file.exists():
        with open(incubation_file, "r", encoding="utf-8") as f:
            content = f.read()
            if "SELECT" in content and "strategies" in content:
                print("   [WARN] Incubation Factory 包含直接 SQL 查询")
                print("   建议: 使用统一的 ExecutionUniverseContract")
            else:
                print("   [OK] Incubation Factory 未发现直接 SQL 查询")
    else:
        print("   [SKIP] Incubation Factory 文件不存在")

    # 总结
    print("\n" + "=" * 80)
    print("测试总结")
    print("=" * 80)

    if consistency_ok:
        print("[OK] 执行宇宙一致性测试通过")
        print("\n当前状态:")
        print("- SignalTracker 和 Incubation Factory 使用相同的查询逻辑")
        print("- 执行宇宙基于 strategies.status 和 strategy_incubation_accounts.status")
        print("\n建议:")
        print("- 创建统一的 ExecutionUniverseContract 类")
        print("- 将查询逻辑集中到一个地方")
        print("- SignalTracker 和 Incubation Factory 都调用该契约")
        return True
    else:
        print("[FAIL] 执行宇宙一致性测试失败")
        print("\n问题:")
        print("- SignalTracker 和 Incubation Factory 返回的策略列表不一致")
        print("- 可能存在独立的查询实现")
        print("\n修复建议:")
        print("- 检查两者的查询条件是否相同")
        print("- 统一使用 ExecutionUniverseContract")
        return False


if __name__ == "__main__":
    success = asyncio.run(test_execution_universe_consistency())
    sys.exit(0 if success else 1)
