#!/usr/bin/env python3
"""Read-only audit of paper fills against daily bars and quote freshness.

The database is opened with SQLite ``mode=ro``. This command never imports the
MCP server or changes paper state.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path


def _db_path() -> Path:
    import os

    explicit = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (Path(os.getenv("AIASK_RUNTIME_HOME", Path.home() / ".aiask")).expanduser() / "data/db/akshare_mcp.sqlite3").resolve()


def audit(db_path: Path, since: str | None = None) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        query = """
            SELECT t.*, o.signal_date, o.status AS order_status,
                   p.quantity AS current_position_quantity,
                   a.status AS paper_account_status,
                   ia.status AS incubation_account_status
            FROM paper_trades t
            LEFT JOIN paper_orders o ON o.id = t.source_order_id
            LEFT JOIN paper_positions p ON p.account_id = t.account_id AND p.stock_code = t.stock_code
            LEFT JOIN paper_accounts a ON a.id = t.account_id
            LEFT JOIN strategy_incubation_accounts ia ON ia.account_id = t.account_id
            WHERE (? IS NULL OR substr(t.trade_time,1,10) >= ?)
            ORDER BY t.trade_time, t.account_id, t.id
        """
        fills = [dict(row) for row in con.execute(query, (since, since))]
        order_summary = [dict(row) for row in con.execute(
            """SELECT COALESCE(signal_date, substr(created_at,1,10)) AS trade_date,
                      account_id, status, COUNT(*) AS order_count
               FROM paper_orders
               WHERE (? IS NULL OR COALESCE(signal_date, substr(created_at,1,10)) >= ?)
               GROUP BY 1,2,3 ORDER BY 1,2,3""", (since, since)
        )]
        position_summary = [dict(row) for row in con.execute(
            """SELECT account_id, stock_code, quantity, cost_price, current_price,
                      market_value, CASE WHEN quantity > 0 THEN 'open' ELSE 'closed' END AS position_status
               FROM paper_positions ORDER BY account_id, stock_code"""
        )]
        nav_summary = [dict(row) for row in con.execute(
            """SELECT nav_date, account_id, total_value, cash, market_value, daily_return
               FROM paper_nav WHERE (? IS NULL OR nav_date >= ?) ORDER BY nav_date, account_id""",
            (since, since),
        )]
        bars = defaultdict(list)
        for row in con.execute("SELECT code,time,open,high,low,close,updated_at FROM kline_1d"):
            bars[(str(row[0]), str(row[1])[:10])].append(dict(zip(("code", "time", "open", "high", "low", "close", "updated_at"), row)))
        quote_rows = [dict(row) for row in con.execute("SELECT code,time,price,updated_at FROM stock_quotes")]
        quote_by_code_day = defaultdict(list)
        for q in quote_rows:
            quote_by_code_day[str(q["code"])].append(q)

        categories = Counter()
        by_date = defaultdict(Counter)
        by_account = defaultdict(Counter)
        details = []
        for fill in fills:
            day = str(fill.get("trade_time") or "")[:10]
            code = str(fill.get("stock_code") or "")
            price = float(fill.get("price") or 0)
            day_bars = bars.get((code, day), [])
            categories_for_fill = []
            if day_bars:
                bar = day_bars[-1]
                lo, hi = float(bar.get("low") or 0), float(bar.get("high") or 0)
                if price < lo or price > hi:
                    categories_for_fill.append("outside_session_range")
                if str(bar.get("updated_at") or "") > str(fill.get("trade_time") or ""):
                    categories_for_fill.append("no_bar_at_fill_time")
            else:
                categories_for_fill.append("no_bar_at_fill_time")

            for quote in quote_by_code_day.get(code, []):
                try:
                    quote_price = float(quote.get("price") or 0)
                    quote_day = str(quote.get("updated_at") or quote.get("time") or "")[:10]
                    # MatchingEngine previously added a 0.05% impact to DB
                    # snapshots. Allow up to 0.1% so the provenance check
                    # still catches 3.131565 sourced from the stale 3.13 quote.
                    tolerance = max(0.00001, abs(quote_price) * 0.001)
                    if quote_day and quote_day < day and abs(quote_price - price) <= tolerance:
                        categories_for_fill.append("stale_quote_source")
                        break
                except (TypeError, ValueError):
                    continue
            categories_for_fill = sorted(set(categories_for_fill))
            for category in categories_for_fill:
                categories[category] += 1
                by_date[day][category] += 1
                by_account[str(fill.get("account_id"))][category] += 1
            details.append({
                "trade_id": fill.get("id"), "account_id": fill.get("account_id"),
                "strategy_id": fill.get("strategy_id"), "stock_code": code,
                "trade_date": day, "trade_type": fill.get("trade_type"),
                "price": price, "quantity": fill.get("quantity"),
                "categories": categories_for_fill,
                "paper_account_status": fill.get("paper_account_status"),
                "incubation_account_status": fill.get("incubation_account_status"),
            })
        return {
            "database": str(db_path), "read_only": True, "since": since,
            "fill_count": len(fills), "category_counts": dict(categories),
            "by_date": {k: dict(v) for k, v in sorted(by_date.items())},
            "by_account": {k: dict(v) for k, v in sorted(by_account.items())},
            "orders": {
                "by_date_account_status": order_summary,
                "count": sum(int(row["order_count"]) for row in order_summary),
            },
            "positions": {
                "rows": position_summary,
                "count": len(position_summary),
                "open_count": sum(row["position_status"] == "open" for row in position_summary),
                "by_account": dict(Counter(row["account_id"] for row in position_summary)),
            },
            "nav": {
                "by_date_account": nav_summary,
                "count": len(nav_summary),
                "by_date": dict(Counter(row["nav_date"] for row in nav_summary)),
            },
            "fills": details,
            "account_status_coverage": {
                "paper_account_missing": sum(x.get("paper_account_status") is None for x in fills),
                "incubation_account_missing": sum(x.get("incubation_account_status") is None for x in fills),
            },
        }
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_db_path())
    parser.add_argument("--since", help="inclusive YYYY-MM-DD trade date")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit(args.db.expanduser().resolve(), args.since)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
