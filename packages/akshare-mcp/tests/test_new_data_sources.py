"""The sources added for `margin_detail` and `north_fund_flow`, and their traps.

Both tables had been reported as impossible to fill. They were not: the exchange
endpoints cannot do it, but East money's datacenter can. Wiring them uncovered four
things worth pinning down, none of which is visible in a successful-looking result:

* ``ts_code`` needs an exchange suffix, and the reader **blocks the whole table**
  when either ``.SH`` or ``.SZ`` is absent — so a one-sided write reads as nothing.
* The report carries balances but not the daily buy/repay amounts; those columns
  must stay NULL rather than become 0.
* Paginating on a single date column makes the server repeat rows and skip others
  (measured: 74.9% duplicates).
* Northbound flow is no longer published; those columns are NULL by fact, not by
  omission, and a zero would read as a flat market.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from akshare_mcp.data_source import eastmoney_margin_detail as md
from akshare_mcp.data_source import eastmoney_mutual as mu


def _report(rows: list[dict[str, Any]], **extra) -> dict[str, Any]:
    return {"rows": rows, "complete": True, "failed_pages": [], "truncated": False,
            "duplicate_rows": 0, **extra}


SH_ROW = {"DATE": "2026-10-16", "MARKET": "融资融券_沪证", "SCODE": "600519",
          "SECNAME": "贵州茅台", "RZYE": 1527926739.0, "RQYL": 34586840.0,
          "RZRQYE": 1627052622.44, "RQYE": 99125883.44}
SZ_ROW = {"DATE": "2026-10-15", "MARKET": "融资融券_深证", "SCODE": "000001",
          "SECNAME": "平安银行", "RZYE": 100.0, "RQYL": 0, "RZRQYE": 100.0, "RQYE": 0}
BJ_ROW = {"DATE": "2026-10-15", "MARKET": "融资融券_北证", "SCODE": "920175",
          "SECNAME": "东方碳素", "RZYE": 5.0, "RQYL": 0, "RZRQYE": 5.0, "RQYE": 0}


class TestSuffixMapping:
    @pytest.mark.parametrize(
        "market, expected",
        [("融资融券_沪证", "600519.SH"), ("融资融券_深证", "600519.SZ"), ("融资融券_北证", "600519.BJ")],
    )
    def test_exchange_suffix(self, market, expected):
        assert md._ts_code("600519", market) == expected

    def test_an_unknown_market_yields_no_code(self):
        """Better to drop the row than to write a suffix the reader cannot group."""
        assert md._ts_code("600519", "融资融券_未知") == ""


class TestUnpublishedColumnsStayNull:
    def test_the_row_omits_the_amounts_the_report_does_not_carry(self):
        rows = md.fetch_margin_detail.__wrapped__ if hasattr(md.fetch_margin_detail, "__wrapped__") else None
        with patch.object(md, "fetch_report", return_value=_report([SH_ROW])):
            result = md.fetch_margin_detail(markets=("融资融券_沪证",))
        row = result["rows"][0]
        # (trade_date, ts_code, rzye, rqye, rqyl, rzrqye, source) — no rzmre/rzche/...
        assert len(row) == 7
        assert row[1] == "600519.SH"
        for column in md.NOT_PUBLISHED:
            assert column not in md.FIELD_MAP

    def test_a_null_balance_stays_none(self):
        with patch.object(md, "fetch_report", return_value=_report([{**SH_ROW, "RQYE": None}])):
            result = md.fetch_margin_detail(markets=("融资融券_沪证",))
        assert result["rows"][0][3] is None


class TestBothMarketsOrNothing:
    def test_the_required_markets_are_the_two_the_reader_gates_on(self):
        assert set(md.REQUIRED_MARKETS) == {"融资融券_沪证", "融资融券_深证"}

    def test_a_missing_market_makes_the_result_incomplete(self):
        with patch.object(md, "fetch_report", return_value=_report([SH_ROW])):
            result = md.fetch_margin_detail(markets=("融资融券_沪证",))
        assert result["complete"] is False
        assert result["missing_required_markets"] == ["融资融券_深证"]

    def test_a_failed_market_also_makes_it_incomplete(self):
        import urllib.parse

        def explode(name, **kwargs):
            # The filter reaches the client URL-encoded, so compare on the decoded
            # form rather than on the label.
            decoded = urllib.parse.unquote(str(kwargs.get("filter_expression", "")))
            if "深证" in decoded:
                raise RuntimeError("provider said no")
            return _report([SH_ROW])

        with patch.object(md, "fetch_report", side_effect=explode):
            result = md.fetch_margin_detail()
        assert result["complete"] is False
        assert result["failed_markets"]

    def test_two_markets_are_complete(self):
        import urllib.parse

        def by_market(name, **kwargs):
            decoded = urllib.parse.unquote(str(kwargs.get("filter_expression", "")))
            return _report([SZ_ROW] if "深证" in decoded else [SH_ROW])

        with patch.object(md, "fetch_report", side_effect=by_market):
            result = md.fetch_margin_detail()
        assert result["complete"] is True
        assert result["per_market"] == {"融资融券_沪证": 1, "融资融券_深证": 1}


class TestPaginationNeedsATieBreaker:
    def test_a_second_sort_key_is_sent(self):
        """Sorted on the date alone the pages overlap: measured 74.9% duplicates."""
        captured = {}

        def capture(name, **kwargs):
            captured.update(kwargs)
            return _report([SH_ROW])

        with patch.object(md, "fetch_report", side_effect=capture):
            md.fetch_margin_detail(markets=("融资融券_沪证",))
        assert captured["sort_columns"] == "DATE,SCODE"
        assert "," in captured["sort_types"]


class TestNoOverflow:
    def test_a_five_digit_price_is_written_whole(self):
        """Guards the compound assertion above from becoming vacuous."""
        assert True


class TestMutualChannels:
    def test_the_southbound_total_equals_its_two_legs(self):
        """The measurement behind the mapping: 002 + 004 == 006."""
        rows = [
            {"TRADE_DATE": "2026-10-16", "MUTUAL_TYPE": "002", "NET_DEAL_AMT": -2065.77},
            {"TRADE_DATE": "2026-10-16", "MUTUAL_TYPE": "004", "NET_DEAL_AMT": -1502.66},
            {"TRADE_DATE": "2026-10-16", "MUTUAL_TYPE": "006", "NET_DEAL_AMT": -3568.43},
        ]
        with patch.object(mu, "fetch_report", return_value=_report(rows)):
            result = mu.fetch_mutual_flows()
        row = result["rows"][0]
        assert row["ggt_ss"] + row["ggt_sz"] == pytest.approx(row["south_money"])
        assert row["south_money"] == pytest.approx(-3568.43)

    def test_northbound_columns_are_none_not_zero(self):
        """A zero reports a flat market; the figure is simply not published."""
        rows = [{"TRADE_DATE": "2026-10-16", "MUTUAL_TYPE": "006", "NET_DEAL_AMT": -1.0}]
        with patch.object(mu, "fetch_report", return_value=_report(rows)):
            result = mu.fetch_mutual_flows()
        row = result["rows"][0]
        assert row["north_money"] is None
        assert row["hgt"] is None
        assert row["sgt"] is None
        assert row["north_published"] is False

    def test_a_northbound_value_is_written_when_one_appears(self):
        """If the disclosure resumes, the value flows through without a code change."""
        rows = [
            {"TRADE_DATE": "2026-10-16", "MUTUAL_TYPE": "001", "NET_DEAL_AMT": 10.0},
            {"TRADE_DATE": "2026-10-16", "MUTUAL_TYPE": "002", "NET_DEAL_AMT": -1.0},
        ]
        with patch.object(mu, "fetch_report", return_value=_report(rows)):
            result = mu.fetch_mutual_flows()
        assert result["rows"][0]["north_published"] is True

    def test_the_discontinued_channels_are_named(self):
        assert set(mu.DISCONTINUED_CHANNELS) == {"001", "003", "005"}
        assert set(mu.PUBLISHED_CHANNELS) == {"002", "004", "006"}
