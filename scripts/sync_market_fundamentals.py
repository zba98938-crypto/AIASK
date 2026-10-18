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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
for _rel in ("packages/akshare-mcp/src", "packages/aiask-quant-core/src", "packages/strategy-factory/src"):
    _path = str(REPO_ROOT / _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _db_sync_common import DB_PATH  # noqa: E402
from akshare_mcp.data_source.eastmoney_datacenter import fetch_report  # noqa: E402

CATEGORIES = ("calendar", "kline_change_pct", "financials", "fundflow", "dragon", "blocks", "margin", "margin_detail", "north")

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
    parser.add_argument(
        "--margin-days",
        type=int,
        default=60,
        help="how many days back the SSE margin endpoint is queried (default 60)",
    )
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


def _register_completeness(
    conn: sqlite3.Connection,
    data_key: str,
    *,
    row_count: int,
    as_of_date: Optional[str],
    detail: dict[str, Any],
) -> dict[str, Any]:
    """Record this dataset in ``tdx_data_completeness``, which is the gate.

    Having rows is not the same as being usable here: every reader of
    `margin_market_flow`, `stock_fund_flow` and `block_stocks` first consults
    `_get_market_data_quality_gate`, which reads this registry and blocks with
    ``completeness_missing`` when there is no row for the key. Writing data
    without registering it leaves the table populated and still unreadable —
    which is exactly what a first attempt at the margin sync produced.

    ``status`` follows the project service's rule (``ok`` when the table holds
    rows, ``missing`` when it does not), and the limitations go in ``detail``
    where the reader passes them through to the caller.
    """

    conn.execute(
        """
        INSERT INTO tdx_data_completeness (data_key, status, as_of_date, row_count, detail, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT (data_key) DO UPDATE SET
            status = excluded.status,
            as_of_date = excluded.as_of_date,
            row_count = excluded.row_count,
            detail = excluded.detail,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            data_key,
            "ok" if row_count else "missing",
            as_of_date,
            int(row_count or 0),
            json.dumps(detail, ensure_ascii=False, default=str),
        ),
    )
    return {"data_key": data_key, "status": "ok" if row_count else "missing", "row_count": row_count}


def _financial_row(row: dict[str, Any]) -> tuple:
    """Map an F10 main-financial-indicator row onto ``financials``.

    One report carries all fourteen columns, so a row cannot mix two report
    periods. The source it replaces (`RPT_LICO_FN_CPD`) has no balance-sheet or
    profitability fields at all, which is why `net_margin`, `debt_ratio`,
    `current_ratio` and `roa` were NULL for every one of the 119,819 rows.

    Units are not free here, and they differ per column:

    * ``debt_ratio`` is stored as a **fraction** because that is how every
      consumer reads it (`max(1.0 - debt_ratio, 0.0)` in
      `factor_calculator/fundamental.py`, and the documented example
      ``"debt_ratio": 0.21`` in `backtest/single_factor_strategy.py`). The report
      states ``ZCFZL`` as a percentage, so it is divided by 100.
    * the margins and returns stay as **percentages**, which is how the report
      states them and how the quality factor divides them (``roe / 30``,
      ``roa / 15``, ``gross_margin / 50``).
    * ``current_ratio`` is a plain ratio (1.92, not 192).
    """

    debt_percent = _as_float(row.get("ZCFZL"))
    return (
        str(row.get("SECURITY_CODE") or "").strip(),
        _date10(row.get("REPORT_DATE")),
        _as_float(row.get("TOTALOPERATEREVE")),     # 营业总收入
        _as_float(row.get("PARENTNETPROFIT")),      # 归母净利润
        _as_float(row.get("XSMLL")),                # 销售毛利率 %
        _as_float(row.get("XSJLL")),                # 销售净利率 %
        None if debt_percent is None else debt_percent / 100.0,   # 资产负债率 -> fraction
        _as_float(row.get("LD")),                   # 流动比率
        _as_float(row.get("EPSJB")),                # 基本每股收益
        _as_float(row.get("ROEJQ")),                # 加权净资产收益率 %
        _as_float(row.get("BPS")),                  # 每股净资产
        _as_float(row.get("ZZCJLL")),               # 总资产净利率 %
        _as_float(row.get("TOTALOPERATEREVETZ")),   # 营收同比 %
        _as_float(row.get("PARENTNETPROFITTZ")),    # 净利同比 %
    )


def sync_financials(conn: sqlite3.Connection, args) -> dict[str, Any]:
    expression = ""
    if args.financial_years:
        expression = f"(REPORT_DATE>='{2026 - int(args.financial_years)}-01-01')"
    result = _report(
        "RPT_F10_FINANCE_MAINFINADATA",
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
                net_margin = COALESCE(excluded.net_margin, financials.net_margin),
                debt_ratio = COALESCE(excluded.debt_ratio, financials.debt_ratio),
                current_ratio = COALESCE(excluded.current_ratio, financials.current_ratio),
                eps = COALESCE(excluded.eps, financials.eps),
                roe = COALESCE(excluded.roe, financials.roe),
                bvps = COALESCE(excluded.bvps, financials.bvps),
                roa = COALESCE(excluded.roa, financials.roa),
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
        # Nothing is left unmapped: `RPT_F10_FINANCE_MAINFINADATA` carries all
        # fourteen columns, which is why this moved off the earnings report.
        "unmapped_columns": [],
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
    registration = None
    if args.apply and stored[0]:
        # The gate for this key spans two tables, so `as_of_date` comes from the
        # board table's own timestamp column — the one the registry spec names.
        as_of = conn.execute("SELECT max(updated_at) FROM market_blocks").fetchone()[0]
        registration = _register_completeness(
            conn,
            "sync_sector_basic",
            row_count=int(stored[0]),
            as_of_date=as_of,
            detail={
                "tables": [
                    {"table": "market_blocks", "date_column": "updated_at", "where": ""},
                    {"table": "block_stocks", "date_column": "updated_at", "where": ""},
                ],
                "source": result["source"],
                "coverage_note": "industry board membership from the F10 earnings report",
            },
        )
        conn.commit()
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
        "completeness_registered": registration,
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
    registration = None
    if args.apply and stored[0]:
        registration = _register_completeness(
            conn,
            "stock_fund_flow",
            row_count=int(stored[0]),
            as_of_date=stored[2],
            detail={
                "table": "stock_fund_flow",
                "date_column": "trade_date",
                "where": "",
                "source": result["source"],
                "coverage_note": (
                    "one trading day per run; the report is a daily snapshot and "
                    "does not backfill history"
                ),
            },
        )
        conn.commit()
    return {
        "category": "fundflow",
        "source": result["source"],
        "rows_read": len(result["rows"]),
        "written": written,
        "complete": result["complete"],
        "stored_rows": stored[0],
        "stored_codes": stored[1],
        "latest": stored[2],
        "completeness_registered": registration,
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


def _price_limit_percent(stock_code: str, stock_name: str) -> float:
    """The daily move limit for a board, in percent.

    Used only to *flag* bars that moved further than a trade can: on an unadjusted
    series an ex-dividend or split shows up as a gap the limit would not permit, so
    a value beyond it is almost certainly a corporate action rather than a price
    move. The flag is reported, not enforced — the computed number is the true
    bar-to-bar change of the series as stored.
    """

    code = str(stock_code or "")
    if "ST" in str(stock_name or "").upper():
        return 5.0
    if code.startswith(("30", "688")):
        return 20.0
    if code.startswith(("4", "8", "92")):
        return 30.0
    return 10.0


def backfill_kline_change_pct(conn: sqlite3.Connection, *, apply: bool) -> dict[str, Any]:
    """Derive ``kline_1d.change_pct`` from the series itself.

    No kline channel publishes it — Sina and Tencent both return six fields, date
    through volume — and the column held values for 2,433 of 1,738,879 rows. It is
    derivable from the bars already stored, which is what this does: the previous
    close of the same code, in time order.

    **The basis is unadjusted.** `kline_1d` stores 不复权 prices, so on an
    ex-dividend day the derived figure includes the corporate action and can exceed
    the board's daily limit. Those rows are counted and reported rather than
    corrected: the number is the real change of the stored series, and inventing a
    "clean" one would hide the event that caused it.

    The first bar of each code has no predecessor. It stays NULL — not 0, which
    would read as "this bar did not move" — and is counted.
    """

    before = conn.execute("SELECT count(change_pct) FROM kline_1d").fetchone()[0]
    total = conn.execute("SELECT count(*) FROM kline_1d").fetchone()[0]
    if not apply:
        return {
            "category": "kline_change_pct",
            "source": "derived:kline_1d(prev close, same code)",
            "mode": "report only",
            "rows": total,
            "with_value_before": before,
        }

    # One row per (code, date), and the canonical timestamp wins. `time` is a
    # timestamp but 99 rows in this table hold a *bare date* — a different primary
    # key under `PRIMARY KEY (time, code)`, so both a date and its timestamped form
    # can coexist for the same day. Ordering on the raw column puts the shorter
    # string first, which interleaved two securities' numbers and produced changes
    # of ±100% from a table that was really just doubling back on itself. Ordering
    # on the date, and taking the longest form per date, is immune to that.
    # Deduplicate **first**, then lag. Putting `ROW_NUMBER` and `LAG` in one SELECT
    # does not do this: window functions are evaluated over the whole input before
    # the outer `WHERE rn = 1` removes anything, so the lag of a kept row still sees
    # the discarded duplicates as its predecessor — which is how a date with a bare
    # row underneath it produced +102% instead of +0.66%.
    conn.execute(
        """
        WITH dedup AS (
            SELECT code, time, close, substr(time, 1, 10) AS day,
                   ROW_NUMBER() OVER (
                       PARTITION BY code, substr(time, 1, 10) ORDER BY length(time) DESC
                   ) AS rn
            FROM kline_1d
        ), series AS (
            SELECT code, time, LAG(close) OVER (PARTITION BY code ORDER BY day) AS prev_close
            FROM dedup WHERE rn = 1
        )
        UPDATE kline_1d AS k
        SET change_pct = ROUND((k.close - s.prev_close) / s.prev_close * 100, 6)
        FROM series AS s
        WHERE k.code = s.code AND k.time = s.time
          AND s.prev_close IS NOT NULL AND s.prev_close != 0 AND k.close IS NOT NULL
        """
    )
    conn.commit()

    # A row that lost the per-date contest is excluded from the UPDATE above, so
    # whatever it held before survives — and if a previous run computed it from an
    # interleaved order, it holds a fabricated figure. Clear it rather than leave a
    # number that no longer corresponds to anything: an absent value is honest, a
    # stale one is not.
    cleared = conn.execute(
        """
        UPDATE kline_1d SET change_pct = NULL
        WHERE change_pct IS NOT NULL AND (code, time) IN (
            SELECT code, time FROM (
                SELECT code, time,
                       ROW_NUMBER() OVER (PARTITION BY code, substr(time,1,10)
                                          ORDER BY length(time) DESC) AS rn
                FROM kline_1d
            ) WHERE rn > 1
        )
        """
    ).rowcount
    conn.commit()
    off_format = conn.execute(
        "SELECT count(*) FROM kline_1d WHERE length(time) <= 10"
    ).fetchone()[0]

    after = conn.execute("SELECT count(change_pct) FROM kline_1d").fetchone()[0]
    first_bar_nulls = conn.execute(
        "SELECT count(*) FROM kline_1d WHERE change_pct IS NULL"
    ).fetchone()[0]

    # How many bars moved further than their board permits — the signature of a
    # corporate action on an unadjusted series.
    moved, flagged = 0, 0
    for code, name, pct in conn.execute(
        "SELECT k.code, COALESCE(s.stock_name,''), k.change_pct FROM kline_1d k "
        "LEFT JOIN stocks s ON s.stock_code = k.code WHERE k.change_pct IS NOT NULL"
    ):
        moved += 1
        if abs(float(pct)) > _price_limit_percent(code, name) + 0.01:
            flagged += 1

    return {
        "category": "kline_change_pct",
        "source": "derived:kline_1d(prev close, same code)",
        "basis": "unadjusted",
        "rows": total,
        "with_value_before": before,
        "with_value_after": after,
        "null_first_bar": first_bar_nulls,
        "off_format_timestamps": off_format,
        "change_pct_cleared_on_duplicate_dates": cleared,
        "off_format_note": (
            "time 是时间戳列，但有行只存了裸日期——主键 (time, code) 下它与带时间戳的"
            "同日行是两个不同的键，于是同一天两行并存。推导已按日期去重、取规范形态，"
            "这些行的存在本身需单独处理"
        ),
        "beyond_daily_limit": flagged,
        "beyond_daily_limit_note": (
            "不复权序列上的超限跳变几乎必然是除权/送转而非成交；已计数未修正"
        ),
    }


def sync_margin(conn: sqlite3.Connection, args) -> dict[str, Any]:
    """Fill ``margin_market_flow`` from 上交所's own endpoint.

    Scope is Shanghai only: 深交所 returns 403 through this host's proxy, and the
    SSE endpoint ignores its documented ``stockCode`` parameter, so there is no
    per-stock detail to be had either. Rows carry ``exchange_id='SSE'`` — the
    table's primary key is ``(trade_date, exchange_id)`` precisely so the two
    exchanges can coexist, and a consumer that sums what it finds must be able to
    see that only one of them is present. The reader is told, and so is this
    report.
    """

    from akshare_mcp.data_source.sse_margin import MISSING_EXCHANGE_ID, fetch_margin_market

    end = datetime.now().strftime("%Y%m%d")
    begin = (datetime.now() - timedelta(days=max(1, int(args.margin_days)))).strftime("%Y%m%d")
    result = fetch_margin_market(begin, end)
    rows = result["rows"]

    written = 0
    if args.apply:
        conn.executemany(
            """
            INSERT INTO margin_market_flow
                (trade_date, exchange_id, rzye, rzmre, rzche, rqye, rqmcl, rqyl, rzrqye,
                 source, source_priority, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(trade_date, exchange_id) DO UPDATE SET
                rzye = excluded.rzye, rzmre = excluded.rzmre, rzche = excluded.rzche,
                rqye = excluded.rqye, rqmcl = excluded.rqmcl, rqyl = excluded.rqyl,
                rzrqye = excluded.rzrqye, source = excluded.source,
                source_priority = excluded.source_priority, updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    row["trade_date"], row["exchange_id"], row["rzye"], row["rzmre"], row["rzche"],
                    row["rqye"], row["rqmcl"], row["rqyl"], row["rzrqye"],
                    # Says what it is, including what it is not.
                    f"{result['source']} ({result['exchange_id']} only; "
                    f"{MISSING_EXCHANGE_ID} unavailable)",
                    "sse_exchange",
                )
                for row in rows
            ],
        )
        conn.commit()
        written = len(rows)

    stored = conn.execute(
        "SELECT count(*), count(DISTINCT exchange_id), min(trade_date), max(trade_date) "
        "FROM margin_market_flow"
    ).fetchone()
    registration = None
    if args.apply and stored[0]:
        registration = _register_completeness(
            conn,
            "margin_market_flow",
            row_count=int(stored[0]),
            as_of_date=stored[3],
            detail={
                "table": "margin_market_flow",
                "date_column": "trade_date",
                "where": "",
                "source": result["source"],
                # The gate answers "is this table populated and fresh"; this says
                # how much of the market it covers.
                "exchanges_present": [result["exchange_id"]],
                "exchanges_missing": result["missing_exchanges"],
                "coverage_note": (
                    "Shanghai only. Do not read these figures as a market total."
                ),
            },
        )
        conn.commit()
    return {
        "category": "margin",
        "source": result["source"],
        "exchange_id": result["exchange_id"],
        "missing_exchanges": result["missing_exchanges"],
        "window": f"{begin}..{end}",
        "rows_read": len(rows),
        "written": written,
        "complete": result["complete"],
        "failed_pages": result["failed_pages"],
        "stored_rows": stored[0],
        "stored_exchanges": stored[1],
        "stored_range": (stored[2], stored[3]),
        "completeness_registered": registration,
        "per_stock_detail": (
            "see the margin_detail category — per-security detail comes from East "
            "money's RPTA_WEB_RZRQ_GGMX, not from this exchange endpoint"
        ),
    }


def sync_margin_detail(conn: sqlite3.Connection, args) -> dict[str, Any]:
    """Fill ``margin_detail`` from East money's per-security margin report.

    The exchange endpoints cannot do this — 上交所 answers at market level and
    ignores its ``stockCode`` parameter, 深交所 returns 403 — so this table had no
    source at all. ``RPTA_WEB_RZRQ_GGMX`` has one row per security per day for
    both exchanges and 北交所.

    **Both 沪 and 深 must be written or nothing is.** ``_get_margin_detail_suffix_
    coverage`` blocks the whole table when either suffix is absent, so a one-sided
    write leaves rows present and the reader returning an empty list — harder to
    notice than an empty table, which is why a partial read reports failure here
    instead of writing what it has.
    """

    from akshare_mcp.data_source.eastmoney_margin_detail import fetch_margin_detail

    on_or_after = (datetime.now() - timedelta(days=max(1, int(args.margin_days)))).strftime("%Y-%m-%d")
    result = fetch_margin_detail(on_or_after=on_or_after, max_rows=int(args.max_rows or 0))
    rows = result["rows"]

    written = 0
    refusal = ""
    if not result["complete"]:
        refusal = (
            "refusing to write: a required market is missing "
            f"({result['missing_required_markets'] or result['failed_markets']}); "
            "the reader blocks the table when either .SH or .SZ is absent"
        )
    elif args.apply:
        conn.executemany(
            """
            INSERT INTO margin_detail
                (trade_date, ts_code, rzye, rqye, rqyl, rzrqye, source, source_priority, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'eastmoney_datacenter', CURRENT_TIMESTAMP)
            ON CONFLICT(trade_date, ts_code) DO UPDATE SET
                rzye = excluded.rzye, rqye = excluded.rqye, rqyl = excluded.rqyl,
                rzrqye = excluded.rzrqye, source = excluded.source,
                source_priority = excluded.source_priority, updated_at = CURRENT_TIMESTAMP
            """,
            rows,
        )
        conn.commit()
        written = len(rows)

    stored = conn.execute(
        "SELECT count(*), count(DISTINCT ts_code), min(trade_date), max(trade_date) "
        "FROM margin_detail"
    ).fetchone()
    by_suffix = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT CASE WHEN ts_code LIKE '%.SH' THEN 'SH' WHEN ts_code LIKE '%.SZ' THEN 'SZ' "
            "ELSE 'OTHER' END AS suffix, count(*) FROM margin_detail GROUP BY suffix"
        )
    }
    registration = None
    if args.apply and stored[0]:
        registration = _register_completeness(
            conn,
            "margin_detail",
            row_count=stored[0],
            as_of_date=stored[3],
            detail={
                "table": "margin_detail",
                "date_column": "trade_date",
                "where": "",
                "source": result["source"],
                "rows_by_suffix": by_suffix,
                # The reader insists on both, so say plainly whether both are there.
                "required_suffixes_present": all(by_suffix.get(s) for s in ("SH", "SZ")),
                "not_published_by_the_report": result["not_published"],
                "latest_by_market": result["latest_by_market"],
                "coverage_note": (
                    "Per-security margin balances. 深证 publishes a day behind 沪证. "
                    "The daily buy/repay amounts are not in this report and are NULL."
                ),
            },
        )
        conn.commit()

    return {
        "category": "margin_detail",
        "source": result["source"],
        "window_from": on_or_after,
        "rows_read": len(rows),
        "written": written,
        "refused": refusal,
        "complete": result["complete"],
        "per_market": result["per_market"],
        "latest_by_market": result["latest_by_market"],
        "failed_markets": result["failed_markets"],
        "stored_rows": stored[0],
        "stored_codes": stored[1],
        "stored_range": (stored[2], stored[3]),
        "stored_by_suffix": by_suffix,
        "not_published": result["not_published"],
        "completeness_registered": registration,
    }


def sync_north(conn: sqlite3.Connection, args) -> dict[str, Any]:
    """Fill ``north_fund_flow`` with what that report still publishes.

    Southbound (港股通) updates daily and is written. Northbound has carried no
    value in any recent row — real-time northbound disclosure was discontinued —
    so those columns are NULL and the registry says why. Writing 0.0 would report
    a flat market, and the reader used to coerce the NULLs to 0.0 itself; that is
    fixed in ``get_north_fund_history``.

    146 rows from the akshare era remain untouched: they predate the change and are
    real. The upsert keeps an existing north value when the new read has none,
    rather than letting a "not published" response erase a published one.
    """

    from akshare_mcp.data_source.eastmoney_mutual import fetch_mutual_flows

    on_or_after = (datetime.now() - timedelta(days=max(1, int(args.margin_days)))).strftime("%Y-%m-%d")
    result = fetch_mutual_flows(on_or_after=on_or_after, max_rows=int(args.max_rows or 0))
    rows = result["rows"]

    written = 0
    if args.apply and rows:
        conn.executemany(
            """
            INSERT INTO north_fund_flow
                (trade_date, north_money, south_money, net_amount, ggt_ss, ggt_sz, hgt, sgt,
                 source, source_priority, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'eastmoney_datacenter', CURRENT_TIMESTAMP)
            ON CONFLICT(trade_date) DO UPDATE SET
                south_money = excluded.south_money,
                net_amount = excluded.net_amount,
                ggt_ss = excluded.ggt_ss,
                ggt_sz = excluded.ggt_sz,
                -- Keep a published northbound figure rather than overwriting it with
                -- a NULL that means "no longer published".
                north_money = COALESCE(excluded.north_money, north_fund_flow.north_money),
                hgt = COALESCE(excluded.hgt, north_fund_flow.hgt),
                sgt = COALESCE(excluded.sgt, north_fund_flow.sgt),
                source = excluded.source,
                source_priority = excluded.source_priority,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    row["trade_date"], row["north_money"], row["south_money"], row["net_amount"],
                    row["ggt_ss"], row["ggt_sz"], row["hgt"], row["sgt"],
                    f"{result['source']} (southbound only; {result['discontinued_reason']})",
                )
                for row in rows
            ],
        )
        conn.commit()
        written = len(rows)

    stored = conn.execute(
        "SELECT count(*), count(north_money), count(south_money), min(trade_date), max(trade_date) "
        "FROM north_fund_flow"
    ).fetchone()
    registration = None
    if args.apply and stored[0]:
        registration = _register_completeness(
            conn,
            "north_fund_flow",
            row_count=stored[0],
            as_of_date=stored[4],
            detail={
                "table": "north_fund_flow",
                "date_column": "trade_date",
                "where": "",
                "source": result["source"],
                "published_channels": result["published_channels"],
                "discontinued_channels": result["discontinued_channels"],
                "discontinued_reason": result["discontinued_reason"],
                "rows_with_north_money": stored[1],
                "rows_with_south_money": stored[2],
                "coverage_note": (
                    "Southbound (港股通) is current. Northbound columns are NULL for "
                    "recent dates because the disclosure was discontinued, not because "
                    "the feed is missing. Do not read them as zero."
                ),
            },
        )
        conn.commit()

    return {
        "category": "north",
        "source": result["source"],
        "window_from": on_or_after,
        "days_read": result["days"],
        "written": written,
        "complete": result["complete"],
        "duplicate_rows": result["duplicate_rows"],
        "published_channels": result["published_channels"],
        "discontinued_channels": result["discontinued_channels"],
        "stored_rows": stored[0],
        "stored_with_north_money": stored[1],
        "stored_with_south_money": stored[2],
        "stored_range": (stored[3], stored[4]),
        "completeness_registered": registration,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    chosen = _selected(args.what)
    reports: list[dict[str, Any]] = []

    with contextlib.closing(sqlite3.connect(str(DB_PATH))) as conn:
        if "calendar" in chosen:
            reports.append(sync_calendar(conn, apply=args.apply))
        if "kline_change_pct" in chosen:
            reports.append(backfill_kline_change_pct(conn, apply=args.apply))
        if "financials" in chosen:
            reports.append(sync_financials(conn, args))
        if "blocks" in chosen:
            reports.append(sync_blocks_from_finance(conn, args))
        if "fundflow" in chosen:
            reports.append(sync_fundflow(conn, args))
        if "dragon" in chosen:
            reports.append(sync_dragon(conn, args))
        if "margin" in chosen:
            reports.append(sync_margin(conn, args))
        if "margin_detail" in chosen:
            reports.append(sync_margin_detail(conn, args))
        if "north" in chosen:
            reports.append(sync_north(conn, args))

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
