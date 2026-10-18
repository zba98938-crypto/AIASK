#!/usr/bin/env python3
"""Compare the stored financial indicators against Sina's, column by column.

``financials`` has one provider. Sina publishes the same indicators from a different
vendor, which looks like a fallback — until the two are compared. Several columns
agree to the last decimal (they come from a balance-sheet identity and have one
possible definition) and several do not, because a published indicator has more
than one convention: 加权 versus 摊薄 basis, 单季 versus 累计, as-reported versus
restated. Substituting one for the other would silently change what a column means
part-way through a series, which is the defect this project already fixed once for
industry classifications.

So this is a **diagnostic, not a fallback**. Run it when the East money report
changes shape: it distinguishes "the numbers moved" from "the vendor changed".

Usage::

    python scripts/check_financials_agreement.py --codes 600519,000001,300750
    python scripts/check_financials_agreement.py --sample 40 --json
"""

from __future__ import annotations

import argparse
import datetime
import json
import random
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "akshare-mcp" / "src"))

from _db_sync_common import DB_PATH  # noqa: E402
from akshare_mcp.data_source.sina_financials import (  # noqa: E402
    INDICATOR_MAP,
    fetch_financial_indicators,
)

# A published ratio and its restatement differ in the third decimal at most; a
# difference beyond this is a convention change, not rounding.
TOLERANCE = 1e-3

# Columns whose two sources match on every sampled period. Only these could be
# substituted; the rest are labelled so a reader does not have to re-derive it.
# Recompute by running this script rather than trusting the list.
MATCHING_HINT = {"roe", "roa", "debt_ratio"}


#: East money report field -> (field, conversion to the stored convention).
#: `debt_ratio` is the one that differs: the report states ZCFZL as a percentage
#: (21.3) while the column is read as a fraction, which `sync_market_fundamentals`
#: divides by 100 on write. Comparing the raw field against a converted value
#: reported 0 of 4 matches on every symbol.
EASTMONEY_FIELDS: dict[str, tuple[str, str]] = {}


def _stored(conn: sqlite3.Connection, code: str, columns: list[str], year: int) -> dict[str, list[Any]]:
    """Rows for one report year. The year bound follows ``--year``.

    This was pinned to ``>= '2025-01-01'``, so ``--year 2024`` reported "no
    overlapping periods" for every symbol — a tool result about the tool, not
    about the data.
    """

    return {
        str(row[0]): list(row[1:])
        for row in conn.execute(
            f"SELECT report_date, {','.join(columns)} FROM financials "
            f"WHERE stock_code = ? AND report_date >= ? AND report_date <= ?",
            (code, f"{int(year)}-01-01", f"{int(year)}-12-31"),
        )
    }


def run(codes: list[str], year: int) -> dict[str, Any]:
    columns = [column for column, _ in INDICATOR_MAP.values()]
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    stats = {column: {"match": 0, "compared": 0, "examples": []} for column in columns}
    unreadable: list[dict[str, str]] = []

    for code in codes:
        stored = _stored(conn, code, columns, year)
        try:
            remote = fetch_financial_indicators(code, year)
        except Exception as exc:
            unreadable.append({"code": code, "error": f"{type(exc).__name__}: {str(exc)[:70]}"})
            continue
        for report_date in sorted(set(stored) & set(remote)):
            for index, column in enumerate(columns):
                local_value, remote_value = stored[report_date][index], remote[report_date].get(column)
                if local_value is None or remote_value is None:
                    continue
                stats[column]["compared"] += 1
                if abs(float(local_value) - float(remote_value)) <= TOLERANCE * max(1.0, abs(float(local_value))):
                    stats[column]["match"] += 1
                elif len(stats[column]["examples"]) < 3:
                    stats[column]["examples"].append(
                        {"code": code, "date": report_date, "stored": local_value, "sina": remote_value}
                    )
    conn.close()

    for column, entry in stats.items():
        entry["rate"] = round(entry["match"] / entry["compared"], 4) if entry["compared"] else None
    return {
        "as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
        "codes": len(codes),
        "year": year,
        "stats": stats,
        "unreadable": unreadable,
        "substitutable": sorted(
            column for column, entry in stats.items() if entry["rate"] == 1.0 and entry["compared"]
        ),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--codes", default="")
    parser.add_argument("--sample", type=int, default=0, help="random listed codes if --codes is empty")
    parser.add_argument(
        "--year",
        type=int,
        default=datetime.date.today().year - 1,
        help="report year to compare. Defaults to the last COMPLETE year: an "
             "in-progress year's interim columns do not line up with the stored "
             "periods, and comparing them says more about the page than the data",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        pool = [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT stock_code FROM financials WHERE report_date >= '2025-01-01' "
                "AND stock_code GLOB '[0-9]*'"
            )
        ]
        conn.close()
        codes = random.Random(20261016).sample(pool, min(args.sample or 40, len(pool)))

    report = run(codes, args.year)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"{report['codes']} symbols, reports from {args.year}")
    print(f"\n{'column':18} {'match':>7}  verdict")
    for column, entry in sorted(report["stats"].items(), key=lambda kv: (kv[1]["rate"] or 0)):
        rate = entry["rate"]
        if rate is None:
            verdict = "no overlapping periods"
        elif rate == 1.0:
            verdict = "identical on every sampled period" + (" [substitutable]" if column in MATCHING_HINT else "")
        elif rate >= 0.8:
            verdict = "close, but a convention difference remains"
        else:
            verdict = "DIFFERENT CONVENTION — must not be substituted"
        shown = f"{rate:.0%}" if rate is not None else "-"
        print(f"  {column:16} {shown:>6} of {entry['compared']:<4} {verdict}")
        for example in entry["examples"][:1]:
            print(f"        e.g. {example['code']} {example['date']}: stored {example['stored']} vs sina {example['sina']}")
    print(f"\nsubstitutable columns: {report['substitutable'] or 'none'}")
    if report["unreadable"]:
        print(f"page unreadable for {len(report['unreadable'])} symbols: {report['unreadable'][:2]}")
    print("\nSina is a diagnostic here, not a fallback: a column whose definition")
    print("differs between vendors cannot be switched part-way through a series.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
