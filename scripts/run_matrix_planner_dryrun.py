"""PR-S22 实测：在本地 DB 上跑一次 StockStrategyMatrixPlanner.plan()，
验证 BULK 加载量与 task artifact 中 stock_profile_summary 是否到位。

用法：
    python scripts/run_matrix_planner_dryrun.py
"""

from __future__ import annotations

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

# 打开 BULK
os.environ["STRATEGY_FACTORY_BULK_ENABLED"] = "1"
os.environ["STRATEGY_FACTORY_BULK_STOCK_MATRIX_ENABLED"] = "1"
os.environ["STRATEGY_FACTORY_BULK_STOCK_MATRIX_UNIVERSE_LIMIT"] = "6000"
os.environ["STRATEGY_FACTORY_BULK_STOCK_MATRIX_RUN_WINDOW"] = "always"
os.environ.setdefault(
    "AKSHARE_MCP_SQLITE_PATH",
    str(ROOT / "data" / "db" / "akshare_mcp.sqlite3"),
)


async def main() -> int:
    # 必须在打开 env 之后再 import，使常量正确读取
    from akshare_mcp.storage import get_db, close_db
    from strategy_factory.application.research.matrix import StockStrategyMatrixPlanner

    db = get_db()
    await db.initialize()
    started = time.perf_counter()
    try:
        planner = StockStrategyMatrixPlanner()
        snapshot = {
            "date": "2026-05-17",
            "fear_greed_index": 55,
            "fg_components": {"volatility": 50.0},
            "north_fund_3d_net": 0.0,
            "margin_5d_change_pct": 0.0,
        }
        report = await planner.plan(db, snapshot)
    finally:
        await close_db()
    elapsed = round(time.perf_counter() - started, 2)

    summary = dict(report.get("summary") or {})
    tasks = list(report.get("tasks") or [])
    sample_tasks = tasks[:3]

    out = {
        "elapsed_sec": elapsed,
        "summary": {
            k: summary.get(k)
            for k in [
                "enabled",
                "task_count",
                "stock_count",
                "loaded_stock_count",
                "pages_loaded",
                "load_error",
                "skip_reason",
                "profile_loaded_count",
                "profile_missing_count",
                "profile_load_error",
                "profile_quality_distribution",
                "profile_archetype_distribution",
                "similar_profile_lookup_count",
                "similar_profile_hit_count",
                "verified_strategy_index_count",
                "vector_reuse_eligible_count",
                "vector_reuse_count",
                "vector_reuse_avg_similarity",
                "vector_reuse_enabled",
            ]
        },
        "sample_tasks": [
            {
                "task_id": t.get("task_id"),
                "candidate_family": t.get("candidate_family"),
                "target_symbols": t.get("target_symbols"),
                "holding_window": t.get("holding_window"),
                "alpha_source": t.get("alpha_source"),
                "risk_level": t.get("risk_level"),
                "stock_profile_summary": (t.get("stock_profile_summary") or {}).get("primary_archetype"),
                "factor_dimension_scores_keys": sorted(
                    list((t.get("stock_profile_summary") or {}).get("factor_dimension_scores") or {})
                ),
                "param_search_space_keys": sorted(list((t.get("param_search_space") or {}).keys())),
                "vector_reuse_hit": bool(t.get("vector_reuse_hit")),
            }
            for t in sample_tasks
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
