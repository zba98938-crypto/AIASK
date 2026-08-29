#!/usr/bin/env python3
"""监控50轮测试进度"""

import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
AIASK_ROOT = SCRIPT_DIR.parents[1]
if str(AIASK_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(AIASK_ROOT / "scripts"))

from _integration_paths import runtime_home

REPORT_PATH = runtime_home() / "reports" / "testing" / "incubation_factory_50_rounds_report.json"
LOG_PATH = runtime_home() / "logs" / "testing" / "incubation_factory_50_rounds_test.log"


def parse_progress_from_log():
    """从日志文件解析当前进度"""
    if not LOG_PATH.exists():
        return None, 0, 0

    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

        current_round = 0
        completed_rounds = 0

        for line in reversed(lines):
            if "========== Round" in line:
                # Extract round number
                parts = line.split("Round")[1].split("/")
                current_round = int(parts[0].strip())
                break

        # Count completed rounds
        for line in lines:
            if "Round " in line and "completed:" in line:
                completed_rounds += 1

        return current_round, completed_rounds, len(lines)

    except Exception as exc:
        return None, 0, 0


def format_time(seconds):
    """格式化时间"""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}min"
    else:
        return f"{seconds/3600:.1f}h"


def main():
    """主函数"""
    print("监控50轮测试进度...")
    print(f"日志文件: {LOG_PATH}")
    print(f"报告文件: {REPORT_PATH}")
    print("-" * 60)

    start_time = time.time()
    last_round = 0
    last_completed = 0

    while True:
        current_round, completed_rounds, log_lines = parse_progress_from_log()

        if current_round is None:
            print("等待测试启动...")
            time.sleep(5)
            continue

        elapsed = time.time() - start_time

        # 只在有进展时输出
        if current_round != last_round or completed_rounds != last_completed:
            avg_time = elapsed / completed_rounds if completed_rounds > 0 else 0
            remaining_rounds = 50 - completed_rounds
            eta = avg_time * remaining_rounds if avg_time > 0 else 0

            print(f"[{format_time(elapsed)}] Round {current_round}/50 | "
                  f"Completed: {completed_rounds} | "
                  f"Avg: {format_time(avg_time)}/round | "
                  f"ETA: {format_time(eta)}")

            last_round = current_round
            last_completed = completed_rounds

        # 检查是否完成
        if REPORT_PATH.exists():
            with open(REPORT_PATH, "r", encoding="utf-8") as f:
                report = json.load(f)

            if report.get("statistics", {}).get("total_rounds") == 50:
                print("\n" + "=" * 60)
                print("测试完成！")
                print("=" * 60)

                stats = report["statistics"]
                print(f"总轮数: {stats['total_rounds']}")
                print(f"成功: {stats['successful_rounds']} ({stats['success_rate']:.1f}%)")
                print(f"失败: {stats['error_rounds']}")
                print(f"平均耗时: {format_time(stats['avg_time_per_round'])}/轮")
                print(f"最快: {format_time(stats['min_time'])}")
                print(f"最慢: {format_time(stats['max_time'])}")
                print(f"总耗时: {format_time(stats['total_time'])}")
                print(f"\n报告: {REPORT_PATH}")
                print("=" * 60)
                break

        time.sleep(10)  # 每10秒检查一次


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n监控已停止")
