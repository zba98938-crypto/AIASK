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
| `akshare` | **not declared** | **no** | **27 files** | 27 modules take their `except ImportError` branch |
| `baostock` | `legacy` extra | no | 8 files | same |
| `mootdx`, `tqcenter` | not declared | no | 1 file each | `tdx_tqcenter` returns `[]` for everything |

`akshare` is the sharp edge: it is not a declared dependency, so installing the
project never provided it, yet 27 modules import it defensively. Every one of
those paths has been degrading silently since the tree was extracted. **Nothing in
this document relies on it.** If those call sites are wanted, either install it
(`pip install akshare`) or delete them; leaving them is the option that keeps
producing "no data" with no explanation.

## 3. What the project needs, and where it comes from

| Category | Source | Endpoint / report | Coverage now | Refresh cost |
|---|---|---|---|---|
| Stock listing | Sina Market Center | `Market_Center.getHQNodeData?node=hs_a` | 5,575 listed, 70 pages | ~45 s |
| Daily klines | Sina | `CN_MarketData.getKLineData?scale=240` | 1,733,871 rows, 5,582 codes, 100% | ~11 min at concurrency 4 |
| Index klines | Sina | same, `sh000001` / `sz399300` / … | 5 indices | seconds |
| Industry, sector | East money F10 | `RPT_F10_BASIC_ORGINFO` (`EM2016`) | 5,577 / 5,577 = 100% | ~14 s |
| Market cap, PE, PB | Sina listing | same response as the listing | 5,582 / 5,575 | free with the listing |
| Financials | East money datacenter | `RPT_LICO_FN_CPD` | 119,819 rows, 5,575 codes, 2021-03 -> 2026-09 | ~4 min |
| Industry board membership | East money datacenter | `RPT_LICO_FN_CPD` (`BOARD_CODE`) | 5,575 members / 128 boards | free with financials |
| Dragon-tiger 龙虎榜 | East money datacenter | `RPT_DAILYBILLBOARD_DETAILSNEW` | 269,446 rows, 2004-06 -> 2026-10-15 | ~2 min |
| Fund flow 个股资金流 | East money datacenter | `RPT_DMSK_TS_STOCKNEW` | 5,200 rows, **one day only** | ~5 s |
| Trading calendar | derived from `kline_1d` | — | 1,399 dates, 2021-01-13 -> 2026-10-15 | free |
| Announcements, notices | project's own ingest | `run_market_event_ingest.py --once` | 283 documents, 253 events | seconds |
| Minute klines | Sina | `scale=5/15/30/60` | **available, not wired** | — |
| Balance-sheet ratios | East money datacenter | `RPT_DMSK_FN_BALANCE` | **available, not wired** | — |
| Dividends | East money datacenter | `RPT_SHAREBONUS_DET` | **available, not wired** | — |
| Margin 融资融券 | SSE only | `query.sse.com.cn/marketdata/tradedata/queryMargin.do` | **Shanghai only**; 深交所 is 403 | — |

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

**TDX online (`tdx_local`, `pytdx`).** The socket layer is fine and the project's
client still cannot use it. Only **2 of the 12 servers** in
`tdx_local.DEFAULT_TDX_HQ_SERVERS` answer a protocol handshake
(`180.153.18.170`, `60.191.117.167`); the other ten hang for the full timeout, and
the pool's **first** entry `119.147.212.81` is one of the dead ones. Naming a
responsive server first via `TDX_SERVER_POOL` did **not** make the client return
within 300 s, so the hang is not only server ordering. Until that is diagnosed,
treat TDX as unavailable and do not route a data need through it.

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
| Margin 融资融券 | 上交所 answers; 深交所 returns 403 through the proxy | needs a Shenzhen route; the SSE half is worth wiring on its own |
| `stock_fund_flow` | the report is a one-day snapshot, no history | call it daily; it does not backfill |
| `financials.net_margin`, `debt_ratio`, `current_ratio`, `roa` | the earnings report has no balance-sheet fields | wire `RPT_DMSK_FN_BALANCE`; they are NULL rather than 0.0 today |
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

## 8. Verification snapshot (2026-10-15)

```bash
python scripts/aiask_readiness.py --strict                # ready: True, 0 critical
python scripts/check_silent_exception_swallows.py         # 0 silent handlers
python scripts/sync_market_universe.py --with-industry    # coverage + source
python scripts/sync_market_fundamentals.py                # per-category report
```

| Table | Rows | Codes | Range |
|---|---:|---:|---|
| `kline_1d` | 1,733,871 | 5,582 | 2021-01-13 -> 2026-10-15 |
| `stocks` | 5,582 | 5,582 | industry + sector 100% |
| `financials` | 119,819 | 5,575 | 2021-03-31 -> 2026-09-30 |
| `dragon_tiger` | 269,446 | 6,433 | 2004-06-25 -> 2026-10-15 |
| `block_stocks` | 5,575 | 5,575 | 128 industry boards |
| `trading_dates` | 1,399 | — | 2021-01-13 -> 2026-10-15 |
| `stock_fund_flow` | 5,200 | 5,200 | 2026-10-15 only |

`market_blocks` also holds 80 rows of `block_type='industry_legacy'`: a 2026-07-16
snapshot from akshare sector-fund-flow that has no members in `block_stocks`, and
43 of whose names duplicate a live board. They are labelled rather than deleted,
so `block_type='industry'` returns exactly the 128 boards that have members.
