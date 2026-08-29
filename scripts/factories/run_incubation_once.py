#!/usr/bin/env python3
"""
手动触发孵化工厂运行一次
"""
import asyncio
import sys
from pathlib import Path

# 添加 PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "packages/akshare-mcp/src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "packages/strategy-factory/src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "packages/aiask-quant-core/src"))

async def main():
    print("=" * 80)
    print("手动运行孵化工厂 - run_once")
    print("=" * 80)

    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.incubation import build_incubation_runtime

    ensure_default_runtime_services()
    runtime = build_incubation_runtime()
    result = await runtime.run_once()

    print("\n执行结果:")
    print("-" * 80)
    for key, value in result.items():
        print(f"  {key}: {value}")

    print("\n" + "=" * 80)
    print("执行完成")
    print("=" * 80)

if __name__ == '__main__':
    asyncio.run(main())
