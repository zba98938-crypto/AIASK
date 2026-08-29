"""数据源候选实测：扫一遍 ak / EM / tushare / efinance / baostock 看哪些
能在当前环境拿到关键缺口数据。

只看：是否网络通、是否要 token、返回是否有真值。每项最长 15 秒。
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from datetime import datetime, timedelta

def _aiask_root() -> Path:
    override = os.getenv("AIASK_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = Path(__file__).resolve()
    for parent in (current.parent, *current.parents):
        if (parent / "packages").is_dir() and (parent / "scripts").is_dir():
            return parent
    return Path.cwd()


def _aiask_db_path() -> str:
    override = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    if override:
        return str(Path(override).expanduser().resolve())
    return str(_aiask_root() / "data" / "db" / "akshare_mcp.sqlite3")

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

results = {}
_pool = ThreadPoolExecutor(max_workers=2)
_TIMEOUT = 12  # 每项硬上限 12 秒


def _trial(name, fn):
    started = time.time()
    try:
        future = _pool.submit(fn)
        v = future.result(timeout=_TIMEOUT)
        elapsed = round(time.time() - started, 2)
        if v is None:
            results[name] = {"ok": False, "elapsed": elapsed, "error": "返回 None"}
            print(f"  [{name:<46s}] NULL  ({elapsed}s)", flush=True)
            return
        if hasattr(v, "shape"):
            shape = list(v.shape)
            try:
                head = v.head(1).to_dict() if hasattr(v, "head") else str(v)[:200]
            except Exception:
                head = str(v)[:200]
            results[name] = {"ok": True, "elapsed": elapsed, "shape": shape,
                              "head": str(head)[:300]}
            print(f"  [{name:<46s}] OK   shape={shape}  ({elapsed}s)", flush=True)
        elif isinstance(v, list):
            results[name] = {"ok": True, "elapsed": elapsed, "len": len(v),
                              "first": (v[0] if v else None)}
            print(f"  [{name:<46s}] OK   len={len(v)}  ({elapsed}s)", flush=True)
        elif isinstance(v, dict):
            keys = list(v.keys())
            results[name] = {"ok": True, "elapsed": elapsed, "keys": keys[:8]}
            print(f"  [{name:<46s}] OK   keys={keys[:5]}  ({elapsed}s)", flush=True)
        else:
            results[name] = {"ok": True, "elapsed": elapsed, "value": str(v)[:200]}
            print(f"  [{name:<46s}] OK   value={v}  ({elapsed}s)", flush=True)
    except FTimeout:
        elapsed = round(time.time() - started, 2)
        results[name] = {"ok": False, "elapsed": elapsed,
                          "error": f"超时 (>{_TIMEOUT}s)"}
        print(f"  [{name:<46s}] TMOUT ({elapsed}s)", flush=True)
    except Exception as e:
        elapsed = round(time.time() - started, 2)
        msg = f"{type(e).__name__}: {e}"
        results[name] = {"ok": False, "elapsed": elapsed, "error": msg[:300]}
        print(f"  [{name:<46s}] FAIL ({elapsed}s) {msg[:80]}", flush=True)


# ========== AKShare ==========
print("\n=== AKShare 网络可达性测试 ===")
import akshare as ak
from pathlib import Path

# 1) 北向资金（沪深股通）
_trial("ak.stock_hsgt_hist_em",
       lambda: ak.stock_hsgt_hist_em(symbol="北向资金"))
_trial("ak.stock_hsgt_fund_flow_summary_em",
       lambda: ak.stock_hsgt_fund_flow_summary_em())
_trial("ak.stock_hsgt_north_acc_flow_in_em (deprecated check)",
       lambda: ak.stock_hsgt_north_acc_flow_in_em(symbol="北上"))

# 2) 北向单股持股
_trial("ak.stock_hsgt_individual_em",
       lambda: ak.stock_hsgt_individual_em(stock="600519"))

# 3) 龙虎榜
_trial("ak.stock_lhb_detail_daily_sina",
       lambda: ak.stock_lhb_detail_daily_sina(date="20260516"))
_trial("ak.stock_lhb_detail_em",
       lambda: ak.stock_lhb_detail_em(start_date="20260514", end_date="20260518"))

# 4) 融资融券
_trial("ak.stock_margin_account_info",
       lambda: ak.stock_margin_account_info())
_trial("ak.stock_margin_underlying_info_szse (深市)",
       lambda: ak.stock_margin_underlying_info_szse(date="20260516"))

# 5) 大宗交易
_trial("ak.stock_dzjy_mrtj",
       lambda: ak.stock_dzjy_mrtj(start_date="20260514", end_date="20260518"))

# 6) 涨停板池
_trial("ak.stock_zt_pool_em",
       lambda: ak.stock_zt_pool_em(date="20260516"))

# 7) 股东户数
_trial("ak.stock_zh_a_gdhs",
       lambda: ak.stock_zh_a_gdhs(symbol="20260331"))

# 8) 盘后资金（个股资金流）
_trial("ak.stock_individual_fund_flow",
       lambda: ak.stock_individual_fund_flow(stock="600519", market="sh"))

# 9) 板块资金流
_trial("ak.stock_sector_fund_flow_rank",
       lambda: ak.stock_sector_fund_flow_rank(indicator="今日"))

# 10) 公告
_trial("ak.stock_notice_report",
       lambda: ak.stock_notice_report(symbol="全部", date="20260518"))

# 11) 研报
_trial("ak.stock_research_report_em",
       lambda: ak.stock_research_report_em(symbol="600519"))

# 12) 财务历史（季度）
_trial("ak.stock_financial_abstract_ths",
       lambda: ak.stock_financial_abstract_ths(symbol="600519", indicator="按报告期"))
_trial("ak.stock_financial_abstract",
       lambda: ak.stock_financial_abstract(symbol="600519"))
_trial("ak.stock_financial_analysis_indicator",
       lambda: ak.stock_financial_analysis_indicator(symbol="600519"))

# 13) 业绩预告 / 业绩快报
_trial("ak.stock_yjbb_em (业绩快报)",
       lambda: ak.stock_yjbb_em(date="20251231"))

# 14) 个股新闻
_trial("ak.stock_news_em",
       lambda: ak.stock_news_em(symbol="600519"))

# ========== Tushare ==========
print("\n=== Tushare（取决于 TUSHARE_TOKEN）===")
try:
    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    print(f"  TUSHARE_TOKEN 配置: {'有' if token else '无'}")
    if token:
        ts.set_token(token)
        pro = ts.pro_api()
        _trial("ts.pro.daily 600519",
               lambda: pro.daily(ts_code="600519.SH", start_date="20260101", end_date="20260518"))
        _trial("ts.pro.daily_basic 600519 PE/PB 历史",
               lambda: pro.daily_basic(ts_code="600519.SH", start_date="20260101", end_date="20260518",
                                        fields="trade_date,pe_ttm,pb,total_mv"))
        _trial("ts.pro.fina_indicator 600519 多季",
               lambda: pro.fina_indicator(ts_code="600519.SH",
                                           start_date="20240101", end_date="20260518"))
        _trial("ts.pro.moneyflow_hsgt (北向)",
               lambda: pro.moneyflow_hsgt(start_date="20260514", end_date="20260518"))
        _trial("ts.pro.top_list 龙虎榜",
               lambda: pro.top_list(trade_date="20260516"))
        _trial("ts.pro.margin 融资融券",
               lambda: pro.margin(trade_date="20260516"))
        _trial("ts.pro.block_trade 大宗",
               lambda: pro.block_trade(trade_date="20260516"))
        _trial("ts.pro.anns 公告",
               lambda: pro.anns(start_date="20260516", end_date="20260518"))
    else:
        print("  跳过 (无 token)")
except Exception as e:
    print(f"  Tushare init failed: {e}")

# ========== eFinance（无需 token）==========
print("\n=== eFinance ===")
try:
    import efinance as ef
    _trial("ef.stock.get_quote_history 600519",
           lambda: ef.stock.get_quote_history("600519"))
    _trial("ef.stock.get_latest_quote",
           lambda: ef.stock.get_latest_quote(["600519"]))
    _trial("ef.stock.get_belong_board",
           lambda: ef.stock.get_belong_board("600519"))
    _trial("ef.fund.get_fund_codes (ETF)",
           lambda: ef.fund.get_fund_codes())
except Exception as e:
    print(f"  ef trial failed: {e}")

# ========== Baostock ==========
print("\n=== Baostock ===")
try:
    import baostock as bs
    bs.login(user_id="anonymous", password="123456")
    rs = bs.query_history_k_data_plus(
        "sh.600519",
        "date,open,close,turn,peTTM,pbMRQ",
        start_date="2026-04-01", end_date="2026-05-18", frequency="d", adjustflag="3",
    )
    n = 0
    while (rs.error_code == "0") and rs.next():
        n += 1
    print(f"  bs.query_history_k_data_plus 600519 -> {n} rows")
    results["bs.query_history_k_data_plus"] = {"ok": rs.error_code == "0", "rows": n}
    bs.logout()
except Exception as e:
    print(f"  baostock failed: {e}")

# ========== EM datacenter (push2 / dataapi) 直连 ==========
print("\n=== 东财直连 (push2 / dataapi) ===")
import requests
def _em_call(url, params, timeout=8):
    r = requests.get(url, params=params, timeout=timeout,
                     headers={"User-Agent": "Mozilla/5.0", "Referer": "http://data.eastmoney.com/"})
    r.raise_for_status()
    return r.json()

_trial("em.push2.fflow.kline (个股资金流)",
       lambda: _em_call("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
                        {"secid": "1.600519", "klt": 101, "fields1": "f1,f2",
                         "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62", "lmt": 5}))

_trial("em.dataapi RPT_MUTUAL_DEAL_HISTORY (北向)",
       lambda: _em_call("https://datacenter-web.eastmoney.com/api/data/v1/get",
                        {"sortColumns": "TRADE_DATE", "sortTypes": -1,
                         "pageSize": 5, "pageNumber": 1,
                         "reportName": "RPT_MUTUAL_DEAL_HISTORY",
                         "columns": "TRADE_DATE,NORTH_DAILY_NET,SOUTH_DAILY_NET,NORTH_TOTAL_NET"}))

_trial("em.dataapi RPTA_WEB_RZRQ_GGMX (融资融券)",
       lambda: _em_call("https://datacenter-web.eastmoney.com/api/data/v1/get",
                        {"sortColumns": "TRADE_DATE", "sortTypes": -1,
                         "pageSize": 5, "pageNumber": 1,
                         "reportName": "RPTA_WEB_RZRQ_GGMX",
                         "columns": "TRADE_DATE,SCODE,SECURITY_NAME_ABBR,RZYE,RQYE"}))

_trial("em.dataapi RPT_DAILY_BILLBOARD (龙虎榜)",
       lambda: _em_call("https://datacenter-web.eastmoney.com/api/data/v1/get",
                        {"sortColumns": "TRADE_DATE", "sortTypes": -1,
                         "pageSize": 5, "pageNumber": 1,
                         "reportName": "RPT_BILLBOARD_DAILYDETAILS",
                         "columns": "TRADE_DATE,SECURITY_CODE,SECURITY_NAME_ABBR,RANK_REASON_TYPE"}))

# ============================================================
# 输出汇总
print("\n=== 总结 ===")
ok = sum(1 for v in results.values() if v.get("ok"))
fail = sum(1 for v in results.values() if not v.get("ok"))
print(f"OK: {ok}  FAIL: {fail}  TOTAL: {len(results)}")

# 写到文件
out = "scripts/tdx_probe/datasource_probe.json"
with open(str(_aiask_root() / out), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2, default=str)
print(f"\n详细结果: {out}")
