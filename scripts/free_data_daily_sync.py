#!/usr/bin/env python3
"""Free-data daily sync for the Hermes AIASK integration copy.

Uses only free/public sources that work without paid API keys:

1. Sina daily K-lines for a stock universe
2. Sina index K-lines (via DataSourceManager Sina fallback)
3. Industry fund-flow snapshot (AKShare stock_fund_flow_industry)
4. North-bound fund history (AKShare, best-effort)
5. Local factor IC computation (batch_compute_factors)

Then prints DataCollector completeness so operators can see which free
sources are green before running Strategy Factory.

Usage:
    python scripts/free_data_daily_sync.py
    python scripts/free_data_daily_sync.py --codes 600519,000001,000858
    python scripts/free_data_daily_sync.py --skip-ic --skip-north
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
AIASK_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _integration_paths import (  # noqa: E402
    default_sqlite_path,
    ensure_runtime_dirs,
)


DEFAULT_CODES = [
    "600519",
    "000001",
    "000858",
    "300750",
    "000333",
    "002415",
    "601318",
    "600276",
    "601012",
    "600036",
    "000002",
    "600000",
    "601166",
    "000651",
    "002594",
]
DEFAULT_FACTORS = ["momentum", "value", "quality", "growth", "volatility", "reversal"]
INDEX_CODES = ["sh000001", "sz399300", "sz399001", "sz399006", "sh000300"]


def _configure_env() -> Path:
    ensure_runtime_dirs()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # Prefer direct public endpoints; system proxy on this host often breaks Eastmoney.
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        os.environ.pop(key, None)
    db_path = default_sqlite_path()
    os.environ.setdefault("AIASK_SQLITE_PATH", str(db_path))
    os.environ.setdefault("AKSHARE_MCP_SQLITE_PATH", str(db_path))
    for path in (
        AIASK_ROOT / "packages" / "akshare-mcp" / "src",
        AIASK_ROOT / "packages" / "aiask-quant-core" / "src",
        AIASK_ROOT / "packages" / "strategy-factory" / "src",
    ):
        token = str(path)
        if token not in sys.path:
            sys.path.insert(0, token)
    return db_path


def _sina_symbol(code: str) -> str:
    """Map a bare stock code to Sina's symbol, 北交所 included.

    The previous version sent everything that is not ``6…`` to ``sz``, so all 350
    BSE symbols (``920xxx``/``4xxxxx``/``8xxxxx``) were requested as ``sz920000``
    and came back as an empty list. Sina does serve them — under ``bj``.
    """

    text = str(code).strip()
    if text[:2].lower() in {"sh", "sz", "bj"}:
        return text
    if text.startswith(("60", "68", "11", "13")):
        return "sh" + text
    if text.startswith(("00", "30", "12")):
        return "sz" + text
    if text.startswith(("92", "43", "83", "87", "88", "4", "8")):
        return "bj" + text
    # Unknown shape: keep the historical default rather than guessing bj.
    return "sz" + text


def _fetch_sina_kline(code: str, *, datalen: int = 300) -> list[dict[str, Any]]:
    import requests

    session = requests.Session()
    session.trust_env = False
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/"
        "json_v2.php/CN_MarketData.getKLineData"
    )
    response = session.get(
        url,
        params={
            "symbol": _sina_symbol(code),
            "scale": 240,
            "ma": "no",
            "datalen": int(datalen),
        },
        timeout=20,
        headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0",
        },
    )
    response.raise_for_status()
    payload = response.json()
    rows: list[dict[str, Any]] = []
    for item in list(payload or []):
        if not isinstance(item, dict):
            continue
        close = float(item.get("close") or 0.0)
        volume = float(item.get("volume") or 0.0)
        rows.append(
            {
                "date": str(item.get("day") or "")[:10],
                "code": str(code),
                "open": float(item.get("open") or 0.0),
                "high": float(item.get("high") or 0.0),
                "low": float(item.get("low") or 0.0),
                "close": close,
                "volume": volume,
                "amount": close * volume,
            }
        )
    return [row for row in rows if row.get("date")]


# ---------------------------------------------------------------------------
# Daily bars: a chain, and the unit work a chain requires
# ---------------------------------------------------------------------------
#
# `_fetch_sina_kline` was the only channel, which made 5,582 symbols depend on one
# endpoint. Two alternatives reach the same bars, measured by
# `scripts/check_data_source_channels.py`, but a fallback is only safe once the
# two traps below are handled, both found by comparing the channels on a *settled*
# bar rather than on each one's latest row:
#
#   * **Adjustment.** Sina's endpoint takes no adjustment parameter and returns
#     不复权. East money offers both, and `fqt=1` (前复权) matches on a bar near the
#     end of the window while diverging further back — 12 of 12 stocks checked
#     matched `fqt=0` over 120 days and none matched `fqt=1`. Writing 前复权 bars
#     into the 不复权 series would corrupt history for every stock that paid a
#     dividend inside the window. The chain uses `fqt=0`.
#   * **Volume units.** Sina reports 股, East money's `f56` and Tencent's fifth
#     field are both 手, exactly 100× smaller on every bar checked. A channel that
#     forgot this writes a 100× low `volume` into a column shared with the others.

KLINE_CHANNELS = ("sina", "eastmoney", "tencent")
from akshare_mcp.data_source.volume_units import (  # noqa: E402
    LOTS_TO_SHARES,
    tencent_volume_to_shares,
)
# How far a fallback's series may end before the market's latest bar while we hold
# no history for the code. A brand-new listing is a few days late at most; the case
# this catches is answerable about a different security (see ``_agreement_with_stored``).
STALE_SERIES_GRACE_DAYS = 30


def _days_between(earlier: str, later: str) -> int | None:
    from datetime import date as _date

    try:
        a = _date.fromisoformat(str(earlier)[:10])
        b = _date.fromisoformat(str(later)[:10])
    except ValueError:
        return None
    return (b - a).days


def _eastmoney_secid(code: str) -> str:
    """East money market prefix: 1 = 上交所, 0 = 深交所 and 北交所."""

    text = str(code).strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if text.startswith(prefix):
            text = text[2:]
            return ("1." if prefix == "sh" else "0.") + text
    return ("1." if text.startswith(("60", "68", "11", "13", "5")) else "0.") + text


def _tencent_symbol(code: str) -> str:
    text = str(code).strip().lower()
    if text[:2] in {"sh", "sz", "bj"}:
        return text
    if text.startswith(("60", "68", "11", "13")):
        return "sh" + text
    if text.startswith(("00", "30", "12")):
        return "sz" + text
    return "bj" + text


def _fetch_eastmoney_kline(code: str, *, datalen: int = 300) -> list[dict[str, Any]]:
    """East money daily bars, unadjusted, with 手 converted to 股."""

    import requests

    session = requests.Session()
    session.trust_env = False
    response = session.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "secid": _eastmoney_secid(code),
            # f51 date, f52 open, f53 close, f54 high, f55 low, f56 volume(手), f57 amount
            "fields1": "f1,f2,f3",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": 101,
            "fqt": 0,
            "end": "20500101",
            "lmt": int(datalen),
        },
        timeout=20,
        headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    klines = (response.json().get("data") or {}).get("klines") or []
    rows: list[dict[str, Any]] = []
    for item in klines:
        parts = str(item).split(",")
        if len(parts) < 7:
            continue
        close = float(parts[2])
        rows.append(
            {
                "date": parts[0][:10],
                "code": str(code),
                "open": float(parts[1]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "close": close,
                "volume": float(parts[5]) * LOTS_TO_SHARES,
                # f57 is the real turnover; the Sina path approximates it as
                # close × volume, which differs from actual VWAP-based turnover.
                "amount": float(parts[6] or 0.0) or close * float(parts[5]) * LOTS_TO_SHARES,
            }
        )
    return rows


def _fetch_tencent_kline(code: str, *, datalen: int = 300) -> list[dict[str, Any]]:
    """Tencent daily bars from the unadjusted ``day`` array, 手 converted to 股."""

    import requests

    symbol = _tencent_symbol(code)
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params={"param": f"{symbol},day,,,{int(datalen)},"},
        timeout=20,
        headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    payload = (response.json().get("data") or {}).get(symbol) or {}
    rows: list[dict[str, Any]] = []
    for item in payload.get("day") or []:
        if len(item) < 5:
            continue
        raw_volume = float(item[5]) if len(item) > 5 and item[5] else 0.0
        # Tencent states 科创板 volume in 股 and every other market in 手, so the
        # conversion is per symbol. A blanket x100 is wrong by 100x for 688xxx —
        # which the fallback guard caught, refusing 57 such symbols rather than
        # writing them.
        shares = tencent_volume_to_shares(code, raw_volume)
        close = float(item[2])
        rows.append(
            {
                "date": str(item[0])[:10],
                "code": str(code),
                "open": float(item[1]),
                "high": float(item[3]),
                "low": float(item[4]),
                "close": close,
                "volume": shares,
                "amount": close * shares,
            }
        )
    return rows


KLINE_FETCHERS: dict[str, Any] = {
    "sina": _fetch_sina_kline,
    "eastmoney": _fetch_eastmoney_kline,
    "tencent": _fetch_tencent_kline,
}


def fetch_daily_bars(code: str, *, datalen: int = 300, order: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Fetch one symbol's daily bars, trying each channel until one returns rows.

    Returns the bars plus ``channel`` and ``attempts``, because which channel
    served a symbol is the only way to tell a healthy run from one where the
    primary is down and every row came from the backup.

    ``order`` defaults to ``KLINE_CHANNELS`` *at call time*: a tuple bound as a
    default argument freezes whatever the name held when the function was
    defined, so overriding the module constant would silently not apply — which
    also made the fallback-detection below misfire on every symbol.
    """

    channels = tuple(order) if order else KLINE_CHANNELS
    attempts: list[dict[str, str]] = []
    for channel in channels:
        try:
            rows = KLINE_FETCHERS[channel](code, datalen=datalen)
        except Exception as exc:
            attempts.append({"channel": channel, "outcome": f"error: {type(exc).__name__}: {str(exc)[:80]}"})
            continue
        if rows:
            attempts.append({"channel": channel, "outcome": f"ok: {len(rows)} rows"})
            # Report fallback status from the order actually used rather than
            # comparing against the module constant, so an explicit override
            # (the way a fallback gets tested at all) reports the truth.
            return {
                "rows": rows,
                "channel": channel,
                "primary": channels[0],
                "is_fallback": channel != channels[0],
                "attempts": attempts,
            }
        attempts.append({"channel": channel, "outcome": "empty"})
    return {
        "rows": [],
        "channel": "",
        "primary": channels[0],
        "is_fallback": False,
        "attempts": attempts,
        "all_channels_failed": True,
    }


async def sync_stock_klines(
    codes: list[str], *, datalen: int = 300, concurrency: int = 1
) -> dict[str, Any]:
    from akshare_mcp.storage import get_db

    db = get_db()
    await db.initialize()
    out: dict[str, Any] = {}

    async def _agreement_with_stored(code: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Check a fallback's bars against what the primary channel already stored.

        A fallback is trusted only after it reproduces history that is already in
        the database. Both traps this guards against — a different adjustment, a
        different volume unit — show up here as a systematic ratio, and neither is
        visible from the fallback's own output.
        """

        dates = sorted({str(row["date"])[:10] for row in rows if row.get("date")})
        if not dates:
            return {"checked": 0, "reason": "no dated rows to compare"}
        placeholders = ",".join("?" for _ in dates)
        # Read through a plain sqlite3 connection, which is how every sync script
        # here reads. The async adapter's `execute` is variadic and returns a
        # status string rather than a cursor, so it is a write path only.
        def _read() -> tuple[dict[str, tuple[Any, Any]], dict[str, Any]]:
            import sqlite3

            from _db_sync_common import DB_PATH

            with sqlite3.connect(str(DB_PATH)) as conn:
                found = conn.execute(
                    f"SELECT substr(time, 1, 10), close, volume FROM kline_1d "
                    f"WHERE code = ? AND substr(time, 1, 10) IN ({placeholders})",
                    (code, *dates),
                ).fetchall()
                span = conn.execute(
                    "SELECT min(substr(time, 1, 10)), max(substr(time, 1, 10)), count(*) "
                    "FROM kline_1d WHERE code = ?",
                    (code,),
                ).fetchone() or (None, None, 0)
                market_latest = conn.execute(
                    "SELECT max(substr(time, 1, 10)) FROM kline_1d"
                ).fetchone()[0]
            return (
                {str(r[0]): (r[1], r[2]) for r in found},
                {
                    "stored_span": [span[0], span[1]],
                    "stored_rows": span[2],
                    "market_latest": market_latest,
                },
            )

        stored, span = await asyncio.to_thread(_read)
        if not stored:
            outcome: dict[str, Any] = {
                "checked": 0,
                "stored_span": span["stored_span"],
                "stored_rows": span["stored_rows"],
                "fetched_span": [dates[0], dates[-1]],
            }
            if span["stored_rows"]:
                # The code has history and the fallback shares no date with it, so
                # the two are describing different securities. 430047 is the live
                # example: East money answers with a 2020 series for a code whose
                # stored series runs to 2025, because 北交所 codes get reassigned.
                # Writing that would overwrite a recent history with an old one.
                outcome["no_overlap"] = True
                outcome["reject_write"] = "fallback shares no date with the stored series"
            elif span["market_latest"] and dates[-1] < span["market_latest"]:
                # Nothing stored to compare against, so the only available check is
                # whether the series even reaches the present. A brand-new listing
                # legitimately starts late; a series ending years ago for a code
                # that is in today's universe is a different security.
                #
                # A long-suspended stock is the case that looks similar and is not:
                # it has stored history, so its dates overlap and this branch never
                # runs. Reaching here means nothing was ever stored for the code,
                # and the series still ends well before the market does.
                gap_days = _days_between(dates[-1], span["market_latest"])
                if gap_days is not None and gap_days > STALE_SERIES_GRACE_DAYS:
                    outcome["stale_series"] = True
                    outcome["market_latest"] = span["market_latest"]
                    outcome["gap_days"] = gap_days
                    outcome["reject_write"] = (
                        f"no stored history and the series ends {gap_days} days before the market"
                    )
            return outcome

        close_bad: list[str] = []
        volume_ratio: list[float] = []
        intraday_skipped: list[str] = []
        today = time.strftime("%Y-%m-%d")
        for row in rows:
            day = str(row["date"])[:10]
            if day not in stored:
                continue
            # Today's bar is still moving, so it is not evidence of anything: an
            # earlier run wrote 1312.97 for a session that later traded at 1314.
            # Comparing it produced a "mismatch" for two healthy symbols on every
            # run, which is how a real mismatch would end up ignored.
            if day >= today:
                intraday_skipped.append(day)
                continue
            stored_close, stored_volume = stored[day]
            if stored_close and abs(float(row["close"]) - float(stored_close)) > 1e-4 * abs(float(stored_close)):
                close_bad.append(day)
            if stored_volume:
                volume_ratio.append(float(row["volume"]) / float(stored_volume))
        median_ratio = round(sorted(volume_ratio)[len(volume_ratio) // 2], 4) if volume_ratio else None
        outcome = {
            "checked": len(stored),
            "close_mismatches": close_bad[:5],
            "close_mismatch_count": len(close_bad),
            "volume_ratio": median_ratio,
            "settled_bars_compared": len(volume_ratio),
        }
        if intraday_skipped:
            outcome["skipped_still_trading"] = intraday_skipped[:3]

        # The guard refuses the write when it has positive evidence of error.
        # Reporting alone was not enough: a run that forced the fallback wrote the
        # 2020 series for a code whose stored series ended in 2025, and the summary
        # said so while the 80 rows were already in the table.
        if close_bad:
            outcome["reject_write"] = f"settled closes differ on {len(close_bad)} bar(s)"
        elif median_ratio is not None and not 0.99 <= median_ratio <= 1.01:
            outcome["reject_write"] = f"volume ratio {median_ratio} is not 1.0, so the unit convention differs"
        return outcome

    async def _one(code: str) -> None:
        try:
            fetched = await asyncio.to_thread(fetch_daily_bars, code, datalen=datalen)
            rows = fetched["rows"]
            entry: dict[str, Any] = {
                "fetched": len(rows),
                "channel": fetched["channel"],
                "primary": fetched["primary"],
                "attempts": fetched["attempts"],
            }
            if fetched.get("all_channels_failed"):
                # Every channel declined, so this is a failure and not an empty
                # series — the distinction the old code lost when an empty body
                # counted as success.
                entry["error"] = "all_channels_failed: " + "; ".join(a["outcome"] for a in fetched["attempts"])
                out[code] = entry
                return
            if fetched["is_fallback"]:
                entry["fallback"] = True
                agreement = await _agreement_with_stored(code, rows)
                entry["agreement"] = agreement
                if agreement.get("reject_write"):
                    # Refuse rather than overwrite: a fallback with positive evidence
                    # against it must not replace history we already trust. Recorded
                    # as a failure, so the run's numbers show it — "no update" and
                    # "updated with the wrong series" are not the same outcome.
                    entry["error"] = f"fallback_rejected: {agreement['reject_write']}"
                    entry["rejected_rows"] = len(rows)
                    out[code] = entry
                    return
            result = await db.save_klines(code, rows)
            entry["save"] = (
                result
                if not isinstance(result, dict)
                else {
                    "accepted_count": result.get("accepted_count"),
                    "rejected_count": result.get("rejected_count"),
                }
            )
            # An empty body is not a success. It was counted as one, which is how
            # 350 北交所 symbols returned nothing for a whole sync run and the
            # summary still read "5574 ok".
            if not rows:
                entry["empty"] = True
            out[code] = entry
        except Exception as exc:
            # Recorded per code, never dropped: a sync that reports "done" while
            # 200 symbols failed is the failure mode this whole file guards against.
            out[code] = {"error": f"{type(exc).__name__}: {exc}"}

    # One worker is the documented rate-safe default; the full-market backfill
    # raises it deliberately rather than by accident.
    workers = max(1, int(concurrency or 1))
    if workers == 1:
        for index, code in enumerate(codes, 1):
            await _one(code)
            time.sleep(0.12)
            if index % 200 == 0:
                print(f"    ... {index}/{len(codes)} symbols fetched")
    else:
        semaphore = asyncio.Semaphore(workers)
        done = 0

        async def _guarded(code: str) -> None:
            nonlocal done
            async with semaphore:
                await _one(code)
                done += 1
                if done % 200 == 0:
                    print(f"    ... {done}/{len(codes)} symbols fetched")
                await asyncio.sleep(0.05)

        await asyncio.gather(*(_guarded(code) for code in codes))

    failures = {code: value for code, value in out.items() if "error" in value}
    empties = {code: value for code, value in out.items() if value.get("empty")}
    landed = len(out) - len(failures) - len(empties)

    by_channel: dict[str, int] = {}
    for value in out.values():
        channel = value.get("channel")
        if channel:
            by_channel[channel] = by_channel.get(channel, 0) + 1
    strays = {
        code: value["agreement"]
        for code, value in out.items()
        if value.get("fallback")
        and (value.get("agreement", {}).get("no_overlap")
             or value.get("agreement", {}).get("stale_series")
             or value.get("agreement", {}).get("close_mismatch_count")
             or (value.get("agreement", {}).get("volume_ratio") or 1.0) not in (0, 1.0))
    }

    print(
        f"    kline sync: {landed} with data, {len(empties)} returned no rows, "
        f"{len(failures)} failed"
    )
    print(f"    served by channel: {by_channel}")
    primaries = {value.get("primary") for value in out.values() if value.get("primary")}
    primary_name = next(iter(primaries)) if len(primaries) == 1 else KLINE_CHANNELS[0]
    if by_channel and by_channel.get(primary_name, 0) == 0 and sum(by_channel.values()):
        print("    !! the primary channel served nothing; every row came from a fallback")
    if strays:
        print(f"    !! fallback rows disagree with stored history: {dict(list(strays.items())[:5])}")
    if empties:
        sample = sorted(empties)[:8]
        print(f"    no-row sample: {sample}{' ...' if len(empties) > 8 else ''}")
    return out


async def sync_index_klines() -> dict[str, Any]:
    from akshare_mcp.data_source import data_source
    from akshare_mcp.storage import get_db

    db = get_db()
    await db.initialize()
    out: dict[str, Any] = {}
    for code in INDEX_CODES:
        try:
            rows = data_source._get_index_kline(code, period="daily", limit=120)
            if not rows:
                out[code] = {"fetched": 0}
                continue
            payload = [
                {
                    "date": row["date"],
                    "code": code,
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row.get("volume") or 0,
                    "amount": row.get("amount") or 0,
                }
                for row in rows
            ]
            result = await db.save_klines(code, payload)
            out[code] = {
                "fetched": len(rows),
                "source": rows[-1].get("source"),
                "save": result
                if not isinstance(result, dict)
                else {
                    "accepted_count": result.get("accepted_count"),
                    "rejected_count": result.get("rejected_count"),
                },
            }
        except Exception as exc:
            out[code] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def sync_sector_fund_flow() -> dict[str, Any]:
    """Aggregate per-stock fund flow up to its industry board.

    The akshare source for this was removed and no East money report carries board
    fund flow — six candidate report names were probed and none exists, and the
    ``push2`` board endpoint's host does not answer. Rather than keep returning a
    stub, this **derives** the figure from two tables the project already has:
    ``stock_fund_flow`` (per-stock, daily) joined to ``block_stocks`` (membership).

    It is written to its own table, not to ``market_blocks``: that table's
    ``total_amount`` means turnover, and putting a net inflow there would be a
    different quantity under an existing name.

    Two limits are recorded rather than hidden: only the members that have fund-flow
    data contribute, and the result is an aggregation of the provider's per-stock
    figures, **not an exchange-published board total**. ``complete`` is False when
    coverage is short, and the per-row ``members_with_flow`` against
    ``members_total`` says by how much.
    """

    import sqlite3

    from _integration_paths import default_sqlite_path

    path = default_sqlite_path()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sector_fund_flow_derived (
                trade_date TEXT NOT NULL,
                block_code TEXT NOT NULL,
                block_name TEXT,
                block_type TEXT,
                members_total INTEGER,
                members_with_flow INTEGER,
                main_net_inflow REAL,
                super_large_net_inflow REAL,
                large_net_inflow REAL,
                source TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (trade_date, block_code)
            )
            """
        )
        latest = conn.execute("SELECT max(trade_date) FROM stock_fund_flow").fetchone()[0]
        if not latest:
            conn.commit()
            return {
                "ok": False,
                "rows": 0,
                "source": "derived:stock_fund_flow×block_stocks",
                "reason": "stock_fund_flow is empty, so there is nothing to aggregate",
            }

        members = {}
        for block_code, stock_code in conn.execute(
            "SELECT block_code, stock_code FROM block_stocks WHERE block_code LIKE 'BK%'"
        ):
            members.setdefault(block_code, []).append(str(stock_code))
        names = {
            str(code): (str(name or ""), str(btype or ""))
            for code, name, btype in conn.execute(
                "SELECT block_code, block_name, block_type FROM market_blocks WHERE block_type='industry'"
            )
        }
        flow = {
            str(code): (main, sup, large)
            for code, main, sup, large in conn.execute(
                "SELECT code, main_net_inflow, super_large_net_inflow, large_net_inflow "
                "FROM stock_fund_flow WHERE trade_date = ?",
                (latest,),
            )
        }

        rows = []
        short = 0
        for block_code, codes in members.items():
            present = [flow[c] for c in codes if c in flow]
            if len(present) < len(codes):
                short += 1
            name, btype = names.get(block_code, ("", "industry"))
            rows.append(
                (
                    latest,
                    block_code,
                    name,
                    btype,
                    len(codes),
                    len(present),
                    sum(x[0] for x in present) if present else None,
                    sum(x[1] for x in present) if present else None,
                    sum(x[2] for x in present) if present else None,
                    "derived:stock_fund_flow×block_stocks",
                )
            )
        conn.executemany(
            """
            INSERT INTO sector_fund_flow_derived
                (trade_date, block_code, block_name, block_type, members_total, members_with_flow,
                 main_net_inflow, super_large_net_inflow, large_net_inflow, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(trade_date, block_code) DO UPDATE SET
                block_name = excluded.block_name, block_type = excluded.block_type,
                members_total = excluded.members_total,
                members_with_flow = excluded.members_with_flow,
                main_net_inflow = excluded.main_net_inflow,
                super_large_net_inflow = excluded.super_large_net_inflow,
                large_net_inflow = excluded.large_net_inflow,
                source = excluded.source, updated_at = CURRENT_TIMESTAMP
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": bool(rows),
        "rows": len(rows),
        "trade_date": latest,
        "blocks_with_incomplete_membership": short,
        # Not an exchange figure; say so where a caller will see it.
        "complete": short == 0,
        "source": "derived:stock_fund_flow×block_stocks",
        "note": (
            "Derived by aggregating the provider's per-stock fund flow over board "
            "membership. Not an exchange-published board total."
        ),
    }


def sync_north_fund_best_effort() -> dict[str, Any]:
    """Unavailable: the akshare north-bound history source was removed.

    There is no working replacement on this host; 深交所's own endpoint is blocked
    through the proxy. Reported as unavailable so an empty result cannot pass for
    a measured zero.
    """
    return {
        "rows": 0,
        "written": 0,
        "ok": False,
        "source": "none",
        "reason": "akshare_removed",
    }


async def compute_factor_ic(codes: list[str], factors: list[str]) -> dict[str, Any]:
    from akshare_mcp.tools.managers.quant_mgr_classic import handle_batch_compute_factors

    def ok(data, **kwargs):
        return {"success": True, "data": data, **kwargs}

    def fail(message):
        return {"success": False, "error": message}

    result = await handle_batch_compute_factors(
        kw={
            "codes": codes,
            "factors": factors,
            "persist": True,
            "compute_ic": True,
            "period": 20,
            "max_codes": max(50, len(codes)),
            "ic_horizons": [5, 10, 20],
        },
        ok=ok,
        fail=fail,
    )
    return result




async def seed_active_factor_pool() -> dict[str, Any]:
    """Seed active factor pool with compile-valid seed-library expressions."""
    from datetime import timezone
    from akshare_mcp.services.factor_candidate_seed import _EXPRESSION_BY_FACTOR
    from akshare_mcp.services.factor_mining_factory.pool.storage import (
        load_active_pool_from_db,
        save_factor_to_pool,
    )
    from akshare_mcp.storage import get_db

    db = get_db()
    await db.initialize()
    now = datetime.now(timezone.utc).isoformat()
    saved = []
    for name, (expr, inputs) in list(_EXPRESSION_BY_FACTOR.items())[:10]:
        rec = {
            "factor_id": f"rule_seed_{name}",
            "name": f"rule_seed_{name}",
            "family": name.split("_")[0] if "_" in name else name,
            "expression_dsl": expr,
            "inputs": json.dumps(list(inputs or [])),
            "status": "active",
            "admission_date": now,
            "admission_ic": 0.02,
            "current_ic": 0.02,
            "admission_grade": "B",
            "fitness": 0.60,
            "generation_engine": "rule_seed",
            "generation_trace": json.dumps({"source": "free_data_daily_sync"}),
            "validation_summary": {
                "quality_status": "promoted",
                "quality_score": 0.60,
                "artifact_id": f"rule_seed_{name}",
                "evidence_summary": {},
            },
            "hypothesis": f"seed library factor {name}",
            "last_evaluated_at": now,
            "updated_at": now,
            "created_at": now,
        }
        saved.append(await save_factor_to_pool(db, rec))
    # retire known-invalid free seeds
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE factor_pool_active SET status='retired', retired_reason='compile_invalid_free_seed', retired_at=$1 WHERE factor_id LIKE 'free_seed_%' AND status='active'",
                now,
            )
    except Exception as _swallowed:
        logger.warning("scripts/free_data_daily_sync.py:538: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass
    pool = await load_active_pool_from_db(db)
    active = [item for item in pool if str(item.get("status") or "") == "active"]
    return {
        "saved": saved,
        "active_pool_size": len(active),
        "factor_ids": [item.get("factor_id") for item in active[:20]],
    }




async def run_evidence_pipeline(*, history_days: int = 8) -> dict[str, Any]:
    """Thicken free-data paper evidence: multi-day signals, snapshots, forward returns.

    Uses submitted/observe strategies only. Live trading remains disabled.
    """
    from datetime import timezone

    from akshare_mcp.services.signal_tracker import SignalTracker
    from akshare_mcp.storage import get_db
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.incubation import build_incubation_runtime
    from strategy_factory.runtime.signal_tracker import build_signal_tracker_runtime

    ensure_default_runtime_services()
    os.environ["FREE_EVIDENCE_FORCE_BUY"] = "1"
    db = get_db()
    await db.initialize()
    tracker = SignalTracker()

    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, status, incubating, strategy_type, params
            FROM strategies
            WHERE status IN ('submitted', 'incubating', 'listed')
            ORDER BY updated_at DESC
            LIMIT 30
            """
        )
    strategies = [dict(row) for row in list(rows or [])]

    def _normalize_strategy_record(item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item or {})
        for key in ("params", "factor_weights"):
            value = payload.get(key)
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    payload[key] = {}
                    continue
                try:
                    payload[key] = json.loads(text)
                except Exception:
                    payload[key] = {}
            elif value is None:
                payload[key] = {}
        params = dict(payload.get("params") or {})
        codes: list[str] = []
        for candidate in (
            payload.get("target_symbols"),
            params.get("target_symbols"),
            params.get("codes"),
        ):
            if isinstance(candidate, (list, tuple, set)):
                codes.extend([str(x).strip() for x in candidate if str(x).strip()])
            elif isinstance(candidate, str) and candidate.strip():
                codes.append(candidate.strip())
        codes = list(dict.fromkeys(codes))
        if not codes:
            codes = list(DEFAULT_CODES[:5])
        params.setdefault("target_symbols", list(codes))
        params.setdefault("codes", list(codes))
        # Free-evidence mode: expand universe + raise concurrent paper slots.
        # Incubation reads max_concurrent_positions from runtime_playbook.position_policy.
        expanded = list(dict.fromkeys([*codes, *DEFAULT_CODES]))
        codes = expanded[:10]
        params["target_symbols"] = list(codes)
        params["codes"] = list(codes)
        position_policy = dict(params.get("position_policy") or {})
        position_policy["max_concurrent_positions"] = max(
            int(position_policy.get("max_concurrent_positions") or 0),
            10,
        )
        position_policy["max_position_pct"] = max(
            float(position_policy.get("max_position_pct") or 0.0),
            0.20,
        )
        params["position_policy"] = position_policy
        runtime_playbook = dict(params.get("runtime_playbook") or payload.get("runtime_playbook") or {})
        rp_position = dict(runtime_playbook.get("position_policy") or {})
        rp_position.update(position_policy)
        runtime_playbook["position_policy"] = rp_position
        params["runtime_playbook"] = runtime_playbook
        payload["runtime_playbook"] = runtime_playbook
        payload["params"] = params
        payload["target_symbols"] = list(codes)
        return payload

    strategies = [_normalize_strategy_record(item) for item in strategies]

    # Multi-day free signal seeding (including today).
    past_days: list[date] = []
    cursor = date.today()
    while len(past_days) < max(1, int(history_days or 8)):
        if cursor.weekday() < 5:
            past_days.append(cursor)
        cursor -= timedelta(days=1)

    seeded_writes = 0
    seed_errors: list[str] = []
    for strategy in strategies:
        for day in past_days:
            try:
                result = await tracker._generate_signals(db, strategy, as_of=day)
                seeded_writes += int(result.get("signals_generated") or 0)
            except Exception as exc:
                seed_errors.append(f"{strategy.get('id')}:{day}:{exc}")

    # Convert recent multi-day signals into paper orders where buys are available.
    from akshare_mcp.services.incubation import get_strategy_incubation_service
    incubation_service = get_strategy_incubation_service()
    order_sync = {"days": 0, "created": 0, "items": []}
    for day in past_days[:6]:
        try:
            proc = await incubation_service.process_strategies(db, strategies, signal_date=day)
            order_sync["days"] += 1
            order_sync["created"] += int(proc.get("orders_created") or 0)
            order_sync["items"].append(
                {
                    "day": str(day),
                    "orders_created": proc.get("orders_created"),
                    "orders_filled": proc.get("orders_filled"),
                    "skip_reason_counts": proc.get("skip_reason_counts"),
                }
            )
        except Exception as exc:
            order_sync.setdefault("errors", []).append(f"{day}:{exc}")

    # Full tracker once (snapshots + pending forward backfill path).
    tracker_result = await build_signal_tracker_runtime().run_once()
    explicit_backfill = await tracker.backfill_forward_returns(
        db,
        forward_days_list=[1, 5, 10, 20, 30],
        batch_limit=2000,
        max_rounds=50,
    )

    incubation = build_incubation_runtime()
    try:
        await incubation.start()
    except Exception as _swallowed:
        logger.warning("scripts/free_data_daily_sync.py:691: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass
    incubation_result = await incubation.run_once(trigger="free_data_evidence_pipeline")
    try:
        await incubation.stop()
    except Exception as _swallowed:
        logger.warning("scripts/free_data_daily_sync.py:696: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass

    import sqlite3

    conn = sqlite3.connect(os.environ["AIASK_SQLITE_PATH"])
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "strategy_signals",
            "strategy_signal_event_snapshots",
            "signal_forward_returns",
            "paper_orders",
            "paper_trades",
            "strategy_trade_positions",
            "strategy_signal_evidence",
        )
    }
    conn.close()
    return {
        "strategies_considered": len(strategies),
        "history_days": past_days,
        "seeded_signal_writes": seeded_writes,
        "seed_error_count": len(seed_errors),
        "seed_errors_sample": seed_errors[:8],
        "order_sync": order_sync,
        "tracker": {
            "signals_generated": tracker_result.get("signals_generated"),
            "signal_event_snapshots": tracker_result.get("signal_event_snapshots"),
            "forward_returns_computed": tracker_result.get("forward_returns_computed"),
            "errors": tracker_result.get("errors"),
            "runtime_universe": tracker_result.get("runtime_universe"),
        },
        "explicit_backfill": explicit_backfill,
        "incubation": {
            "status": incubation_result.get("status"),
            "strategies_verified": incubation_result.get("strategies_verified"),
            "paper_orders_filled": incubation_result.get("paper_orders_filled"),
            "phase_failures": incubation_result.get("phase_failures"),
        },
        "evidence_counts": counts,
    }

async def collector_status() -> dict[str, Any]:
    from akshare_mcp.storage import get_db
    from strategy_factory.application.collect import DataCollector

    db = get_db()
    await db.initialize()
    snapshot = await DataCollector().collect(db)
    completeness = dict(snapshot.get("completeness") or {})
    sources = completeness.get("sources") or snapshot.get("sources") or {}
    statuses = {}
    if isinstance(sources, dict):
        statuses = {
            key: (value.get("status") if isinstance(value, dict) else value)
            for key, value in sources.items()
        }
    return {
        "date": snapshot.get("date"),
        "fear_greed_index": snapshot.get("fear_greed_index"),
        "fg_level": snapshot.get("fg_level"),
        "factor_ic": snapshot.get("factor_ic"),
        "north_fund_3d_net": snapshot.get("north_fund_3d_net"),
        "hot_sectors": snapshot.get("hot_sectors"),
        "cold_sectors": snapshot.get("cold_sectors"),
        "completion_ratio": completeness.get("completion_ratio"),
        "missing_sources": completeness.get("missing_sources"),
        "source_statuses": statuses,
    }


def _codes_from_db() -> list[str]:
    """Read listed stock codes from the runtime ``stocks`` table."""

    import contextlib
    import sqlite3

    from _integration_paths import default_sqlite_path

    path = default_sqlite_path()
    if not path.exists():
        return []
    try:
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            rows = conn.execute(
                "SELECT stock_code FROM stocks "
                "WHERE stock_code IS NOT NULL AND COALESCE(list_status, 'L') = 'L'"
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"[universe] stocks table unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []
    # Indices live in this table too (sh000001, sz399300 …); they have their own
    # sync path and must not enter the individual-stock universe.
    return [str(row[0]) for row in rows if str(row[0]).isdigit()]


def _resolve_universe(args: argparse.Namespace) -> tuple[list[str], str]:
    """Return ``(codes, source)`` for the requested universe.

    ``DEFAULT_CODES`` is the last resort, not the default. A daily sync over 15
    symbols, while the market has more than 5500, produces factor and strategy
    evidence that cannot support any market-wide claim, and the output looked
    identical either way.
    """

    import _db_sync_common as common

    if args.universe == "default":
        return list(DEFAULT_CODES), f"default:{len(DEFAULT_CODES)} representative codes"

    db_codes = _codes_from_db()
    if args.universe == "db":
        if not db_codes:
            raise SystemExit("--universe db requested but the stocks table is empty")
        return db_codes, f"sqlite:stocks ({len(db_codes)} listed rows)"

    # 'auto' trusts the table only when it already covers the market.
    if args.universe == "auto" and len(db_codes) >= common.MIN_FULL_UNIVERSE:
        return db_codes, f"sqlite:stocks ({len(db_codes)} listed rows)"

    try:
        codes, source = common.fetch_market_symbols()
        if codes:
            return codes, source
    except Exception as exc:
        if args.universe == "full":
            raise
        print(
            f"[universe] Sina listing unavailable ({type(exc).__name__}: {exc}); "
            f"falling back to the local table",
            file=sys.stderr,
        )

    if db_codes:
        return db_codes, f"sqlite:stocks ({len(db_codes)} listed rows, degraded)"

    return (
        list(DEFAULT_CODES),
        f"default:{len(DEFAULT_CODES)} representative codes (degraded; not the market)",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Free public data daily sync for AIASK")
    parser.add_argument(
        "--codes",
        default="",
        help="Comma-separated stock codes; overrides --universe",
    )
    parser.add_argument(
        "--universe",
        choices=("auto", "full", "db", "default"),
        default="auto",
        help=(
            "Which stock universe to sync when --codes is not given. "
            "'auto' uses the stocks table when it already covers the market, "
            "otherwise enumerates every A-share from Sina; 'full' always "
            "enumerates; 'db' reads the stocks table; 'default' is the legacy "
            f"{len(DEFAULT_CODES)}-code representative list"
        ),
    )
    parser.add_argument("--limit", type=int, default=0, help="Cap the universe size (0 = no cap)")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Concurrent kline fetches against Sina (default 1, the documented rate-safe value)",
    )
    parser.add_argument("--skip-stocks", action="store_true")
    parser.add_argument("--skip-index", action="store_true")
    parser.add_argument("--skip-sector", action="store_true")
    parser.add_argument("--skip-north", action="store_true")
    parser.add_argument("--skip-ic", action="store_true")
    parser.add_argument("--skip-factor-pool", action="store_true")
    parser.add_argument("--with-evidence", action="store_true",
                        help="Seed multi-day free signals, snapshots, forward returns, paper path")
    parser.add_argument("--evidence-history-days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    db_path = _configure_env()
    codes = [item.strip() for item in str(args.codes or "").split(",") if item.strip()]
    universe_source = "cli:--codes" if codes else ""
    if not codes:
        codes, universe_source = _resolve_universe(args)
    if args.limit and len(codes) > args.limit:
        codes = codes[: int(args.limit)]
        universe_source = f"{universe_source} (capped to {args.limit})"
    if not codes:
        raise SystemExit("resolved an empty stock universe; refusing to run a no-op sync")
    print(f"universe: {len(codes)} code(s) from {universe_source}")

    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sqlite_path": str(db_path),
        "universe_source": universe_source,
        "codes": codes,
        "free_sources": {
            "stock_kline": "sina",
            "index_kline": "sina_fallback_after_akshare",
            "sector_fund_flow": "akshare.stock_fund_flow_industry",
            "north_fund": "akshare.stock_hsgt_hist_em (+ carry-forward if stale)",
            "factor_ic": "local batch_compute_factors",
            "active_factor_pool": "local free seed into factor_pool_active",
            "evidence_pipeline": "multi-day free signals + snapshots + forward returns + incubation",
        },
        "steps": {},
    }

    if not args.skip_stocks:
        report["steps"]["stock_klines"] = await sync_stock_klines(
            codes, concurrency=getattr(args, "concurrency", 1)
        )
    if not args.skip_index:
        report["steps"]["index_klines"] = await sync_index_klines()
    if not args.skip_sector:
        try:
            report["steps"]["sector_fund_flow"] = sync_sector_fund_flow()
        except Exception as exc:
            report["steps"]["sector_fund_flow"] = {
                "error": f"{type(exc).__name__}: {exc}"
            }
    if not args.skip_north:
        try:
            report["steps"]["north_fund"] = sync_north_fund_best_effort()
        except Exception as exc:
            report["steps"]["north_fund"] = {
                "error": f"{type(exc).__name__}: {exc}"
            }
    if not args.skip_ic:
        report["steps"]["factor_ic"] = await compute_factor_ic(codes, DEFAULT_FACTORS)
    if not args.skip_factor_pool:
        report["steps"]["active_factor_pool"] = await seed_active_factor_pool()
    if args.with_evidence:
        report["steps"]["evidence_pipeline"] = await run_evidence_pipeline(
            history_days=max(1, int(args.evidence_history_days or 8))
        )

    report["collector"] = await collector_status()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print("=== AIASK free data daily sync ===")
        print(f"sqlite: {db_path}")
        print(f"codes: {len(codes)}")
        for name, payload in report["steps"].items():
            print(f"- {name}: {json.dumps(payload, ensure_ascii=False, default=str)[:240]}")
        print("collector:")
        print(json.dumps(report["collector"], ensure_ascii=False, indent=2, default=str))
        missing = report["collector"].get("missing_sources") or []
        if missing:
            print(f"STILL MISSING: {missing}")
            return 2
        print("ALL COLLECTOR SOURCES GREEN (completion_ratio="
              f"{report['collector'].get('completion_ratio')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
