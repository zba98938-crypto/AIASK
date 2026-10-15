#!/usr/bin/env python3
"""Populate the ``stocks`` table with the whole A-share market.

Why this exists
---------------
The factories had been running on a 15-code default list, and the ``stocks``
table held 165 rows, while the A-share market has more than 5500 listed stocks.
Nothing in the tree could enumerate the market: Tushare needs a token, the TDX
local vipdoc needs an installed client, ``push2.eastmoney.com`` is unreachable
behind this host's proxy, and the TDX/TQCenter bridge needs a ``tqcenter`` module
that is not installed. Sina's Market Center is reachable, so it is the listing
source of record.

Existing rows are updated in place, never deleted: ``stocks`` carries derived
columns (industry, market_cap, tdx_*) that this listing does not produce, and
overwriting them with NULLs would destroy data a different sync path owns.

Usage::

    python scripts/sync_market_universe.py                # report, write nothing
    python scripts/sync_market_universe.py --apply
    python scripts/sync_market_universe.py --apply --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _rel in ("packages/akshare-mcp/src", "packages/aiask-quant-core/src", "packages/strategy-factory/src"):
    _path = str(REPO_ROOT / _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _db_sync_common import DB_PATH  # noqa: E402
from akshare_mcp.data_source.sina_listing import (  # noqa: E402
    fetch_all_a_share_symbols,
    fetch_industry_membership,
)
from akshare_mcp.data_source.eastmoney_listing import (  # noqa: E402
    fetch_industry_classification,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the rows; without this nothing changes")
    parser.add_argument(
        "--with-industry",
        action="store_true",
        help="also fetch the industry classification",
    )
    parser.add_argument(
        "--industry-source",
        choices=("auto", "eastmoney", "sina"),
        default="auto",
        help=(
            "which classification to use. 'eastmoney' is the F10 report: 27 "
            "level-1 industries over the whole market in about 50 requests. "
            "'sina' is the 49-node board list, which classifies about half the "
            "market and counts 次新股 as an industry. 'auto' prefers eastmoney "
            "and says so when it falls back"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser.parse_args(argv)


def _resolve_industry(source: str) -> dict:
    """Return ``{code: {"industry", "sector"}}`` plus how it was obtained.

    The two sources use different taxonomies. They are never merged into one
    column silently: the result carries the source name, and a fallback is
    reported rather than passed off as the preferred classification.
    """

    if source in ("auto", "eastmoney"):
        try:
            result = fetch_industry_classification()
            return {
                "by_code": result["by_code"],
                "source": result["source"],
                "rows": result["rows"],
                "pages": result["pages"],
                "missing": result["missing_industry"],
                "complete": result["complete"],
                "failed_pages": result["failed_pages"],
                "fallback": False,
            }
        except Exception as exc:
            if source == "eastmoney":
                raise
            print(
                f"[industry] eastmoney F10 unavailable ({type(exc).__name__}: {exc}); "
                f"falling back to Sina's board list, which covers part of the market",
                file=sys.stderr,
            )

    result = fetch_industry_membership()
    return {
        "by_code": {code: {"industry": value.get("industry"), "sector": None}
                    for code, value in result["by_code"].items()},
        "source": "sina:Market_Center[industry nodes]",
        "rows": len(result["by_code"]),
        "pages": result["industries"],
        "missing": 0,
        "complete": not result["failed_industries"],
        "failed_pages": result["failed_industries"],
        "fallback": source == "auto",
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    result = fetch_all_a_share_symbols()
    symbols = result["symbols"]
    listed_codes = {item["code"] for item in symbols}

    industry: dict = {"by_code": {}, "source": None}
    if args.with_industry:
        industry = _resolve_industry(args.industry_source)

    inserted = updated = 0
    with contextlib.closing(sqlite3.connect(str(DB_PATH))) as conn:
        conn.row_factory = sqlite3.Row
        before = {row["stock_code"] for row in conn.execute("SELECT stock_code FROM stocks")}

        if args.apply:
            for item in symbols:
                code = item["code"]
                row = conn.execute("SELECT stock_code FROM stocks WHERE stock_code = ?", (code,)).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO stocks
                            (stock_code, stock_name, market, market_cap, pe_ratio, pb_ratio,
                             industry, sector, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (
                            code,
                            item["name"],
                            item["market"],
                            item.get("market_cap"),
                            item.get("pe_ratio"),
                            item.get("pb_ratio"),
                            (industry["by_code"].get(code) or {}).get("industry"),
                            (industry["by_code"].get(code) or {}).get("sector"),
                        ),
                    )
                    inserted += 1
                else:
                    # Only the columns this listing actually owns. NULL fields are
                    # skipped so a source that does not carry a value cannot erase
                    # one another sync path already filled.
                    conn.execute(
                        """
                        UPDATE stocks
                        SET stock_name = ?,
                            market = ?,
                            market_cap = COALESCE(?, market_cap),
                            pe_ratio = COALESCE(?, pe_ratio),
                            pb_ratio = COALESCE(?, pb_ratio),
                            industry = COALESCE(?, industry),
                            sector = COALESCE(?, sector),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE stock_code = ?
                        """,
                        (
                            item["name"],
                            item["market"],
                            item.get("market_cap"),
                            item.get("pe_ratio"),
                            item.get("pb_ratio"),
                            (industry["by_code"].get(code) or {}).get("industry"),
                            (industry["by_code"].get(code) or {}).get("sector"),
                            code,
                        ),
                    )
                    updated += 1
            conn.commit()
        after = {row["stock_code"] for row in conn.execute("SELECT stock_code FROM stocks")}

    report = {
        "source": result["source"],
        "complete": result["complete"],
        "failed_pages": result["failed_pages"],
        "pages": result["pages"],
        "transports": result["transports"],
        "listed": len(symbols),
        "before": len(before),
        "after": len(after),
        "inserted": inserted,
        "updated": updated,
        "listed_but_absent_before": len({item["code"] for item in symbols} - before),
        "in_db_but_not_listed": sorted(before - {item["code"] for item in symbols})[:20],
        "industry_source": industry.get("source"),
        # Coverage is measured against this universe, not against the report's own
        # row count: eastmoney returns every security it knows (24,812, including
        # 新三板), so "24812 mapped" said 445% of a 5,575-code universe.
        "industry_mapped": len(listed_codes & set(industry["by_code"])),
        "industry_unclassified_in_universe": sum(
            1 for code in listed_codes if (industry["by_code"].get(code) or {}).get("industry") is None
        ),
        "industry_covered_now": sum(
            1 for code in listed_codes
            if (industry["by_code"].get(code) or {}).get("industry") is not None
        ),
        "industry_rows": industry.get("rows"),
        "industry_pages": industry.get("pages"),
        "industry_unclassified": industry.get("missing"),
        "industry_complete": industry.get("complete"),
        "industry_failed": industry.get("failed_pages") or [],
        "industry_fallback": industry.get("fallback", False),
        "applied": bool(args.apply),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"listing source  : {report['source']}")
    print(f"listing complete: {report['complete']}  (pages={report['pages']}, "
          f"failed={report['failed_pages'] or 'none'}, transports={'/'.join(report['transports'])})")
    print(f"listed symbols  : {report['listed']}")
    print(f"stocks table    : {report['before']} -> {report['after']}")
    if args.apply:
        print(f"inserted/updated: {report['inserted']} / {report['updated']}")
    else:
        print(f"would add       : {report['listed_but_absent_before']} rows "
              "(re-run with --apply to write)")
    if report["in_db_but_not_listed"]:
        print(f"in DB, not listed: {len(report['in_db_but_not_listed'])} "
              f"(e.g. {report['in_db_but_not_listed']})")
    if args.with_industry:
        covered = report["industry_covered_now"]
        universe_size = len(listed_codes)
        print(f"industry source  : {report['industry_source']}"
              + ("  (fallback)" if report["industry_fallback"] else ""))
        print(f"industry coverage: {covered}/{universe_size} of this universe "
              f"({100 * covered / max(1, universe_size):.1f}%); the report itself "
              f"returned {report['industry_rows']} rows over {report['industry_pages']} page(s)")
        if report["industry_unclassified_in_universe"]:
            print(f"unclassified     : {report['industry_unclassified_in_universe']} of this "
                  "universe have no classification in the source")
        if report["industry_failed"]:
            print(f"industry failures: {report['industry_failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
