"""Shared constants + helpers for db_sync script."""

import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parent.parent

import sys
for _pkg in ('akshare-mcp', 'strategy-factory', 'aiask-quant-core'):
    _p = str(PROJECT_ROOT / 'packages' / _pkg / 'src')
    if _p not in sys.path:
        sys.path.insert(0, _p)

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "akshare-mcp" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "strategy-factory" / "src"))

from _integration_paths import default_sqlite_path, default_task_board_path, ensure_runtime_dirs

ensure_runtime_dirs()
DB_PATH = default_sqlite_path()
os.environ["AKSHARE_MCP_SQLITE_PATH"] = str(DB_PATH)
os.environ["AIASK_SQLITE_PATH"] = str(DB_PATH)
os.environ["STRATEGY_FACTORY_TASK_BOARD_PATH"] = str(default_task_board_path())

# 加载 .env
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and not os.environ.get(key):
                    os.environ[key] = val


# ─────────────────────────────────────────────────────────────────────
# 数据源初始化（TDX 必备，Tushare 可选）
# ─────────────────────────────────────────────────────────────────────

# TDX 本地源（必备）
from akshare_mcp.data_source.tdx_local import get_tdx_local_source

tdx_local = get_tdx_local_source()

# Tushare Pro（可选）
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "").strip()
TDX_LOCAL_ONLY = os.environ.get("TDX_LOCAL_ONLY", "0") == "1"

pro = None
if TUSHARE_TOKEN and not TDX_LOCAL_ONLY:
    try:
        import tushare as ts

        ts.set_token(TUSHARE_TOKEN)
        pro = ts.pro_api(TUSHARE_TOKEN)
    except Exception as exc:  # pragma: no cover - 离线环境兼容
        print(f"⚠️ Tushare 初始化失败，将仅使用 TDX 本地源: {exc}")
        pro = None


def _require_tushare(action: str) -> bool:
    if pro is not None:
        return True
    print(f"⚠️ 跳过 {action}：需要配置 TUSHARE_TOKEN（或将 TDX_LOCAL_ONLY 设为 0）")
    return False


# ─────────────────────────────────────────────────────────────────────
# 同步任务定义
# ─────────────────────────────────────────────────────────────────────

# 代表性股票（策略工厂核心依赖）
REPRESENTATIVE_STOCKS = [
    "600519", "000858", "601318", "600036", "000333",
    "002415", "600276", "601012", "300750", "000001",
]

# 全量同步的股票池（沪深300核心成分）
FULL_UNIVERSE = REPRESENTATIVE_STOCKS + [
    "601166", "600030", "601398", "600900", "000651",
    "601888", "600809", "000568", "002304", "600887",
    "601668", "600585", "000725", "002142", "600031",
    "601601", "000002", "600048", "601818", "600000",
]


def _get_all_stocks() -> list[str]:
    """获取全量 A 股代码列表。优先级：本地 SQLite → 本地 TDX vipdoc → Tushare → 默认池。"""
    # 优先从 DB 读取
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            rows = conn.execute("SELECT stock_code FROM stocks WHERE stock_code IS NOT NULL").fetchall()
            conn.close()
            if rows and len(rows) > 100:
                return [r[0] for r in rows]
        except Exception:
            pass
    # 从本地 TDX vipdoc 列出（已内置过滤，只返回 A 股个股）
    if tdx_local.has_local:
        rows = tdx_local.list_local_stocks()
        codes = [r["code"] for r in rows]
        if codes:
            return codes
    # 从 Tushare 获取
    if pro is not None:
        try:
            df = pro.stock_basic(exchange='', list_status='L', fields='symbol')
            if df is not None and not df.empty:
                return df['symbol'].tolist()
        except Exception:
            pass
    return list(FULL_UNIVERSE)


def _to_ts_code(code: str) -> str:
    """将纯数字代码转为 Tushare ts_code 格式。"""
    code = code.strip()
    if code.startswith(("6", "5", "9")):
        return f"{code}.SH"
    elif code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


# ─────────────────────────────────────────────────────────────────────
# TDX 本地源同步函数（首选）
# ─────────────────────────────────────────────────────────────────────
