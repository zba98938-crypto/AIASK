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
# AIASK_LOAD_PROJECT_ENV=0 (or the hermetic test-session marker) turns off
# guessing the repo .env; same contract as akshare_mcp.env_loader.
_env_discovery_off = str(os.getenv("AIASK_LOAD_PROJECT_ENV", "")).strip().lower() in {"0", "false", "no", "off"} or (
    "aiask_hermetic_test_session" in sys.modules
)
if env_file.exists() and not _env_discovery_off:
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

# 种子股票池：30 只沪深权重股。
#
# 它不是全市场，而是"拿不到真实股票列表"时的兜底。这里以前叫 FULL_UNIVERSE
# 并注释为「全量同步的股票池」，于是这条降级路径产出的 30 只股票会被下游当成
# 全市场 —— strategy_factory_full_market_scores 这个表名就是这么来的，实际
# 每次只覆盖 160 余只。符号名保留以便兼容引用，语义按实际写清楚；判断"是否
# 降级"请用 resolve_stock_universe() 返回的 source。
SEED_UNIVERSE = REPRESENTATIVE_STOCKS + [
    "601166", "600030", "601398", "600900", "000651",
    "601888", "600809", "000568", "002304", "600887",
    "601668", "600585", "000725", "002142", "600031",
    "601601", "000002", "600048", "601818", "600000",
]

# 兼容旧引用；见上：这不是全市场。
FULL_UNIVERSE = SEED_UNIVERSE

# 股票池来源标识，供调用方判断是否降级到种子池。
UNIVERSE_SOURCE_DB = "sqlite:stocks"
UNIVERSE_SOURCE_SINA = "sina:Market_Center[hs_a]"
UNIVERSE_SOURCE_TDX = "tdx:vipdoc"
UNIVERSE_SOURCE_TUSHARE = "tushare:stock_basic"
UNIVERSE_SOURCE_SEED = "seed:30"

#: ``stocks`` 表至少要有这么多行，``auto`` 才直接信它。
#:
#: 这里曾经是 100，在那个门槛下 165 行的残缺表会被当成一份可用的股票列表，
#: 于是"股票池=165 只"就固化下来了 —— 而 A 股实际有 5500 余只。门槛现在按
#: 全市场规模设定：表够大就直接用（快），不够就去重新列举（准）。
MIN_FULL_UNIVERSE = 4000

def fetch_market_symbols() -> tuple[list[str], str]:
    """向 Sina 列举全部 A 股代码，返回 (代码, 来源)。

    失败时抛异常，不返回空列表：列举失败和"市场里没有股票"是两件事，
    让它们长得一样正是这份代码此前的问题。
    """

    from akshare_mcp.data_source.sina_listing import fetch_all_a_share_symbols

    result = fetch_all_a_share_symbols()
    codes = [item["code"] for item in result["symbols"]]
    if not result.get("complete"):
        # 部分成功仍然可用，但必须说清楚它不完整。
        print(
            f"[universe] {UNIVERSE_SOURCE_SINA}: {len(codes)} symbols from "
            f"{result['pages']} page(s), INCOMPLETE (failed pages: {result['failed_pages']})",
            file=sys.stderr,
        )
    return codes, UNIVERSE_SOURCE_SINA


def resolve_stock_universe() -> tuple[list[str], str]:
    """返回 (股票代码, 来源标识)。

    优先级：本地 SQLite（仅当它足够大）→ Sina 全市场 → 本地 TDX vipdoc
    → Tushare → 种子池。

    每个被跳过的来源都会记下原因并打到 stderr。旧实现用 `except Exception:
    pass` 吞掉失败，于是"数据库读挂了"和"数据库是空的"以及"真的只有 30 只
    股票"三种情况在输出上完全一样。
    """

    skipped: list[str] = []

    if DB_PATH.exists():
        try:
            with sqlite3.connect(str(DB_PATH)) as conn:
                rows = conn.execute(
                    "SELECT stock_code FROM stocks WHERE stock_code IS NOT NULL"
                ).fetchall()
            if len(rows) >= MIN_FULL_UNIVERSE:
                return [r[0] for r in rows], UNIVERSE_SOURCE_DB
            skipped.append(
                f"{UNIVERSE_SOURCE_DB}: {len(rows)} row(s), below the "
                f"{MIN_FULL_UNIVERSE}-row full-market floor"
            )
        except Exception as exc:
            skipped.append(f"{UNIVERSE_SOURCE_DB}: {type(exc).__name__}: {exc}")
    else:
        skipped.append(f"{UNIVERSE_SOURCE_DB}: {DB_PATH} does not exist")

    # Sina 全市场清单：本机唯一可达的完整列举来源。
    try:
        codes, source = fetch_market_symbols()
        if codes:
            return codes, source
        skipped.append(f"{UNIVERSE_SOURCE_SINA}: no symbols returned")
    except Exception as exc:
        skipped.append(f"{UNIVERSE_SOURCE_SINA}: {type(exc).__name__}: {exc}")

    # 从本地 TDX vipdoc 列出（已内置过滤，只返回 A 股个股）
    if tdx_local.has_local:
        rows = tdx_local.list_local_stocks()
        codes = [r["code"] for r in rows]
        if codes:
            return codes, UNIVERSE_SOURCE_TDX
        skipped.append(f"{UNIVERSE_SOURCE_TDX}: no local stocks listed")
    else:
        skipped.append(f"{UNIVERSE_SOURCE_TDX}: no local vipdoc directory")

    # 从 Tushare 获取
    if pro is not None:
        try:
            df = pro.stock_basic(exchange='', list_status='L', fields='symbol')
            if df is not None and not df.empty:
                return df['symbol'].tolist(), UNIVERSE_SOURCE_TUSHARE
            skipped.append(f"{UNIVERSE_SOURCE_TUSHARE}: empty response")
        except Exception as exc:
            skipped.append(f"{UNIVERSE_SOURCE_TUSHARE}: {type(exc).__name__}: {exc}")
    else:
        skipped.append(f"{UNIVERSE_SOURCE_TUSHARE}: TUSHARE_TOKEN is not configured")

    print(
        f"[universe] falling back to the {len(SEED_UNIVERSE)}-code seed pool; "
        "this is not the full A-share market. Skipped sources: " + "; ".join(skipped),
        file=sys.stderr,
    )
    return list(SEED_UNIVERSE), UNIVERSE_SOURCE_SEED


def _get_all_stocks() -> list[str]:
    """股票代码列表。需要知道来源（是否降级）时用 resolve_stock_universe()。"""

    return resolve_stock_universe()[0]


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
