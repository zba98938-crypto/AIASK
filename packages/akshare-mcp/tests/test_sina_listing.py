"""The Sina listing source must enumerate the market, and admit when it cannot.

This is the only full-market listing reachable from this host: Tushare needs a
token, the TDX local vipdoc needs an installed client, ``push2.eastmoney.com``
is unreachable behind the proxy, and the TDX/TQCenter bridge needs a ``tqcenter``
module that is not installed. Everything here is offline — the network call is
stubbed — so the contract is asserted rather than observed.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

from akshare_mcp.data_source import sina_listing

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_free_data_sync():
    """Import ``scripts/free_data_daily_sync.py`` without running its CLI."""

    scripts = REPO_ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "_free_data_daily_sync_probe", scripts / "free_data_daily_sync.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("sh600519", True),
        ("sh688981", True),
        ("sz000001", True),
        ("sz300750", True),
        ("bj920000", True),
        ("bj430047", True),
        ("sh000001", False),  # 上证指数
        ("sz399300", False),  # 沪深300指数
        ("sz159915", False),  # ETF
        ("600519", False),  # 不带市场前缀
        ("", False),
    ],
)
def test_individual_a_share_filter(symbol: str, expected: bool) -> None:
    assert sina_listing._is_individual_a_share(symbol) is expected


def test_listing_raises_rather_than_returning_an_empty_market(monkeypatch) -> None:
    """Zero symbols is an outage, not an empty market.

    Returning ``[]`` there is how "the listing provider is down" became "there
    are no stocks", and it flowed into a sync that quietly did nothing.

    It also has to give up quickly: the first version kept retrying all 400 pages
    with a sleep between attempts, so an outage took five minutes to report.
    """

    def _no_pages(*_args, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(sina_listing, "_fetch_page", _no_pages)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="appears to be down"):
        sina_listing.fetch_all_a_share_symbols(retries=1, sleep_seconds=0)
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"an outage took {elapsed:.1f}s to report"


def test_listing_raises_when_pages_are_read_but_carry_nothing(monkeypatch) -> None:
    """A reachable endpoint returning nothing the filter accepts is also an error."""

    monkeypatch.setattr(sina_listing, "_fetch_page", lambda page, **_kwargs: [] if page > 1 else [{"symbol": "sh000001", "name": "上证指数"}])
    with pytest.raises(RuntimeError, match="no usable symbol"):
        sina_listing.fetch_all_a_share_symbols(retries=1, sleep_seconds=0)


def test_listing_reports_incomplete_pages_instead_of_hiding_them(monkeypatch) -> None:
    """One failed page must surface as ``complete=False``, not as a shorter list."""

    rows = [
        {"symbol": "sh600519", "name": "贵州茅台"},
        {"symbol": "sz000001", "name": "平安银行"},
    ]

    def _page(page, **_kwargs):
        if page == 2:
            raise OSError("one page is down")
        return rows if page == 1 else []

    monkeypatch.setattr(sina_listing, "_fetch_page", _page)
    result = sina_listing.fetch_all_a_share_symbols(retries=1, sleep_seconds=0)

    assert result["complete"] is False
    assert result["failed_pages"] == [2]
    assert len(result["symbols"]) == 2


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("600519", "sh600519"),
        ("688981", "sh688981"),
        ("000001", "sz000001"),
        ("300750", "sz300750"),
        # 北交所: the previous mapping sent these to `sz`, and Sina answered with
        # an empty list for all 350 of them while the sync called it a success.
        ("920000", "bj920000"),
        ("430047", "bj430047"),
        ("830799", "bj830799"),
        ("bj920001", "bj920001"),
    ],
)
def test_sina_symbol_maps_bse_to_bj(code: str, expected: str) -> None:
    module = _load_free_data_sync()
    assert module._sina_symbol(code) == expected
