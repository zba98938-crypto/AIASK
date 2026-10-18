"""The datacenter client and the sync that fills the empty market tables.

Everything here is offline: the report fetch is stubbed, so the contract is
asserted rather than observed. The properties under test are the ones that keep a
partial read from passing for a whole one, and keep a field the report does not
carry from being written as a number.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from akshare_mcp.data_source import eastmoney_datacenter

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_sync():
    scripts = REPO_ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "_sync_market_fundamentals_probe", scripts / "sync_market_fundamentals.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------


def _stub(monkeypatch, pages, *, count):
    def _fake(report, page, **_kwargs):
        if page not in pages:
            raise OSError(f"page {page} not stubbed")
        return {"data": pages[page], "count": count}

    monkeypatch.setattr(eastmoney_datacenter, "_fetch_page", _fake)
    monkeypatch.setattr(eastmoney_datacenter.time, "sleep", lambda *_a: None)


def test_a_whole_report_reports_itself_complete(monkeypatch) -> None:
    _stub(monkeypatch, {1: [{"A": 1}, {"A": 2}]}, count=2)

    result = eastmoney_datacenter.fetch_report("RPT_LICO_FN_CPD")

    assert result["complete"] is True
    assert result["truncated"] is False
    assert result["failed_pages"] == []
    assert len(result["rows"]) == 2
    assert result["source"] == "eastmoney:RPT_LICO_FN_CPD"


def test_a_row_cap_is_reported_as_truncation_not_a_small_dataset(monkeypatch) -> None:
    """A cap must not be indistinguishable from the report simply being short."""

    _stub(monkeypatch, {1: [{"A": i} for i in range(10)]}, count=1000)

    result = eastmoney_datacenter.fetch_report("RPT_LICO_FN_CPD", max_rows=10)

    assert result["truncated"] is True
    assert result["complete"] is False
    assert len(result["rows"]) == 10


def test_a_failed_page_makes_the_read_incomplete_and_is_named(monkeypatch) -> None:
    _stub(monkeypatch, {1: [{"A": 1}], 3: [{"A": 3}]}, count=3)

    result = eastmoney_datacenter.fetch_report("RPT_LICO_FN_CPD", retries=1)

    assert result["complete"] is False
    assert result["failed_pages"] == [2]
    assert len(result["rows"]) == 2


def test_reading_nothing_raises_instead_of_returning_an_empty_dataset(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise OSError("datacenter unreachable")

    monkeypatch.setattr(eastmoney_datacenter, "_fetch_page", _boom)
    monkeypatch.setattr(eastmoney_datacenter.time, "sleep", lambda *_a: None)

    with pytest.raises(RuntimeError, match="unavailable"):
        eastmoney_datacenter.fetch_report("RPT_LICO_FN_CPD", retries=1)


def test_paging_stops_at_the_reported_page_count(monkeypatch) -> None:
    asked: list[int] = []

    def _fake(report, page, **_kwargs):
        asked.append(page)
        return {"data": [{"A": page}], "count": 2}

    monkeypatch.setattr(eastmoney_datacenter, "_fetch_page", _fake)
    eastmoney_datacenter.fetch_report("RPT_LICO_FN_CPD", page_size=1, retries=1)

    assert asked == [1, 2]


# --------------------------------------------------------------------------
# the sync
# --------------------------------------------------------------------------


@pytest.fixture()
def ledger(tmp_path):
    """A database with only the columns the sync touches."""

    conn = sqlite3.connect(str(tmp_path / "ledger.sqlite3"))
    conn.executescript(
        """
        CREATE TABLE stocks (stock_code TEXT PRIMARY KEY, list_status TEXT DEFAULT 'L');
        CREATE TABLE financials (
            stock_code TEXT NOT NULL, report_date TEXT NOT NULL,
            revenue REAL, net_profit REAL, gross_margin REAL, net_margin REAL,
            debt_ratio REAL, current_ratio REAL, eps REAL, roe REAL, bvps REAL,
            roa REAL, revenue_growth REAL, profit_growth REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (stock_code, report_date)
        );
        CREATE TABLE block_stocks (
            id INTEGER PRIMARY KEY, block_code TEXT NOT NULL, stock_code TEXT NOT NULL,
            stock_name TEXT, weight REAL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (block_code, stock_code)
        );
        CREATE TABLE market_blocks (
            id INTEGER PRIMARY KEY, block_code TEXT NOT NULL, block_name TEXT NOT NULL,
            block_type TEXT NOT NULL, stock_count INTEGER DEFAULT 0,
            avg_change_pct REAL, total_amount REAL, leader_code TEXT, leader_name TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE (block_code, block_type)
        );
        CREATE TABLE trading_dates (
            trade_date TEXT PRIMARY KEY, exchange TEXT DEFAULT 'SSE',
            is_open INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        -- The registry the readers gate on: rows here, not rows in the data
        -- tables, decide whether a dataset counts as usable.
        CREATE TABLE tdx_data_completeness (
            data_key TEXT PRIMARY KEY, status TEXT NOT NULL, as_of_date TEXT,
            row_count INTEGER DEFAULT 0, detail TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.executemany("INSERT INTO stocks (stock_code) VALUES (?)", [("600519",), ("000001",)])
    conn.commit()
    yield conn
    conn.close()


_ARGS = SimpleNamespace(apply=True, financial_years=5, max_rows=0)


def _main_finance_rows():
    """One row of the F10 main-financial-indicator report, which carries every
    column `financials` stores."""

    return [
        {
            "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "REPORT_DATE": "2026-06-30 00:00:00",
            "TOTALOPERATEREVE": 8.9e10,
            "PARENTNETPROFIT": 4.5e10,
            "XSMLL": 91.5,          # 销售毛利率 %
            "XSJLL": 50.6,          # 销售净利率 %
            "ZCFZL": 21.3,          # 资产负债率 %, stored as a fraction
            "LD": 3.4,              # 流动比率
            "EPSJB": 35.6,
            "ROEJQ": 17.2,
            "BPS": 200.0,
            "ZZCJLL": 12.1,         # 总资产净利率 %
            "TOTALOPERATEREVETZ": 9.1,
            "PARENTNETPROFITTZ": 8.4,
        },
        # Outside this project's universe: must not be written.
        {
            "SECURITY_CODE": "400001",
            "SECURITY_NAME_ABBR": "某新三板",
            "REPORT_DATE": "2026-06-30 00:00:00",
            "TOTALOPERATEREVE": 1.0,
        },
    ]


def _earnings_rows():
    """The earnings report, which is what carries the industry board."""

    return [
        {
            "SECURITY_CODE": "600519",
            "SECURITY_NAME_ABBR": "贵州茅台",
            "REPORTDATE": "2026-06-30 00:00:00",
            "BOARD_CODE": "BK0477",
            "BOARD_NAME": "酿酒行业",
        },
        {
            "SECURITY_CODE": "400001",
            "SECURITY_NAME_ABBR": "某新三板",
            "REPORTDATE": "2026-06-30 00:00:00",
            "BOARD_CODE": "BK0001",
            "BOARD_NAME": "别的行业",
        },
    ]


def test_financials_map_all_fourteen_columns_from_one_report(ledger, monkeypatch) -> None:
    """The four balance-sheet and profitability columns are populated, and
    `debt_ratio` is converted because its consumers read it as a fraction."""

    sync = _load_sync()
    monkeypatch.setattr(
        sync,
        "_report",
        lambda *_a, **_k: {"rows": _main_finance_rows(), "source": "eastmoney:test",
                           "complete": True, "truncated": False, "failed_pages": []},
    )

    report = sync.sync_financials(ledger, _ARGS)

    stored = ledger.execute(
        "SELECT revenue, net_profit, gross_margin, net_margin, debt_ratio, current_ratio, "
        "eps, roe, bvps, roa, revenue_growth, profit_growth FROM financials WHERE stock_code='600519'"
    ).fetchone()
    assert stored[0] == pytest.approx(8.9e10)
    assert stored[1] == pytest.approx(4.5e10)
    assert stored[2] == pytest.approx(91.5)
    assert stored[3] == pytest.approx(50.6)
    # 21.3% in the report, 0.213 in the table: `1.0 - debt_ratio` only means
    # anything on a fraction, and every consumer reads it that way.
    assert stored[4] == pytest.approx(0.213)
    assert stored[5] == pytest.approx(3.4)
    assert stored[6] == pytest.approx(35.6)
    assert stored[7] == pytest.approx(17.2)
    assert stored[8] == pytest.approx(200.0)
    assert stored[9] == pytest.approx(12.1)
    assert stored[10] == pytest.approx(9.1)
    assert stored[11] == pytest.approx(8.4)
    assert report["unmapped_columns"] == []
    # Only this project's universe is written.
    assert ledger.execute("SELECT count(*) FROM financials").fetchone()[0] == 1
    assert report["skipped_out_of_universe"] == 1


def test_a_second_run_updates_the_columns_it_added(ledger, monkeypatch) -> None:
    """The production path is a conflict, not an insert.

    The first version of this change updated the mapping but not the upsert's
    ``DO UPDATE`` list, so the four new columns were written only for rows that
    did not exist yet — 18 of 119,837. Every existing row kept its NULL, and the
    test passed because a fresh test database never hits the conflict branch.
    """

    sync = _load_sync()
    rows = _main_finance_rows()
    monkeypatch.setattr(
        sync,
        "_report",
        lambda *_a, **_k: {"rows": rows, "source": "eastmoney:test",
                           "complete": True, "truncated": False, "failed_pages": []},
    )

    # First run inserts the row.
    sync.sync_financials(ledger, _ARGS)
    # Somebody else's older sync had left these columns empty.
    ledger.execute(
        "UPDATE financials SET net_margin=NULL, debt_ratio=NULL, current_ratio=NULL, roa=NULL "
        "WHERE stock_code='600519'"
    )
    ledger.commit()

    # Second run must fill them in on the conflict path.
    sync.sync_financials(ledger, _ARGS)

    stored = ledger.execute(
        "SELECT net_margin, debt_ratio, current_ratio, roa FROM financials WHERE stock_code='600519'"
    ).fetchone()
    assert stored == (pytest.approx(50.6), pytest.approx(0.213), pytest.approx(3.4), pytest.approx(12.1))


def test_a_missing_debt_ratio_stays_null_rather_than_becoming_zero(ledger, monkeypatch) -> None:
    """A zero debt ratio passes every leverage ceiling; absence must not."""

    sync = _load_sync()
    rows = _main_finance_rows()
    rows[0]["ZCFZL"] = None
    monkeypatch.setattr(
        sync,
        "_report",
        lambda *_a, **_k: {"rows": rows, "source": "eastmoney:test",
                           "complete": True, "truncated": False, "failed_pages": []},
    )

    sync.sync_financials(ledger, _ARGS)

    assert ledger.execute(
        "SELECT debt_ratio FROM financials WHERE stock_code='600519'"
    ).fetchone()[0] is None


def test_blocks_write_membership_and_the_board_that_holds_it(ledger, monkeypatch) -> None:
    sync = _load_sync()
    monkeypatch.setattr(
        sync,
        "_report",
        lambda *_a, **_k: {"rows": _earnings_rows(), "source": "eastmoney:test",
                           "complete": True, "truncated": False, "failed_pages": []},
    )

    report = sync.sync_blocks_from_finance(ledger, _ARGS)

    assert ledger.execute("SELECT block_code, stock_code FROM block_stocks").fetchall() == [("BK0477", "600519")]
    board = ledger.execute(
        "SELECT block_name, block_type, stock_count FROM market_blocks WHERE block_code='BK0477'"
    ).fetchone()
    assert board == ("酿酒行业", "industry", 1)
    assert report["stocks_with_a_board"] == 1
    assert report["boards"] == 1


def test_calendar_is_derived_from_the_prices_it_describes(ledger) -> None:
    """The calendar comes from the klines, so it cannot disagree with them."""

    sync = _load_sync()
    ledger.executescript(
        """
        CREATE TABLE kline_1d (time TEXT NOT NULL, code TEXT NOT NULL, close REAL, PRIMARY KEY (time, code));
        INSERT INTO kline_1d (time, code, close) VALUES
            ('2026-10-13T15:00:00+08:00', '600519', 1.0),
            ('2026-10-13T15:00:00+08:00', '000001', 1.0),
            ('2026-10-15T15:00:00+08:00', '600519', 1.0);
        """
    )
    ledger.commit()

    report = sync.sync_calendar(ledger, apply=True)

    assert report["derived_dates"] == 2
    assert ledger.execute("SELECT trade_date FROM trading_dates ORDER BY 1").fetchall() == [
        ("20261013",),
        ("20261015",),
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("calendar,blocks", ["calendar", "blocks"]),
        # `margin_detail` and `north` were added when their sources were found:
        # per-security margin from RPTA_WEB_RZRQ_GGMX and the mutual-market flows
        # from RPT_MUTUAL_DEAL_HISTORY.
        ("all", ["calendar", "financials", "fundflow", "dragon", "blocks", "margin",
                 "margin_detail", "north"]),
    ],
)
def test_category_selection(raw: str, expected: list[str]) -> None:
    assert _load_sync()._selected(raw) == expected


def test_an_unknown_category_is_refused() -> None:
    with pytest.raises(SystemExit, match="unknown category"):
        _load_sync()._selected("calendar,stocks")
