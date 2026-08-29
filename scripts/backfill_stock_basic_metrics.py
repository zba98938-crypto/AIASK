#!/usr/bin/env python3
"""R5.1: 从 stock_quotes 回填 stocks.market_cap / pe_ratio / pb_ratio。

stock_quotes 表里有最新一天的 mkt_cap（万元）、pe、pb。
本脚本把这些值写回 stocks 表的 market_cap / pe_ratio / pb_ratio 列。

用法：
    python scripts/backfill_stock_basic_metrics.py
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

from _integration_paths import default_sqlite_path

DB_PATH = default_sqlite_path()


def main():
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    # 找到 stock_quotes 里每只股票最新一天的 mkt_cap / pe / pb
    # stock_quotes.code 对应 stocks.stock_code
    print("Backfilling stocks.market_cap / pe_ratio / pb_ratio from stock_quotes...")
    t0 = time.time()

    # 先看有多少 stocks 需要回填
    total_stocks = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    null_cap = conn.execute("SELECT COUNT(*) FROM stocks WHERE market_cap IS NULL").fetchone()[0]
    print(f"  stocks total={total_stocks}, market_cap NULL={null_cap}")

    # 用子查询取每只 code 最新一天的 mkt_cap / pe / pb
    updated = conn.execute("""
        UPDATE stocks
        SET
            market_cap = (
                SELECT q.mkt_cap
                FROM stock_quotes q
                WHERE q.code = stocks.stock_code
                ORDER BY q.time DESC
                LIMIT 1
            ),
            pe_ratio = (
                SELECT q.pe
                FROM stock_quotes q
                WHERE q.code = stocks.stock_code
                ORDER BY q.time DESC
                LIMIT 1
            ),
            pb_ratio = (
                SELECT q.pb
                FROM stock_quotes q
                WHERE q.code = stocks.stock_code
                ORDER BY q.time DESC
                LIMIT 1
            ),
            updated_at = CURRENT_TIMESTAMP
        WHERE EXISTS (
            SELECT 1 FROM stock_quotes q WHERE q.code = stocks.stock_code
        )
    """).rowcount

    conn.commit()
    elapsed = time.time() - t0

    # 验证
    still_null = conn.execute("SELECT COUNT(*) FROM stocks WHERE market_cap IS NULL").fetchone()[0]
    has_cap = conn.execute("SELECT COUNT(*) FROM stocks WHERE market_cap IS NOT NULL AND market_cap > 0").fetchone()[0]
    has_pe = conn.execute("SELECT COUNT(*) FROM stocks WHERE pe_ratio IS NOT NULL").fetchone()[0]
    has_pb = conn.execute("SELECT COUNT(*) FROM stocks WHERE pb_ratio IS NOT NULL").fetchone()[0]

    print(f"  Updated {updated} rows in {elapsed:.2f}s")
    print(f"  market_cap: {has_cap} non-null ({has_cap*100//total_stocks}%)")
    print(f"  pe_ratio:   {has_pe} non-null ({has_pe*100//total_stocks}%)")
    print(f"  pb_ratio:   {has_pb} non-null ({has_pb*100//total_stocks}%)")
    print(f"  still NULL market_cap: {still_null}")

    if still_null == 0:
        print("✅ All stocks have market_cap filled!")
    elif still_null < total_stocks * 0.05:
        print(f"⚠️  {still_null} stocks still missing market_cap (< 5%, acceptable)")
    else:
        print(f"❌ {still_null} stocks still missing market_cap — check stock_quotes coverage")

    conn.close()


if __name__ == "__main__":
    main()
