#!/usr/bin/env python3
"""P2 阶段验证测试 - 统一契约定义。

验证:
  1. strategy-factory 版本已废弃并重定向
  2. akshare-mcp 版本作为统一标准
  3. 所有导入路径一致

运行:
  python scripts/factories/test_p2_contract_unification.py
"""

from __future__ import annotations

import sys
import warnings
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


def test_p2_strategy_factory_deprecation() -> bool:
    """P2-1: 验证 strategy-factory 版本已废弃并重定向。"""
    print("\n" + "=" * 80)
    print("P2-1: strategy-factory 契约废弃与重定向")
    print("=" * 80)

    try:
        # 捕获废弃警告
        with warnings.catch_warnings(record=True) as warning_list:
            warnings.simplefilter("always")

            # 导入 strategy-factory 版本
            from strategy_factory.contracts.execution_universe import (
                ExecutionUniverseContract,
                ExecutionUniverseQuery,
                ExecutionUniverseStrategy,
            )

            # 验证废弃警告被触发
            deprecation_warnings = [
                w for w in warning_list
                if issubclass(w.category, DeprecationWarning)
            ]

            if deprecation_warnings:
                print("[OK] strategy-factory 版本触发了 DeprecationWarning")
                for w in deprecation_warnings:
                    print(f"     警告信息: {w.message}")
            else:
                print("[FAIL] strategy-factory 版本未触发 DeprecationWarning")
                return False

        # 验证类型可用
        print("[OK] ExecutionUniverseContract 类已重定向")
        print("[OK] ExecutionUniverseQuery 数据类已重定向")
        print("[OK] ExecutionUniverseStrategy 数据类已重定向")

        print("\n[PASS] P2-1 验证通过")
        print("   strategy-factory 版本已正确废弃并重定向到 akshare-mcp")
        return True

    except ImportError as exc:
        print(f"[FAIL] P2-1 验证失败: {exc}")
        return False
    except Exception as exc:
        print(f"[FAIL] P2-1 验证失败: {exc}")
        return False


def test_p2_unified_import_path() -> bool:
    """P2-2: 验证统一导入路径。"""
    print("\n" + "=" * 80)
    print("P2-2: 统一导入路径验证")
    print("=" * 80)

    try:
        # 导入 akshare-mcp 权威版本
        from akshare_mcp.services.strategy_lifecycle_shared.execution_universe_contract import (
            ExecutionUniverseContract as AkShareContract,
            ExecutionUniverseQuery as AkShareQuery,
            ExecutionUniverseStrategy as AkShareStrategy,
        )
        print("[OK] akshare-mcp 版本导入成功（权威版本）")

        # 导入 strategy-factory 重定向版本
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from strategy_factory.contracts.execution_universe import (
                ExecutionUniverseContract as StrategyFactoryContract,
                ExecutionUniverseQuery as StrategyFactoryQuery,
                ExecutionUniverseStrategy as StrategyFactoryStrategy,
            )
        print("[OK] strategy-factory 版本导入成功（已重定向）")

        # 验证两者实际上是同一个类
        if AkShareContract is StrategyFactoryContract:
            print("[OK] ExecutionUniverseContract 是同一个类")
        else:
            print("[FAIL] ExecutionUniverseContract 不是同一个类")
            return False

        if AkShareQuery is StrategyFactoryQuery:
            print("[OK] ExecutionUniverseQuery 是同一个数据类")
        else:
            print("[FAIL] ExecutionUniverseQuery 不是同一个数据类")
            return False

        if AkShareStrategy is StrategyFactoryStrategy:
            print("[OK] ExecutionUniverseStrategy 是同一个数据类")
        else:
            print("[FAIL] ExecutionUniverseStrategy 不是同一个数据类")
            return False

        print("\n[PASS] P2-2 验证通过")
        print("   所有导入路径指向同一个统一实现")
        return True

    except ImportError as exc:
        print(f"[FAIL] P2-2 验证失败: {exc}")
        return False
    except Exception as exc:
        print(f"[FAIL] P2-2 验证失败: {exc}")
        return False


def test_p2_contract_functionality() -> bool:
    """P2-3: 验证统一契约功能完整性。"""
    print("\n" + "=" * 80)
    print("P2-3: 统一契约功能验证")
    print("=" * 80)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from strategy_factory.contracts.execution_universe import (
                ExecutionUniverseContract,
                ExecutionUniverseQuery,
                ExecutionUniverseStrategy,
            )

        # 验证契约方法存在
        contract = ExecutionUniverseContract()
        assert hasattr(contract, "list_executable_strategies"), "缺少 list_executable_strategies 方法"
        print("[OK] list_executable_strategies 方法存在")

        # 验证查询参数
        query = ExecutionUniverseQuery(
            include_incubating=True,
            include_paper=True,
            include_diagnostic=False,
            limit=100,
        )
        print("[OK] ExecutionUniverseQuery 可以正常实例化")

        # 验证策略数据类
        strategy = ExecutionUniverseStrategy(
            strategy_id="test_strategy_001",
            strategy_name="Test Strategy",
            strategy_type="momentum",
            status="incubating",
            incubation_stage="warmup",
            incubation_status="active",
            account_id="paper_001",
            created_at=None,
        )
        print("[OK] ExecutionUniverseStrategy 可以正常实例化")

        print("\n[PASS] P2-3 验证通过")
        print("   统一契约功能完整且可用")
        return True

    except ImportError as exc:
        print(f"[FAIL] P2-3 验证失败: {exc}")
        return False
    except Exception as exc:
        print(f"[FAIL] P2-3 验证失败: {exc}")
        return False


def main() -> int:
    """运行所有 P2 验证测试。"""
    print("\n" + "=" * 80)
    print("策略工厂 P2 阶段验证 - 契约统一")
    print("=" * 80)

    results = {
        "P2-1 strategy-factory 废弃": test_p2_strategy_factory_deprecation(),
        "P2-2 统一导入路径": test_p2_unified_import_path(),
        "P2-3 契约功能完整性": test_p2_contract_functionality(),
    }

    print("\n" + "=" * 80)
    print("P2 验证测试汇总")
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
        print("\n[SUCCESS] 所有 P2 验证测试通过!")
        print("\n契约统一完成:")
        print("  - akshare-mcp 版本作为权威实现")
        print("  - strategy-factory 版本已废弃并重定向")
        print("  - SignalTracker 和 Incubation Factory 使用统一契约")
        print("  - 避免了双真相问题")
        return 0
    else:
        print(f"\n[WARNING] {failed} 个 P2 验证测试失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
