#!/usr/bin/env python3
"""Fill the market-data tables that have readers but no rows.

Why this exists
---------------
The runtime holds 146 tables and 80 are empty, all of them read by something:
`financials` (130 reader sites), `trading_dates` (80), `block_stocks` (31),
`stock_fund_flow` (20), `dragon_tiger` (17), `margin_*` (18). The project's
declared way in is Tushare, which is installed but has no token, so every one of
those paths returns nothing and says nothing.

East money's datacenter reports cover most of them in bulk. This script reads
those reports and writes the tables; :mod:`docs/DATA_SOURCES.md` records which
category comes from where, what it costs, and what is still missing.

Nothing here invents a value. A field the report does not carry is left NULL
rather than defaulted: `financials.net_margin`, `debt_ratio`, `current_ratio` and
`roa` need the balance-sheet report, and `stock_fund_flow` carries no
middle/small order split, so those columns stay empty and the report says so.

Usage::

    python scripts/sync_market_fundamentals.py                      # report only
    python scripts/sync_market_fundamentals.py --apply
    python scripts/sync_market_fundamentals.py --apply --what calendar,blocks
    python scripts/sync_market_fundamentals.py --apply --financial-years 3
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
for _rel in ("packages/akshare-mcp/src", "packages/aiask-quant-core/src", "packages/strategy-factory/src"):
    _path = str(REPO_ROOT / _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _db_sync_common import DB_PATH  # noqa: E402
from akshare_mcp.data_source.eastmoney_datacenter import fetch_report  # noqa: E402

CATEGORIES = ("calendar", "financials", "fundflow", "dragon", "blocks")

#: Reports read more than once in a run are read once. The earnings report is
#: roughly 100k rows in the default window, and both the financials and the
#: blocks pass need it; without this the same pages come down twice.
_REPORT_CACHE: dict[tuple, dict[str, Any]] = {}


def _report(name: str, **kwargs) -> dict[str, Any]:
    key = (name, tuple(sorted((k, str(v)) for k, v in kwargs.items())))
    if key not in _REPORT_CACHE:
        _REPORT_CACHE[key] = fetch_report(name, **kwargs)
    return _REPORT_CACHE[key]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the rows; without this nothing changes")
    parser.add_argument(
        "--what",
        default="all",
        help=f"comma-separated subset of {','.join(CATEGORIES)}, or 'all'",
    )
    parser.add_argument(
        "--financial-years",
        type=int,
        default=5,
        help="how far back to read the earnings report (default 5 years, 0 = everything)",
    )
    parser.add_argument("--max-rows", type=int, default=0, help="cap rows per report (0 = no cap)")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _selected(what: str) -> list[str]:
    if what.strip() == "all":
        return list(CATEGORIES)
    chosen = [part.strip() for part in what.split(",") if part.strip()]
    unknown = [part for part in chosen if part not in CATEGORIES]
    if unknown:
        raise SystemExit(f"unknown category: {', '.join(unknown)}; known: {', '.join(CATEGORIES)}")
    return chosen


def _date10(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else None


def _universe_codes(conn: sqlite3.Connection) -> set[str]:
    """Listed codes from ``stocks`` — the universe every other table uses.

    East money's reports cover about 24,000 securities including 新三板, roughly
    double this project's universe. Writing all of them would put more codes in
    ``financials`` than exist in ``stocks``, so a reader joining the two would be
    joining across two different definitions of "a listed company".
    """

    return {
        str(row[0])
        for row in conn.execute(
            "SELECT stock_code FROM stocks "
            "WHERE COALESCE(list_status, 'L') = 'L' AND stock_code GLOB '[0-9]*'"
        )
    }


def _as_float(value: Any) -> Optional[float]:
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _filter(expression: str) -> str:
    """URL-encode a datacenter filter such as ``(REPORTDATE>='2021-01-01')``."""

    return f"&filter={urllib.parse.quote(expression)}" if expression else ""


# --------------------------------------------------------------------------
# calendar
# --------------------------------------------------------------------------


def sync_calendar(conn: sqlite3.Connection, *, apply: bool) -> dict[str, Any]:
    """Derive the trading calendar from the kline table itself.

    Every bar in ``kline_1d`` is a day some stock traded, so the distinct dates
    are the exchange calendar for exactly the window the project has data for —
    no extra source, and it cannot disagree with the prices it describes. The
    table held 127 rows covering 2026-01-05..2026-07-15 before this.
    """

    dates = [
        row[0][:10].replace("-", "")
        for row in conn.execute("SELECT DISTINCT substr(time, 1, 10) FROM kline_1d")
    ]
    dates = sorted(d for d in dates if len(d) == 8 and d.isdigit())
    before = conn.execute("SELECT count(*) FROM trading_dates").fetchone()[0]
    if apply:
        conn.executemany(
            "INSERT OR IGNORE INTO trading_dates (trade_date, exchange, is_open) VALUES (?, 'SSE', 1)",
            [(d,) for d in dates],
        )
        conn.commit()
    after = conn.execute("SELECT count(*) FROM trading_dates").fetchone()[0]
    return {
        "category": "calendar",
        "source": "derived:kline_1d",
        "derived_dates": len(dates),
        "range": (dates[0], dates[-1]) if dates else None,
        "before": before,
        "after": after,
    }


# --------------------------------------------------------------------------
# financials (+ industry board membership, read in the same pass)
# --------------------------------------------------------------------------


def _financial_row(row: dict[str, Any]) -> tuple:
    return (
        str(row.get("SECURITY_CODE") or "").strip(),
        _date10(row.get("REPORTDATE")),
        _as_float(row.get("TOTAL_OPERATE_INCOME")),
        _as_float(row.get("PARENT_NETPROFIT")),
        _as_float(row.get("XSMLL")),          # 销售毛利率
        None,                                  # net_margin: not in this report
        None,                                  # debt_ratio: balance sheet only
        None,                                  # current_ratio: balance sheet only
        _as_float(row.get("BASIC_EPS")),
        _as_float(row.get("WEIGHTAVG_ROE")),
        _as_float(row.get("BPS")),
        None,                                  # roa: balance sheet only
        _as_float(row.get("YSTZ")),            # 营收同比
        _as_float(row.get("SJLTZ")),           # 净利同比
    )


def sync_financials(conn: sqlite3.Connection, args) -> dict[str, Any]:
    expression = ""
    if args.financial_years:
        expression = f"(REPORTDATE>='{2026 - int(args.financial_years)}-01-01')"
    result = _report(
        "RPT_LICO_FN_CPD",
        filter_expression=_filter(expression),
        max_rows=int(args.max_rows or 0),
    )
    rows = [_financial_row(r) for r in result["rows"]]
    rows = [r for r in rows if r[0] and r[1]]
    universe = _universe_codes(conn)
    in_universe = [r for r in rows if r[0] in universe]
    out_of_universe = len(rows) - len(in_universe)
    rows = in_universe

    written = 0
    if args.apply:
        conn.executemany(
            """
            INSERT INTO financials
                (stock_code, report_date, revenue, net_profit, gross_margin, net_margin,
                 debt_ratio, current_ratio, eps, roe, bvps, roa, revenue_growth,
                 profit_growth, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(stock_code, report_date) DO UPDATE SET
                revenue = excluded.revenue,
                net_profit = excluded.net_profit,
                gross_margin = COALESCE(excluded.gross_margin, financials.gross_margin),
                eps = COALESCE(excluded.eps, financials.eps),
                roe = COALESCE(excluded.roe, financials.roe),
                bvps = COALESCE(excluded.bvps, financials.bvps),
                revenue_growth = COALESCE(excluded.revenue_growth, financials.revenue_growth),
                profit_growth = COALESCE(excluded.profit_growth, financials.profit_growth),
                updated_at = CURRENT_TIMESTAMP
            """,
            rows,
        )
        conn.commit()
        written = len(rows)

    stored = conn.execute("SELECT count(*), count(DISTINCT stock_code), max(report_date) FROM financials").fetchone()
    return {
        "category": "financials",
        "source": result["source"],
        "filter": expression or "none",
        "rows_read": len(result["rows"]),
        "rows_usable": len(rows),
        "skipped_out_of_universe": out_of_universe,
        "written": written,
        "complete": result["complete"],
        "truncated": result["truncated"],
        "failed_pages": result["failed_pages"],
        "stored_rows": stored[0],
        "stored_codes": stored[1],
        "latest_report": stored[2],
        "unmapped_columns": ["net_margin", "debt_ratio", "current_ratio", "roa"],
    }


def sync_blocks_from_finance(conn: sqlite3.Connection, args) -> dict[str, Any]:
    """Industry board membership from the earnings report's own board fields.

    ``stocks.industry`` says which industry a company is in; this says which
    board code carries it, which is what ``sector_manager`` looks up. Each
    financial row names its board, so the membership comes from the same read
    rather than a second full pass.
    """

    expression = ""
    if args.financial_years:
        expression = f"(REPORTDATE>='{2026 - int(args.financial_years)}-01-01')"
    result = _report(
        "RPT_LICO_FN_CPD",
        columns="SECURITY_CODE,SECURITY_NAME_ABBR,REPORTDATE,BOARD_CODE,BOARD_NAME",
        filter_expression=_filter(expression),
        max_rows=int(args.max_rows or 0),
    )

    # Latest report per stock decides its current board.
    latest: dict[str, tuple[str, str, str, str]] = {}
    for row in result["rows"]:
        code = str(row.get("SECURITY_CODE") or "").strip()
        board_code = str(row.get("BOARD_CODE") or "").strip()
        board_name = str(row.get("BOARD_NAME") or "").strip()
        report_date = _date10(row.get("REPORTDATE")) or ""
        if not code or not board_code:
            continue
        current = latest.get(code)
        if current is None or report_date > current[0]:
            latest[code] = (report_date, board_code, board_name, str(row.get("SECURITY_NAME_ABBR") or "").strip())

    universe = _universe_codes(conn)
    latest = {c: v for c, v in latest.items() if c in universe}
    members = [(b, c, n) for c, (_d, b, _bn, n) in latest.items()]
    boards = {b: bn for _c, (_d, b, bn, _n) in latest.items()}

    written = 0
    if args.apply:
        conn.executemany(
            """
            INSERT INTO block_stocks (block_code, stock_code, stock_name, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(block_code, stock_code) DO UPDATE SET
                stock_name = excluded.stock_name, updated_at = CURRENT_TIMESTAMP
            """,
            members,
        )
        counts = dict(conn.execute("SELECT block_code, count(*) FROM block_stocks GROUP BY block_code"))
        conn.executemany(
            """
            INSERT INTO market_blocks (block_code, block_name, block_type, stock_count, updated_at)
            VALUES (?, ?, 'industry', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(block_code, block_type) DO UPDATE SET
                block_name = excluded.block_name,
                stock_count = excluded.stock_count,
                updated_at = CURRENT_TIMESTAMP
            """,
            [(code, name, counts.get(code, 0)) for code, name in boards.items()],
        )
        conn.commit()
        written = len(members)

    stored = conn.execute("SELECT count(*), count(DISTINCT block_code) FROM block_stocks").fetchone()
    return {
        "category": "blocks",
        "source": result["source"],
        "stocks_with_a_board": len(latest),
        "boards": len(boards),
        "written": written,
        "complete": result["complete"],
        "truncated": result["truncated"],
        "stored_rows": stored[0],
        "stored_boards": stored[1],
    }


# --------------------------------------------------------------------------
# fund flow  /  dragon-tiger
# --------------------------------------------------------------------------


def sync_fundflow(conn: sqlite3.Connection, args) -> dict[str, Any]:
    result = _report("RPT_DMSK_TS_STOCKNEW", max_rows=int(args.max_rows or 0))
    rows: list[tuple] = []
    for row in result["rows"]:
        code = str(row.get("SECURITY_CODE") or "").strip()
        trade_date = _date10(row.get("TRADE_DATE"))
        if not code or not trade_date:
            continue
        super_large = (_as_float(row.get("SUPERDEAL_INFLOW")) or 0.0) - (_as_float(row.get("SUPERDEAL_OUTFLOW")) or 0.0)
        large = (_as_float(row.get("BIGDEAL_INFLOW")) or 0.0) - (_as_float(row.get("BIGDEAL_OUTFLOW")) or 0.0)
        ratio = _as_float(row.get("RATIO"))
        rows.append(
            (
                code,
                trade_date,
                str(row.get("SECURITY_NAME_ABBR") or "").strip() or None,
                super_large + large,
                ratio * 100 if ratio is not None else None,
                super_large,
                large,
                None,   # middle_net_inflow: not in this report
                None,   # small_net_inflow: not in this report
                result["source"],
            )
        )

    written = 0
    if args.apply:
        conn.executemany(
            """
            INSERT INTO stock_fund_flow
                (code, trade_date, name, main_net_inflow, main_inflow_percent,
                 super_large_net_inflow, large_net_inflow, middle_net_inflow,
                 small_net_inflow, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(code, trade_date) DO UPDATE SET
                name = excluded.name,
                main_net_inflow = excluded.main_net_inflow,
                main_inflow_percent = excluded.main_inflow_percent,
                super_large_net_inflow = excluded.super_large_net_inflow,
                large_net_inflow = excluded.large_net_inflow,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            rows,
        )
        conn.commit()
        written = len(rows)

    stored = conn.execute("SELECT count(*), count(DISTINCT code), max(trade_date) FROM stock_fund_flow").fetchone()
    return {
        "category": "fundflow",
        "source": result["source"],
        "rows_read": len(result["rows"]),
        "written": written,
        "complete": result["complete"],
        "stored_rows": stored[0],
        "stored_codes": stored[1],
        "latest": stored[2],
        "unmapped_columns": ["middle_net_inflow", "small_net_inflow"],
        "note": "this report is a one-day snapshot; it does not backfill history",
    }


def sync_dragon(conn: sqlite3.Connection, args) -> dict[str, Any]:
    result = _report("RPT_DAILYBILLBOARD_DETAILSNEW", max_rows=int(args.max_rows or 0))
    rows: list[tuple] = []
    for row in result["rows"]:
        code = str(row.get("SECURITY_CODE") or "").strip()
        trade_date = _date10(row.get("TRADE_DATE"))
        reason = str(row.get("EXPLANATION") or "").strip() or None
        if not code or not trade_date or not reason:
            continue
        rows.append(
            (
                code,
                trade_date,
                reason,
                _as_float(row.get("BILLBOARD_BUY_AMT")),
                _as_float(row.get("BILLBOARD_SELL_AMT")),
                _as_float(row.get("BILLBOARD_NET_AMT")),
                None,   # buyer_type: not in this report
            )
        )

    written = 0
    if args.apply:
        conn.executemany(
            """
            INSERT INTO dragon_tiger
                (code, trade_date, reason, buy_amount, sell_amount, net_buy, buyer_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, trade_date, reason) DO UPDATE SET
                buy_amount = excluded.buy_amount,
                sell_amount = excluded.sell_amount,
                net_buy = excluded.net_buy
            """,
            rows,
        )
        conn.commit()
        written = len(rows)

    stored = conn.execute("SELECT count(*), count(DISTINCT code), max(trade_date) FROM dragon_tiger").fetchone()
    return {
        "category": "dragon",
        "source": result["source"],
        "rows_read": len(result["rows"]),
        "written": written,
        "complete": result["complete"],
        "stored_rows": stored[0],
        "stored_codes": stored[1],
        "latest": stored[2],
        "unmapped_columns": ["buyer_type"],
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    chosen = _selected(args.what)
    reports: list[dict[str, Any]] = []

    with contextlib.closing(sqlite3.connect(str(DB_PATH))) as conn:
        if "calendar" in chosen:
            reports.append(sync_calendar(conn, apply=args.apply))
        if "financials" in chosen:
            reports.append(sync_financials(conn, args))
        if "blocks" in chosen:
            reports.append(sync_blocks_from_finance(conn, args))
        if "fundflow" in chosen:
            reports.append(sync_fundflow(conn, args))
        if "dragon" in chosen:
            reports.append(sync_dragon(conn, args))

    if args.json:
        print(json.dumps({"applied": bool(args.apply), "reports": reports}, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"sqlite : {DB_PATH}")
    print(f"mode   : {'apply' if args.apply else 'report only'}")
    print()
    for item in reports:
        print(f"[{item['category']}] source={item['source']}")
        for key, value in item.items():
            if key in ("category", "source"):
                continue
            print(f"    {key:22s} {value}")
        print()
    if not args.apply:
        print("nothing written; re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
