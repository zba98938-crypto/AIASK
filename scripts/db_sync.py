#!/usr/bin/env python3
"""统一数据同步脚本（唯一入口）。

数据源策略与 ``data_source`` 单例保持一致：
    1. **本地 TDX vipdoc**（默认，零网络）— 通过 ``akshare_mcp.data_source.tdx_local``
    2. **Tushare Pro**（可选回退）— 仅在 ``--source tushare`` 或 ``TDX_LOCAL_ONLY=0`` 且配置了
       ``TUSHARE_TOKEN`` 时启用，覆盖 TDX 不提供的"北向资金 / 融资融券 / 龙虎榜 / 财务"。

用法：
    python scripts/db_sync.py --full                 # 全量同步（首次使用，默认 TDX）
    python scripts/db_sync.py --incremental          # 增量同步（日常，默认 TDX）
    python scripts/db_sync.py --type kline           # 仅同步 K 线
    python scripts/db_sync.py --type stocks          # 仅同步股票列表
    python scripts/db_sync.py --type calendar        # 仅同步交易日历
    python scripts/db_sync.py --type north_fund      # 仅同步北向资金（需 Tushare）
    python scripts/db_sync.py --type margin          # 仅同步融资融券（需 Tushare）
    python scripts/db_sync.py --type financial       # 仅同步财务数据（需 Tushare）
    python scripts/db_sync.py --source tdx --full    # 显式仅用 TDX 离线源
    python scripts/db_sync.py --source tushare --full# 显式仅用 Tushare（恢复旧行为）
    python scripts/db_sync.py --source auto --full   # 默认：TDX 优先 + Tushare 补齐 TDX 不提供的项
    python scripts/db_sync.py --status               # 查看同步状态

历史脚本 ``scripts/db_sync_tdx.py`` 已合并到本文件，仅作过渡期保留。
"""

import argparse
import asyncio
import os
import sys
import time
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# 设置项目路径
from _db_sync_common import (
    pro,
    tdx_local,
    DB_PATH,
    FULL_UNIVERSE,
    PROJECT_ROOT,
    REPRESENTATIVE_STOCKS,
    TDX_LOCAL_ONLY,
    TUSHARE_TOKEN,
    _get_all_stocks,
    _require_tushare,
    _to_ts_code,
)
from _db_sync_tasks import (
    sync_block_stocks,
    sync_calendar,
    sync_calendar_tdx,
    sync_daily_basic,
    sync_dragon_tiger,
    sync_financial,
    sync_klines,
    sync_klines_tdx,
    sync_margin,
    sync_margin_detail,
    sync_north_fund,
    sync_sector_flow,
    sync_stock_fund_flow,
    sync_stocks,
    sync_stocks_tdx,
)
async def show_status(db):
    """显示数据库同步状态。"""
    print("数据库同步状态")
    print("=" * 50)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # K 线
    try:
        row = conn.execute("SELECT COUNT(DISTINCT code) as cnt, MAX(time) as latest FROM kline_1d").fetchone()
        print(f"  K 线: {row['cnt']} 只股票, 最新: {row['latest']}")
    except Exception:
        print("  K 线: 无数据")

    # 北向资金
    try:
        row = conn.execute("SELECT COUNT(*) as cnt, MAX(trade_date) as latest FROM north_fund_flow").fetchone()
        print(f"  北向资金: {row['cnt']} 条, 最新: {row['latest']}")
    except Exception:
        print("  北向资金: 无数据")

    # 融资融券
    try:
        row = conn.execute("SELECT COUNT(*) as cnt, MAX(trade_date) as latest FROM margin_market_flow").fetchone()
        print(f"  融资融券(市场): {row['cnt']} 条, 最新: {row['latest']}")
    except Exception:
        print("  融资融券(市场): 无数据")

    # 融资融券明细
    try:
        row = conn.execute("SELECT COUNT(*) as cnt, COUNT(DISTINCT ts_code) as stocks, MAX(trade_date) as latest FROM margin_detail").fetchone()
        print(f"  融资融券(个股): {row['cnt']} 条, {row['stocks']} 只, 最新: {row['latest']}")
    except Exception:
        print("  融资融券(个股): 无数据")

    # 股票列表
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM stocks").fetchone()
        print(f"  股票列表: {row['cnt']} 只")
    except Exception:
        print("  股票列表: 无数据")

    # 交易日历
    try:
        row = conn.execute("SELECT COUNT(*) as cnt, MAX(trade_date) as latest FROM trading_dates").fetchone()
        print(f"  交易日历: {row['cnt']} 天, 最新: {row['latest']}")
    except Exception:
        print("  交易日历: 无数据")

    # 财务数据
    try:
        row = conn.execute("SELECT COUNT(DISTINCT stock_code) as cnt, MAX(updated_at) as latest FROM financials").fetchone()
        print(f"  财务数据: {row['cnt']} 只股票, 最新更新: {row['latest']}")
    except Exception:
        print("  财务数据: 无数据")

    # 板块资金
    try:
        row = conn.execute("SELECT COUNT(*) as cnt, MAX(updated_at) as latest FROM market_blocks").fetchone()
        print(f"  板块数据: {row['cnt']} 条, 最新: {row['latest']}")
    except Exception:
        print("  板块数据: 无数据")

    conn.close()

    # 数据库大小
    if DB_PATH.exists():
        size_mb = DB_PATH.stat().st_size / 1024 / 1024
        print(f"\n  数据库大小: {size_mb:.1f} MB")
        print(f"  数据库路径: {DB_PATH}")
    print(f"\n  数据源: Tushare Pro (token configured: {bool(TUSHARE_TOKEN)})")


# ─────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="统一数据同步脚本（默认 TDX 本地 + Tushare 补齐）"
    )
    parser.add_argument("--full", action="store_true", help="全量同步（首次使用）")
    parser.add_argument("--incremental", action="store_true", help="增量同步（日常）")
    parser.add_argument(
        "--type",
        choices=[
            "kline",
            "north_fund",
            "margin",
            "margin_detail",
            "financial",
            "stocks",
            "calendar",
            "sector_flow",
            "daily_basic",
            "stock_fund_flow",
            "dragon_tiger",
            "block_stocks",
        ],
        help="同步特定类型",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "tdx", "tushare"],
        default="auto",
        help="数据源：auto(默认，TDX 优先 + Tushare 补齐) / tdx(纯本地) / tushare(纯网络)",
    )
    parser.add_argument("--codes", type=str, default="", help="指定股票代码（逗号分隔）")
    parser.add_argument("--days", type=int, default=2000, help="K线天数（默认 2000，覆盖 8 年）")
    parser.add_argument("--status", action="store_true", help="查看同步状态")
    args = parser.parse_args()

    # 数据源解析
    use_tdx = args.source in ("auto", "tdx") and tdx_local.has_local
    use_tushare = args.source in ("auto", "tushare") and pro is not None

    if args.source == "tushare" and pro is None:
        print("❌ 错误: --source tushare 需要 TUSHARE_TOKEN 配置")
        sys.exit(1)
    if args.source == "tdx" and not tdx_local.has_local:
        print("❌ 错误: --source tdx 需要 TDX_INSTALL_DIR 指向通达信安装目录")
        sys.exit(1)
    if not use_tdx and not use_tushare:
        print("❌ 错误: 没有可用的数据源（TDX 与 Tushare 都未配置）")
        sys.exit(1)

    # 确保数据库已初始化
    if not DB_PATH.exists():
        print("数据库不存在，先运行初始化...")
        os.system(f"{sys.executable} {PROJECT_ROOT / 'scripts' / 'db_init.py'}")
        print()

    from akshare_mcp.storage.sqlite import get_db
    db = get_db()
    await db.initialize()

    if args.status:
        await show_status(db)
        return

    if not (args.full or args.incremental or args.type):
        parser.print_help()
        return

    start = time.time()
    results: dict[str, Any] = {}
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else REPRESENTATIVE_STOCKS

    label = "[全量]" if args.full else ("[增量]" if args.incremental else f"[{args.type}]")
    src_label = []
    if use_tdx:
        src_label.append(f"TDX({tdx_local.install_dir})")
    if use_tushare:
        src_label.append("Tushare Pro")
    print("=" * 60)
    print(f"数据同步 {label}")
    print(f"数据库: {DB_PATH}")
    print(f"数据源: {' + '.join(src_label) or 'none'}")
    print("=" * 60)
    print()

    # ── stocks（TDX 优先） ──
    async def _do_stocks() -> None:
        if use_tdx:
            r = await sync_stocks_tdx()
            results["stocks_tdx"] = r
            print(f"    → 股票列表(TDX): {r.get('count', 0)} 只")
        if use_tushare:
            r = await sync_stocks(db)
            results["stocks"] = r
            print(f"    → 股票列表(Tushare): {r.get('count', 0)} 只 名称/行业补齐")

    # ── calendar（TDX 优先） ──
    async def _do_calendar() -> None:
        if use_tdx:
            r = await sync_calendar_tdx()
            results["calendar_tdx"] = r
            print(f"    → 交易日历(TDX): {r.get('count', 0)} 天")
        if use_tushare and not (use_tdx and results.get("calendar_tdx", {}).get("count", 0) > 0):
            r = await sync_calendar(db)
            results["calendar"] = r
            print(f"    → 交易日历(Tushare): {r.get('count', 0)} 天")

    # ── kline（TDX 优先；Tushare 仅在 use_tushare && !use_tdx 时回灌） ──
    async def _do_klines(target_codes: list[str], days: int) -> None:
        if use_tdx:
            r = await sync_klines_tdx(db, target_codes, days=days)
            results["kline_tdx"] = r
            print(
                f"    → K 线(TDX): 成功 {r.get('synced', 0)}, 失败 {r.get('failed', 0)}, 总数 {r.get('total', 0)}"
            )
        elif use_tushare:
            r = await sync_klines(db, target_codes, days=min(days, 250))
            results["kline"] = r
            print(f"    → K 线(Tushare): 成功 {r.get('synced', 0)}, 失败 {r.get('failed', 0)}")

    if args.full:
        await _do_stocks()
        await asyncio.sleep(0.2)
        await _do_calendar()

        all_stocks = _get_all_stocks()
        print(f"    → 全量股票池: {len(all_stocks)} 只")
        await _do_klines(all_stocks, args.days)

        if use_tushare:
            await asyncio.sleep(1)
            results["north_fund"] = await sync_north_fund(db)
            print(f"    → 北向资金: {results['north_fund'].get('count', 0)} 条")

            await asyncio.sleep(1)
            results["margin"] = await sync_margin(db)
            print(f"    → 融资融券: {results['margin'].get('count', 0)} 条")

            await asyncio.sleep(1)
            results["margin_detail"] = await sync_margin_detail(db, REPRESENTATIVE_STOCKS)
            print(f"    → 融资融券明细: {results['margin_detail'].get('count', 0)} 条")

            await asyncio.sleep(1)
            results["daily_basic"] = await sync_daily_basic(db, REPRESENTATIVE_STOCKS)
            print(f"    → 估值数据: {results['daily_basic'].get('count', 0)} 条")

            await asyncio.sleep(1)
            results["stock_fund_flow"] = await sync_stock_fund_flow(db, REPRESENTATIVE_STOCKS)
            print(f"    → 个股资金流: {results['stock_fund_flow'].get('count', 0)} 条")

            await asyncio.sleep(1)
            results["dragon_tiger"] = await sync_dragon_tiger(db)
            print(f"    → 龙虎榜: {results['dragon_tiger'].get('count', 0)} 条")

            await asyncio.sleep(1)
            results["financial"] = await sync_financial(db, all_stocks)
            print(
                f"    → 财务数据: 成功 {results['financial'].get('synced', 0)}, 失败 {results['financial'].get('failed', 0)}"
            )

        # 板块从 stocks.industry 聚合（无须网络），始终执行
        await asyncio.sleep(0.5)
        results["sector_flow"] = await sync_sector_flow(db)
        print(f"    → 板块资金: {results['sector_flow'].get('count', 0)} 条")

        await asyncio.sleep(0.5)
        results["block_stocks"] = await sync_block_stocks(db)
        print(f"    → 板块成分股: {results['block_stocks'].get('count', 0)} 条")

    elif args.incremental:
        await _do_klines(codes, days=10 if use_tdx else 5)
        if use_tushare:
            await asyncio.sleep(0.5)
            results["north_fund"] = await sync_north_fund(db)
            print(f"    → 北向资金: {results['north_fund'].get('count', 0)} 条")

            await asyncio.sleep(0.5)
            results["margin"] = await sync_margin(db)
            print(f"    → 融资融券: {results['margin'].get('count', 0)} 条")

    elif args.type:
        if args.type == "kline":
            # 不传 --codes 时走全量 A 股
            kline_codes = codes if args.codes else _get_all_stocks()
            await _do_klines(kline_codes, args.days)
        elif args.type == "stocks":
            await _do_stocks()
        elif args.type == "calendar":
            await _do_calendar()
        elif args.type == "sector_flow":
            results["sector_flow"] = await sync_sector_flow(db)
        elif args.type == "block_stocks":
            results["block_stocks"] = await sync_block_stocks(db)
        elif args.type == "north_fund":
            results["north_fund"] = await sync_north_fund(db)
        elif args.type == "margin":
            results["margin"] = await sync_margin(db)
        elif args.type == "margin_detail":
            results["margin_detail"] = await sync_margin_detail(db, codes)
        elif args.type == "financial":
            results["financial"] = await sync_financial(db, codes)
        elif args.type == "daily_basic":
            results["daily_basic"] = await sync_daily_basic(db, codes)
        elif args.type == "stock_fund_flow":
            results["stock_fund_flow"] = await sync_stock_fund_flow(db, codes)
        elif args.type == "dragon_tiger":
            results["dragon_tiger"] = await sync_dragon_tiger(db)

        for name, result in results.items():
            status = "✅" if result.get("success") else "❌"
            print(f"    {status} {name}: {result}")

    elapsed = time.time() - start
    print()
    print(f"完成，耗时 {elapsed:.1f}s")

    # 显示最终状态
    print()
    await show_status(db)


if __name__ == "__main__":
    asyncio.run(main())
