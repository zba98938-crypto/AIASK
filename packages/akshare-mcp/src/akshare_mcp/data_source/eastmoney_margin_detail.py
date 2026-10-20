"""Per-security margin balances from East money's datacenter.

``margin_detail`` had no source. 上交所's endpoint answers at market level and
ignores its documented ``stockCode`` parameter, 深交所 returns 403 from this host,
and the conclusion drawn from that was "per-stock margin is unavailable here".
It is not: ``RPTA_WEB_RZRQ_GGMX`` carries 6.9M rows — one per security per day,
**both exchanges and 北交所**.

Two things about that report are load-bearing:

* **It must be queried per market.** Sorted by date descending, the first 2,000
  rows of a date are all 沪证; 深证 never appears in an unfiltered page.
* **``ts_code`` needs an exchange suffix.** The reader derives its coverage from
  ``ts_code LIKE '%.SH'`` / ``'%.SZ'`` and **blocks the whole table when either is
  absent**, so a sync that writes only 沪市 leaves the table unreadable — worse
  than leaving it empty, because the rows are there and the gate is what refuses.

The report carries balances, not the daily buy/repay amounts, so ``rzmre``,
``rzche``, ``rqmcl`` and ``rqchl`` are left out of the row rather than written as
0. A zero there would read as "nobody borrowed or repaid today".
"""

from __future__ import annotations

from typing import Any, Optional

from .eastmoney_datacenter import fetch_report, report_filter

SOURCE_NAME = "eastmoney:RPTA_WEB_RZRQ_GGMX"

#: East money's market label -> the exchange suffix the readers group by.
MARKET_SUFFIX: dict[str, str] = {
    "融资融券_沪证": "SH",
    "融资融券_深证": "SZ",
    "融资融券_北证": "BJ",
}
#: The two the reader insists on. See the module docstring: missing either blocks
#: the table, so a sync must produce both or report failure.
REQUIRED_MARKETS = ("融资融券_沪证", "融资融券_深证")

#: Report column -> ``margin_detail`` column. Absent ones are simply not written.
FIELD_MAP: dict[str, str] = {
    "SCODE": "ts_code",
    "RZYE": "rzye",
    "RQYE": "rqye",
    "RQYL": "rqyl",
    "RZRQYE": "rzrqye",
}

#: Columns the report does not publish, named so the omission is deliberate and
#: greppable rather than an oversight.
NOT_PUBLISHED = ("rzmre", "rzche", "rqmcl", "rqchl")


def _ts_code(raw_code: Any, market: str) -> str:
    suffix = MARKET_SUFFIX.get(str(market), "")
    digits = "".join(ch for ch in str(raw_code or "") if ch.isdigit())
    if not digits or not suffix:
        return ""
    return f"{digits}.{suffix}"


def _row(raw: dict[str, Any], market: str) -> Optional[tuple]:
    ts_code = _ts_code(raw.get("SCODE"), market)
    trade_date = str(raw.get("DATE") or "")[:10]
    if not ts_code or not trade_date:
        return None

    def number(key: str) -> Optional[float]:
        value = raw.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return (
        trade_date,
        ts_code,
        number("RZYE"),
        number("RQYE"),
        number("RQYL"),
        number("RZRQYE"),
        SOURCE_NAME,
    )


def fetch_margin_detail(
    *,
    markets: tuple[str, ...] = REQUIRED_MARKETS,
    max_rows: int = 0,
    page_size: int = 500,
    on_or_after: str = "",
) -> dict[str, Any]:
    """Read per-security margin balances, one query per market.

    ``on_or_after`` is a ``YYYY-MM-DD`` lower bound; without it the report is read
    from newest backwards, which for a daily sync is unnecessary traffic over 6.9M
    rows.
    """

    rows: list[tuple] = []
    per_market: dict[str, int] = {}
    failed: list[dict[str, str]] = []
    latest: dict[str, str] = {}

    for market in markets:
        expression = f'(MARKET="{market}")'
        if on_or_after:
            expression += f'(DATE>=\'{on_or_after}\')'
        try:
            result = fetch_report(
                "RPTA_WEB_RZRQ_GGMX",
                filter_expression=report_filter(expression),
                page_size=page_size,
                max_rows=int(max_rows or 0),
                # `SCODE` is a tie-breaker, not decoration. Sorted on `DATE` alone a
                # single day holds thousands of tied rows, the server's page
                # boundaries move between requests, and the pages come back with
                # rows repeated and others skipped: measured 74.9% duplicates, so
                # ~4,000 rows of a day arrived as 2,000 unique and the rest were
                # lost. With the second key, the same read yields 0% duplicates.
                sort_columns="DATE,SCODE",
                sort_types="-1,1",
            )
        except Exception as exc:
            failed.append({"market": market, "error": f"{type(exc).__name__}: {str(exc)[:90]}"})
            continue

        kept = 0
        for raw in result.get("rows") or []:
            mapped = _row(raw, market)
            if mapped is None:
                continue
            rows.append(mapped)
            kept += 1
            day = mapped[0]
            if day > latest.get(market, ""):
                latest[market] = day
        per_market[market] = kept
        if result.get("truncated"):
            failed.append({"market": market, "error": "truncated by max_rows"})
        # fetch_report can return partial rows with complete=False and non-empty
        # failed_pages instead of raising. Only checking `truncated` turned a
        # known-partial fetch into complete=True.
        if result.get("complete") is False:
            failed.append(
                {
                    "market": market,
                    "error": "incomplete_fetch",
                    "failed_pages": list(result.get("failed_pages") or []),
                    "pagination_unstable": bool(result.get("pagination_unstable")),
                }
            )

    missing = [m for m in REQUIRED_MARKETS if not per_market.get(m)]
    return {
        "rows": rows,
        "per_market": per_market,
        "latest_by_market": latest,
        "failed_markets": failed,
        "missing_required_markets": missing,
        # A sync that cannot produce both required markets must not write: the
        # reader blocks the table when either suffix is absent, so a one-sided
        # write looks like progress and reads as nothing.
        "complete": not missing and not failed,
        "source": SOURCE_NAME,
        "not_published": list(NOT_PUBLISHED),
    }
