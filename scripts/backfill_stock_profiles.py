"""PR-S18 / PR-S22 全量回填脚本：把 stock_profile_embeddings 写满。

用法：
    python scripts/backfill_stock_profiles.py --limit 50 --dry-run    # 试跑
    python scripts/backfill_stock_profiles.py --limit 6000           # 全量

环境：使用 AIASK_SQLITE_PATH / AKSHARE_MCP_SQLITE_PATH，默认在 AIASK_RUNTIME_HOME/data/db/akshare_mcp.sqlite3。
脚本会自动按 list_stock_universe 的过滤规则跳过北交所/B 股/老三板。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PATHS = [
    ROOT / "packages" / "akshare-mcp" / "src",
    ROOT / "packages" / "strategy-factory" / "src",
]
for p in SRC_PATHS:
    sys.path.insert(0, str(p))

from _integration_paths import default_sqlite_path, ensure_runtime_dirs


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--profile-types", type=str, default="both")
    parser.add_argument("--kline-limit", type=int, default=90)
    parser.add_argument("--version", type=str, default="stock-profile-v2")
    parser.add_argument("--rebuild-existing", action="store_true", default=False)
    parser.add_argument("--dry-run", action="store_true", default=False)
    args = parser.parse_args()

    # 默认指向项目主 DB
    ensure_runtime_dirs()
    sqlite_path = default_sqlite_path()
    os.environ.setdefault("AIASK_SQLITE_PATH", str(sqlite_path))
    os.environ.setdefault("AKSHARE_MCP_SQLITE_PATH", str(sqlite_path))

    from akshare_mcp.storage import get_db, close_db
    from akshare_mcp.services.stock_profile_pipeline import backfill_stock_profile_vectors

    db = get_db()
    await db.initialize()

    started = time.perf_counter()
    try:
        result = await backfill_stock_profile_vectors(
            db,
            code_limit=args.limit,
            profile_types=args.profile_types,
            kline_limit=args.kline_limit,
            version=args.version,
            rebuild_existing=args.rebuild_existing,
            dry_run=args.dry_run,
        )
    finally:
        await close_db()
    elapsed = round(time.perf_counter() - started, 2)
    summary = {
        "elapsed_sec": elapsed,
        "code_count": result.get("code_count"),
        "processed_codes": result.get("processed_codes"),
        "skipped_codes": result.get("skipped_codes"),
        "candidate_profiles": result.get("candidate_profiles"),
        "saved_profiles": result.get("saved_profiles"),
        "skipped_existing_profiles": result.get("skipped_existing_profiles"),
        "errors": result.get("errors") or [],
        "profile_quality_distribution": result.get("profile_quality_distribution"),
        "profile_archetype_distribution": result.get("profile_archetype_distribution"),
        "version": result.get("version"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
