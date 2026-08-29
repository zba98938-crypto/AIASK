"""
TDX 二轮深探：聚焦上一轮失败 / 返回 -- 的接口，并把全字段枚举一遍。
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

TDX_PYPLUGINS = r"C:\new_tdx_test\PYPlugins\sys"
if TDX_PYPLUGINS not in sys.path:
    sys.path.insert(0, TDX_PYPLUGINS)

OUT_DIR = Path(__file__).resolve().parent
RESULT_PATH = OUT_DIR / "result_v2.json"

results: dict = {}


def _truncate(obj, depth=0, max_list=3, max_keys=20):
    if depth > 6:
        return "<truncated depth>"
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        keys = list(obj.keys())
        sliced = keys[:max_keys]
        out = {}
        for k in sliced:
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
    try:
        if hasattr(obj, "shape") and hasattr(obj, "iloc"):
            shape = getattr(obj, "shape", None)
            try:
                head = obj.head(2)
                head_repr = head.to_string()[:600]
            except Exception:
                head_repr = str(obj)[:300]
            return {"__df_shape__": str(shape), "__head_repr__": head_repr}
    except Exception:
        pass
    return f"<{type(obj).__name__}> {str(obj)[:200]}"


def section(name: str, fn):
    print(f"\n=== {name} ===")
    started = time.time()
    try:
        v = fn()
        results[name] = {
            "ok": True,
            "elapsed_sec": round(time.time() - started, 3),
            "raw_type": type(v).__name__,
            "raw_len": (len(v) if hasattr(v, "__len__") else None),
            "sample": _truncate(v),
        }
        print(f"  OK type={type(v).__name__} len={results[name]['raw_len']}")
    except Exception as e:
        results[name] = {
            "ok": False,
            "elapsed_sec": round(time.time() - started, 3),
            "error": f"{type(e).__name__}: {e}",
            "tb": traceback.format_exc(limit=4),
        }
        print(f"  FAIL {type(e).__name__}: {e}")


def _summarize_value_dict(value_dict):
    """把 {field: [...]} 或 {field: [{Date,Value:[...]}, ...]} 压成 sample。"""
    if not isinstance(value_dict, dict):
        return value_dict
    out = {}
    for k, v in list(value_dict.items())[:50]:
        if isinstance(v, list):
            real = [x for x in v if x not in ("--", None) and not (isinstance(x, str) and x.strip("-") == "")]
            out[k] = {
                "len": len(v),
                "non_empty_first": real[:2],
                "all_dashes": all(x == "--" or x is None for x in v) if v else None,
                "first": v[:2],
            }
        else:
            out[k] = v
    return out


def main():
    print(f"[v2] python = {sys.version}")
    print(f"[v2] now    = {datetime.now().isoformat()}")

    from tqcenter import tq

    section("00_initialize", lambda: tq.initialize(__file__))

    # 1. K线返回是 {field: DataFrame}，上一轮 truncate 把它变成了 None。这次拿真实形态。
    def _kline_daily():
        d = tq.get_market_data(field_list=[], stock_list=["600519.SH"], period="1d",
                               start_time="", end_time="", count=5,
                               dividend_type="front", fill_data=True)
        if d is None:
            return None
        # 把 DataFrame -> 文本，避免 Timestamp 当 key
        out = {"fields": list(d.keys())}
        for f, df in d.items():
            try:
                out[f] = {
                    "shape": list(getattr(df, "shape", [None, None])),
                    "head": df.head(3).to_string()[:400],
                }
            except Exception as e:
                out[f] = f"err:{e}"
        return out
    section("10_kline_daily_real", _kline_daily)

    # 2. 分红
    def _divid():
        d = tq.get_divid_factors(stock_code="600519.SH",
                                 start_time="20180101", end_time="20251231")
        if d is None:
            return None
        try:
            return {
                "shape": list(getattr(d, "shape", [None, None])),
                "columns": [str(c) for c in d.columns],
                "head": d.head(5).to_string()[:600],
                "tail": d.tail(2).to_string()[:300],
            }
        except Exception as e:
            return f"err:{e}"
    section("11_divid_factors_real", _divid)

    # 3. 财务：把"基础"和"专业"分别试。基础财务在 get_stock_info 里。
    section("20_stock_info_finance_fields",
            lambda: tq.get_stock_info(stock_code="000001.SZ", field_list=[]))

    # 专业财务用尽量靠后的真实日期；mmdd=331 取年报
    section("21_financial_data_history_2023",
            lambda: tq.get_financial_data(stock_list=["600519.SH"],
                                          field_list=["FN1", "FN6", "FN183", "FN197", "FN210", "FN230", "FN232"],
                                          start_time="20230101",
                                          end_time="20231231",
                                          report_type="announce_time"))

    section("22_financial_data_by_date_latest_q",
            lambda: tq.get_financial_data_by_date(
                stock_list=["600519.SH"],
                field_list=["FN1", "FN6", "FN197", "FN210", "FN230", "FN232"],
                year=2024, mmdd=1231))

    # 4. 个股交易：完整字段走一遍，2023 - 2025 历史范围
    GP_FIELDS = ["GP01", "GP02", "GP03", "GP04", "GP05", "GP06", "GP07", "GP08", "GP09",
                 "GP10", "GP11", "GP12", "GP13", "GP14", "GP15", "GP16", "GP17", "GP18",
                 "GP19", "GP20", "GP21", "GP22", "GP23", "GP24", "GP25", "GP26", "GP27",
                 "GP28", "GP29", "GP30", "GP31", "GP32", "GP33", "GP34", "GP35", "GP36",
                 "GP37", "GP38", "GP39", "GP40", "GP41", "GP42", "GP43", "GP44", "GP45", "GP46"]

    def _gp_full():
        d = tq.get_gpjy_value(stock_list=["600519.SH"],
                              field_list=GP_FIELDS,
                              start_time="20240101", end_time="20241231")
        if d is None:
            return None
        if not isinstance(d, dict):
            return d
        # 先看 600519 的字段集合
        first = d.get("600519.SH")
        if first is None:
            return {"raw_keys": list(d.keys()), "first_value": None}
        return _summarize_value_dict(first)
    section("30_gpjy_full_2024", _gp_full)

    # 5. 板块：sector 880xxx，2024 历史
    BK_FIELDS = ["BK5", "BK6", "BK7", "BK8", "BK9", "BK10", "BK11", "BK12", "BK13",
                 "BK14", "BK15", "BK16", "BK17", "BK18", "BK19"]
    section("40_bkjy_full_2024",
            lambda: _summarize_value_dict(
                tq.get_bkjy_value(stock_list=["880660.SH"],
                                  field_list=BK_FIELDS,
                                  start_time="20240101", end_time="20241231").get("880660.SH", {})
                if tq.get_bkjy_value(stock_list=["880660.SH"], field_list=BK_FIELDS,
                                      start_time="20240101", end_time="20241231") else {}
            ))

    # 6. 市场：full SC 字段，2024 历史
    SC_FIELDS = [f"SC{str(i).zfill(2)}" for i in range(1, 43)]
    def _sc_full():
        d = tq.get_scjy_value(field_list=SC_FIELDS,
                              start_time="20240101", end_time="20241231")
        if d is None:
            return None
        return _summarize_value_dict(d)
    section("50_scjy_full_2024", _sc_full)

    # 7. KZZ：用 ETF list 拿一个真实活跃的可转债代码
    def _kzz_active():
        kz_list = tq.get_stock_list("32", list_type=1)
        if not kz_list:
            return {"warning": "kzz list empty"}
        first = kz_list[0]
        code = first.get("Code") if isinstance(first, dict) else first
        info = tq.get_kzz_info(stock_code=code, field_list=[])
        return {"tested_code": code, "tested_name": first.get("Name") if isinstance(first, dict) else "", "info": info}
    section("60_kzz_info_active", _kzz_active)

    # 8. ETF info: trackzs_etf 用 HS300 指数代码
    def _etf_track():
        out = {}
        for zs in ["000300.SH", "000016.SH", "000905.SH", "000852.SH", "881001.SH", "950162.CSI"]:
            try:
                v = tq.get_trackzs_etf_info(zs_code=zs)
                out[zs] = {"type": type(v).__name__,
                           "len": (len(v) if hasattr(v, "__len__") else None),
                           "sample": _truncate(v)}
            except Exception as e:
                out[zs] = f"err:{e}"
        return out
    section("61_trackzs_etf_variants", _etf_track)

    # 9. GO codes：完整跑
    GO_FIELDS = [f"GO{i}" for i in range(1, 48)]
    section("70_gp_one_data_full",
            lambda: tq.get_gp_one_data(stock_list=["600519.SH"], field_list=GO_FIELDS))

    # 10. 让我们看看 download_file 的舆情/综合信息文件能不能拿到
    section("80_download_file_news",
            lambda: tq.download_file(stock_code="600519.SH", down_time="", down_type=3))

    section("81_download_file_overview",
            lambda: tq.download_file(stock_code="600519.SH", down_time="", down_type=4))

    # 11. 宏观 .HG —— 用 K 线接口
    section("90_macro_hg_cpi",
            lambda: tq.get_market_data(field_list=[], stock_list=["280002.HG"], period="1mon",
                                       start_time="20230101", end_time="20251231",
                                       count=-1, dividend_type="none", fill_data=True))

    # 写入
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)

    ok = sum(1 for v in results.values() if v.get("ok"))
    fail = sum(1 for v in results.values() if v.get("ok") is False)
    print(f"\n[v2] summary ok={ok} fail={fail} total={len(results)}")
    print(f"[v2] result -> {RESULT_PATH}")


if __name__ == "__main__":
    main()
