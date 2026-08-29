"""一次性 TDX 全量同步入口（生产 DB）。

跑 13 个 sync 任务，落到 .env 配置的真实 SQLite。
专业财务数据包已下载，FN 字段应该返回真值。

运行:
    %AIASK_VENV_PYTHON% scripts\\run_tdx_sync.py
    %AIASK_VENV_PYTHON% scripts\\run_tdx_sync.py --universe-size 100   # 限制每类的股票数量
    %AIASK_VENV_PYTHON% scripts\\run_tdx_sync.py --only sync_financial_pro,sync_gpjy_daily

环境变量优先级：
- AKSHARE_MCP_SQLITE_PATH / AIASK_SQLITE_PATH（默认从 .env 读）
- TDX_LOCAL_ONLY=1（默认 .env 已设）
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
from datetime import datetime
from pathlib import Path

# Windows GBK console 兼容：强制 stdout 用 utf-8
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "packages" / "aiask-quant-core" / "src"))
sys.path.insert(0, str(REPO / "packages" / "akshare-mcp" / "src"))
sys.path.insert(0, str(REPO / "packages" / "strategy-factory" / "src"))

from _integration_paths import default_sqlite_path


def _human(n: float) -> str:
    return f"{n:>10.2f}s"


async def main(args):
    # 加载 .env（env_loader 会从 cwd / repo root 找）
    os.chdir(str(REPO))
    from akshare_mcp.env_loader import load_mcp_env
    load_mcp_env(override=False)

    # 校验 DB 路径
    db_path = os.path.expanduser(
        os.environ.get("AKSHARE_MCP_SQLITE_PATH")
        or os.environ.get("AIASK_SQLITE_PATH")
        or str(default_sqlite_path())
    )
    print(f"[run_tdx_sync] DB        = {db_path}")
    print(f"[run_tdx_sync] DB size   = {os.path.getsize(db_path) / 1024 / 1024:.1f} MB"
          if os.path.exists(db_path) else "[run_tdx_sync] DB    = (will be created)")
    print(f"[run_tdx_sync] tdx_dir   = {os.environ.get('TDX_INSTALL_DIR', '?')}")
    print(f"[run_tdx_sync] tdx_only  = {os.environ.get('TDX_LOCAL_ONLY', '0')}")
    print(f"[run_tdx_sync] start     = {datetime.now().isoformat()}")
    print("")

    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        all_tasks = {
            "sync_trading_dates", "sync_stock_basic", "sync_quote_snapshots",
            "sync_index_klines", "sync_sector_basic",
            "sync_more_info", "sync_consensus", "sync_relation",
            "sync_gpjy_daily", "sync_bkjy_daily", "sync_scjy_daily",
            "sync_kzz_basic", "sync_ipo_events", "sync_divid_events",
            "sync_financial_pro", "sync_basic_financial",
            "sync_stock_fund_flow", "sync_derived_factory_market_data",
            "sync_external_gap_data", "record_tdx_data_completeness",
        }
        os.environ["TDX_SYNC_DISABLE"] = ",".join(sorted(all_tasks - wanted))

    from akshare_mcp.services.tdx_sync_service import TdxSyncService
    from akshare_mcp.storage import close_db

    svc = TdxSyncService(
        limit_more_info=args.universe_size,
        limit_consensus=args.universe_size,
        limit_relation=args.universe_size,
        limit_gpjy=args.universe_size,
        limit_financial=args.financial_size,
        limit_kzz=args.kzz_size,
    )

    started = datetime.now()
    res = await svc.run_all()
    elapsed = (datetime.now() - started).total_seconds()
    summary = res.get("summary", {})

    print("\n=== Task Summary ===")
    print(f"{'task':<24} {'ok':<4} {'skip':<4} {'elapsed':<10} stats")
    print("-" * 100)
    for t in res.get("tasks", []):
        name = t.get("task", "")
        ok = "OK" if t.get("ok") else "FAIL"
        skip = "SKIP" if t.get("skipped") else ""
        el = _human(t.get("elapsed_sec", 0))
        stats = t.get("stats") or {}
        err = t.get("error", "")
        line = f"{name:<24} {ok:<4} {skip:<4} {el}  {stats}"
        if err:
            line += f"  ERR={err[:80]}"
        print(line)

    print("\n=== Summary ===")
    print(f"  ok={summary.get('ok')}  skipped={summary.get('skipped')}  "
          f"failed={summary.get('failed')}  total={summary.get('total')}  "
          f"elapsed={elapsed:.1f}s")

    # DB 表行数核对
    print("\n=== Table Counts (after sync) ===")
    from akshare_mcp.storage import get_db
    db = get_db()
    table_names = [
        "trading_dates", "stocks", "market_blocks", "block_stocks",
        "tdx_stock_extra", "tdx_relation", "tdx_consensus",
        "tdx_kzz_basic", "tdx_gpjy_daily", "tdx_bkjy_daily",
        "tdx_scjy_daily", "tdx_financial_pro",
        "stock_quotes", "north_fund_flow", "margin_market_flow",
        "margin_detail", "stock_fund_flow",
        "strategy_factory_market_internals", "tdx_data_completeness",
        "events",
    ]
    async with db.acquire() as conn:
        for name in table_names:
            try:
                cnt = await conn.fetchval(f"SELECT count(*) FROM {name}")
                print(f"  {name:<22}  {cnt:>10}")
            except Exception as exc:
                print(f"  {name:<22}  ERR: {exc}")

    await close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe-size", type=int, default=300,
                        help="每类股票任务的最大股票数（默认 300）")
    parser.add_argument("--financial-size", type=int, default=300,
                        help="专业财务任务的最大股票数（默认 300）")
    parser.add_argument("--kzz-size", type=int, default=400,
                        help="可转债任务的最大数量（默认 400）")
    parser.add_argument("--only", type=str, default="",
                        help="只跑指定任务，逗号分隔，如 sync_financial_pro,sync_gpjy_daily")
    args = parser.parse_args()
    asyncio.run(main(args))
