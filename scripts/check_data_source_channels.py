#!/usr/bin/env python3
"""Probe every candidate channel for a data category, and compare them.

Why this exists
---------------
Most categories in this project have exactly one working source. If Sina's kline
endpoint changes shape the factories stop, and nothing says so until a run
produces nothing. A second channel is only worth having if it is reachable, fast
enough, and — the part that is usually skipped — **agrees with the first one**. A
fallback that returns different numbers is worse than no fallback, because the
disagreement stays invisible.

Comparing two channels needs care, and the first version of this script got it
wrong: it compared each channel's *latest* bar. Mid-session that is today's bar,
which every provider refreshes on its own cadence, so three correct sources
reported three different "latest" prices. The comparison here is therefore made
on a **settled** bar — the most recent date present in every channel other than
today — and each channel reports separately which dates it carries.

Usage::

    python scripts/check_data_source_channels.py
    python scripts/check_data_source_channels.py --only kline_daily,quote
    python scripts/check_data_source_channels.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
for _rel in ("packages/akshare-mcp/src",):
    _path = str(REPO_ROOT / _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)

TIMEOUT = 20.0
UA = os.getenv("AIASK_HTTP_USER_AGENT", "Mozilla/5.0")
TODAY = time.strftime("%Y-%m-%d")


def _open(url: str, *, referer: str = "https://www.baidu.com", encoding: str = "utf-8", proxy: bool = False) -> str:
    """Fetch a URL. ``proxy=False`` goes direct, which matters on this host."""

    handlers = [] if proxy else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with opener.open(request, timeout=TIMEOUT) as response:
        return response.read().decode(encoding, errors="replace")


def _day(text: str) -> str:
    """Normalise ``20261015`` / ``2026-10-15 14:00:00`` to ``2026-10-15``."""

    digits = "".join(ch for ch in str(text)[:10] if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return str(text)[:10]


def _bars(pairs: list) -> dict[str, Any]:
    return {"rows": len(pairs), "bars": {_day(str(date)): float(close) for date, close in pairs}}


def _minute_key(text: str) -> str:
    """Normalise a minute-bar key across the three providers' formats.

    Sina sends ``2026-10-16 10:00:00``, East money ``2026-10-16 10:00`` and
    Tencent ``202610161000``. Mapping Tencent's to a bare date dropped the time
    entirely, so its bars could never be compared against the others.
    """

    raw = str(text).replace("T", " ").strip()
    if len(raw) >= 16:
        return raw[:16]
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 12:  # 202610161000
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}"
    return raw[:16] if raw else ""


# A single session holds 48 five-minute bars, so any request that returns fewer
# than ~100 only covers today — and today's bars are all still moving, which leaves
# the three channels with nothing settled to be compared on.
MINUTE_BARS = 300


# --------------------------------------------------------------------------
# Daily klines
# --------------------------------------------------------------------------


def sina_kline_daily(code: str = "600519") -> dict[str, Any]:
    raw = _open(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol=sh{code}&scale=240&ma=no&datalen=30",
        referer="https://finance.sina.com.cn",
        encoding="gbk",
    )
    return _bars([(row["day"], row["close"]) for row in json.loads(raw)])


def eastmoney_kline_daily(code: str = "600519") -> dict[str, Any]:
    secid = ("1." if code.startswith(("6", "5")) else "0.") + code
    raw = _open(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&end=20500101&lmt=30",
        referer="https://quote.eastmoney.com/",
    )
    klines = (json.loads(raw).get("data") or {}).get("klines") or []
    return _bars([tuple(str(k).split(",")) for k in klines])


def tencent_kline_daily(code: str = "600519") -> dict[str, Any]:
    symbol = ("sh" if code.startswith(("6", "5")) else "sz") + code
    raw = _open(
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,30,qfq",
        referer="https://gu.qq.com/",
    )
    data = json.loads(raw).get("data", {}).get(symbol, {})
    rows = data.get("qfqday") or data.get("day") or []
    return _bars([(row[0], row[2]) for row in rows])


def push2_clist_rows(code: str = "600519") -> dict[str, Any]:
    """``push2.eastmoney.com`` is the host this project's docs call blocked."""

    raw = _open(
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fs=m:1+t:2&fields=f12,f14",
        referer="https://quote.eastmoney.com/",
    )
    diff = (json.loads(raw).get("data") or {}).get("diff") or []
    return {"rows": len(diff), "sample": [d.get("f12") for d in diff[:3]]}


def sina_listing_snapshot(code: str = "") -> dict[str, Any]:
    """Sina Market Center's paged listing, the source the universe sync already uses."""

    raw = _open(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData?page=1&num=5&sort=symbol&asc=1&node=hs_a",
        referer="https://vip.stock.finance.sina.com.cn",
        encoding="gbk",
    )
    rows = json.loads(raw)
    return {"rows": len(rows), "sample": [r.get("code") for r in rows[:3]]}


def eastmoney_listing_snapshot(code: str = "") -> dict[str, Any]:
    """The F10 profile report behind ``stocks``, probed on a real page."""

    raw = _open(
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPT_F10_BASIC_ORGINFO&columns=SECURITY_CODE&pageSize=5&pageNumber=1"
        "&sortColumns=SECURITY_CODE&sortTypes=1&source=WEB&client=WEB",
        referer="https://data.eastmoney.com/",
    )
    rows = (json.loads(raw).get("result") or {}).get("data") or []
    return {"rows": len(rows), "sample": [r.get("SECURITY_CODE") for r in rows[:3]]}


# --------------------------------------------------------------------------
# Realtime quotes
# --------------------------------------------------------------------------


def sina_quote(code: str = "600519") -> dict[str, Any]:
    raw = _open(f"https://hq.sinajs.cn/list=sh{code}", referer="https://finance.sina.com.cn", encoding="gbk")
    fields = raw.split('"')[1].split(",")
    return {"rows": 1, "price": float(fields[3]), "date": _day(fields[30]) if len(fields) > 30 else ""}


def tencent_quote(code: str = "600519") -> dict[str, Any]:
    raw = _open(f"https://qt.gtimg.cn/q=sh{code}", referer="https://gu.qq.com/", encoding="gbk")
    fields = raw.split('"')[1].split("~")
    return {"rows": 1, "price": float(fields[3]), "date": _day(fields[30]) if len(fields) > 30 else ""}


def eastmoney_quote(code: str = "600519") -> dict[str, Any]:
    secid = ("1." if code.startswith(("6", "5")) else "0.") + code
    raw = _open(
        f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f57,f58",
        referer="https://quote.eastmoney.com/",
    )
    data = json.loads(raw).get("data") or {}
    return {"rows": 1, "price": float(data.get("f43")) / 100.0, "date": TODAY}


# --------------------------------------------------------------------------
# Minute klines
# --------------------------------------------------------------------------


def _minute_bars(rows: list, time_index: int, close_index: int) -> dict[str, Any]:
    return {"rows": len(rows), "bars": {_minute_key(r[time_index]): float(r[close_index]) for r in rows}}


def sina_kline_minute(code: str = "600519") -> dict[str, Any]:
    raw = _open(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol=sh{code}&scale=5&ma=no&datalen={MINUTE_BARS}",
        referer="https://finance.sina.com.cn",
        encoding="gbk",
    )
    rows = [(r["day"], r["close"]) for r in json.loads(raw)]
    return _minute_bars(rows, 0, 1)


def eastmoney_kline_minute(code: str = "600519") -> dict[str, Any]:
    secid = ("1." if code.startswith(("6", "5")) else "0.") + code
    raw = _open(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f53&klt=5&fqt=0&end=20500101&lmt={MINUTE_BARS}",
        referer="https://quote.eastmoney.com/",
    )
    klines = (json.loads(raw).get("data") or {}).get("klines") or []
    return _minute_bars([tuple(str(k).split(",")) for k in klines], 0, 1)


def tencent_kline_minute(code: str = "600519") -> dict[str, Any]:
    symbol = "sh" + code
    # `web.ifzq.gtimg.cn` serves this path to nobody from here — TLS closes early,
    # over https and plain http alike — while the same path on the unsuffixed host
    # answers. The daily endpoint happens to work on both, which is what made this
    # look like a TLS problem instead of a hostname one.
    raw = _open(
        f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={symbol},m5,,{MINUTE_BARS}",
        referer="https://gu.qq.com/",
    )
    rows = (json.loads(raw).get("data", {}).get(symbol, {}) or {}).get("m5") or []
    return _minute_bars(rows, 0, 2)


# --------------------------------------------------------------------------
# Index klines — a different code space, so it is probed separately
# --------------------------------------------------------------------------


def sina_index_daily(code: str = "000001") -> dict[str, Any]:
    raw = _open(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol=sh{code}&scale=240&ma=no&datalen=30",
        referer="https://finance.sina.com.cn",
        encoding="gbk",
    )
    return _bars([(row["day"], row["close"]) for row in json.loads(raw)])


def eastmoney_index_daily(code: str = "000001") -> dict[str, Any]:
    raw = _open(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid=1.{code}&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&end=20500101&lmt=30",
        referer="https://quote.eastmoney.com/",
    )
    klines = (json.loads(raw).get("data") or {}).get("klines") or []
    return _bars([tuple(str(k).split(",")) for k in klines])


CATEGORIES: dict[str, dict[str, Callable[..., dict[str, Any]]]] = {
    "kline_daily": {
        "sina": sina_kline_daily,
        "eastmoney.push2his": eastmoney_kline_daily,
        "tencent": tencent_kline_daily,
    },
    "kline_index_daily": {
        "sina": sina_index_daily,
        "eastmoney.push2his": eastmoney_index_daily,
    },
    "kline_minute": {
        "sina": sina_kline_minute,
        "eastmoney.push2his": eastmoney_kline_minute,
        "tencent.mkline": tencent_kline_minute,
    },
    "quote": {
        "sina.hq": sina_quote,
        "tencent.qt": tencent_quote,
        "eastmoney.push2": eastmoney_quote,
    },
    "listing_snapshot": {
        "eastmoney.push2.clist": push2_clist_rows,
        "sina.market_center": sina_listing_snapshot,
        "eastmoney.f10": eastmoney_listing_snapshot,
    },
}


def _run(channel: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        payload = channel()
    except Exception as exc:
        return {
            "ok": False,
            "elapsed": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {str(exc)[:110]}",
        }
    payload["ok"] = True
    payload["elapsed"] = round(time.monotonic() - started, 3)
    return payload


# Two channels quoting the same number to different precision is not a
# disagreement: Sina reports an index as 3707.476 and East money as 3707.48, a
# relative difference of 1e-6. The tolerance below is a *rounding* allowance, and
# the spread is reported on every run so that a value that barely passes is
# visible rather than silently waved through.
RELATIVE_TOLERANCE = 1e-4


def _verdict(values: dict[str, float]) -> tuple[str, float, float]:
    """Return (agreement, spread, max relative difference)."""

    spread = max(values.values()) - min(values.values())
    scale = max(abs(v) for v in values.values()) or 1.0
    relative = spread / scale
    return ("agree" if relative <= RELATIVE_TOLERANCE else "DISAGREE"), spread, relative


def _compare_series(series: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Compare channels on the latest settled bar, not on each one's last row."""

    shared = set.intersection(*(set(bars) for bars in series.values()))
    older = sorted(d for d in shared if d[:10] < TODAY)
    if older:
        key, settles = older[-1], "settled"
    else:
        # Nothing settled in common. `today` is deliberately not used as a
        # fallback: for minute bars *every* bar of the current session is still
        # moving, not just the last one, so a comparison inside today says nothing
        # about whether the channels agree.
        latest = sorted(set.union(*(set(b) for b in series.values())))[-1:] or [""]
        return {
            "agreement": None,
            "settled_date": None,
            "reason": f"no settled bar in common; latest seen {latest[0]}",
        }

    values = {name: float(bars[key]) for name, bars in series.items()}
    agreement, spread, relative = _verdict(values)
    return {
        "agreement": agreement,
        "settled_date": key,
        "settles": settles,
        "closes": {n: round(v, 4) for n, v in values.items()},
        "spread": round(spread, 6),
        "relative_spread": f"{relative:.2e}",
        "shared_bars": len(shared),
    }


def _cross_check(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    working = {name: r for name, r in results.items() if r["ok"]}

    series = {name: r["bars"] for name, r in working.items() if r.get("bars")}
    if len(series) >= 2:
        return _compare_series(series)

    prices = {name: float(r["price"]) for name, r in working.items() if r.get("price") is not None}
    if len(prices) >= 2:
        agreement, spread, relative = _verdict(prices)
        return {
            "agreement": agreement,
            "settled_date": TODAY,
            "settles": "live quote",
            "closes": prices,
            "spread": round(spread, 6),
            "relative_spread": f"{relative:.2e}",
        }

    if series or prices:
        return {"agreement": None, "settled_date": None, "reason": "only one channel returned a comparable value"}
    return {"agreement": None, "settled_date": None, "reason": "no comparable values"}


def probe(only: Optional[list[str]] = None) -> dict[str, Any]:
    report: dict[str, Any] = {"as_of": time.strftime("%Y-%m-%d %H:%M:%S"), "categories": {}}
    for category, channels in CATEGORIES.items():
        if only and category not in only:
            continue
        results = {name: _run(fn) for name, fn in channels.items()}
        entry: dict[str, Any] = {
            "channels": results,
            "working": sorted(n for n, r in results.items() if r["ok"]),
        }
        entry.update(_cross_check(results))
        report["categories"][category] = entry
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", default="", help="comma-separated category names")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    only = [x.strip() for x in args.only.split(",") if x.strip()] or None
    report = probe(only)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"as of {report['as_of']}")
    for category, data in report["categories"].items():
        verdict = data.get("agreement") or "n/a"
        on = f" on {data['settled_date']} ({data.get('settles')})" if data.get("settled_date") else ""
        print(f"\n[{category}]  working={len(data['working'])}  agreement={verdict}{on}")
        if data.get("reason"):
            print(f"    ({data['reason']})")
        for name, result in data["channels"].items():
            if result["ok"]:
                detail = f"rows={result['rows']:<4}"
                if result.get("price") is not None:
                    detail += f" price={result['price']}"
                if result.get("bars"):
                    detail += f" latest={max(result['bars'])}"
                if result.get("sample"):
                    detail += f" sample={result['sample']}"
                print(f"    OK   {name:24s} {result['elapsed']:>6.2f}s {detail}")
            else:
                print(f"    FAIL {name:24s} {result['elapsed']:>6.2f}s {result['error']}")
        if verdict == "DISAGREE":
            print(
                f"    !! same bar, different value: {data['closes']}  "
                f"spread={data['spread']}  relative={data.get('relative_spread')}"
            )
        elif verdict == "agree" and data.get("spread"):
            print(f"    (~ they differ by {data['spread']}, relative {data.get('relative_spread')} — rounding)")

    thin = [c for c, d in report["categories"].items() if len(d["working"]) < 2]
    print()
    print(f"single-channel categories: {thin}" if thin else "every category has at least two working channels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
