"""执行一个 TDX sync 任务(逐步执行,每次只跑一个)。

用法:
    python scripts/tdx_probe/run_tdx_sync_one.py gpjy_daily
    python scripts/tdx_probe/run_tdx_sync_one.py scjy_daily
    python scripts/tdx_probe/run_tdx_sync_one.py financial_pro
    python scripts/tdx_probe/run_tdx_sync_one.py bkjy_daily
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from datetime import datetime

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 先加载 .env 让 TDX_SYNC_FREE_TIER=0 生效
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
sys.path.insert(0, os.path.join(_REPO, "packages", "akshare-mcp", "src"))
sys.path.insert(0, os.path.join(_REPO, "packages", "aiask-quant-core", "src"))

from _integration_paths import default_sqlite_path

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_REPO, ".env"))
    print(f"[Init] Loaded .env from {_REPO}")
    print(f"[Init] TDX_SYNC_FREE_TIER = {os.getenv('TDX_SYNC_FREE_TIER', 'unset(default 1)')}")
except ImportError:
    print("[Init] python-dotenv not installed, parsing .env manually")
    env_path = os.path.join(_REPO, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        print(f"[Init] TDX_SYNC_FREE_TIER = {os.getenv('TDX_SYNC_FREE_TIER', 'unset')}")


async def main(task_name: str) -> int:
    from akshare_mcp.services.tdx_sync_service import TdxSyncService
    from akshare_mcp.storage import get_db

    db = get_db()

    # 如果是 quote_snapshots 任务,显式传入全市场 universe(绕过 _resolve_universe 的限制)
    universe = None
    if task_name in ("quote_snapshots", "more_info", "consensus", "relation", "gpjy_daily"):
        try:
            import sqlite3
            sqlite_path = str(default_sqlite_path())
            if os.path.exists(sqlite_path):
                c = sqlite3.connect(sqlite_path)
                cur = c.cursor()
                cur.execute("SELECT stock_code FROM stocks ORDER BY stock_code")
                rows = cur.fetchall()
                c.close()
                from akshare_mcp.data_source.tdx_tqcenter import _normalize_code
                universe = [_normalize_code(r[0]) for r in rows]
                print(f"[Universe] 从 SQLite 直接拉到 {len(universe)} 只股票")
        except Exception as e:
            print(f"[Universe] 直接拉失败: {e}")

    svc = TdxSyncService(universe=universe) if universe else TdxSyncService()
    if universe:
        # 让所有 limit 都覆盖到全市场,绕过默认 200/500 限制
        svc.limit_consensus = len(universe) + 100
        svc.limit_more_info = len(universe) + 100
        svc.limit_gpjy = len(universe) + 100
        svc.limit_relation = len(universe) + 100
        svc.limit_financial = len(universe) + 100

    method_name = f"_sync_{task_name}"
    fn = getattr(svc, method_name, None)
    if fn is None:
        print(f"[FAIL] 未找到任务 {method_name}")
        return 1

    print(f"\n=== 跑任务: {method_name} ===")
    print(f"[Config] FREE_TIER={os.getenv('TDX_SYNC_FREE_TIER')}")

    # 显示当前字段集合
    from akshare_mcp.services import tdx_sync_service as mod
    if task_name == "gpjy_daily":
        print(f"[Fields] GP_FIELDS = {mod.DEFAULT_GP_FIELDS}")
    elif task_name == "scjy_daily":
        print(f"[Fields] SC_FIELDS = {mod.DEFAULT_SC_FIELDS}")
    elif task_name == "bkjy_daily":
        print(f"[Fields] BK_FIELDS = {mod.DEFAULT_BK_FIELDS}")
    elif task_name == "financial_pro":
        print(f"[Fields] FN_FIELDS = {mod.DEFAULT_FN_FIELDS}")

    started = time.time()
    try:
        stats = await fn(db)
        elapsed = round(time.time() - started, 2)
        print(f"\n[OK] elapsed={elapsed}s")
        print(f"[Stats] {stats}")
        return 0
    except Exception as e:
        elapsed = round(time.time() - started, 2)
        import traceback
        print(f"\n[FAIL] elapsed={elapsed}s")
        print(f"[Error] {type(e).__name__}: {e}")
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python run_tdx_sync_one.py <task_name>")
        print("可用任务: trading_dates, stock_basic, sector_basic, gpjy_daily,")
        print("         bkjy_daily, scjy_daily, financial_pro, more_info, consensus,")
        print("         relation, kzz_basic, ipo_events, divid_events,")
        print("         basic_financial, quote_snapshots, index_klines")
        sys.exit(1)
    task = sys.argv[1].strip().lower()
    sys.exit(asyncio.run(main(task)))
