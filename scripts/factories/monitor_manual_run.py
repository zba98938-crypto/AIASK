#!/usr/bin/env python3
"""
实时监控手动 run_once 执行
"""
import time
import sqlite3
from pathlib import Path
from datetime import datetime

from _paths import DB_PATH

def check_formal():
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE incubating = 'formal_incubation'")
    count = cursor.fetchone()[0]
    conn.close()
    return count

def main():
    print("=" * 80)
    print(f"监控手动 run_once - {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 80)

    initial_formal = check_formal()
    print(f"\n[启动] formal_incubation: {initial_formal}")
    print("\n等待 Phase 执行...")
    print("(每 10 秒检查一次，按 Ctrl+C 停止)\n")

    check_count = 0
    while True:
        time.sleep(10)
        check_count += 1

        current_formal = check_formal()
        now = datetime.now().strftime('%H:%M:%S')

        if current_formal != initial_formal:
            print(f"\n{now} [!] formal_incubation 变化: {initial_formal} -> {current_formal}")
            print("="*80)
            print("首批转正成功！")
            print("="*80)
            break
        else:
            print(f"{now} [{check_count}] formal: {current_formal} (等待中...)")

        if check_count >= 60:  # 10 分钟后停止
            print(f"\n{now} [超时] 10 分钟后仍无变化")
            print("建议检查日志:")
            print("  tail -f logs/incubation_manual_run_*.log")
            break

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n监控已停止")
