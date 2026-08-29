#!/usr/bin/env python3
"""[已废弃] TDX 同步入口 — 已合并到 ``scripts/db_sync.py``。

请改用统一脚本：

    python scripts/db_sync.py --source tdx --full        # 等价于旧 db_sync_tdx --all
    python scripts/db_sync.py --source tdx --type kline  # 等价于旧 db_sync_tdx --kline

为兼容旧调用方，本文件透传到统一脚本：
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UNIFIED = PROJECT_ROOT / "scripts" / "db_sync.py"

# 旧参数 → 新参数映射
old_to_new = {
    "--all": ["--full"],
    "--stocks": ["--type", "stocks"],
    "--calendar": ["--type", "calendar"],
    "--kline": ["--type", "kline"],
}

argv: list[str] = []
i = 1
while i < len(sys.argv):
    arg = sys.argv[i]
    if arg in old_to_new:
        argv.extend(old_to_new[arg])
    else:
        argv.append(arg)
    i += 1

# 强制 TDX 源
argv.extend(["--source", "tdx"])

print(
    "[db_sync_tdx.py] 已合并到 db_sync.py，自动转发: "
    f"python {UNIFIED.name} {' '.join(argv)}"
)
sys.argv = [str(UNIFIED), *argv]
exec(compile(UNIFIED.read_text(encoding="utf-8"), str(UNIFIED), "exec"), {"__name__": "__main__", "__file__": str(UNIFIED)})
