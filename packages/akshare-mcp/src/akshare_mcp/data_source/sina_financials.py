"""Financial indicators from Sina — an independent second source for ratios.

``financials`` is filled from East money's ``RPT_F10_FINANCE_MAINFINADATA``, one
report for all fourteen columns but also one provider. Sina publishes the same
indicators on a different endpoint, in a different format, from a different vendor,
which is what makes it usable as a fallback rather than a mirror.

It covers the ten ratio columns. The four absolute amounts (``revenue``,
``net_profit``) live on Sina's income-statement page, so a fallback cannot fill the
whole row — and it does not pretend to: a column with no value in the response is
left out of the row, and the caller records which ones were missing.

Units differ from East money in one place and are converted here: both state
``资产负债率`` as a percentage, while ``financials.debt_ratio`` is read as a
fraction by every consumer (``max(1.0 - debt_ratio, 0.0)``).
"""

from __future__ import annotations

import re
from typing import Any, Optional

import requests

SOURCE_NAME = "sina:FinancialGuideLine"
BASE_URL = (
    "https://money.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine"
    "/stockid/{code}/ctrl/{year}/displaytype/4.phtml"
)

# Sina's label -> the column it fills, and how to convert it.
# ``None`` means "store as reported" (already a percentage or a plain ratio).
#
# Which row to pick was settled by comparing each candidate against the stored
# East money value on the same report date, not by reading the labels. Sina
# publishes both a 摊薄 (fully diluted) and a 加权 (weighted) basis, and East
# money's `EPSJB`/`ROEJQ` are 基本/加权; taking 摊薄 matched 0 of 10 sampled
# periods while 加权 matched 9-10 of 10. roa is `总资产净利润率`, not
# `总资产利润率`, and roe is the weighted row for the same reason.
INDICATOR_MAP: dict[str, tuple[str, Optional[str]]] = {
    "加权每股收益(元)": ("eps", None),
    "每股净资产_调整后(元)": ("bvps", None),
    "加权净资产收益率(%)": ("roe", None),
    "销售毛利率(%)": ("gross_margin", None),
    "销售净利率(%)": ("net_margin", None),
    "总资产净利润率(%)": ("roa", None),
    "主营业务收入增长率(%)": ("revenue_growth", None),
    "净利润增长率(%)": ("profit_growth", None),
    "流动比率": ("current_ratio", None),
    # Sina states this as a percentage (21.3), the column is read as a fraction.
    "资产负债率(%)": ("debt_ratio", "percent_to_fraction"),
}


def _cells(row_html: str) -> list[str]:
    return [
        re.sub(r"<[^>]+>", "", cell).strip().replace("&nbsp;", "")
        for cell in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
    ]


def _to_float(text: str) -> Optional[float]:
    """Sina writes ``--`` for a value it does not have. That is not a zero."""

    cleaned = str(text).strip().replace(",", "")
    if not cleaned or cleaned in {"--", "-", "—"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_financial_indicators(code: str, year: int, *, timeout: float = 20.0) -> dict[str, Any]:
    """Return ``{report_date: {column: value}}`` for one symbol and one year.

    Raises when the page carries no table at all, so "the provider answered with
    nothing" is not read as "this company has no financials".
    """

    session = requests.Session()
    session.trust_env = False
    response = session.get(
        BASE_URL.format(code=str(code), year=int(year)),
        timeout=timeout,
        headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    html = response.text or ""

    rows: list[list[str]] = []
    for raw_row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [c for c in _cells(raw_row) if c]
        if len(cells) >= 2:
            rows.append(cells)
    if not rows:
        raise ValueError(f"sina financial page for {code}/{year} carried no table rows")

    dates: list[str] = []
    values: dict[str, list[Optional[float]]] = {}
    for cells in rows:
        label = cells[0]
        if label == "报告日期":
            dates = [c.strip()[:10] for c in cells[1:]]
            continue
        if label in INDICATOR_MAP:
            values[label] = [_to_float(c) for c in cells[1:]]

    if not dates:
        # The page rendered but the period row is absent, which means the layout
        # changed. Returning an empty result here would look like "no data".
        raise ValueError(f"sina financial page for {code}/{year} had no 报告日期 row")

    out: dict[str, Any] = {}
    for index, report_date in enumerate(dates):
        row: dict[str, Any] = {}
        for label, (column, convert) in INDICATOR_MAP.items():
            series = values.get(label) or []
            if index >= len(series):
                continue
            value = series[index]
            if value is None:
                # Omitted rather than zeroed: an unreported ratio and a ratio that
                # genuinely is zero are different facts.
                continue
            row[column] = value / 100.0 if convert == "percent_to_fraction" else value
        if row:
            row["report_date"] = report_date
            row["stock_code"] = str(code)
            row["source"] = SOURCE_NAME
            out[report_date] = row
    return out
