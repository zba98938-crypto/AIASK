"""Shanghai exchange margin trading, from the exchange's own endpoint.

Why this exists
---------------
`margin_market_flow` has readers and no rows. The declared provider is Tushare,
which has no token; akshare was removed. 上交所 publishes the same figures
directly, and this host can reach `query.sse.com.cn`.

Scope, stated plainly
---------------------
**This is Shanghai only.** 深交所's own endpoint returns 403 through this host's
proxy, so nothing here can produce a market-wide total and nothing pretends to.
The endpoint also accepts a `stockCode` parameter and ignores it — a query for
600519 returns the same market-level totals as a query with no code — so there is
no per-stock detail here either. ``EXCHANGE_ID`` is always ``"SSE"`` and callers
get ``missing_exchanges`` back.

Design rules, the same as the other listing sources: failed pages are named, a
short read reports itself, and reading nothing raises rather than returning an
empty series.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

SSE_MARGIN_URL = "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do"
EXCHANGE_ID = "SSE"
#: The exchange that is *not* covered. Named so callers and logs can say which
#: half is missing instead of implying the figure is market-wide.
MISSING_EXCHANGE_ID = "SZSE"

DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_RETRIES = 3
TRANSPORTS = ("direct", "proxy")


def _opener(transport: str):
    if transport == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _first_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a JSONP body.

    The response is ``jsonpCallback({...})`` followed by more content, so
    ``json.loads`` on the whole body fails with "Extra data".
    """

    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the response body")
    depth = 0
    for index, char in enumerate(text[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : index + 1])
    raise ValueError("unbalanced JSON object in the response body")


def _fetch_page(
    page: int, *, begin: str, end: str, page_size: int, timeout: float, transport: str
) -> dict[str, Any]:
    url = (
        f"{SSE_MARGIN_URL}?jsonCallBack=jsonpCallback&isPagination=true"
        f"&beginDate={begin}&endDate={end}"
        f"&pageHelp.pageSize={int(page_size)}&pageHelp.pageNo={int(page)}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": os.getenv("AIASK_HTTP_USER_AGENT", "Mozilla/5.0"),
            "Referer": "https://www.sse.com.cn/",
        },
    )
    with _opener(transport).open(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
    return _first_json_object(text)


def fetch_margin_market(
    begin: str,
    end: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    max_pages: int = 100,
) -> dict[str, Any]:
    """Return 上交所 daily margin totals between ``begin`` and ``end``.

    ``begin``/``end`` are ``YYYYMMDD``. Each row is::

        {"trade_date": "2026-10-15", "exchange_id": "SSE", "rzye": ..., ...}

    ``trade_date`` is ISO ``YYYY-MM-DD`` on purpose: the consumer
    (`_strategy_crud_market`) parses it with ``date.fromisoformat`` and skips any
    row it cannot parse, so writing the exchange's own ``20261015`` would fill the
    table with rows its only reader silently drops.

    Returns ``rows``, ``exchange_id``, ``missing_exchanges``, ``complete`` and
    ``failed_pages``. Raises ``RuntimeError`` when no row could be read.
    """

    # The exchange silently returns an empty page set for a malformed date rather
    # than an error, which reads as "there was no margin trading that week". The
    # format is checked here so a caller's `2026-10-01` fails loudly.
    for label, value in (("begin", begin), ("end", end)):
        text = str(value or "").strip()
        if len(text) != 8 or not text.isdigit():
            raise ValueError(
                f"{label} must be YYYYMMDD (got {value!r}); "
                "the exchange returns nothing for other formats"
            )

    rows: list[dict[str, Any]] = []
    failed_pages: list[int] = []
    transports_used: set[str] = set()
    pages_read = 0

    for page in range(1, max_pages + 1):
        payload: Optional[dict[str, Any]] = None
        last_error: Optional[BaseException] = None
        for transport in TRANSPORTS:
            for attempt in range(max(1, retries)):
                try:
                    payload = _fetch_page(
                        page,
                        begin=begin,
                        end=end,
                        page_size=page_size,
                        timeout=timeout,
                        transport=transport,
                    )
                    transports_used.add(transport)
                    break
                except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                    last_error = exc
                    time.sleep(0.4 * (attempt + 1))
            if payload is not None:
                break

        if payload is None:
            failed_pages.append(page)
            logger.warning(
                "SSE margin page %d failed on every transport: %s: %s",
                page,
                type(last_error).__name__,
                last_error,
            )
            if not rows:
                raise RuntimeError(
                    f"SSE margin unavailable: page 1 failed "
                    f"({type(last_error).__name__}: {last_error})"
                )
            continue

        batch = (payload.get("pageHelp") or {}).get("data") or payload.get("result") or []
        if not batch:
            break

        pages_read += 1
        for item in batch:
            raw_date = str(item.get("opDate") or "").strip()
            if len(raw_date) != 8 or not raw_date.isdigit():
                continue
            rows.append(
                {
                    "trade_date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
                    "exchange_id": EXCHANGE_ID,
                    "rzye": item.get("rzye"),
                    "rzmre": item.get("rzmre"),
                    "rzche": item.get("rzche"),
                    # rqyl is 融券余量 in shares; rqylje is the same position in
                    # money. The column is 融券余额, so it takes the amount.
                    "rqye": item.get("rqylje"),
                    "rqmcl": item.get("rqmcl"),
                    "rqyl": item.get("rqyl"),
                    "rzrqye": item.get("rzrqjyzl"),
                }
            )

        total_pages = int((payload.get("pageHelp") or {}).get("pageCount") or 0)
        if total_pages and page >= total_pages:
            break
        if len(batch) < page_size:
            break

    if not rows:
        raise RuntimeError(
            f"SSE margin returned no usable row for {begin}..{end} "
            f"(failed pages: {failed_pages or 'none'})"
        )

    return {
        "rows": rows,
        "source": "sse:queryMargin",
        "exchange_id": EXCHANGE_ID,
        "missing_exchanges": [MISSING_EXCHANGE_ID],
        "pages": pages_read,
        "transports": sorted(transports_used),
        "complete": not failed_pages,
        "failed_pages": failed_pages,
    }


__all__ = ["fetch_margin_market", "SSE_MARGIN_URL", "EXCHANGE_ID", "MISSING_EXCHANGE_ID"]
