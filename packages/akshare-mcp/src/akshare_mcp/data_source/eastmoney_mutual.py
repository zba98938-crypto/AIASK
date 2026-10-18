"""沪深港通 daily flows, and which half of them still exists.

``north_fund_flow`` held 146 stale rows from the akshare era and no source, which
read as "we lost the feed". ``RPT_MUTUAL_DEAL_HISTORY`` has 15.6k rows and still
updates — but only for **southbound**.

The report carries six channel codes. What is measured, not assumed:

    on 2026-10-16, and on every one of the previous 400 rows,
    001 / 003 / 005 carry no value at all, and 002 / 004 / 006 do.

    and the totals reconcile arithmetically:
    002 + 004 == 006   (-2065.77 + -1502.66 = -3568.43 on 2026-10-16,
                        -665.23 + -4415.00 = -5080.23 on 2026-10-15, ...)

So 002/004 are the two southbound legs and 006 is their total, while 001/003 are
the northbound legs and 005 their total. **Northbound real-time flow was
discontinued**, which is a market fact rather than a gap in this project's
coverage: ``north_money``, ``hgt`` and ``sgt`` are written as NULL and that is the
honest value. Writing 0.0 would report a flat market.
"""

from __future__ import annotations

from typing import Any, Optional

from .eastmoney_datacenter import fetch_report, report_filter

SOURCE_NAME = "eastmoney:RPT_MUTUAL_DEAL_HISTORY"

#: Channel code -> what it is. The southbound ones are the ones with values.
CHANNELS: dict[str, str] = {
    "001": "northbound_shanghai",
    "003": "northbound_shenzhen",
    "005": "northbound_total",
    "002": "southbound_shanghai",
    "004": "southbound_shenzhen",
    "006": "southbound_total",
}

#: Channels that still publish. The rest are named so their absence is deliberate.
PUBLISHED_CHANNELS = ("002", "004", "006")
DISCONTINUED_CHANNELS = ("001", "003", "005")
DISCONTINUED_REASON = "northbound_realtime_disclosure_discontinued"


def fetch_mutual_flows(*, on_or_after: str = "", max_rows: int = 0, page_size: int = 500) -> dict[str, Any]:
    """Read the mutual-market daily flows and fold them into one row per date."""

    expression = f"(TRADE_DATE>='{on_or_after}')" if on_or_after else ""
    result = fetch_report(
        "RPT_MUTUAL_DEAL_HISTORY",
        filter_expression=report_filter(expression),
        page_size=page_size,
        max_rows=int(max_rows or 0),
        # `TRADE_DATE` alone ties six rows per day; the second key keeps the page
        # boundaries from moving. See `fetch_report`'s duplicate count.
        sort_columns="TRADE_DATE,MUTUAL_TYPE",
        sort_types="-1,1",
    )

    by_date: dict[str, dict[str, Optional[float]]] = {}
    for raw in result.get("rows") or []:
        day = str(raw.get("TRADE_DATE") or "")[:10]
        channel = str(raw.get("MUTUAL_TYPE") or "").strip()
        if not day or channel not in CHANNELS:
            continue
        bucket = by_date.setdefault(day, {})

        def number(key: str) -> Optional[float]:
            value = raw.get(key)
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        bucket[channel] = number("NET_DEAL_AMT")
        bucket["buy_" + channel] = number("BUY_AMT")
        bucket["sell_" + channel] = number("SELL_AMT")

    rows: list[dict[str, Any]] = []
    for day in sorted(by_date, reverse=True):
        bucket = by_date[day]
        rows.append(
            {
                "trade_date": day,
                # Southbound is present, so it is written.
                "south_money": bucket.get("006"),
                "ggt_ss": bucket.get("002"),
                "ggt_sz": bucket.get("004"),
                "net_amount": bucket.get("006"),
                # Northbound is discontinued: NULL, never 0.0.
                "north_money": None,
                "hgt": None,
                "sgt": None,
                # Any of the three northbound channels carrying a value is enough
                # to say the disclosure is back; waiting on the total alone would
                # miss a partial resumption.
                "north_published": any(
                    bucket.get(code) is not None for code in DISCONTINUED_CHANNELS
                ),
            }
        )

    return {
        "rows": rows,
        "source": SOURCE_NAME,
        "complete": bool(result.get("complete")),
        "failed_pages": result.get("failed_pages") or [],
        "truncated": bool(result.get("truncated")),
        "duplicate_rows": int(result.get("duplicate_rows") or 0),
        "published_channels": list(PUBLISHED_CHANNELS),
        "discontinued_channels": list(DISCONTINUED_CHANNELS),
        "discontinued_reason": DISCONTINUED_REASON,
        "days": len(rows),
    }
