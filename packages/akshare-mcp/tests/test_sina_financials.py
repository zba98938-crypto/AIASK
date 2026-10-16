"""Parsing behaviour of the Sina financial-indicator page.

The page is HTML with a 报告日期 row followed by one row per indicator, each
holding one cell per period. The module exists as a *diagnostic* for the single
provider behind ``financials``, not as a fallback, because several columns carry
different conventions between the two vendors. These tests cover the parsing and
the unit handling; the convention question is measured by
``scripts/check_financials_agreement.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from akshare_mcp.data_source import sina_financials as sf


def _page(*rows: str) -> MagicMock:
    response = MagicMock()
    response.text = "<html><body><table>" + "".join(rows) + "</table></body></html>"
    response.raise_for_status.return_value = None
    return response


def _row(*cells: str) -> str:
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


PERIODS = _row("报告日期", "2025-12-31", "2025-09-30")


class TestParsing:
    def test_indicators_are_aligned_to_the_period_row(self):
        """Each indicator cell belongs to the period in the same column."""

        html = _page(
            PERIODS,
            _row("加权净资产收益率(%)", "6.89", "15.37"),
            _row("加权每股收益(元)", "2.3068", "1.668"),
        )
        with patch("requests.Session.get", return_value=html):
            result = sf.fetch_financial_indicators("000858", 2025)

        assert result["2025-12-31"]["roe"] == 6.89
        assert result["2025-09-30"]["roe"] == 15.37
        assert result["2025-12-31"]["eps"] == 2.3068
        assert result["2025-09-30"]["eps"] == 1.668

    def test_debt_ratio_is_converted_to_a_fraction(self):
        """Sina states 资产负债率 as a percentage; the column is read as a fraction.

        Every consumer computes ``1.0 - debt_ratio``, so writing 35.69 would make
        the term negative for every company in the table.
        """

        html = _page(PERIODS, _row("资产负债率(%)", "35.6891", "19.259"))
        with patch("requests.Session.get", return_value=html):
            result = sf.fetch_financial_indicators("000858", 2025)

        assert result["2025-12-31"]["debt_ratio"] == pytest.approx(0.356891)
        assert result["2025-09-30"]["debt_ratio"] == pytest.approx(0.19259)


class TestHonesty:
    def test_a_missing_value_is_omitted_not_zeroed(self):
        """Sina writes ``--`` for an unreported ratio. Zero is a different fact.

        The period is absent from the result rather than present with an empty
        body, so a caller reading ``result[period]`` cannot mistake "no values
        published" for "values published, all zero".
        """

        html = _page(PERIODS, _row("加权净资产收益率(%)", "--", "15.37"))
        with patch("requests.Session.get", return_value=html):
            result = sf.fetch_financial_indicators("000858", 2025)

        assert "2025-12-31" not in result, "a period with no published value has no row"
        assert result["2025-09-30"]["roe"] == 15.37

    def test_a_period_with_no_usable_value_is_left_out_entirely(self):
        html = _page(_row("报告日期", "2025-12-31"), _row("加权净资产收益率(%)", "--"))
        with patch("requests.Session.get", return_value=html):
            result = sf.fetch_financial_indicators("000858", 2025)

        assert result == {}

    def test_a_page_with_no_table_raises_rather_than_returning_empty(self):
        """An empty result would read as 'this company has no financials'."""

        response = MagicMock()
        response.text = "<html><body><p>no table here</p></body></html>"
        response.raise_for_status.return_value = None
        with patch("requests.Session.get", return_value=response):
            with pytest.raises(ValueError, match="no table rows"):
                sf.fetch_financial_indicators("000858", 2025)

    def test_a_page_without_the_period_row_raises(self):
        """The table rendered but the layout changed — do not guess the alignment."""

        html = _page(_row("加权净资产收益率(%)", "6.89"))
        with patch("requests.Session.get", return_value=html):
            with pytest.raises(ValueError, match="报告日期"):
                sf.fetch_financial_indicators("000858", 2025)

    def test_a_short_indicator_row_does_not_shift_other_periods(self):
        """A row with fewer cells than the period row must not be padded with trailing values."""

        html = _page(
            _row("报告日期", "2025-12-31", "2025-09-30", "2025-06-30"),
            _row("加权净资产收益率(%)", "6.89"),
        )
        with patch("requests.Session.get", return_value=html):
            result = sf.fetch_financial_indicators("000858", 2025)

        assert result["2025-12-31"]["roe"] == 6.89
        assert "2025-09-30" not in result, "the short row must not be padded into later periods"
        assert "2025-06-30" not in result


class TestMapping:
    def test_only_ratio_columns_are_mapped(self):
        """Sina's ratio page cannot fill the absolute amounts.

        ``revenue`` and ``net_profit`` come from the income statement, on another
        page, so a caller wiring this up must not expect a whole row.
        """

        columns = {column for column, _ in sf.INDICATOR_MAP.values()}
        assert {"eps", "roe", "roa", "debt_ratio", "current_ratio"} <= columns
        assert "revenue" not in columns
        assert "net_profit" not in columns

    def test_the_weighted_basis_is_used_not_the_diluted_one(self):
        """Sina publishes both bases; East money's EPSJB/ROEJQ are 基本/加权.

        Measured against stored values on the same report date, the 摊薄 row
        matched 0 of 10 sampled periods while 加权 matched 9-10 of 10. Picking the
        wrong row here would look like a vendor disagreement.
        """

        labels = set(sf.INDICATOR_MAP)
        assert "加权每股收益(元)" in labels
        assert "加权净资产收益率(%)" in labels
        assert "摊薄每股收益(元)" not in labels
        assert "净资产收益率(%)" not in labels, "the unweighted row is a different basis"
        assert "总资产净利润率(%)" in labels
