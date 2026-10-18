"""East-money datacenter reports: one client, several datasets.

Why this exists
---------------
The runtime has 146 tables and 80 of them are empty, all with real readers:
`financials`, `stock_fund_flow`, `dragon_tiger`, `block_stocks`, `margin_*`,
`trading_dates`. The conventional way in is `push2.eastmoney.com`, which this
host cannot reach behind its proxy. `datacenter-web.eastmoney.com` is a different
host, it answers, and it serves the same data as paginated reports — one
`reportName` parameter per dataset, no per-symbol requests.

Design rules, the same ones the listing sources keep:

* **A short read is reported, not returned.** ``complete`` and ``failed_pages``
  say whether the caller is looking at the whole report.
* **``max_rows`` truncation is visible.** ``truncated`` is True when the row cap
  was reached before the report ended, so a cap never looks like a small dataset.
* **Reading nothing raises.** An outage is not an empty dataset.

Registered reports live in :data:`REPORTS`; add a name there rather than
hard-coding URLs at call sites.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DEFAULT_PAGE_SIZE = 500
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_RETRIES = 3

#: Transports in preference order; see sina_listing for why both are attempted.
TRANSPORTS = ("direct", "proxy")

#: Reports this project depends on. The value is a short note on what it holds
#: and how far back it goes, so a caller can pick without opening the API.
REPORTS: dict[str, dict[str, str]] = {
    # 主要财务指标: one row per report period with every ratio `financials`
    # needs — EPS, BPS, ROE, ROA, revenue, net profit, gross/net margin,
    # debt/asset ratio, current ratio and both YoY growth figures. Used instead
    # of RPT_LICO_FN_CPD for the `financials` table so a row cannot mix two
    # report periods, and because the earnings report carries none of the four
    # balance-sheet and profitability fields.
    "RPT_F10_FINANCE_MAINFINADATA": {
        "what": "主要财务指标 (all financials columns from one report period)",
        "history": "about 692k rows",
    },
    # 业绩报表: revenue, net profit, EPS, ROE, BPS, YoY growth, dividend summary.
    # About 496k rows (every report period since 1990); the per-stock industry
    # board (BOARD_CODE/BOARD_NAME) comes from here too.
    "RPT_LICO_FN_CPD": {
        "what": "业绩报表 (revenue/net profit/EPS/ROE/BPS/YoY + industry board)",
        "history": "1990 -> present, one row per report period",
    },
    # 资产负债表: adds total assets/liabilities, current ratio, debt ratio.
    "RPT_DMSK_FN_BALANCE": {
        "what": "资产负债表 (balance sheet, leverage and liquidity ratios)",
        "history": "per report period",
    },
    # 个股资金流: a one-day snapshot of the whole market, not a series.
    "RPT_DMSK_TS_STOCKNEW": {
        "what": "个股资金流 (per-day snapshot: 超大单/大单/主力 inflow)",
        "history": "latest trading day only",
    },
    # 龙虎榜: every board appearance with the reason and both seat totals.
    "RPT_DAILYBILLBOARD_DETAILSNEW": {
        "what": "龙虎榜 (dragon-tiger board appearances and seat totals)",
        "history": "full history, about 269k rows",
    },
    # 分红送转: dividend and bonus-share plans with record/ex-dividend dates.
    "RPT_SHAREBONUS_DET": {
        "what": "分红送转 (dividend and bonus share plans)",
        "history": "about 57k rows",
    },
    # 板块成分: every board a security belongs to, index and concept included.
    "RPT_F10_CORETHEME_BOARDTYPE": {
        "what": "所属板块 (all board memberships per security)",
        "history": "about 94k rows",
    },
    # 个股融资融券明细: per-security margin balances, **both** exchanges in one
    # report. The exchange endpoints cannot do this — 上交所's answers at market
    # level and ignores its `stockCode` parameter, and 深交所 returns 403 from
    # this host — so `margin_detail` had no source until this one was found.
    # 沪证 and 深证 must be queried separately: sorted by date descending, the
    # first 2,000 rows of a date are all 沪证 and 深证 never appears.
    "RPTA_WEB_RZRQ_GGMX": {
        "what": "个股融资融券明细 (per-security margin balance, 沪/深/北)",
        "history": "about 6.9M rows, one per security per day",
    },
    # 沪深港通资金流向: one row per channel per day. Types 002/004/006 carry
    # southbound (港股通) totals and still update; 001/003/005 are the northbound
    # channels and have carried no value in any recent row.
    "RPT_MUTUAL_DEAL_HISTORY": {
        "what": "沪深港通资金流向 (northbound and southbound daily flows)",
        "history": "about 15.6k rows; northbound values discontinued",
    },
}


def report_filter(expression: str) -> str:
    """URL-encode a datacenter filter such as ``(REPORT_DATE>='2021-01-01')``.

    The expression has to arrive as ``&filter=<...>``. A call site that passes the
    bare expression does not fail loudly: the server concatenates it onto
    ``pageNumber`` and answers 400 about a malformed integer, which reads as a
    provider outage rather than a caller mistake. Use this, not string joining.
    """

    return f"&filter={urllib.parse.quote(expression)}" if expression else ""


def _opener(transport: str):
    if transport == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _fetch_page(
    report_name: str,
    page: int,
    *,
    columns: str,
    page_size: int,
    filter_expression: str,
    sort_columns: str,
    sort_types: str,
    timeout: float,
    transport: str,
) -> dict[str, Any]:
    url = (
        f"{DATACENTER_URL}?reportName={report_name}&columns={columns}"
        f"&pageSize={int(page_size)}&pageNumber={int(page)}{filter_expression}"
    )
    if sort_columns:
        url += f"&sortColumns={sort_columns}&sortTypes={sort_types}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": os.getenv("AIASK_HTTP_USER_AGENT", "Mozilla/5.0"),
            "Referer": "https://data.eastmoney.com/",
        },
    )
    with _opener(transport).open(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if not payload.get("success"):
        raise RuntimeError(
            f"report {report_name} rejected page {page}: {payload.get('message')}"
        )
    return payload.get("result") or {}


def fetch_report(
    report_name: str,
    *,
    columns: str = "ALL",
    filter_expression: str = "",
    page_size: int = DEFAULT_PAGE_SIZE,
    max_rows: int = 0,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    max_pages: int = 2000,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> dict[str, Any]:
    """Read a datacenter report into memory.

    ``filter_expression`` is passed through verbatim and must already be
    URL-encoded by the caller (the API expects ``(FIELD="value")``).

    ``max_rows`` of 0 reads the whole report. A positive value stops early and
    sets ``truncated`` — the cap is reported rather than silently shaping the
    result.
    """

    if report_name not in REPORTS:
        # Not fatal, but a name nobody registered is usually a typo, and a typo'd
        # report returns "报表配置不存在" from the API rather than a clear error.
        logger.warning("reading unregistered eastmoney report %s", report_name)

    rows: list[dict[str, Any]] = []
    failed_pages: list[int] = []
    transports_used: set[str] = set()
    pages_read = 0
    expected_pages: Optional[int] = None
    truncated = False

    for page in range(1, max_pages + 1):
        result: Optional[dict[str, Any]] = None
        last_error: Optional[BaseException] = None
        for transport in TRANSPORTS:
            for attempt in range(max(1, retries)):
                try:
                    result = _fetch_page(
                        report_name,
                        page,
                        columns=columns,
                        page_size=page_size,
                        filter_expression=filter_expression,
                        sort_columns=sort_columns,
                        sort_types=sort_types,
                        timeout=timeout,
                        transport=transport,
                    )
                    transports_used.add(transport)
                    break
                except (urllib.error.URLError, TimeoutError, ValueError, OSError, RuntimeError) as exc:
                    last_error = exc
                    time.sleep(0.4 * (attempt + 1))
            if result is not None:
                break

        if result is None:
            failed_pages.append(page)
            logger.warning(
                "report %s page %d failed on every transport: %s: %s",
                report_name,
                page,
                type(last_error).__name__,
                last_error,
            )
            if not rows:
                raise RuntimeError(
                    f"report {report_name} unavailable: page 1 failed "
                    f"({type(last_error).__name__}: {last_error})"
                )
            continue

        batch = result.get("data") or []
        if expected_pages is None:
            count = int(result.get("count") or 0)
            expected_pages = max(1, (count + max(1, len(batch)) - 1) // max(1, len(batch)))
        if not batch:
            break

        pages_read += 1
        rows.extend(batch)

        if max_rows and len(rows) >= max_rows:
            rows = rows[:max_rows]
            truncated = True
            break
        if expected_pages is not None and page >= expected_pages:
            break

    if not rows:
        raise RuntimeError(
            f"report {report_name} returned no row (failed pages: {failed_pages or 'none'})"
        )

    # Pagination is offset-based, so a sort key with huge ties lets the server's page
    # boundaries move between requests: rows come back repeated and others are
    # skipped. Measured on `RPTA_WEB_RZRQ_GGMX` sorted on `DATE` alone — one day
    # holds thousands of tied rows and 74.9% of the read was duplicates, with rows
    # missing besides. A repeated *whole* row is never legitimate, so counting them
    # turns the failure mode visible for any report and any sort.
    distinct = len({json.dumps(row, sort_keys=True, default=str) for row in rows})
    duplicate_rows = len(rows) - distinct

    return {
        "rows": rows,
        "source": f"eastmoney:{report_name}",
        "pages": pages_read,
        "transports": sorted(transports_used),
        "complete": not failed_pages and not truncated,
        "failed_pages": failed_pages,
        "truncated": truncated,
        "distinct_rows": distinct,
        "duplicate_rows": duplicate_rows,
        # A caller sorting on one column can lose rows it never sees; this is the
        # only signal that it happened.
        "pagination_unstable": duplicate_rows > 0,
    }


__all__ = ["fetch_report", "report_filter", "REPORTS", "DATACENTER_URL"]
