#!/usr/bin/env python3
"""统一数据库初始化脚本。

创建所有表结构，不写入数据。
数据库路径：AIASK_SQLITE_PATH / AKSHARE_MCP_SQLITE_PATH，默认在 AIASK_RUNTIME_HOME/data/db/akshare_mcp.sqlite3

用法：
    python scripts/db_init.py
"""

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "akshare-mcp" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "strategy-factory" / "src"))

from _integration_paths import default_sqlite_path, default_task_board_path, ensure_runtime_dirs

ensure_runtime_dirs()
DB_PATH = default_sqlite_path()
os.environ["AKSHARE_MCP_SQLITE_PATH"] = str(DB_PATH)
os.environ["AIASK_SQLITE_PATH"] = str(DB_PATH)

TASK_BOARD_PATH = default_task_board_path()
os.environ["STRATEGY_FACTORY_TASK_BOARD_PATH"] = str(TASK_BOARD_PATH)


async def main():
    print("=" * 60)
    print("数据库初始化")
    print("=" * 60)
    print(f"数据库路径: {DB_PATH}")
    print(f"TaskBoard: {TASK_BOARD_PATH}")
    print()

    # 确保目录存在
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 初始化主数据库
    from akshare_mcp.storage.sqlite import get_db
    db = get_db()
    await db.initialize()

    # 验证
    status = db.status()
    print(f"✅ 主数据库初始化完成")
    print(f"   路径: {status.get('path')}")
    print(f"   大小: {status.get('size_mb', 0):.1f} MB")
    print(f"   表数: {status.get('table_count', 'unknown')}")

    # 初始化 TaskBoard
    from strategy_factory.application.factory_task_board import FactoryTaskBoard
    task_board = FactoryTaskBoard(TASK_BOARD_PATH)
    # TaskBoard 在首次使用时自动建表
    print(f"✅ TaskBoard 初始化完成")
    print(f"   路径: {TASK_BOARD_PATH}")

    print()
    print("初始化完成。运行 `python scripts/db_sync.py --full` 进行数据同步。")


if __name__ == "__main__":
    asyncio.run(main())
