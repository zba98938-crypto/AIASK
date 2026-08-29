"""北向资金 EM datacenter Backfill(免费源 — 替代 TDX 付费包)

诊断报告 §2.1 / RFC-001 落地:
- TDX 付费包未购,无法用 GP06/GP07/SC02 拉北向
- EM `RPT_MUTUAL_DEAL_HISTORY`(日频净流入)2024-08-19 起被证监会要求停止公开

可用免费数据 — `RPT_MUTUAL_HOLDSTOCKNORTH_STA`:
- ✅ 个股北向持股数(季报/月报披露)
- ✅ 个股北向持股市值
- ✅ 持股占流通股比例
- ✅ 数据更新到最近季度末(如 2026-03-31)

本脚本:
1. 从 EM RPT_MUTUAL_HOLDSTOCKNORTH_STA 拉所有有北向持股的个股
2. 写入 `north_fund_holding`(若有)或 stock 维度 K 线扩展表

用法:
    python scripts/backfill_north_fund_em.py --top 1000   # 拉前 1000 大
    python scripts/backfill_north_fund_em.py --all        # 拉所有(可能 2000+ 只)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from typing import Any

import requests

from _integration_paths import default_sqlite_path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = str(default_sqlite_path())


def _fetch_em_holdings(page_size: int = 5000, page_number: int = 1) -> list[dict]:
    """从 EM RPT_MUTUAL_HOLDSTOCKNORTH_STA 拉个股北向持股"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "sortColumns": "HOLD_MARKET_CAP",
        "sortTypes": -1,
        "pageSize": page_size,
        "pageNumber": page_number,
        "reportName": "RPT_MUTUAL_HOLDSTOCKNORTH_STA",
        "columns": "TRADE_DATE,SECURITY_CODE,SECURITY_NAME,HOLD_SHARES,HOLD_MARKET_CAP,HOLD_SHARES_RATIO",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://data.eastmoney.com/",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    return (payload.get("result") or {}).get("data") or []


def _ensure_table(c: sqlite3.Connection) -> None:
    """确保 north_fund_holding 表存在(扩展表)"""
    c.execute("""
        CREATE TABLE IF NOT EXISTS north_fund_holding (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            hold_shares REAL,
            hold_market_cap REAL,
            hold_shares_ratio REAL,
            source TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (trade_date, code)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_north_holding_code_date
        ON north_fund_holding (code, trade_date DESC)
    """)
    c.commit()


def _upsert(c: sqlite3.Connection, items: list[dict]) -> dict:
    """UPSERT 到 north_fund_holding 表"""
    cur = c.cursor()
    inserted = 0
    updated = 0
    skipped = 0

    for item in items:
        trade_date = str(item.get("TRADE_DATE") or "").split(" ")[0]
        code = str(item.get("SECURITY_CODE") or "").strip()
        if not trade_date or not code:
            skipped += 1
            continue

        try:
            hold_shares = float(item.get("HOLD_SHARES") or 0.0)
            hold_market_cap = float(item.get("HOLD_MARKET_CAP") or 0.0)
            hold_ratio = float(item.get("HOLD_SHARES_RATIO") or 0.0)
        except (TypeError, ValueError):
            skipped += 1
            continue

        if hold_shares <= 0:
            skipped += 1
            continue

        cur.execute("SELECT 1 FROM north_fund_holding WHERE trade_date=? AND code=?",
                    (trade_date, code))
        exists = cur.fetchone() is not None

        cur.execute("""
            INSERT INTO north_fund_holding
                (trade_date, code, name, hold_shares, hold_market_cap, hold_shares_ratio, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (trade_date, code) DO UPDATE SET
                name              = EXCLUDED.name,
                hold_shares       = EXCLUDED.hold_shares,
                hold_market_cap   = EXCLUDED.hold_market_cap,
                hold_shares_ratio = EXCLUDED.hold_shares_ratio,
                source            = EXCLUDED.source,
                updated_at        = CURRENT_TIMESTAMP
        """, (
            trade_date, code,
            str(item.get("SECURITY_NAME") or ""),
            hold_shares, hold_market_cap, hold_ratio,
            "eastmoney_RPT_MUTUAL_HOLDSTOCKNORTH_STA",
        ))
        if exists:
            updated += 1
        else:
            inserted += 1

    c.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=1000,
                        help="拉前 N 只(按持股市值排序),默认 1000")
    parser.add_argument("--all", action="store_true",
                        help="拉所有 ~2500 只北向持股股票(覆盖更全)")
    args = parser.parse_args()

    page_size = 5000 if args.all else args.top

    print(f"=== EM 个股北向持股 Backfill ===")
    print(f"Mode:      {'ALL' if args.all else f'TOP {args.top}'}")
    print(f"page_size: {page_size}")
    print(f"DB:        {DB_PATH}")
    print()

    if not os.path.exists(DB_PATH):
        print(f"[FATAL] DB 文件不存在: {DB_PATH}")
        return 1

    print(f"[Step 1] 从 EM 拉数据...")
    started = time.time()
    items = _fetch_em_holdings(page_size=page_size)
    elapsed = round(time.time() - started, 2)
    print(f"  原始记录: {len(items)} 条 / 耗时 {elapsed}s")
    if not items:
        print("[FATAL] EM 返回空")
        return 1

    # 看数据日期分布
    dates = sorted({str(x.get('TRADE_DATE') or '').split(' ')[0] for x in items})
    print(f"  覆盖日期: {len(dates)} 个不同日期")
    if dates:
        print(f"    range: {dates[0]} → {dates[-1]}")

    # sample
    sample = items[0]
    print(f"  TOP 1: {sample.get('SECURITY_NAME')} ({sample.get('SECURITY_CODE')}) hold_shares={sample.get('HOLD_SHARES'):,} ratio={sample.get('HOLD_SHARES_RATIO')}%")

    print()
    print(f"[Step 2] UPSERT 到 north_fund_holding...")
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    _ensure_table(c)

    cur = c.cursor()
    cur.execute("SELECT COUNT(*), MAX(trade_date), COUNT(DISTINCT code) FROM north_fund_holding")
    before = cur.fetchone()
    print(f"  Before: rows={before[0]}, max_date={before[1]}, codes={before[2]}")

    result = _upsert(c, items)

    cur.execute("SELECT COUNT(*), MAX(trade_date), COUNT(DISTINCT code) FROM north_fund_holding")
    after = cur.fetchone()
    print(f"  After:  rows={after[0]}, max_date={after[1]}, codes={after[2]}")
    print(f"  Stats:  inserted={result['inserted']}, updated={result['updated']}, skipped={result['skipped']}")

    c.close()

    print()
    print(f"[Done] §2.1 部分缓解:")
    print(f"  - ✅ 个股北向持股(月度/季度)已 backfill")
    print(f"  - ❌ 日频北向资金净流入(`north_money`)2024-08 后停止公开,无免费源")
    print(f"  - ⚠️  生产代码已正确 fallback:")
    print(f"        get_north_fund 走 north_fund_flow 表 → 仍 21 月 stale(EM 也没有日频)")
    print(f"        get_north_fund_holding 走 EM datacenter 实时调用(本表作为 cache)")
    print(f"        get_north_fund_top 走 EM datacenter 实时调用 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
