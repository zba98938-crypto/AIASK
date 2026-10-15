"""A-share symbol listing from Sina's Market Center endpoint.

Why this exists
---------------
The factories ran on a 15-code default list and a 30-code seed pool, because
every other way to enumerate the market was unavailable on this host: Tushare
needs a token, the TDX local vipdoc needs an installed client, and
``push2.eastmoney.com`` (the conventional full-list endpoint) is unreachable
behind the local proxy even though ``push2his.eastmoney.com`` answers. Sina's
Market Center is reachable and returns the whole market, so it is the listing
source of record.

Design rules this module keeps:

* **A partial list is not a list.** Pages that fail after retries are reported in
  ``failed_pages`` and the result carries ``complete: False``. A caller that
  wants the full market can check that flag; one that only needs a sample can
  ignore it. Nothing here ever returns a short list that looks complete.
* **Zero symbols is an error, not an empty result.** Returning ``[]`` would let
  a listing outage read as "the market has no stocks" and flow into a sync that
  quietly does nothing.
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

SINA_MARKET_CENTER_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)

#: ``hs_a`` is Sina's "沪深 A 股" node. It also carries 北交所 listings.
DEFAULT_NODE = "hs_a"
DEFAULT_PAGE_SIZE = 80
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_RETRIES = 3

#: Industry prefixes that are not individual A-share stocks and have no klines.
_EXCLUDED_PREFIXES = (
    "sh000",  # 上证指数系列
    "sz399",  # 深证指数系列
    "sh950",  # 中证指数
    "sz980",
)


def _is_individual_a_share(symbol: str) -> bool:
    """Return whether a Sina symbol is an A-share stock rather than an index."""

    text = str(symbol or "").strip().lower()
    if len(text) != 8 or not text[2:].isdigit():
        return False
    market, code = text[:2], text[2:]
    if text in _EXCLUDED_PREFIXES or any(text.startswith(p) for p in ("sh000", "sz399", "sh950", "sz980")):
        return False
    if market == "sh":
        return code.startswith(("60", "68"))
    if market == "sz":
        return code.startswith(("00", "30"))
    if market == "bj":
        return code.startswith(("8", "4", "92"))
    return False


def _market_label(market: str) -> str:
    return {"sh": "上海", "sz": "深圳", "bj": "北京"}.get(market, market)


def _to_float(value: Any) -> Optional[float]:
    """Parse a Sina numeric field, returning ``None`` rather than 0 on failure."""

    if value in (None, "", "-", "--"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/data_source/sina_listing.py:86: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None
    return parsed


def _fetch_page(
    page: int,
    *,
    node: str,
    page_size: int,
    timeout: float,
    transport: str,
) -> list[dict[str, Any]]:
    url = (
        f"{SINA_MARKET_CENTER_URL}?page={int(page)}&num={int(page_size)}"
        f"&sort=symbol&asc=1&node={node}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": os.getenv("AIASK_HTTP_USER_AGENT", "Mozilla/5.0"),
        },
    )
    opener = _opener(transport)
    with opener.open(request, timeout=timeout) as response:
        # Sina serves this endpoint as GBK even though the payload is JSON.
        raw = response.read().decode("gbk", errors="replace")
    if not raw.strip():
        return []
    payload = json.loads(raw)
    return payload if isinstance(payload, list) else []


#: Transports in preference order. Direct first: this host routes through a local
#: proxy that resolves everything to 198.18.0.0/15, and the project already
#: records that the proxy breaks some Eastmoney endpoints. Both transports work
#: for Sina, so the choice is made per attempt and reported rather than assumed.
TRANSPORTS = ("direct", "proxy")

#: Consecutive failed pages, with nothing collected yet, that mean the source is
#: down rather than one page being flaky. Without this the loop grinds through
#: ``max_pages`` retry-sleep cycles before admitting the outage — five minutes of
#: hanging to produce an error it already knew.
OUTAGE_FAILURE_STREAK = 3

#: Give up once this many pages have failed: past that a partial list is not
#: worth more waiting, and ``complete=False`` already says what happened.
MAX_FAILED_PAGES = 20


def _opener(transport: str):
    if transport == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def fetch_all_a_share_symbols(
    *,
    node: str = DEFAULT_NODE,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    max_pages: int = 400,
    sleep_seconds: float = 0.05,
) -> dict[str, Any]:
    """Enumerate every A-share symbol Sina lists.

    Returns a dict with:

    * ``symbols``: ``[{"code", "symbol", "name", "market"}]``, ordered by symbol
    * ``source``: the endpoint that produced them
    * ``complete``: ``False`` when any page failed past its retries
    * ``failed_pages``: the pages that failed, so a partial result is visible
    * ``pages``: pages successfully read

    Raises ``RuntimeError`` when no symbol could be read at all, because that is
    an outage rather than an empty market.
    """

    symbols: dict[str, dict[str, Any]] = {}
    failed_pages: list[int] = []
    transports_used: set[str] = set()
    pages_read = 0
    consecutive_failures = 0

    for page in range(1, max_pages + 1):
        rows: Optional[list[dict[str, Any]]] = None
        last_error: Optional[BaseException] = None
        attempts_per_transport = max(1, retries)
        for transport in TRANSPORTS:
            for attempt in range(attempts_per_transport):
                try:
                    rows = _fetch_page(
                        page,
                        node=node,
                        page_size=page_size,
                        timeout=timeout,
                        transport=transport,
                    )
                    transports_used.add(transport)
                    break
                except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                    last_error = exc
                    time.sleep(0.4 * (attempt + 1))
            if rows is not None:
                break

        if rows is None:
            failed_pages.append(page)
            consecutive_failures += 1
            logger.warning(
                "Sina listing page %d failed on every transport (%s x%d): %s: %s",
                page,
                "/".join(TRANSPORTS),
                attempts_per_transport,
                type(last_error).__name__,
                last_error,
            )
            if not symbols and consecutive_failures >= OUTAGE_FAILURE_STREAK:
                raise RuntimeError(
                    f"Sina listing appears to be down: {consecutive_failures} consecutive "
                    f"pages failed starting at page {page - consecutive_failures + 1} "
                    f"({type(last_error).__name__}: {last_error})"
                )
            if len(failed_pages) >= MAX_FAILED_PAGES:
                logger.warning(
                    "Sina listing: stopping after %d failed page(s); the result is partial",
                    len(failed_pages),
                )
                break
            continue

        if not rows:
            break

        consecutive_failures = 0
        pages_read += 1
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().lower()
            if not _is_individual_a_share(symbol):
                continue
            # Sina reports market cap in 万元. The ``stocks`` table stores 元
            # (000001 has 1.06e11), so convert here rather than at every reader.
            mktcap_wan = _to_float(row.get("mktcap"))
            symbols[symbol] = {
                "code": symbol[2:],
                "symbol": symbol,
                "name": str(row.get("name") or "").strip(),
                "market": _market_label(symbol[:2]),
                "market_cap": mktcap_wan * 10_000 if mktcap_wan is not None else None,
                "pe_ratio": _to_float(row.get("per")),
                "pb_ratio": _to_float(row.get("pb")),
            }
        if sleep_seconds:
            time.sleep(sleep_seconds)

    if not symbols:
        raise RuntimeError(
            f"Sina listing returned no usable symbol (node={node}, "
            f"failed pages={failed_pages or 'none'})"
        )

    return {
        "symbols": [symbols[key] for key in sorted(symbols)],
        "source": f"sina:Market_Center.getHQNodeData[{node}]",
        "complete": not failed_pages,
        "failed_pages": failed_pages,
        "pages": pages_read,
        "transports": sorted(transports_used),
    }


#: Sina's industry classification, one JSON map for the whole market.
SINA_INDUSTRY_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"


def fetch_industry_membership(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Map every listed code to its Sina industry.

    The exchange listings this module reads carry no industry, and the
    strategies that neutralise by industry need one. Sina publishes 49 industry
    nodes; each node's constituents come back from the same Market Center
    endpoint used for the listing, so this costs about fifty requests total.

    Returns ``{"by_code": {code: {"industry", "sector"}}, "industries",
    "unresolved_codes", "failed_industries"}``. Codes that no industry lists are
    returned in ``unresolved_codes`` rather than being assigned a placeholder:
    "industry unknown" and "industry = 综合" are different facts.
    """

    request = urllib.request.Request(
        SINA_INDUSTRY_URL,
        headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": os.getenv("AIASK_HTTP_USER_AGENT", "Mozilla/5.0"),
        },
    )
    with _opener("direct").open(request, timeout=timeout) as response:
        raw = response.read().decode("gbk", errors="replace")

    # The payload is ``var S_Finance_bankuai_sinaindustry = { ... };``
    _, _, body = raw.partition("=")
    industries = json.loads(body.strip().rstrip(";"))

    by_code: dict[str, dict[str, str]] = {}
    failed: list[str] = []
    empty: list[str] = []
    for sector_code, entry in industries.items():
        name = str(entry).split(",")[1] if "," in str(entry) else str(sector_code)
        members = 0
        # Sina caps `num` at 100 regardless of what is asked, so a single page
        # silently truncates every large industry — 化工/电子信息/房地产 come back
        # at exactly 100 rows. Page until a short page ends the industry.
        for page in range(1, 200):
            rows: Optional[list[dict[str, Any]]] = None
            for transport in TRANSPORTS:
                try:
                    rows = _fetch_page(
                        page, node=sector_code, page_size=100, timeout=timeout, transport=transport
                    )
                    break
                except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                    logger.warning(
                        "Sina industry %s (%s) page %d failed: %s", sector_code, name, page, exc
                    )
            if rows is None:
                failed.append(f"{sector_code}#{page}")
                break
            if not rows:
                break
            for row in rows:
                symbol = str(row.get("symbol") or "").strip().lower()
                if not _is_individual_a_share(symbol):
                    continue
                by_code[symbol[2:]] = {"industry": name, "sector": str(sector_code)}
                members += 1
            if len(rows) < 100:
                break
        if members == 0:
            empty.append(sector_code)

    return {
        "by_code": by_code,
        "industries": len(industries),
        "failed_industries": failed,
        # An industry node that lists nobody is reported rather than counted as
        # mapped: its members would otherwise look like they have no industry.
        "empty_industries": empty,
    }


__all__ = [
    "fetch_all_a_share_symbols",
    "fetch_industry_membership",
    "SINA_MARKET_CENTER_URL",
    "DEFAULT_NODE",
    "TRANSPORTS",
]
