"""Audited incremental bar ingestion that never rewrites an existing date."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("Asia/Shanghai")


def write_evidence(directory: Path, document: Any) -> dict[str, Any]:
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json.gz"
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            with gzip.GzipFile(filename="", fileobj=handle, mode="wb", mtime=0) as compressed:
                compressed.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"path": str(path.resolve()), "sha256": digest}


def closed_date(now: datetime | None = None) -> date:
    now = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
    # Leave the close feed time to settle before ingesting today's daily bar.
    return now.date() if now.time() >= time(16) else now.date() - timedelta(days=1)


def read_history(database: Path, code: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT * FROM kline_1d WHERE code=? ORDER BY time DESC LIMIT 300", (code,)
        )]
    finally:
        connection.close()


def _number(value: Any, *, required: bool = False) -> float | None:
    if value is None:
        if required:
            raise ValueError("required_provider_number_missing")
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_provider_number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid_provider_number")
    return number


def prepare_append(code: str, fetched: dict, history: list[dict], *, cutoff: date) -> dict:
    """Verify source identity/units before planning strictly new closed bars."""
    if fetched.get("all_channels_failed") or not fetched.get("rows"):
        raise ValueError("all_channels_failed")
    channel = fetched.get("channel")
    if channel not in {"sina", "eastmoney", "tencent"}:
        raise ValueError("unknown_bar_provider")
    stored: dict[str, dict] = {}
    for row in history:
        day = str(row["time"])[:10]
        if day in stored:
            # Date-only and timestamp keys can coexist in legacy storage.
            # Selecting either would hide the other raw observation.
            raise ValueError(f"duplicate_stored_trade_date:{code}:{day}")
        stored[day] = row
    if not stored:
        raise ValueError("stored_series_identity_unavailable")
    last_day = max(stored)
    rows: dict[str, dict] = {}
    for raw in fetched["rows"]:
        day = date.fromisoformat(str(raw.get("date")))
        if str(raw.get("date")) != day.isoformat() or raw.get("code") != code:
            raise ValueError("bar_identity_invalid")
        if day > cutoff:
            continue
        if day.weekday() >= 5:
            raise ValueError("nontrading_bar_date")
        values = {key: _number(raw.get(key), required=True) for key in ("open", "high", "low", "close", "volume")}
        if min(values[key] for key in ("open", "high", "low", "close")) <= 0:
            raise ValueError("nonpositive_price")
        if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"]):
            raise ValueError("invalid_ohlc")
        if values["volume"] != int(values["volume"]):
            raise ValueError("fractional_share_volume")
        # Only Eastmoney f57 is an actual turnover observation in these feeds.
        values["amount"] = _number(raw.get("amount")) if channel == "eastmoney" else None
        values["turnover"] = _number(raw.get("turnover"))
        row = {"date": day.isoformat(), "code": code, **values}
        if row["date"] in rows and rows[row["date"]] != row:
            raise ValueError("conflicting_duplicate_provider_date")
        rows[row["date"]] = row
    overlaps = sorted(set(rows) & set(stored))
    if len(overlaps) < 5:
        raise ValueError("insufficient_settled_overlap")
    for day in overlaps:
        old, new = stored[day], rows[day]
        for key in ("open", "high", "low", "close", "volume"):
            a, b = _number(old.get(key), required=True), new[key]
            tolerance = 0.01 if key == "volume" else 1e-4
            if abs(a - b) > tolerance * max(abs(a), 1e-12):
                raise ValueError(f"stored_history_mismatch:{key}:{day}")
    from strategy_factory.domain.trading_calendar import get_trading_calendar

    calendar = get_trading_calendar()
    new_rows = [rows[day] for day in sorted(rows) if day > last_day]
    if any(not calendar.is_trading_day(date.fromisoformat(row["date"])) for row in new_rows):
        raise ValueError("nontrading_bar_date")
    previous = float(stored[last_day]["close"])
    for row in new_rows:
        row["change_pct"] = (row["close"] / previous - 1) * 100
        previous = row["close"]
    return {
        "code": code, "channel": channel, "stored_last_date": last_day,
        "overlap_count": len(overlaps), "new_rows": new_rows,
        "historical_rows_untouched": True,
        "amount_basis": "provider_f57" if channel == "eastmoney" else "unavailable",
        "amount_unavailable_reason": "source_does_not_publish_amount" if channel != "eastmoney" else None,
    }


def append_daily_rows(database: Path, code: str, fetched: dict, *, audit_dir: Path, cutoff: date | None = None) -> dict:
    history = read_history(database, code)
    # Rejected histories are evidence too; persist them before validation.
    try:
        inputs = write_evidence(audit_dir / "inputs", {
            "observed_at": datetime.now(MARKET_TZ).isoformat(),
            "code": code, "fetched": fetched, "stored_history": history,
        })
    except ValueError as exc:
        write_evidence(audit_dir / "invalid_inputs", {
            "code": code, "input_repr": repr(fetched), "stored_history_repr": repr(history),
            "reason": "invalid_provider_number", "serialization_error": str(exc),
        })
        raise ValueError("invalid_provider_number") from exc
    plan = prepare_append(code, fetched, history, cutoff=cutoff or closed_date())
    # Persist inputs and the complete write intent before opening a writer.
    evidence = write_evidence(audit_dir / "plans", {
        "observed_at": datetime.now(MARKET_TZ).isoformat(),
        "plan": plan, "fetched": fetched, "stored_history": history, "inputs": inputs,
    })
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
    inserted = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        latest = connection.execute("SELECT MAX(substr(time,1,10)) FROM kline_1d WHERE code=?", (code,)).fetchone()[0]
        if latest != plan["stored_last_date"]:
            raise ValueError("stored_series_changed_during_fetch")
        for row in plan["new_rows"]:
            cursor = connection.execute(
                "INSERT INTO kline_1d(time,code,open,high,low,close,volume,amount,turnover,change_pct) "
                "SELECT ?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM kline_1d WHERE code=? AND substr(time,1,10)=?)",
                (row["date"] + "T15:00:00+08:00", code, *(row[key] for key in ("open", "high", "low", "close", "volume", "amount", "turnover", "change_pct")), code, row["date"]),
            )
            inserted += cursor.rowcount
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    result = {key: value for key, value in plan.items() if key != "new_rows"}
    result.update(accepted_count=inserted, rejected_count=0, planned_count=len(plan["new_rows"]), evidence=evidence)
    result["missing_amount_count"] = sum(row["amount"] is None for row in plan["new_rows"])
    result["receipt"] = write_evidence(audit_dir / "receipts", result)
    return result
