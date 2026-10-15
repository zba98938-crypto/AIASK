"""East-money F10 industry classification, fetched in bulk.

Why this exists
---------------
Sina's 49 industry nodes classify about 48% of the market and mix in
non-industries (``次新股`` is a listing-age bucket, not an industry), so
industry-neutralised factor work only had a label for half the universe. East
money's F10 basic-information report answers the whole market in 50 paginated
requests — 24,812 rows in about 14 seconds, covering 5,577 of 5,577 listed
symbols including 北京证券交易所.

The taxonomy is East money's own three-level ``EM2016`` string, e.g.
``金融-银行-股份制与城商行``. This module returns all three levels and lets the
caller pick: ``stocks.industry`` takes level 1 (27 values) and ``stocks.sector``
takes level 2, so the two columns are the same taxonomy at different depths
rather than two taxonomies that cannot be compared.

Design rules:

* **A row without a classification is reported, not filled.** ``missing_industry``
  counts them; nothing invents a label.
* **Failed pages are named.** ``complete`` is False and ``failed_pages`` lists
  them, so a partial read cannot pass for the whole market.
* **Zero rows raises.** That is an outage and the caller must not read it as
  "no listed company has an industry".
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

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_F10_BASIC_ORGINFO"
COLUMNS = "SECURITY_CODE,SECURITY_NAME_ABBR,EM2016,INDUSTRYCSRC1,TRADE_MARKET,SECURITY_TYPE"

DEFAULT_PAGE_SIZE = 500
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_RETRIES = 3

#: Transports in preference order; see sina_listing for why both are attempted.
TRANSPORTS = ("direct", "proxy")


def _opener(transport: str):
    if transport == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _fetch_page(page: int, *, page_size: int, timeout: float, transport: str) -> dict[str, Any]:
    url = (
        f"{DATACENTER_URL}?reportName={REPORT_NAME}&columns={COLUMNS}"
        f"&pageSize={int(page_size)}&pageNumber={int(page)}"
        "&sortColumns=SECURITY_CODE&sortTypes=1"
    )
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
            f"eastmoney report {REPORT_NAME} rejected page {page}: {payload.get('message')}"
        )
    return payload.get("result") or {}


def split_em2016(value: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(level1, level2, full)`` from an ``EM2016`` string.

    ``None`` for a level the taxonomy does not provide, so a one-level industry
    is not reported as having a second level it never had.
    """

    text = str(value or "").strip()
    if not text:
        return None, None, None
    parts = [part.strip() for part in text.split("-") if part.strip()]
    if not parts:
        return None, None, None
    return parts[0], (parts[1] if len(parts) > 1 else None), text


def fetch_industry_classification(
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    max_pages: int = 400,
) -> dict[str, Any]:
    """Return East money's industry classification for every listed security.

    Returns a dict with:

    * ``by_code``: ``{code: {"industry", "sector", "industry_path", "csrc",
      "name", "security_type"}}`` — ``industry``/``sector`` are levels 1 and 2,
      any of which may be ``None`` when the source has no classification
    * ``source``, ``rows``, ``pages``
    * ``missing_industry``: rows the source returned without an ``EM2016``
    * ``complete`` / ``failed_pages``

    Raises ``RuntimeError`` when no row could be read at all.
    """

    by_code: dict[str, dict[str, Any]] = {}
    failed_pages: list[int] = []
    pages_read = 0
    transports_used: set[str] = set()
    missing_industry = 0
    rows_total = 0
    expected_pages: Optional[int] = None

    for page in range(1, max_pages + 1):
        result: Optional[dict[str, Any]] = None
        last_error: Optional[BaseException] = None
        for transport in TRANSPORTS:
            for attempt in range(max(1, retries)):
                try:
                    result = _fetch_page(
                        page, page_size=page_size, timeout=timeout, transport=transport
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
                "eastmoney F10 page %d failed on every transport: %s: %s",
                page,
                type(last_error).__name__,
                last_error,
            )
            if not by_code:
                raise RuntimeError(
                    f"eastmoney F10 unavailable: page 1 failed ({type(last_error).__name__}: {last_error})"
                )
            continue

        rows = result.get("data") or []
        if expected_pages is None:
            count = int(result.get("count") or 0)
            expected_pages = max(1, (count + max(1, len(rows)) - 1) // max(1, len(rows)))
        if not rows:
            break

        pages_read += 1
        rows_total += len(rows)
        for row in rows:
            code = str(row.get("SECURITY_CODE") or "").strip()
            if not code:
                continue
            level1, level2, full = split_em2016(row.get("EM2016"))
            if level1 is None:
                missing_industry += 1
            by_code[code] = {
                "industry": level1,
                "sector": level2,
                "industry_path": full,
                "csrc": (str(row.get("INDUSTRYCSRC1")).strip() or None)
                if row.get("INDUSTRYCSRC1")
                else None,
                "name": str(row.get("SECURITY_NAME_ABBR") or "").strip(),
                "security_type": str(row.get("SECURITY_TYPE") or "").strip() or None,
            }

        if expected_pages is not None and page >= expected_pages:
            break

    if not by_code:
        raise RuntimeError(
            f"eastmoney report {REPORT_NAME} returned no usable row "
            f"(failed pages: {failed_pages or 'none'})"
        )

    return {
        "by_code": by_code,
        "source": f"eastmoney:{REPORT_NAME}",
        "rows": rows_total,
        "pages": pages_read,
        "transports": sorted(transports_used),
        "missing_industry": missing_industry,
        "complete": not failed_pages,
        "failed_pages": failed_pages,
    }


__all__ = [
    "fetch_industry_classification",
    "split_em2016",
    "REPORT_NAME",
    "DATACENTER_URL",
]
