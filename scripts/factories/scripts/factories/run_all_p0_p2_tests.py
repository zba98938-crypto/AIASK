#!/usr/bin/env python3
"""一键运行所有 P0-P2 验证测试。

运行:
  python scripts/factories/run_all_p0_p2_tests.py
"""

from __future__ import annotations

import subprocess
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parents[1]


def _python_path() -> str:
    candidate = os.getenv("AIASK_FACTORY_PYTHON") or os.getenv("AIASK_VENV_PYTHON")
    if not candidate:
        raise SystemExit(
            "AIASK P0-P2 runner requires AIASK_FACTORY_PYTHON or AIASK_VENV_PYTHON "
            "pointing at an isolated Python >=3.12,<3.14 venv; it does not fall "
            "back to a hard-coded Python 3.11 interpreter."
        )
    return candidate


def run_test(test_script: Path, test_name: str) -> bool:
    """运行单个测试脚本。"""
    print(f"\n{'='*80}")
    print(f"运行 {test_name}")
    print(f"{'='*80}")

    try:
        result = subprocess.run(
            [_python_path(), str(test_script)],
            cwd=SCRIPTS_DIR,
            capture_output=False,
            text=True,
            encoding="utf-8",
        )
        return result.returncode == 0
    except Exception as exc:
        print(f"[ERROR] 测试执行失败: {exc}")
        return False


def main() -> int:
    """运行所有 P0-P2 测试。"""
    print("\n" + "="*80)
    print("策略工厂 P0-P2 全量验证")
    print("="*80)

    tests = [
        (SCRIPTS_DIR / "test_p0_fixes.py", "P0 基础合规性测试"),
        (SCRIPTS_DIR / "test_p1_integration.py", "P1 统一集成测试"),
        (SCRIPTS_DIR / "test_p2_contract_unification.py", "P2 契约统一测试"),
    ]

    results = {}
    for test_script, test_name in tests:
        if not test_script.exists():
            print(f"[WARNING] 测试脚本不存在: {test_script}")
            results[test_name] = False
            continue

        results[test_name] = run_test(test_script, test_name)

    # 汇总报告
    print("\n" + "="*80)
    print("P0-P2 全量验证汇总")
    print("="*80)

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
    print(f"总计: {passed}/{len(results)} 个测试套件通过")

    if failed == 0:
        print("\n[SUCCESS] 所有 P0-P2 验证测试通过!")
        print("\n策略工厂架构修复完成:")
        print("  - P0: 基础规范合规（补偿逻辑默认禁用，测试工具兼容 GBK）")
        print("  - P1: 统一集成（SignalTracker + 诊断工具使用统一契约/账本）")
        print("  - P2: 契约统一（strategy-factory 重定向到 akshare-mcp）")
        print("\n详细报告: docs/factory-architecture/P0_P2_COMPLETION_REPORT.md")
        return 0
    else:
        print(f"\n[WARNING] {failed} 个测试套件失败")
        print("请检查失败的测试输出并修复问题")
        return 1


if __name__ == "__main__":
    sys.exit(main())
