"""
TDX (tqcenter) 全能力探测脚本
=================================

目的：在真实通达信客户端上调用文档里所列的每一个 API，把"实际能拿到什么"落到 JSON 里，
作为后续把项目数据源切到 TDX 的事实依据。

运行方式（PowerShell 或 cmd）:
    F:\Python311\python.exe scripts\tdx_probe\probe_tdx_all.py

要求:
- 通达信客户端已启动并登录
- C:\new_tdx_test\PYPlugins\sys\tqcenter.py 已存在
- 已下载: 上证指数 999999 的盘后数据、专业财务数据包、股票数据包、ETF/可转债 数据包

输出:
- scripts\tdx_probe\result.json    每项的 ok/error/sample
- scripts\tdx_probe\result.log     完整 stdout
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# tqcenter 必须放在 sys.path 最前
TDX_PYPLUGINS = r"C:\new_tdx_test\PYPlugins\sys"
if TDX_PYPLUGINS not in sys.path:
    sys.path.insert(0, TDX_PYPLUGINS)

OUT_DIR = Path(__file__).resolve().parent
RESULT_PATH = OUT_DIR / "result.json"
LOG_PATH = OUT_DIR / "result.log"

results: dict = {}
log_lines: list = []


def _truncate(obj, depth=0, max_list=3, max_keys=20):
    """把返回值压成可读样本：列表只保留前 N 个，dict 只保留前 N 个 key。"""
    if depth > 6:
        return f"<truncated depth>"
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        keys = list(obj.keys())
        sliced = keys[:max_keys]
        out = {}
        for k in sliced:
            # 强制把 key 转成字符串，避免 pandas Timestamp 之类不可序列化
            try:
                key_str = str(k)
            except Exception:
                key_str = repr(k)
            out[key_str] = _truncate(obj[k], depth + 1, max_list, max_keys)
        if len(keys) > max_keys:
            out["__more_keys__"] = len(keys) - max_keys
        return out
    if isinstance(obj, (list, tuple)):
        out = [_truncate(x, depth + 1, max_list, max_keys) for x in obj[:max_list]]
        if len(obj) > max_list:
            out.append(f"<+{len(obj) - max_list} more>")
        return out
    # pandas / numpy 兜底
    try:
        import pandas as pd  # noqa
        if hasattr(obj, "shape") and hasattr(obj, "iloc"):
            shape = getattr(obj, "shape", None)
            try:
                head = obj.head(2).to_dict()
            except Exception:
                head = str(obj)[:300]
            return {"__type__": type(obj).__name__, "__shape__": shape, "__head__": head}
    except Exception:
        pass
    return f"<{type(obj).__name__}> {str(obj)[:200]}"


def section(name: str, fn):
    """运行一项测试并捕获结果。"""
    print(f"\n=== {name} ===")
    log_lines.append(f"\n=== {name} ===")
    started = time.time()
    try:
        value = fn()
        elapsed = round(time.time() - started, 3)
        sample = _truncate(value)
        results[name] = {
            "ok": True,
            "elapsed_sec": elapsed,
            "sample": sample,
            "raw_type": type(value).__name__,
            "raw_len": (len(value) if hasattr(value, "__len__") else None),
        }
        print(f"  OK  ({elapsed}s, type={type(value).__name__})")
        log_lines.append(f"  OK  ({elapsed}s, type={type(value).__name__})")
        log_lines.append(f"  sample: {json.dumps(sample, ensure_ascii=False)[:1500]}")
    except Exception as e:
        elapsed = round(time.time() - started, 3)
        tb = traceback.format_exc(limit=4)
        results[name] = {
            "ok": False,
            "elapsed_sec": elapsed,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
        }
        print(f"  FAIL  ({elapsed}s) {type(e).__name__}: {e}")
        log_lines.append(f"  FAIL  ({elapsed}s) {type(e).__name__}: {e}")
        log_lines.append(tb)


def main():
    print(f"[probe] tdx_pyplugins = {TDX_PYPLUGINS}")
    print(f"[probe] python       = {sys.version}")
    print(f"[probe] cwd          = {os.getcwd()}")
    log_lines.append(f"[probe] python = {sys.version}")
    log_lines.append(f"[probe] now    = {datetime.now().isoformat()}")

    # --- import + initialize ---
    try:
        from tqcenter import tq
        from tqcenter import tqconst
    except Exception as e:
        print(f"[FATAL] cannot import tqcenter: {e}")
        results["__import__"] = {"ok": False, "error": str(e)}
        with open(RESULT_PATH, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        return

    section("00_initialize", lambda: tq.initialize(__file__))

    # --- 通用 / 元数据 ---
    section("01_get_trading_dates_recent10",
            lambda: tq.get_trading_dates(market="SH", start_time="20240101", end_time="", count=10))

    # 股票列表（覆盖文档里 30+ market 编码里最常用的几个）
    section("02_get_stock_list_5_all_a", lambda: tq.get_stock_list("5", list_type=1))
    section("03_get_stock_list_23_hs300", lambda: tq.get_stock_list("23", list_type=1))
    section("04_get_stock_list_24_zz500", lambda: tq.get_stock_list("24", list_type=1))
    section("05_get_stock_list_25_zz1000", lambda: tq.get_stock_list("25", list_type=1))
    section("06_get_stock_list_28_a500", lambda: tq.get_stock_list("28", list_type=1))
    section("07_get_stock_list_31_etf", lambda: tq.get_stock_list("31", list_type=1))
    section("08_get_stock_list_32_kzz", lambda: tq.get_stock_list("32", list_type=1))
    section("09_get_stock_list_51_cyb", lambda: tq.get_stock_list("51", list_type=1))
    section("10_get_stock_list_52_kcb", lambda: tq.get_stock_list("52", list_type=1))
    section("11_get_stock_list_53_bj", lambda: tq.get_stock_list("53", list_type=1))
    section("12_get_stock_list_16_research_l1", lambda: tq.get_stock_list("16", list_type=1))

    # 板块
    section("20_get_sector_list", lambda: tq.get_sector_list(list_type=1))
    section("21_get_user_sector", lambda: tq.get_user_sector())
    # 拿一个行业板块代码再取成份股：先用 16（研究行业一级）的第一个
    def _block_stocks_one():
        block_list = tq.get_stock_list("16", list_type=1)
        if not block_list:
            return {"warning": "no block from get_stock_list('16')"}
        first_code = block_list[0].get("Code") if isinstance(block_list[0], dict) else block_list[0]
        return {"block": first_code, "members": tq.get_stock_list_in_sector(first_code)[:20]}
    section("22_get_stock_list_in_sector_industry_first", _block_stocks_one)

    # --- 行情 ---
    test_codes = ["600519.SH", "000001.SZ", "688318.SH"]
    section("30_get_market_data_daily_5",
            lambda: tq.get_market_data(field_list=[], stock_list=test_codes, period="1d",
                                       start_time="", end_time="", count=5,
                                       dividend_type="front", fill_data=True))

    section("31_get_market_data_5min_3",
            lambda: tq.get_market_data(field_list=[], stock_list=["600519.SH"], period="5m",
                                       start_time="", end_time="", count=3,
                                       dividend_type="none", fill_data=True))

    section("32_get_market_data_weekly_3",
            lambda: tq.get_market_data(field_list=[], stock_list=["600519.SH"], period="1w",
                                       start_time="", end_time="", count=3,
                                       dividend_type="front", fill_data=True))

    section("33_get_market_snapshot_one",
            lambda: tq.get_market_snapshot(stock_code="600519.SH", field_list=[]))

    section("34_get_more_info_one",
            lambda: tq.get_more_info(stock_code="600519.SH", field_list=[]))

    section("35_get_stock_info_one",
            lambda: tq.get_stock_info(stock_code="600519.SH", field_list=[]))

    section("36_get_relation_one",
            lambda: tq.get_relation(stock_code="600519.SH"))

    section("37_get_divid_factors",
            lambda: tq.get_divid_factors(stock_code="600519.SH",
                                         start_time="20200101", end_time="20251231"))

    section("38_get_ipo_info_today_and_after",
            lambda: tq.get_ipo_info(ipo_type=2, ipo_date=1))

    section("39_get_gb_info_two_dates",
            lambda: tq.get_gb_info(stock_code="600519.SH",
                                   date_list=["20240101", "20250601"], count=2))

    # --- 财务 ---
    section("40_get_financial_data_5fields",
            lambda: tq.get_financial_data(stock_list=["600519.SH"],
                                          field_list=["FN1", "FN6", "FN183", "FN197", "FN210"],
                                          start_time="20240101", end_time="",
                                          report_type="announce_time"))

    section("41_get_financial_data_by_date_latest",
            lambda: tq.get_financial_data_by_date(stock_list=["600519.SH"],
                                                   field_list=["FN1", "FN6", "FN197", "FN210"],
                                                   year=0, mmdd=0))

    section("42_get_gp_one_data_consensus",
            lambda: tq.get_gp_one_data(stock_list=["600519.SH"],
                                       field_list=["GO1", "GO2", "GO3", "GO5", "GO8", "GO11",
                                                   "GO20", "GO23", "GO29", "GO33", "GO35", "GO42"]))

    # --- 个股交易类（GP）：龙虎榜/融资融券/陆股通/大宗交易/涨跌停/转融券/股权质押/分红 ---
    section("50_get_gpjy_value_lhb_rzrq_hgt_dz",
            lambda: tq.get_gpjy_value(stock_list=["600519.SH"],
                                       field_list=["GP01", "GP02", "GP03", "GP04", "GP06", "GP07",
                                                   "GP08", "GP09", "GP11", "GP12", "GP13", "GP15",
                                                   "GP16", "GP21", "GP24", "GP31", "GP32"],
                                       start_time="20250101", end_time="20251231"))

    section("51_get_gpjy_value_by_date_latest",
            lambda: tq.get_gpjy_value_by_date(stock_list=["600519.SH"],
                                               field_list=["GP01", "GP03", "GP06", "GP16", "GP21"],
                                               year=0, mmdd=0))

    # --- 板块交易类（BK）：板块 PE/PB/PS、市值、涨跌停、融资融券、陆股通、股息率 ---
    # 用 get_sector_list 拿一个真实板块代码
    def _bk_test():
        sec = tq.get_sector_list(list_type=1)
        if not sec:
            return {"warning": "empty sector_list"}
        # 找一个 880xxx 的行业板块
        first = None
        for s in sec:
            code = s.get("Code") if isinstance(s, dict) else s
            if str(code).startswith("880"):
                first = code
                break
        if not first:
            first = sec[0].get("Code") if isinstance(sec[0], dict) else sec[0]
        return {
            "block": first,
            "data": tq.get_bkjy_value(stock_list=[first],
                                      field_list=["BK5", "BK6", "BK7", "BK8", "BK9", "BK10",
                                                  "BK11", "BK12", "BK15", "BK16", "BK18", "BK19"],
                                      start_time="20250101", end_time="20251231"),
        }
    section("60_get_bkjy_value_industry", _bk_test)

    section("61_get_bkjy_value_by_date_latest",
            lambda: tq.get_bkjy_value_by_date(stock_list=["880660.SH"],
                                               field_list=["BK5", "BK6", "BK10", "BK11", "BK16"],
                                               year=0, mmdd=0))

    # --- 市场交易类（SC）：北向、融资融券、龙虎榜、ETF、央行投放、新高新低… ---
    section("70_get_scjy_value_market_wide",
            lambda: tq.get_scjy_value(field_list=["SC01", "SC02", "SC03", "SC04", "SC11",
                                                  "SC15", "SC16", "SC17", "SC20", "SC23",
                                                  "SC27", "SC28", "SC30", "SC34", "SC38", "SC42"],
                                       start_time="20250101", end_time="20251231"))

    section("71_get_scjy_value_by_date_latest",
            lambda: tq.get_scjy_value_by_date(field_list=["SC01", "SC02", "SC03", "SC11", "SC20"],
                                               year=0, mmdd=0))

    # --- ETF / 可转债 ---
    section("80_get_kzz_info",
            lambda: tq.get_kzz_info(stock_code="123039.SZ", field_list=[]))

    section("81_get_trackzs_etf_info",
            lambda: tq.get_trackzs_etf_info(zs_code="000300.SH"))

    # --- 公式互通（不写入，只读公式） ---
    def _formula_zb():
        tq.formula_set_data_info(stock_code="600519.SH", stock_period="1d",
                                  count=20, dividend_type=1)
        return tq.formula_zb(formula_name="MACD", formula_arg="12,26,9", xsflag=6)
    section("90_formula_zb_macd", _formula_zb)

    section("91_formula_process_mul_zb_macd_3stocks",
            lambda: tq.formula_process_mul_zb(formula_name="MACD",
                                               formula_arg="12,26,9",
                                               return_count=1,
                                               return_date=False,
                                               stock_list=test_codes,
                                               stock_period="1d",
                                               count=30,
                                               dividend_type=1))

    # 写入最终结果
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))

    print(f"\n[probe] result -> {RESULT_PATH}")
    print(f"[probe] log    -> {LOG_PATH}")

    # 简短汇总
    ok = sum(1 for v in results.values() if isinstance(v, dict) and v.get("ok"))
    fail = sum(1 for v in results.values() if isinstance(v, dict) and v.get("ok") is False)
    print(f"[probe] summary: ok={ok}  fail={fail}  total={len(results)}")


if __name__ == "__main__":
    main()
