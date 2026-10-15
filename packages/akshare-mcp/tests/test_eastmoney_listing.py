"""The industry classification must arrive whole, and admit when it does not.

Sina's board list classified 48% of the market and counted ``次新股`` — a
listing-age bucket — as an industry, so industry-neutralised factor work only had
a label for half the universe. East money's F10 report covers all of it; these
tests pin the properties that make it safe to depend on, and they are offline, so
the contract is asserted rather than observed.
"""

from __future__ import annotations

from typing import Any

import pytest

from akshare_mcp.data_source import eastmoney_listing


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("金融-银行-股份制与城商行", ("金融", "银行", "金融-银行-股份制与城商行")),
        ("交运设备-汽车-汽车", ("交运设备", "汽车", "交运设备-汽车-汽车")),
        ("综合", ("综合", None, "综合")),
        ("  电子设备-半导体  ", ("电子设备", "半导体", "电子设备-半导体")),
        ("", (None, None, None)),
        (None, (None, None, None)),
        ("---", (None, None, None)),
    ],
)
def test_em2016_splits_into_levels(value, expected) -> None:
    """A level the taxonomy does not provide must be None, not invented."""

    assert eastmoney_listing.split_em2016(value) == expected


def _row(code: str, em2016: str | None, **extra: Any) -> dict:
    row = {"SECURITY_CODE": code, "SECURITY_NAME_ABBR": f"名称{code}", "EM2016": em2016}
    row.update(extra)
    return row


def _stub_pages(monkeypatch, pages: dict[int, list[dict]], *, count: int) -> None:
    def _fake(page, **_kwargs):
        if page not in pages:
            raise OSError(f"page {page} not stubbed")
        return {"data": pages[page], "count": count}

    monkeypatch.setattr(eastmoney_listing, "_fetch_page", _fake)
    monkeypatch.setattr(eastmoney_listing.time, "sleep", lambda *_a: None)


def test_classification_maps_codes_to_two_levels(monkeypatch) -> None:
    _stub_pages(
        monkeypatch,
        {
            1: [
                _row("600519", "食品饮料-酿酒-白酒", INDUSTRYCSRC1="制造业-酒、饮料和精制茶制造业"),
                _row("920000", "交运设备-汽车-汽车", INDUSTRYCSRC1="制造业-汽车制造业"),
            ]
        },
        count=2,
    )

    result = eastmoney_listing.fetch_industry_classification()

    assert result["source"] == "eastmoney:RPT_F10_BASIC_ORGINFO"
    assert result["complete"] is True
    assert result["rows"] == 2
    assert result["by_code"]["600519"]["industry"] == "食品饮料"
    assert result["by_code"]["600519"]["sector"] == "酿酒"
    assert result["by_code"]["600519"]["industry_path"] == "食品饮料-酿酒-白酒"
    assert result["by_code"]["920000"]["industry"] == "交运设备"
    # The two columns must come from one string, so they are always comparable.
    for entry in result["by_code"].values():
        if entry["sector"] is not None:
            assert entry["industry_path"].startswith(entry["industry"])


def test_a_row_without_a_classification_is_counted_not_filled(monkeypatch) -> None:
    _stub_pages(
        monkeypatch,
        {1: [_row("600519", "食品饮料-酿酒-白酒"), _row("000003", None)]},
        count=2,
    )

    result = eastmoney_listing.fetch_industry_classification()

    assert result["missing_industry"] == 1
    assert result["by_code"]["000003"]["industry"] is None
    assert result["by_code"]["000003"]["sector"] is None


def test_a_failed_page_is_named_and_the_result_is_not_complete(monkeypatch) -> None:
    _stub_pages(
        monkeypatch,
        {1: [_row("600519", "食品饮料-酿酒-白酒")], 3: [_row("000001", "金融-银行-银行")]},
        count=3,
    )

    result = eastmoney_listing.fetch_industry_classification(retries=1)

    assert result["complete"] is False
    assert result["failed_pages"] == [2]
    # A partial read is still returned; it just cannot pass for the whole market.
    assert len(result["by_code"]) == 2


def test_an_outage_raises_rather_than_returning_an_empty_market(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise OSError("datacenter unreachable")

    monkeypatch.setattr(eastmoney_listing, "_fetch_page", _boom)
    monkeypatch.setattr(eastmoney_listing.time, "sleep", lambda *_a: None)

    with pytest.raises(RuntimeError, match="eastmoney F10 unavailable"):
        eastmoney_listing.fetch_industry_classification(retries=1)


def test_paging_stops_at_the_reported_page_count(monkeypatch) -> None:
    """The loop must not keep asking past the end of the report."""

    asked: list[int] = []

    def _fake(page, **kwargs):
        asked.append(page)
        return {"data": [_row(f"{600000 + page:06d}", "综合")], "count": 2}

    monkeypatch.setattr(eastmoney_listing, "_fetch_page", _fake)
    result = eastmoney_listing.fetch_industry_classification(page_size=1, retries=1)

    assert asked == [1, 2]
    assert result["pages"] == 2
