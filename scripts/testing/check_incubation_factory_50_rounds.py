#!/usr/bin/env python3
"""孵化工厂50轮压力测试脚本"""

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from _integration_paths import runtime_home

STRATEGY_FACTORY_SRC = PROJECT_ROOT / "packages" / "strategy-factory" / "src"
AKSHARE_MCP_SRC = PROJECT_ROOT / "packages" / "akshare-mcp" / "src"
REPORT_PATH = runtime_home() / "reports" / "testing" / "incubation_factory_50_rounds_report.json"

if str(STRATEGY_FACTORY_SRC) not in sys.path:
    sys.path.insert(0, str(STRATEGY_FACTORY_SRC))
if str(AKSHARE_MCP_SRC) not in sys.path:
    sys.path.insert(0, str(AKSHARE_MCP_SRC))

from strategy_factory.runtime_bootstrap import ensure_factory_runtime

ensure_factory_runtime(
    project_root=PROJECT_ROOT,
    script_path=Path(__file__).resolve(),
    argv=[],
    editable_packages=(
        "packages/strategy-factory",
        "packages/aiask-quant-core",
        "packages/akshare-mcp",
    ),
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


async def run_one_round(round_num: int, total_rounds: int) -> dict:
    """运行一轮孵化工厂"""
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.incubation import build_incubation_runtime

    logger.info(f"========== Round {round_num}/{total_rounds} ==========")

    ensure_default_runtime_services()
    runtime = build_incubation_runtime()

    start_time = datetime.now()

    try:
        result = await runtime.run_once(
            trigger=f"test_round_{round_num}",
            dry_run=True,  # 干跑模式，不写数据库
        )

        end_time = datetime.now()
        elapsed = (end_time - start_time).total_seconds()

        return {
            "round": round_num,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "elapsed_seconds": elapsed,
            "status": result.get("status", "unknown"),
            "result": result,
        }
    except Exception as exc:
        end_time = datetime.now()
        elapsed = (end_time - start_time).total_seconds()

        logger.error(f"Round {round_num} failed: {exc}")

        return {
            "round": round_num,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "elapsed_seconds": elapsed,
            "status": "error",
            "error": str(exc),
        }


async def main():
    """主函数"""
    total_rounds = 50
    results = []

    logger.info(f"开始50轮孵化工厂测试 (dry-run 模式)")
    logger.info(f"输出报告: {REPORT_PATH}")

    overall_start = datetime.now()

    for i in range(1, total_rounds + 1):
        round_result = await run_one_round(i, total_rounds)
        results.append(round_result)

        # 每轮后简要输出
        status = round_result.get("status")
        elapsed = round_result.get("elapsed_seconds", 0)
        logger.info(f"Round {i} completed: status={status}, elapsed={elapsed:.1f}s")

        # 每10轮输出中间统计
        if i % 10 == 0:
            success_count = sum(1 for r in results if r.get("status") in ["completed", "partial"])
            avg_time = sum(r.get("elapsed_seconds", 0) for r in results) / len(results)
            logger.info(f"Progress: {i}/{total_rounds} rounds, {success_count} successful, avg {avg_time:.1f}s/round")

    overall_end = datetime.now()
    overall_elapsed = (overall_end - overall_start).total_seconds()

    # 汇总统计
    success_count = sum(1 for r in results if r.get("status") in ["completed", "partial"])
    error_count = sum(1 for r in results if r.get("status") == "error")
    avg_time = sum(r.get("elapsed_seconds", 0) for r in results) / len(results)
    min_time = min(r.get("elapsed_seconds", 0) for r in results)
    max_time = max(r.get("elapsed_seconds", 0) for r in results)

    summary = {
        "test_config": {
            "total_rounds": total_rounds,
            "mode": "dry_run",
            "start_time": overall_start.isoformat(),
            "end_time": overall_end.isoformat(),
            "total_elapsed_seconds": overall_elapsed,
        },
        "statistics": {
            "total_rounds": total_rounds,
            "successful_rounds": success_count,
            "error_rounds": error_count,
            "success_rate": success_count / total_rounds * 100,
            "avg_time_per_round": avg_time,
            "min_time": min_time,
            "max_time": max_time,
            "total_time": overall_elapsed,
        },
        "rounds": results,
    }

    # 写入报告
    report_path = REPORT_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    logger.info(f"\n{'='*60}")
    logger.info(f"50轮测试完成")
    logger.info(f"{'='*60}")
    logger.info(f"总耗时: {overall_elapsed:.1f}s ({overall_elapsed/60:.1f} 分钟)")
    logger.info(f"成功: {success_count}/{total_rounds} ({success_count/total_rounds*100:.1f}%)")
    logger.info(f"失败: {error_count}/{total_rounds}")
    logger.info(f"平均耗时: {avg_time:.1f}s/轮")
    logger.info(f"最快: {min_time:.1f}s")
    logger.info(f"最慢: {max_time:.1f}s")
    logger.info(f"报告已保存: {report_path}")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
