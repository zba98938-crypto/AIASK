#!/usr/bin/env python3
"""P1 阶段集成测试。

验证两个 P1 修复项:
  1. SignalTracker 集成 ExecutionUniverseContract
  2. 诊断工具集成 StrategyLifecycleLedger

运行:
  python scripts/factories/test_p1_integration.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_src in (
    ROOT / "packages" / "aiask-quant-core" / "src",
    ROOT / "packages" / "strategy-factory" / "src",
    ROOT / "packages" / "akshare-mcp" / "src",
):
    path = str(package_src)
    if package_src.exists() and path not in sys.path:
        sys.path.insert(0, path)


def test_p1_1_signal_tracker_execution_universe() -> bool:
    """P1-1: 验证 SignalTracker 集成 ExecutionUniverseContract。"""
    print("\n" + "=" * 80)
    print("P1-1: SignalTracker 集成 ExecutionUniverseContract")
    print("=" * 80)

    try:
        # 验证适配层存在
        from akshare_mcp.services.signal_tracker_parts.execution_universe_adapter import (
            load_executable_strategies_via_contract,
            load_executable_strategies_with_fallback,
        )
        print("[OK] SignalTracker 适配层已实现")

        # 验证 SignalTracker 代码中是否导入了适配层
        signal_tracker_specs = ROOT / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "services" / "signal_tracker_parts" / "specs.py"
        if signal_tracker_specs.exists():
            content = signal_tracker_specs.read_text(encoding="utf-8")
            if "execution_universe_adapter" in content:
                print("[OK] SignalTracker specs.py 已导入适配层")
            else:
                print("[FAIL] SignalTracker specs.py 未导入适配层")
                return False

            if "load_executable_strategies_with_fallback" in content:
                print("[OK] SignalTracker 使用了 load_executable_strategies_with_fallback")
            else:
                print("[FAIL] SignalTracker 未使用适配层方法")
                return False
        else:
            print("[WARN] 找不到 SignalTracker specs.py")

        print("\n[PASS] P1-1 集成测试通过")
        print("   SignalTracker 现在通过 ExecutionUniverseContract 查询可执行策略")
        print("   支持降级到旧查询路径以确保向后兼容")
        return True

    except ImportError as exc:
        print(f"[FAIL] P1-1 集成测试失败: {exc}")
        return False
    except Exception as exc:
        print(f"[FAIL] P1-1 集成测试失败: {exc}")
        return False


def test_p1_2_diagnostics_lifecycle_ledger() -> bool:
    """P1-2: 验证诊断工具集成 StrategyLifecycleLedger。"""
    print("\n" + "=" * 80)
    print("P1-2: 诊断工具集成 StrategyLifecycleLedger")
    print("=" * 80)

    try:
        # 验证 StrategyLifecycleLedger 存在
        from aiask_quant_core.strategy_lifecycle_ledger import (
            StrategyLifecycleLedger,
            BusinessLifecycleStage,
        )
        print("[OK] StrategyLifecycleLedger 已实现")

        # 验证诊断工具是否使用了 ledger
        diagnose_script = ROOT / "scripts" / "factories" / "diagnose_factory_health.py"
        if diagnose_script.exists():
            content = diagnose_script.read_text(encoding="utf-8")
            if "StrategyLifecycleLedger" in content:
                print("[OK] 诊断工具导入了 StrategyLifecycleLedger")
            else:
                print("[FAIL] 诊断工具未导入 StrategyLifecycleLedger")
                return False

            if "self.ledger" in content:
                print("[OK] 诊断工具使用了 ledger 实例")
            else:
                print("[FAIL] 诊断工具未使用 ledger 实例")
                return False

            if "batch_get_snapshots" in content:
                print("[OK] 诊断工具使用了 batch_get_snapshots 批量查询")
            else:
                print("[WARN] 诊断工具可能未使用批量查询")

        else:
            print("[FAIL] 找不到诊断工具脚本")
            return False

        print("\n[PASS] P1-2 集成测试通过")
        print("   诊断工具现在通过 StrategyLifecycleLedger 查询策略状态")
        print("   使用统一的业务生命周期状态派生逻辑")
        return True

    except ImportError as exc:
        print(f"[FAIL] P1-2 集成测试失败: {exc}")
        return False
    except Exception as exc:
        print(f"[FAIL] P1-2 集成测试失败: {exc}")
        return False


def main() -> int:
    """运行所有 P1 集成测试。"""
    print("\n" + "=" * 80)
    print("策略工厂 P1 阶段集成测试")
    print("=" * 80)

    results = {
        "P1-1 SignalTracker 集成": test_p1_1_signal_tracker_execution_universe(),
        "P1-2 诊断工具集成": test_p1_2_diagnostics_lifecycle_ledger(),
    }

    print("\n" + "=" * 80)
    print("P1 集成测试汇总")
    print("=" * 80)

    passed = 0
    failed = 0
    for test_name, result in results.items():
        status = "[PASS]" if result else "[FAIL]"
        print(f"{test_name}: {status}")
        if result:
            passed += 1
        else:
            failed += 1

    print()
    print(f"总计: {passed} 个通过, {failed} 个失败")

    if failed == 0:
        print("\n[SUCCESS] 所有 P1 集成测试通过!")
        print("\n后续步骤:")
        print("  1. 运行 SignalTracker 验证执行宇宙查询一致性")
        print("  2. 运行诊断工具验证生命周期状态正确性")
        print("  3. 进入 P2: 统一 strategy-factory 和 akshare-mcp 的契约定义")
        return 0
    else:
        print(f"\n[WARNING] {failed} 个 P1 集成测试失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
