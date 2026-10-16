# Data sources

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
| `tushare` | core dependency | yes, 1.4.29 | 2 files | no token -> every call fails |
| `efinance` | `legacy` extra | yes, 0.5.8 | 2 files | `get_realtime_quotes` fails with JSONDecodeError (it uses `push2`) |
| `pytdx` | not declared | yes, 1.72 | 0 files directly | used through `tdx_local`; see section 5 |
| `akshare` | **not declared** | **no** | **was 26 files — now 0** | every reference was removed; see below |
| `baostock` | `legacy` extra | no | 8 files | same |
| `mootdx`, `tqcenter` | not declared | no | 1 file each | `tdx_tqcenter` returns `[]` for everything |

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
| `dragon_tiger.buyer_type` | not in the report | needs a different report |
| `dragon_tiger` codes | 6,433 codes, 898 outside today's universe | historical appearances including since-delisted companies; kept on purpose |
| Industry | 100% from East money | `stocks.sector` is `EM2016` level 2, `stocks.industry` level 1 — one taxonomy, two depths |
| 新三板 | East money reports cover about 24,800 securities | filtered to the `stocks` universe on write, so no table holds a different definition of "listed" |

## 7. What every one of these sources owes the caller

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

## 8. Verification snapshot (2026-10-16)

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
