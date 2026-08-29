#!/usr/bin/env python3
"""
P0-P2 修复完整测试套件

按顺序运行所有 P0-P2 测试并汇总结果。
"""
import subprocess
import sys
from pathlib import Path

def run_test_file(test_file: Path) -> dict:
    """运行单个测试文件并返回结果"""
    print(f"\n{'='*70}")
    print(f"运行测试: {test_file.name}")
    print('='*70)

    try:
        result = subprocess.run(
            [sys.executable, str(test_file)],
            capture_output=True,
            text=True,
            timeout=300
        )

        success = result.returncode == 0

        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

        return {
            'name': test_file.name,
            'success': success,
            'returncode': result.returncode
        }
    except subprocess.TimeoutExpired:
        print(f"❌ 测试超时: {test_file.name}")
        return {
            'name': test_file.name,
            'success': False,
            'returncode': -1
        }
    except Exception as e:
        print(f"❌ 测试执行错误: {e}")
        return {
            'name': test_file.name,
            'success': False,
            'returncode': -2
        }

def main():
    """主测试流程"""
    script_dir = Path(__file__).parent

    # 按顺序运行测试
    test_files = [
        script_dir / 'test_p0_fixes.py',
        script_dir / 'test_p1_integration.py',
        script_dir / 'test_p2_contract_unification.py'
    ]

    results = []
    for test_file in test_files:
        if not test_file.exists():
            print(f"⚠️  测试文件不存在: {test_file}")
            results.append({
                'name': test_file.name,
                'success': False,
                'returncode': -3
            })
            continue

        result = run_test_file(test_file)
        results.append(result)

    # 汇总结果
    print(f"\n{'='*70}")
    print("P0-P2 测试汇总")
    print('='*70)

    total = len(results)
    passed = sum(1 for r in results if r['success'])
    failed = total - passed

    for result in results:
        status = "[PASS]" if result['success'] else "[FAIL]"
        print(f"{status} - {result['name']}")

    print(f"\n总计: {passed}/{total} 通过")

    if failed > 0:
        print(f"\n[FAIL] 有 {failed} 个测试失败")
        sys.exit(1)
    else:
        print("\n[PASS] 所有测试通过!")
        sys.exit(0)

if __name__ == '__main__':
    main()
