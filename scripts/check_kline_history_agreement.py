#!/usr/bin/env python3
"""Check the stored daily history against an independent channel.

Every row in ``kline_1d`` came from Sina. The incremental sync now has a channel
chain, but that says nothing about the 1.7 M rows already stored: if the incumbent
had a systematic quirk, every row carries it and no later run would notice, because
the chain only refuses writes it can compare against that same history.

This samples symbols across all four markets and compares the stored bars against
East money's unadjusted series on the dates they share. Different vendor, different
endpoint, same question — so a systematic difference shows up as a rate rather than
as an anecdote.

Note on volume: East money reports 手 and the stored column is 股, so the comparison
applies the same x100 conversion the sync uses. A uniform 100x discrepancy here
means the conversion is wrong, not that the vendors disagree.

Usage::

    python scripts/check_kline_history_agreement.py --sample 300
    python scripts/check_kline_history_agreement.py --sample 50 --json
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _db_sync_common import DB_PATH  # noqa: E402

TIMEOUT = 20.0
LOTS_TO_SHARES = 100.0
# A close must match to within this relative difference. Vendors round to 2 or 3
# decimals, which on a 15 yuan stock is 3e-4, so this is a rounding allowance.
CLOSE_TOLERANCE = 5e-4
# Volume is aggregated per vendor, so intraday bars differ by ~0.5%. Daily bars are
# expected to be identical; this bound is what makes "identical" falsifiable.
VOLUME_TOLERANCE = 0.01
TODAY = time.strftime("%Y-%m-%d")
# Shared across symbols; see the note in `_eastmoney_bars`.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# A per-symbol pause. A burst of a few hundred requests gets the provider to answer
# 501 for everything — measured: ~1,300 requests in 15 minutes across runs did it, and
# the block outlasted the run. The run then reports "unreachable" for symbols that are
# fine, which is why the summary now says so explicitly rather than showing 0% match.
PAUSE_SECONDS = 0.5


def _eastmoney_bars(code: str) -> list[tuple[str, float, float]]:
    """Return ``[(date, close, volume_in_shares)]`` unadjusted."""

    secid = ("1." if code.startswith(("6", "5")) else "0.") + code
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
        "&klt=101&fqt=0&end=20500101&lmt=1000"
    )
    # One opener for the process: building one per symbol leaked a socket each
    # time, which showed up as pages of `unclosed SSLSocket` over a few hundred
    # symbols and helped trip the rate limiter.
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    )
    with _OPENER.open(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    out: list[tuple[str, float, float]] = []
    for item in (payload.get("data") or {}).get("klines") or []:
        parts = str(item).split(",")
        if len(parts) < 6:
            continue
        out.append((parts[0][:10], float(parts[2]), float(parts[5]) * LOTS_TO_SHARES))
    return out


def _tencent_bars(code: str) -> list[tuple[str, float, float]]:
    """The same bars from a second, independent vendor.

    This checker had one channel, which is the defect it exists to find: when East
    money's kline host went down, the verification silently became "0 bars
    compared" and could not be run at all. Tencent answers from a different vendor
    on a different endpoint, and its volume needs the per-market conversion —
    科创板 in 股, every other market in 手.
    """

    from akshare_mcp.data_source.volume_units import tencent_volume_to_shares

    symbol = ("sh" if code.startswith(("6", "5")) else "bj" if code.startswith(("92", "43", "83", "87", "88", "4", "8")) else "sz") + code
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,640,"
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    )
    with _OPENER.open(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    rows = (payload.get("data") or {}).get(symbol, {}).get("day") or []
    out: list[tuple[str, float, float]] = []
    for item in rows:
        if len(item) < 6:
            continue
        out.append(
            (
                str(item[0])[:10],
                float(item[2]),
                tencent_volume_to_shares(code, float(item[5])),
            )
        )
    return out


CHANNELS = {"eastmoney": _eastmoney_bars, "tencent": _tencent_bars}
# Tried in order. East money is preferred because it is a different endpoint family
# from the one that wrote the rows, but either is an independent vendor.
CHANNEL_ORDER = ("eastmoney", "tencent")


def _bars(code: str, channel: str) -> list[tuple[str, float, float]]:
    """Fetch ``code`` from ``channel``, or from the first channel that answers."""

    if channel != "auto":
        if channel not in CHANNELS:
            raise SystemExit(f"unknown --channel {channel!r}; known: {','.join(CHANNELS)}")
        return CHANNELS[channel](code)

    last: Exception | None = None
    for name in CHANNEL_ORDER:
        try:
            rows = CHANNELS[name](code)
        except Exception as exc:  # a dead channel must not read as "no disagreement"
            last = exc
            continue
        if rows:
            return rows
    raise RuntimeError(f"every channel failed or returned nothing: {type(last).__name__}: {last}")


def _stored_bars(conn: sqlite3.Connection, code: str) -> dict[str, tuple[float, float]]:
    return {
        str(row[0]): (float(row[1]), float(row[2] or 0))
        for row in conn.execute(
            "SELECT substr(time, 1, 10), close, volume FROM kline_1d WHERE code = ?",
            (code,),
        )
    }


def _sample_codes(conn: sqlite3.Connection, size: int, seed: int) -> list[str]:
    """Stratify across markets so a quirk in one of them cannot hide in an average."""

    buckets = {
        "上交所主板": "stock_code GLOB '60*'",
        "科创板": "stock_code GLOB '68*'",
        "深交所主板": "stock_code GLOB '00*'",
        "创业板": "stock_code GLOB '30*'",
        "北交所": "stock_code GLOB '9[2]*' OR stock_code GLOB '4*' OR stock_code GLOB '8*'",
    }
    rng = random.Random(seed)
    picked: list[str] = []
    per_bucket = max(1, size // len(buckets))
    for clause in buckets.values():
        candidates = [
            str(row[0])
            for row in conn.execute(
                f"SELECT DISTINCT code FROM kline_1d WHERE {clause.replace('stock_code', 'code')} "
                f"AND code GLOB '[0-9]*'"
            )
        ]
        picked.extend(rng.sample(candidates, min(per_bucket, len(candidates))))
    return picked


def run(sample: int, seed: int, channel: str = "auto", day: str = "") -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    codes = _sample_codes(conn, sample, seed)
    report: dict[str, Any] = {
        "as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sampled": len(codes),
        "channel": channel,
        "day": day or "all settled dates",
        "per_symbol": [],
        "failures": [],
    }
    totals = {"compared": 0, "close_ok": 0, "volume_ok": 0}
    ratios: list[float] = []

    for index, code in enumerate(codes, 1):
        stored = _stored_bars(conn, code)
        try:
            remote = {d: (c, v) for d, c, v in _bars(code, channel)}
        except Exception as exc:
            report["failures"].append({"code": code, "error": f"{type(exc).__name__}: {str(exc)[:70]}"})
            continue
        # Only settled bars: today's is still moving, and comparing it would report a
        # disagreement for every symbol that trades.
        shared = sorted(
            d for d in set(stored) & set(remote) if d < TODAY and (not day or d == day)
        )
        if not shared:
            report["per_symbol"].append({"code": code, "shared": 0})
            continue

        close_bad, volume_bad = [], []
        for day in shared:
            stored_close, stored_volume = stored[day]
            remote_close, remote_volume = remote[day]
            totals["compared"] += 1
            if stored_close and abs(stored_close - remote_close) <= CLOSE_TOLERANCE * abs(stored_close):
                totals["close_ok"] += 1
            else:
                close_bad.append({"date": day, "stored": stored_close, "remote": remote_close})
            if stored_volume and remote_volume:
                ratio = stored_volume / remote_volume
                ratios.append(ratio)
                if abs(ratio - 1.0) <= VOLUME_TOLERANCE:
                    totals["volume_ok"] += 1
                else:
                    volume_bad.append({"date": day, "stored": stored_volume, "remote": remote_volume})
        report["per_symbol"].append(
            {
                "code": code,
                "shared": len(shared),
                "close_mismatches": len(close_bad),
                "volume_mismatches": len(volume_bad),
                "worst_close": close_bad[:2],
                "worst_volume": volume_bad[:2],
            }
        )
        if index % 50 == 0:
            print(f"    ... {index}/{len(codes)} symbols compared", flush=True)
        time.sleep(PAUSE_SECONDS)

    conn.close()
    compared = totals["compared"] or 1
    ratio_sorted = sorted(ratios) or [0.0]
    report["totals"] = totals
    report["close_agreement"] = round(totals["close_ok"] / compared, 5)
    report["volume_agreement"] = round(totals["volume_ok"] / compared, 5)
    report["volume_ratio_median"] = round(ratio_sorted[len(ratio_sorted) // 2], 4)
    report["volume_ratio_p01"] = round(ratio_sorted[int(len(ratio_sorted) * 0.01)], 4)
    report["volume_ratio_p99"] = round(ratio_sorted[int(len(ratio_sorted) * 0.99)], 4)
    report["symbols_with_any_close_mismatch"] = sorted(
        row["code"] for row in report["per_symbol"] if row.get("close_mismatches")
    )
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20261016)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--day",
        default="",
        help="compare only this date (YYYY-MM-DD). Use it to check a bar a sync has "
             "just written, which is otherwise one of ~250 per symbol",
    )
    parser.add_argument(
        "--channel",
        default="auto",
        help="eastmoney | tencent | auto (try in that order). Any of them is an "
             "independent vendor from the one that wrote the rows",
    )
    args = parser.parse_args(argv)

    print(f"comparing {args.sample} symbols across four markets via channel={args.channel}")
    report = run(args.sample, args.seed, args.channel, args.day)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    totals = report["totals"]
    print(f"\n=== {report['sampled']} sampled, {len(report['failures'])} unreachable ===")
    if len(report["failures"]) == report["sampled"] and report["sampled"]:
        # A checker that cannot reach its channel must not be read as agreement.
        print("  EVERY symbol was unreachable — this run proves nothing; check the channel")
    print(f"  bars compared            : {totals['compared']:,}")
    print(f"  close agreement          : {report['close_agreement']:.4%}")
    print(f"  volume agreement (±1%)   : {report['volume_agreement']:.4%}")
    print(
        f"  stored/remote volume     : median {report['volume_ratio_median']} "
        f"p01 {report['volume_ratio_p01']} p99 {report['volume_ratio_p99']}"
    )
    bad = report["symbols_with_any_close_mismatch"]
    print(f"  symbols with a close gap : {len(bad)} {bad[:8]}{' ...' if len(bad) > 8 else ''}")
    for row in report["per_symbol"]:
        if row.get("worst_close"):
            print(f"    {row['code']}: {row['worst_close']}")
    if report["failures"]:
        print(f"  unreachable: {report['failures'][:4]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
