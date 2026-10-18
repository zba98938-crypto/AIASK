#!/usr/bin/env python3
"""Validate the built-in trading calendar against the market's own record.

`strategy_factory.domain.trading_calendar` falls back to a hand-maintained holiday
list when neither optional calendar package is installed — which is the state here.
A hand-maintained list goes stale, and it had: `2026-10-08` was listed as a holiday
while the market traded (5,561 bars and the exchange's own margin feed), and
`2026-09-25` was missing so a closed day read as open. Worse, the fallback returned
True for 调休 workdays, so four weekend days read as sessions.

None of that is visible from the calendar itself. What makes it visible is the data
the project already stores: a bar exists only on a day the market traded, and the
exchange publishes its margin figures only on trading days. This compares the two
and names every disagreement.

Run it after any edit to the holiday list, and before trusting a scheduling decision
that depends on it — `factory_scheduler` gates a whole factory run on
`is_trading_day`.

Usage::

    python scripts/check_trading_calendar_agreement.py
    python scripts/check_trading_calendar_agreement.py --from 2026-01-01 --json
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "strategy-factory" / "src"))

from _db_sync_common import DB_PATH  # noqa: E402

#: Bars on these dates come from a test run, not from the market. Verified by
#: re-reading the source: only these three symbols, only this date.
KNOWN_POLLUTION = {
    ("2026-06-19", "600519"),
    ("2026-06-19", "000001"),
    ("2026-06-19", "000858"),
}


def _evidence(conn: sqlite3.Connection, start: str) -> tuple[set[str], set[str], list[str]]:
    """Trading days the data attests to, trading days the exchange attests to, and
    the rows excluded as test pollution."""

    rows = list(conn.execute(
        "SELECT DISTINCT substr(time, 1, 10), code FROM kline_1d WHERE substr(time, 1, 10) >= ?",
        (start,),
    ))
    excluded = []
    bars: set[str] = set()
    for day, code in rows:
        if (day, str(code)) in KNOWN_POLLUTION:
            excluded.append(f"{day}/{code}")
            continue
        bars.add(day)
    exchange = {d for (d,) in conn.execute(
        "SELECT DISTINCT trade_date FROM margin_market_flow WHERE trade_date >= ?", (start,)
    )}
    return bars, exchange, excluded


def run(start: str, end: str) -> dict[str, Any]:
    from strategy_factory.domain.trading_calendar import get_trading_calendar

    calendar = get_trading_calendar()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    bars, exchange, excluded = _evidence(conn, start)
    conn.close()

    attested = bars | exchange
    said_closed_but_traded: list[str] = []
    said_open_but_no_evidence: list[str] = []

    for day in sorted(attested):
        if not calendar.is_trading_day(datetime.date.fromisoformat(day)):
            said_closed_but_traded.append(day)

    cursor = datetime.date.fromisoformat(start)
    last = datetime.date.fromisoformat(end)
    while cursor <= last:
        iso = cursor.isoformat()
        if calendar.is_trading_day(cursor) and iso not in attested:
            said_open_but_no_evidence.append(iso)
        cursor += datetime.timedelta(days=1)

    return {
        "as_of": datetime.datetime.now().isoformat(timespec="seconds"),
        "window": [start, end],
        "attested_trading_days": len(attested),
        "from_bars": len(bars),
        "from_exchange": len(exchange),
        "excluded_as_test_pollution": excluded,
        "said_closed_but_traded": said_closed_but_traded,
        "said_open_but_no_evidence": said_open_but_no_evidence,
        "agree": not said_closed_but_traded and not said_open_but_no_evidence,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="start", default="2026-01-01")
    parser.add_argument("--to", dest="end", default=datetime.date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = run(args.start, args.end)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"window {report['window'][0]} .. {report['window'][1]}")
    print(f"  trading days attested: {report['attested_trading_days']} "
          f"({report['from_bars']} from bars, {report['from_exchange']} from the exchange's margin feed)")
    if report["excluded_as_test_pollution"]:
        print(f"  excluded as test pollution: {report['excluded_as_test_pollution']}")
    closed = report["said_closed_but_traded"]
    open_no_evidence = report["said_open_but_no_evidence"]
    print(f"\n  calendar says closed, market traded : {len(closed)} {closed[:8]}")
    print(f"  calendar says open, nothing attests : {len(open_no_evidence)} {open_no_evidence[:8]}")
    if report["agree"]:
        print("\n  the built-in calendar agrees with the market on every day in the window")
        return 0
    print("\n  !! the built-in holiday list disagrees with the market; fix the list")
    print("     (a wrong answer here gates a whole factory run: factory_scheduler")
    print("      calls is_trading_day before starting one)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
