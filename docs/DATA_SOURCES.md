# Data sources

Latest measured state (2026-11-04): daily bars reach 2026-11-03 (5,574 codes;
completeness still partial). Stored real-time quotes remain at 2026-10-18 and
fund-flow completeness at 2026-10-16. See the
[current capability review](reviews/项目能力与四工厂审查-当前结论-2026-11-04.md)
and [read-only SQL evidence](strategy-factory-quality/continuous-20261104/evidence/current-capability-data.json).
The 2026-10-29 section below is retained as a historical measurement.

## Runtime and paper-evidence state (measured 2026-10-29)

The authoritative database is not currently a complete market-data baseline.
Read-only inspection found `kline_1d` latest at **2026-10-23** with 53 rows on
each of 10-21..10-23 and 57 rows on 10-19..10-20, while 10-16 has 5,570 rows.
`stock_quotes` held 171 rows and the maximum `updated_at` was
`2026-10-18 10:42:26`. The local machine has no verified real-time quote
provider. Consequently a stale DB quote must not trigger matching, stops or
risk reduction; after the P2 guard is enabled these actions will be refused and
counted until a fresh quote source exists. Day-close stop evaluation is a
separate follow-up, not an excuse to trade on stale marks.

The `~/.aiask-local` sandbox snapshot is a **separate fork**, not an approved
restore source for the authoritative store. Its bars must not be copied into
the authoritative database without the provider/write gate and sampled source
agreement. The MCP `DataSyncScheduler` has historically started by default,
including in a read-only tool-only server, so it is being changed to default
off in tool-only mode and on in full mode; an explicit
`DATA_SYNC_SCHEDULER_ENABLED` still overrides the default.

The 10-29 integrity audit and repair authorization are not complete. Therefore
the known out-of-range paper fills and weekend-dated NAV/metrics remain
unrepaired; do not present historical paper performance as clean evidence.
Reproduce data freshness without importing the server:

```bash
/opt/miniconda3/envs/aiask/bin/python - <<'PY'
import sqlite3
from pathlib import Path
db = Path.home()/".aiask/data/db/akshare_mcp.sqlite3"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
print(con.execute("SELECT substr(time,1,10),COUNT(*) FROM kline_1d GROUP BY 1 ORDER BY 1 DESC LIMIT 10").fetchall())
print(con.execute("SELECT MAX(updated_at),COUNT(*) FROM stock_quotes").fetchone())
con.close()
PY
```

This repo had no git remote at measurement time and no hosted CI run is claimed.
`scripts/ci_local.sh` is the local compileall + eight-package pytest gate.

What this project reads, from where, and what was verified on 2026-10-15 on this
machine. Every "works" below was measured, not assumed; the command that measures
it is in the row.

The short version: **Tushare is the declared provider and has no token here, so
nothing it feeds was ever populated.** Sina and East money are reachable and now
cover the market data the project needs. 146 tables existed with 80 empty and all
80 read by something; the categories below are those gaps, now mostly closed.

## 1. The constraint that shapes everything: this host's proxy

`http_proxy=http://127.0.0.1:7892`, and DNS resolves every host into
`198.18.0.0/15` (a fake-IP range), so all traffic goes through the proxy. Which
host answers is therefore not a property of the provider, it is a property of the
proxy's rules. Measured:

| Host | Status |
|---|---|
| `vip.stock.finance.sina.com.cn`, `money.finance.sina.com.cn`, `hq.sinajs.cn` | works |
| `push2his.eastmoney.com` | works |
| `datacenter-web.eastmoney.com` | works |
| `emweb.securities.eastmoney.com` | works |
| `qt.gtimg.cn`, `web.ifzq.gtimg.cn` (Tencent) | works |
| `q.10jqka.com.cn`, `basic.10jqka.com.cn`, `d.10jqka.com.cn` | works |
| `query.sse.com.cn` (上交所) | works |
| `pypi.org` | works |
| **`push2.eastmoney.com`** | **blocked** — the conventional quote/list/sector API |
| `82.push2.eastmoney.com` | blocked |
| `www.szse.cn` (深交所) | 403 |
| `www.csindex.com.cn` | 404 |
| `api.tushare.pro` | reachable, but no token is configured |

Two consequences worth remembering: the East money data that looks unavailable is
mostly available on `datacenter-web` instead, and the TDX path below is reachable
at the socket level while still being unusable in practice.

Prefer `direct` (no proxy) for these hosts — it is faster and the project already
records that the proxy breaks some East money endpoints. The new clients try
direct first and fall back to the proxy, recording which transport answered.

## 2. Declared versus actually installed

| Library | Declared | Installed | Referenced in | Consequence |
|---|---|---|---|---|
| `tushare` | core dependency | yes, 1.4.29 | 4 files | installed but no `TUSHARE_TOKEN` configured; the legacy fallback chain is off by default (`DATA_SOURCE_KEEP_LEGACY_FALLBACK=0`), so it stays dormant |
| `efinance` | `legacy` extra | yes, 0.5.8 | 2 files | `get_realtime_quotes` fails with JSONDecodeError (it uses `push2`) |
| `pytdx` | not declared | yes, 1.72 | 0 files directly | used through `tdx_local`; see section 5 |
| `akshare` | **not declared** | **no** | **was 26 files — now 0** | every reference was removed; see below |
| `baostock` | `legacy` extra | yes, 0.8.9 | 8 files | installed 2026-10-21 so the declared dependency matches the environment; `baostock_client` is live (still gated behind the same legacy fallback toggle) |
| `mootdx`, `tqcenter` | not declared | no | 1 file each | `tdx_tqcenter` returns `[]` for everything |

### Broker SDKs: documented as out of reach, not installed

The finance MCP sub-servers for **QMT** (`xtquant`) and **易盛/THS** (`easytrader`)
lazy-import their vendor SDKs and raise a descriptive `RuntimeError` when a tool is
called without the SDK — a tool-level error, never a server crash. Those SDKs are
vendor-proprietary and out of reach for this environment, so they are **not
installed**; live trading tools remain gated behind their env flags and are not
enabled. This is a documented unreachable dependency, not a silent one.

### akshare: removed, not installed

`akshare` was not a declared dependency, so installing the project never provided
it, yet 26 modules imported it behind `except ImportError` and degraded to an
empty list. Every one of those paths had been dead since the tree was extracted
and nothing said so. **They have all been removed.**

The removal is commit `0ea53ef`. It was done with a one-shot codemod that has
since been deleted — it referenced a pre-change state that no longer exists, and
`CLAUDE.md` records what such a rewrite has to prove. The shape was:

* 20 module-level `try: import akshare as ak / except: ak = None` guards deleted;
* 15 `if ak is None:` bodies unwrapped, because those were the ones actually
  running;
* 20 `if ak is not None:` blocks and 13 unreachable spans deleted — code that
  could never execute;
* 5 ternary expressions resolved to their live branch;
* the akshare-only helpers (`_get_financials_akshare*`, `_get_minute_kline_from_akshare`,
  `_build_akshare_client`, the three `sync_*_from_akshare` service methods,
  `sync_sector_fund_flow`, `sync_north_fund_best_effort`, the three
  `_backfill_*` steps) replaced with stubs that return an explicit
  `source_removed` / `provider_unavailable` reason rather than an empty result;
* `scripts/tdx_probe/probe_datasources.py` lost its akshare section.

`grep` for the library now returns 0 imports and 0 uses of the `ak` name across
`packages/` and `scripts/`.

Behaviour is unchanged where it mattered: the guarded branches were already the
live ones. Where akshare was the only implementation, the function now says so —
`get_option_chain` still returns `success=true` with an empty chain plus the
reason, `get_stock_info` falls back to the local reference table, and
`get_margin_data` reports that East money's datacenter report returned nothing.

`baostock` was left alone: it *is* declared, as an opt-in `legacy` extra.

## 3. What the project needs, and where it comes from

| Category | Source | Endpoint / report | Coverage now | Refresh cost |
|---|---|---|---|---|
| Stock listing | Sina Market Center | `Market_Center.getHQNodeData?node=hs_a` | 5,575 listed, 70 pages | ~45 s |
| Daily klines | Sina | `CN_MarketData.getKLineData?scale=240` | 1,733,871 rows, 5,582 codes, 100% | ~11 min at concurrency 4 |
| Index klines | Sina | same, `sh000001` / `sz399300` / … | 5 indices | seconds |
| Industry, sector | East money F10 | `RPT_F10_BASIC_ORGINFO` (`EM2016`) | 5,577 / 5,577 = 100% | ~14 s |
| Market cap, PE, PB | Sina listing | same response as the listing | 5,582 / 5,575 | free with the listing |
| Financials | East money datacenter | `RPT_F10_FINANCE_MAINFINADATA` | 119,837 rows, 5,575 codes, all 14 columns | ~27 min |
| Industry board membership | East money datacenter | `RPT_LICO_FN_CPD` (`BOARD_CODE`) | 5,575 members / 128 boards | free with the universe sync |
| Dragon-tiger 龙虎榜 | East money datacenter | `RPT_DAILYBILLBOARD_DETAILSNEW` | 269,446 rows, 2004-06 -> 2026-10-15 | ~2 min |
| Fund flow 个股资金流 | East money datacenter | `RPT_DMSK_TS_STOCKNEW` | 5,200 rows, **one day only** | ~5 s |
| Trading calendar | derived from `kline_1d` | — | 1,399 dates, 2021-01-13 -> 2026-10-15 | free |
| Announcements, notices | project's own ingest | `run_market_event_ingest.py --once` | 374 documents, 325 events | seconds |
| Market temperature | derived from klines + industry | `refresh_market_temperature_snapshot_cache` | cached on first call | one request |
| Minute klines | Sina / Tencent | `scale=5/15/30/60` | written back on use; full-market backfill is a separate job | ~45 min per scale for the market |
| Margin 融资融券 (market) | 上交所 | `query.sse.com.cn/.../queryMargin.do` | 38 rows, 2026-08-17 -> 2026-10-15, **Shanghai only** | seconds |
| Dividends | East money datacenter | `RPT_SHAREBONUS_DET` | **available, not wired** | — |

Reports already registered in code but not yet consumed are in
`eastmoney_datacenter.REPORTS`.

## 4. Refreshing

```bash
# listing + market cap + industry (about 1 minute)
python scripts/sync_market_universe.py --apply --with-industry

# daily klines, whole market
python scripts/free_data_daily_sync.py --universe db --concurrency 4 \
  --skip-sector --skip-north --skip-ic --skip-factor-pool

# calendar, financials, board membership, fund flow, dragon-tiger
python scripts/sync_market_fundamentals.py --apply --what all
python scripts/sync_market_fundamentals.py              # report only, writes nothing
```

## 5. Verified as *not* usable

**TDX online (`tdx_local`, `pytdx`).** Two separate findings, both now settled.

*The hang was a real bug in this project, and it is fixed.* It was never the data
call: `get_realtime_quote` returned in 0.1 s and `get_kline` in 4.3 s, and the
process then never terminated. `TdxHq_API(heartbeat=True)` starts a **non-daemon**
thread on every successful connect, and CPython's shutdown runs
`threading._shutdown()` — which waits for every non-daemon thread — *before* it
runs atexit callbacks. So nothing registered at exit could stop it, and
`faulthandler` put the stack at `pytdx/heartbeat.py:24` under
`threading.py:1543 _shutdown`. **Every** process that ever opened an online TDX
connection would have hung at exit, the MCP server included. The client now
constructs with `heartbeat=False`; a dropped socket surfaces as an error on the
next call, which the existing cooldown and reconnect logic already handles.
Measured after the fix: exit code 0 in 5 s.

*The data is still not there, and that part is the server's.* Against
`180.153.18.170` and `60.191.117.167` — the only two of the twelve pool entries
that answer — `get_security_count(0)` returns 24,444 securities, so the protocol
and the connection are genuine, while `get_security_bars` (every category tried),
`get_index_bars`, `get_security_list` and `get_security_quotes` all return empty.
A server that answers one administrative query and serves no market data is a
server-side limit. Treat TDX as unavailable and do not route a data need through
it; the ten dead pool entries and the dead-first ordering remain as they are.

```bash
# reproduces the server measurement
python - <<'PY'
import socket, time
handshake = bytes.fromhex("0c0218930001030003000d0001")
for host in ("119.147.212.81", "180.153.18.170", "60.191.117.167"):
    t0 = time.time()
    try:
        s = socket.create_connection((host, 7709), timeout=4)
        s.sendall(handshake); s.settimeout(4)
        print(host, len(s.recv(32)), f"{time.time() - t0:.2f}s")
        s.close()
    except Exception as exc:
        print(host, type(exc).__name__, f"{time.time() - t0:.2f}s")
PY
```

`tqcenter` (通达信插件) is not installed, so every `tdx_tqcenter` call returns
`[]`. `status()` reports this, and the call sites log the reason.

## 6. Partially satisfied, and how

| Gap | Reality | What to do |
|---|---|---|
| Margin 融资融券 | **wired for 上交所 only.** 深交所 returns 403 through the proxy, and the SSE endpoint ignores its `stockCode` parameter, so `margin_detail` cannot be filled from here either | needs a Shenzhen route; the reader already reports the coverage gap |
| `stock_fund_flow` | the report is a one-day snapshot, no history | call it daily; it does not backfill |
| `financials` | all 14 columns now come from one report, `RPT_F10_FINANCE_MAINFINADATA`, so a row cannot mix two report periods | `debt_ratio` is stored as a fraction (`ZCFZL / 100`) because every consumer reads `1.0 - debt_ratio` |
| `kline_intraday` | fills on use: `get_minute_kline` now writes its bars back, which it never did | a full-market backfill is a separate multi-hour job |
| daily bars beyond the chain | the three channels carry ~300 bars and are rate-limited per host | a multi-year backfill is a separate job; the chain serves the daily incremental |
| `dragon_tiger.buyer_type` | not in the report | needs a different report |
| `dragon_tiger` codes | 6,433 codes, 898 outside today's universe | historical appearances including since-delisted companies; kept on purpose |
| Industry | 100% from East money | `stocks.sector` is `EM2016` level 2, `stocks.industry` level 1 — one taxonomy, two depths |
| 新三板 | East money reports cover about 24,800 securities | filtered to the `stocks` universe on write, so no table holds a different definition of "listed" |

## 7. Channels: what is redundant, and how that was established

`scripts/check_data_source_channels.py` probes every candidate for a category, then
cross-checks them **against each other on a settled bar**. Re-run it after a
provider change, and before trusting any new channel.

```
$ python scripts/check_data_source_channels.py
[kline_daily]        working=3  agreement=agree on 2026-10-15 (settled)
    sina 0.10s | eastmoney.push2his 0.11s | tencent 0.16s
[kline_index_daily]  working=2  agreement=agree on 2026-10-15 (~0.004 apart, relative 1e-06)
[kline_minute]       working=2  agreement=agree on 2026-10-15 15:00 (settled)
[quote]              working=3  agreement=agree   (all three read 1313.57 at the same instant)
[listing_snapshot]   working=3  (sina market centre, eastmoney f10, eastmoney push2)
```

Every category has at least two working channels. Tencent's minute endpoint
now answers too: it was never a TLS fault. `web.ifzq.gtimg.cn` closes that path
early — over https *and* plain http — while the same path on the unsuffixed host
returns fine, and the daily endpoint works on both, which is what made it look
like TLS. Fixing the hostname restored a fallback that had been dead for every call.

### The history was checked against a second vendor, not just assumed

`scripts/check_kline_history_agreement.py` samples symbols across all four markets
and compares stored bars against East money on the dates they share:

```
$ python scripts/check_kline_history_agreement.py --sample 400
  400 sampled, 0 unreachable
  bars compared            : 121,650
  close agreement          : 100.0000%
  volume agreement (±1%)   : 99.9180%
  stored/remote volume     : median 1.0  p01 0.9999  p99 1.0001
  symbols with a close gap : 0
```

So the 1.7 M rows written by a single provider are corroborated rather than merely
old. Run this after any change to the sync.

### Comparing two channels needs a settled bar

The first version of the probe compared each channel's *latest* row and reported
three correct sources disagreeing (1312.97 / 1311.57 / 1314.20). They were quoting
different days: mid-session, "latest" is today's moving bar, and each provider
refreshes it on its own cadence. The probe now compares the most recent date that
all channels share and that is **not** today, and says which date it used.

### Traps a retry would not catch

Both were found by comparing channels *before* wiring them, in a sync that fills
1.73 M rows:

| Trap | Evidence | Handling |
|---|---|---|
| **Volume units** | East money `f56` and Tencent's fifth field are 手; Sina is 股. The ratio measured **exactly 100.00** on every bar | multiply by 100 before writing |
| **Adjustment** | Sina's endpoint takes no adjustment parameter (不复权). Over 120 days, **12 of 12** stocks matched East money `fqt=0` and **none** matched `fqt=1` | request `fqt=0`; use Tencent's `day` array, not `qfqday` |
| **Same code, different security** | `430047` returns a **2020** series from East money and a **2025** one from Sina — reassigned 北交所 codes | the write is refused (below) |
| **Units vary by market, not just by vendor** | Tencent states 科创板 (688xxx) volume in **股** and every other market in **手**. Measured: 688 (n=6 daily, 144 intraday) ratio 1.00; 300/60/00 (n=14 daily, 288 intraday) ratio 100.00 | `volume_units.tencent_volume_to_shares()` decides per symbol |

Near the end of the window the two adjustments *coincide* — 前复权 is anchored at
the latest price — so a short comparison says they agree: 600519 over 40 days
showed no difference. The 120-day check across twelve stocks is what settled it.
One stock over forty days would have proved nothing.

The unit rule was found the same way, and only because the guard refused to write:
the sync's fallback path served 57 科创板 symbols from Tencent, the guard compared
them against stored history and rejected every one with `volume ratio 100.0 is not
1.0`, and the cause was **our own blanket ×100** rather than the provider. The
earlier "Tencent reports 手" measurement had been taken on 600519, 000001, 300750
and 002594 — all of them markets where it is true.

### The chain refuses a write it has evidence against

`fetch_daily_bars()` tries `sina → eastmoney → tencent` and reports which channel
served each symbol, because a run served entirely by a fallback is a different
result from a normal one. When a fallback serves a symbol, it is checked against
the stored history before anything is written:

* settled closes must match within 1e-4 relative,
* the volume ratio must be 1.0 (this is what verifies the 手→股 conversion),
* and the series must overlap the stored one — or, with no history stored, must
  reach within 30 days of the market's latest bar.

Anything else is **refused and counted as a failure**: not written, and not counted
as success. Reporting alone was tried first and was not enough — a run wrote the
2020 series over a code whose stored series ended in 2025, printing a warning while
the rows were already in the table.

Today's bar is excluded from the comparison. It is still moving (an earlier run
stored 1312.97 for a session that later traded at 1314), and comparing it raised a
false "mismatch" for healthy symbols on every run — which is how a real mismatch
ends up ignored.

### Failover was drilled, not just reasoned about

The chain was tested by denying DNS for the primary host (`socket.getaddrinfo`
raising `gaierror`), so `requests`' own connection, retry and error paths ran
rather than an exception injected into the sync. Result: 8 of 8 symbols served by
the fallback, 60 rows each, every one checked against 59 settled stored bars with a
volume ratio of 1.0 and zero close mismatches, and the summary correctly reporting
that the primary had served nothing.

### Fundamentals: the stored values are faithful, and the cross-check works on complete years

`financials` is filled from East money's `RPT_F10_FINANCE_MAINFINADATA`. The obvious
worry is that a field was mapped to the wrong column, so every value was checked
against the report itself — which needs a filter that works. `(SECURITY_CODE="X")`
in **double** quotes is accepted; the single-quoted form is rejected by the search
grammar with an `ANTLR InputMismatchException`, which is what made this look
impossible to query at all.

Checked that way, the stored column equals the report field exactly:

    report_date   stored roe   ROEJQ    ROEJQTZ   ROEKCJQ
    2025-03-31         3.26    3.26   -68.29%      None
    2025-06-30         3.41    3.41   -75.11%       3.40
    2025-09-30         4.89    4.89   -73.32%      None
    2025-12-31         6.89    6.89   -70.49%       6.78

`ROEJQ` is the field that was mapped, `ROEJQTZ` is the **year-over-year change**
(not a level), and `ROEKCJQ` is the 扣非 variant — a different measure. The stored
series is monotone across the year, which is the shape of a cumulative figure, and
`ROEJQTZ = -68.29%` reconciles with it: ``10.28 × (1 - 0.6829) = 3.26``.

`scripts/check_financials_agreement.py` compares those stored values against Sina.
Two bugs in that tool had produced an earlier, much worse verdict:

* the stored-side query was pinned to ``>= '2025-01-01'``, so ``--year 2024``
  reported "no overlapping periods" for every symbol — a finding about the tool;
* it defaulted to the **current, incomplete** year, whose interim columns on Sina's
  page do not line up with the stored periods. That single page produced the
  original "no substitutable column" table, and the growth-rate columns carried the
  whole conclusion.

Corrected, over 20 symbols and a complete year:

    column            2024     2023
    current_ratio     98%      95%
    debt_ratio        98%      95%
    roe               95%      90%
    net_margin        95%      95%
    roa               95%      86%
    bvps              94%      84%
    revenue_growth    91%      90%
    eps               86%      65%
    profit_growth     27%      30%

The level and balance-sheet columns agree with an independent vendor **95-98%**; the
growth-rate columns do not (`profit_growth` 27-30%), and `eps` is unstable between
years. So the earlier reading was too harsh and the decision is unchanged for a
better reason: **95% agreement still means 5% silently wrong**, and "substitutable"
requires agreeing on every period. Sina stays a diagnostic — but it *is* a working
diagnostic, and it is what corroborates the stored values.

**The 000858 question is closed.** It was "the stored early-2025 quarters
disagree with Sina about threefold". Both halves now have answers: the stored values
faithfully reproduce East money's field (above), and Sina's `ctrl/2025` page carries
interim columns that do not correspond to the periods their labels name — it is
incoherent as a cumulative series (10.56 → 13.63 → 15.37 → **6.89**), while East
money's is monotone and reconciles with its own YoY field. On complete years the two
agree 95-98%, which is what makes the 2025 page the outlier rather than the data.
No row was changed on this basis: nothing in `financials` was wrong.


### Sources that were reported unavailable and turned out to exist

Three items were carried as "no source on this host". Closing them required
questioning the earlier probes rather than the providers:

| Table | What was concluded | What was actually true |
|---|---|---|
| `margin_detail` | 上交所 answers only at market level and ignores `stockCode`; 深交所 is 403 — so per-stock margin is unavailable | 上交所 and 深交所 are both correct **and irrelevant**: East money's `RPTA_WEB_RZRQ_GGMX` carries 6.9M per-security rows covering 沪, 深 **and 北** |
| `north_fund_flow` | the akshare source was removed, no replacement | `RPT_MUTUAL_DEAL_HISTORY` still updates — but **only southbound** |
| TDX online | 2 of 12 pool servers answer | all 12 accept a TCP connection; 2 complete the handshake and answer `get_security_count` (24,444) while **every data command returns nothing** |

The TDX line is worth restating with its parameter question closed. The first
probe paired **上海** codes with market 0 and **深圳** codes with market 1 — pytdx
uses 0 = 深圳 and 1 = 上海 — so "no data" could have been a statement about the
arguments rather than the servers. Re-run with the pairings corrected, against the
two servers that complete the handshake: `get_security_count` returns 24,444 (深)
and 27,627 (沪), and `get_security_bars` at categories 9 and 0, `get_index_bars`,
`get_security_list` and `get_security_quotes` all still return **zero rows**. The
conclusion stands, and now it stands on a probe whose parameters have been checked.
A negative result is worth exactly as much as its arguments.

**Northbound flow is not missing, it is discontinued.** Measured across 400
consecutive rows: channels 001/003/005 carry no value, 002/004/006 do, and the
totals reconcile (`002 + 004 == 006` exactly, e.g. -2065.77 + -1502.66 = -3568.43).
So `north_money`, `hgt` and `sgt` are written as **NULL** and the reader returns
`None`, with `total_net = None` and `reason = northbound_not_published`. A zero
there would report a flat market. The 146 pre-existing rows keep their published
northbound values: the upsert uses `COALESCE` so a "no longer published" response
cannot erase one that was.

**Sector fund flow is derived, not sourced.** No East money report carries board
fund flow (six candidate names probed, none exists) and the `push2` board endpoint
is on a host that does not answer. `sector_fund_flow_derived` is computed inside the
database from `stock_fund_flow` × `block_stocks`, is labelled
`derived:stock_fund_flow×block_stocks`, and reports `complete: false` with a count of
blocks whose membership is only partly covered (74 of 128 on 2026-10-16). It is an
aggregation of per-stock figures, **not an exchange-published board total**, and it
is a separate table because `market_blocks.total_amount` already means turnover.

### Traps found while wiring the new sources

| Trap | Evidence | Handling |
|---|---|---|
| **Single-column pagination is unstable** | Sorting `RPTA_WEB_RZRQ_GGMX` on `DATE` alone: 6,000 rows read → 557 unique, **74.9% duplicates**, and rows missing besides | always send a tie-breaker (`DATE,SCODE`); `fetch_report` now reports `duplicate_rows` / `pagination_unstable` for any report |
| **The suffix gate blocks the whole table** | `_get_margin_detail_suffix_coverage` blocks when either `.SH` or `.SZ` has no rows | a sync that cannot produce both reports failure instead of writing one side |
| **A reader can undo an honest NULL** | `get_north_fund_history` and the `margin_detail` mappers used `float(row.get(k) or 0.0)`, turning "not published" into "zero" | `_stored_float` passes `None` through; `total_net` is `None` + a reason |
| **A `source_unavailable` status nothing ever set** | the branch could only inherit it from a previous row, so a first run reported `missing`/`stale` for a provider that is gone | the status is now assigned for the TDX keys, with the probe recorded in `detail` |

### Current channel state (2026-10-18)

    kline_daily        2 of 3   sina + tencent agree on 2026-10-16
    kline_minute       2 of 3   sina + tencent agree on 2026-10-16 15:00
    quote              2 of 3   sina + tencent agree live
    kline_index_daily  1 of 2   eastmoney.push2his unreachable

`push2his` and `push2` have been answering `RemoteDisconnected` for every request
since the sync ran; `datacenter-web` has stayed up throughout, which is why the
fundamental and margin sources keep working. Tencent's 501 block, self-inflicted by
~1,300 requests in 15 minutes, has lifted. **Redundancy here is a measurement, not a
property** — this section is what it was when the probe last ran.

## 8. What every one of these sources owes the caller

The providers in `packages/akshare-mcp/src/akshare_mcp/data_source/` follow the
same rules, because the previous versions did not:

1. **A short read is reported, not returned.** `complete` / `failed_pages` (and
   `truncated` for a row cap) travel with the data. A partial page set can never
   be mistaken for the whole report.
2. **Reading nothing raises.** An outage is not an empty market. Returning `[]`
   for "provider unavailable" is how a full-market listing once produced zero
   symbols without a word.
3. **A field the source does not carry is `None`, never 0.** A fabricated `0.0`
   debt ratio passes every leverage ceiling, and an undefined IC written as `0.0`
   reads as a measured zero.
4. **The source is named.** `resolve_stock_universe()`, `--with-industry` and the
   sync reports all print which source produced the data, and warn on stderr when
   they fall back.
5. **A fallback says it is one.** Sina's board list is still reachable and still
   used when East money fails; it is reported as a fallback, not as the
   classification.
6. **Rows in a table are not the same as a usable dataset.** Every reader of
   `margin_market_flow`, `stock_fund_flow` and `block_stocks` first consults
   `tdx_data_completeness`, and blocks with `completeness_missing` when there is
   no row for its key. A sync that writes data without updating that registry
   leaves the table populated and *still unreadable* — which is exactly what the
   first attempt at the margin sync produced: 38 rows written, and
   `get_margin_market_history()` returned zero. `_register_completeness()` in
   `scripts/sync_market_fundamentals.py` now writes the registry alongside the
   data, with the limitations in `detail` where the reader passes them through.

## 9. Verification snapshot (2026-10-16)

```bash
python scripts/aiask_readiness.py --strict                # ready: True, 0 critical
python scripts/check_silent_exception_swallows.py         # 0 pass / 0 continue / 0 return-zeroish
python scripts/sync_market_universe.py --with-industry    # coverage + source
python scripts/sync_market_fundamentals.py                # per-category report
```

| Table | Rows | Codes | Range |
|---|---:|---:|---|
| `kline_1d` | 1,733,871 | 5,582 | 2021-01-13 -> 2026-10-15 |
| `factor_values` | 339,756 | 5,560 | full market, latest 2026-10-16 |
| `factor_ic_history` | 2,007 | 18 rows/day | 6 factors x 3 horizons over ~5,520 names |
| `stocks` | 5,582 | 5,575 listed | industry + sector 100%; 2 delisted rows marked `D` |
| `financials` | 119,819 | 5,575 | 2021-03-31 -> 2026-09-30 |
| `dragon_tiger` | 269,446 | 6,433 | 2004-06-25 -> 2026-10-15 |
| `block_stocks` | 5,575 | 5,575 | 128 industry boards |
| `trading_dates` | 1,399 | — | 2021-01-13 -> 2026-10-15 |
| `stock_fund_flow` | 5,200 | 5,200 | 2026-10-15 only |
| `market_temperature_snapshots` | 1 | — | warmed on demand, not a standing feed |

11 of the 5,575 listed symbols have no bar for 2026-10-15. They are suspended
(`*ST岭南`, `*ST萃华`, `*ST康佳A` and similar); a suspended stock having no bar
for a day it did not trade is the correct result, not a sync failure.

Four market-data tables are still empty and each has a reader: `margin_detail`
and `margin_market_flow` (SSE endpoint proven reachable, 深交所 blocked),
`kline_intraday` (Sina's `scale=5/15/30/60` proven reachable), and
`market_temperature_snapshots` (fills on first call). Wiring them is separate
work from a sync run; nothing here pretends they are current.

`market_blocks` also holds 80 rows of `block_type='industry_legacy'`: a 2026-07-16
snapshot from akshare sector-fund-flow that has no members in `block_stocks`, and
43 of whose names duplicate a live board. They are labelled rather than deleted,
so `block_type='industry'` returns exactly the 128 boards that have members.

## 10. Runtime databases: which file is authoritative

Two SQLite files both live under `~/.aiask` and only one of them is real:

| Path | Status | Notes |
|---|---|---|
| `~/.aiask/data/db/akshare_mcp.sqlite3` | **authoritative** | 4.2 GB, 152 tables, all market/factor/factory data. Overridable by `AIASK_SQLITE_PATH` / `AKSHARE_MCP_SQLITE_PATH`. |
| `~/.aiask/akshare_mcp.sqlite3` | legacy shell (0 rows) | Historical leftover at the old location. Do not route reads through it; `factory_cli evidence --legacy-db` is the only intentional accessor and its envelope says so. |
| `~/.aiask/data/db/strategy_factory_task_board.sqlite3` | authoritative | Task board (762 tasks). Resolved by `STRATEGY_FACTORY_TASK_BOARD_PATH` or `resolve_task_board_path()`. |

The resolver is `aiask_quant_core.runtime_paths.resolve_sqlite_path()`:
`AIASK_SQLITE_PATH` > `AKSHARE_MCP_SQLITE_PATH` > `<AIASK_RUNTIME_HOME or ~/.aiask>/data/db/akshare_mcp.sqlite3`.

`factory_cli evidence` now defaults to the resolver (previously it pointed at
the legacy 0-row shell, which made every evidence count read back as 0).

2026-10-29 read-only remeasurement: the authoritative `kline_1d` ended at
2026-10-23 (53 codes on 10-21..10-23, 57 on 10-19..10-20, 5,570 on 10-16);
`stock_quotes` had 171 rows and was last updated 2026-10-18 10:42:26. The
`~/.aiask-local` snapshot is a fork, not a verified restore source; do not copy
its bars into this authoritative file without sampled source agreement and the
normal write gate. MCP `DataSyncScheduler` must default off in `tool-only`
startup to keep a nominally read-only service from writing this database.
Reproduce the measurements with a `sqlite3.connect(file_uri, uri=True)` URI
using `mode=ro`; do not import `akshare_mcp.server` for this audit.

## 11. TDX: marked unavailable (2026-10)

`pytdx` is installed and the `heartbeat=False` exit-hang bug is fixed (section
5), but the two live pool servers answer only administrative queries and serve
no market data — 12 servers probed, 12/12 supply zero bars. **Treat TDX as
unavailable and do not route a data need through it.** The ten dead pool
entries and dead-first ordering remain as-is.
