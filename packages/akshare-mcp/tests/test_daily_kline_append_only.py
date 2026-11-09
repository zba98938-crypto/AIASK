"""Real SQLite writes in a hermetic temporary database, never operator data."""
from datetime import date
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from _daily_kline_ingest import append_daily_rows, prepare_append, read_history


@pytest.fixture
def series(tmp_path):
    database = tmp_path / "bars.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE kline_1d(time TEXT,code TEXT,open REAL,high REAL,low REAL,close REAL,volume INTEGER,amount REAL,turnover REAL,change_pct REAL,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(time,code))")
        connection.executemany("INSERT INTO kline_1d(time,code,open,high,low,close,volume,amount) VALUES (?, '000001',10,12,9,11,1000,12345)", [(f"2026-10-{day:02d}T15:00:00+08:00",) for day in [26,27,28,29,30]])
    rows = [{"date": f"2026-10-{day:02d}", "code": "000001", "open":10,"high":12,"low":9,"close":11,"volume":1000,"amount":11001} for day in [26,27,28,29,30]]
    rows += [{"date":"2026-11-02","code":"000001","open":11,"high":13,"low":10,"close":12,"volume":2000,"amount":23000}]
    return database, {"channel":"eastmoney","rows":rows,"primary":"eastmoney","is_fallback":False,"attempts":[]}


def test_append_preserves_history_exactly_and_is_idempotent(series, tmp_path):
    database, fetched = series
    before = read_history(database, "000001")
    first = append_daily_rows(database,"000001",fetched,audit_dir=tmp_path/"audit",cutoff=date(2026,11,2))
    assert first["accepted_count"] == 1
    assert read_history(database, "000001")[1:] == before
    assert read_history(database, "000001")[0]["amount"] == 23000
    second = append_daily_rows(database,"000001",fetched,audit_dir=tmp_path/"audit",cutoff=date(2026,11,2))
    assert second["accepted_count"] == 0
    raw=gzip.decompress(Path(first["evidence"]["path"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == first["evidence"]["sha256"]
    assert json.loads(raw)["stored_history"] == before
    assert Path(first["receipt"]["path"]).exists()


@pytest.mark.parametrize("channel", ["sina", "tencent"])
def test_undisclosed_amount_stays_null_in_actual_database(series,tmp_path,channel):
    database, fetched = series
    fetched["channel"]=channel
    append_daily_rows(database,"000001",fetched,audit_dir=tmp_path/"audit",cutoff=date(2026,11,2))
    assert read_history(database,"000001")[0]["amount"] is None


@pytest.mark.parametrize("change,reason", [
    ("volume","stored_history_mismatch:volume"), ("adjustment","stored_history_mismatch:close"),
    ("code","bar_identity_invalid"), ("nan","invalid_provider_number"),
    ("weekend","nontrading_bar_date"), ("missing_volume","required_provider_number_missing"),
])
def test_invalid_source_cannot_write_or_create_receipt(series,tmp_path,change,reason):
    database, fetched = series
    if change=="volume":fetched["rows"][0]["volume"]*=100
    if change=="adjustment":fetched["rows"][0]["close"]=10
    if change=="code":fetched["rows"][0]["code"]="600519"
    if change=="nan":fetched["rows"][-1]["close"]=float("nan")
    if change=="weekend":fetched["rows"][-1]["date"]="2026-11-01"
    if change=="missing_volume":fetched["rows"][-1]["volume"]=None
    before=read_history(database,"000001")
    with pytest.raises(ValueError,match=reason):append_daily_rows(database,"000001",fetched,audit_dir=tmp_path/"audit",cutoff=date(2026,11,2))
    assert read_history(database,"000001")==before
    assert not (tmp_path/"audit/receipts").exists()
    assert list((tmp_path/"audit").rglob("*.json.gz"))


def test_unverified_security_and_insufficient_overlap_fail(series):
    database,fetched=series
    with pytest.raises(ValueError,match="stored_series_identity_unavailable"):prepare_append("000001",fetched,[],cutoff=date(2026,11,2))
    fetched["rows"]=fetched["rows"][-2:]
    with pytest.raises(ValueError,match="insufficient_settled_overlap"):prepare_append("000001",fetched,read_history(database,"000001"),cutoff=date(2026,11,2))


def test_audit_failure_prevents_all_writes(series,tmp_path):
    database,fetched=series
    before=read_history(database,"000001")
    blocked=tmp_path/"not-directory";blocked.write_text("blocked")
    with pytest.raises(OSError):append_daily_rows(database,"000001",fetched,audit_dir=blocked,cutoff=date(2026,11,2))
    assert read_history(database,"000001")==before


def test_unclosed_bar_is_not_written(series,tmp_path):
    database,fetched=series
    result=append_daily_rows(database,"000001",fetched,audit_dir=tmp_path/"audit",cutoff=date(2026,10,30))
    assert result["accepted_count"]==0


def test_weekday_exchange_holiday_is_refused(series, tmp_path):
    database, fetched = series
    settled_dates = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-28"]
    with sqlite3.connect(database) as connection:
        for old, new in zip([26,27,28,29,30], settled_dates):
            connection.execute("UPDATE kline_1d SET time=? WHERE time=?", (new, f"2026-10-{old:02d}T15:00:00+08:00"))
    for row, day in zip(fetched["rows"], settled_dates):
        row["date"] = day
    fetched["rows"][-1]["date"] = "2026-10-01"
    before = read_history(database, "000001")
    with pytest.raises(ValueError, match="nontrading_bar_date"):
        append_daily_rows(database, "000001", fetched, audit_dir=tmp_path/"audit", cutoff=date(2026,10,8))
    assert read_history(database, "000001") == before


def test_changed_series_detected_before_commit(series,tmp_path,monkeypatch):
    import _daily_kline_ingest as ingest
    database,fetched=series
    original=ingest.write_evidence
    def mutate(directory,document):
        result=original(directory,document)
        if directory.name=="plans":
            with sqlite3.connect(database) as connection:connection.execute("INSERT INTO kline_1d(time,code,open,high,low,close,volume) VALUES('2026-11-02','000001',10,12,9,11,1000)")
        return result
    monkeypatch.setattr(ingest,"write_evidence",mutate)
    with pytest.raises(ValueError,match="stored_series_changed_during_fetch"):append_daily_rows(database,"000001",fetched,audit_dir=tmp_path/"audit",cutoff=date(2026,11,2))
    with sqlite3.connect(database) as connection:assert connection.execute("SELECT COUNT(*) FROM kline_1d").fetchone()[0]==6


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_stored_calendar_day_cannot_be_selected_implicitly(series, tmp_path, conflicting):
    database, fetched = series
    # The date-only row sorts after the timestamp row in the read snapshot.
    # A dict keyed by calendar day would silently choose this matching row.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO kline_1d(time,code,open,high,low,close,volume,amount) "
            "VALUES ('2026-10-26','000001',10,12,9,11,1000,12345)"
        )
        if conflicting:
            connection.execute("UPDATE kline_1d SET close=10 WHERE time='2026-10-26T15:00:00+08:00'")
    before = read_history(database, "000001")
    with pytest.raises(ValueError, match="duplicate_stored_trade_date:000001:2026-10-26"):
        append_daily_rows(database, "000001", fetched, audit_dir=tmp_path / "audit", cutoff=date(2026, 11, 2))
    assert read_history(database, "000001") == before
    assert list((tmp_path / "audit/inputs").glob("*.json.gz"))
    assert not (tmp_path / "audit/receipts").exists()
