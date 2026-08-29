"""TDX 数据缺口针对性探针(2026-05-25)

目标:验证诊断报告 §2/§4.1 列出的 4 个 P0 数据缺口能否从 TDX/tqcenter 拿到,
然后再决定数据库迁移和回填策略。

测试矩阵(对应诊断报告章节):

    §2.1  北向资金日频净流入 + 个股持股 → tqcenter GP06? scjy SC? bkjy BK?
    §2.2  上证指数 sh000001 收盘点位 → get_kline 999999.SH / get_realtime_quote
    §4.1.3 龙虎榜每日明细 → tqcenter has?
    §4.1.4 融资融券市场总额 → tqcenter scjy?
    §4.1.5 stock_quotes PE/PB/市值 → 财务字段 J_jyl/J_mgsy 等
    §4.5.4 行业分类 → get_sector_list / get_stock_list_in_sector
    §2.3  strategy_factory 信号 — TDX 能否提供 PE_TTM/PB/ROE 给因子计算

每项探针:
- 调用 tqcenter API(若已封装)+ 直接尝试 raw tq.* 调用(若没封装)
- 记录返回结构 / 字段实际值(非 placeholder "--")
- 输出 verdict: ✅可用 / ⚠️部分可用 / ❌不可用
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 把 akshare-mcp 的 src 加到 path
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "packages", "akshare-mcp", "src"))

from akshare_mcp.data_source import tdx_tqcenter as tdx  # noqa: E402

results: dict[str, dict] = {}


def _log(name: str, verdict: str, payload: dict) -> None:
    results[name] = {"verdict": verdict, **payload}
    print(f"  [{name:<55s}] {verdict}", flush=True)


def _has_real_value(value: object) -> bool:
    """判断字段值是否为 placeholder '--' / 空 / 0"""
    if value is None:
        return False
    if isinstance(value, str):
        s = value.strip()
        if s in ("", "--", "0", "0.0", "0.00", "null", "None"):
            return False
        return True
    if isinstance(value, (int, float)):
        return abs(float(value)) > 1e-9
    if isinstance(value, list):
        return any(_has_real_value(v) for v in value)
    if isinstance(value, dict):
        return any(_has_real_value(v) for v in value.values())
    return True


def _count_real(values: list) -> tuple[int, int]:
    """list 中非 placeholder 计数"""
    real = sum(1 for v in values if _has_real_value(v))
    return real, len(values)


# ---------------------------------------------------------------------------
# 0. tqcenter 连接性(必须先通)
# ---------------------------------------------------------------------------
print("\n=== [0] tqcenter 连接性测试 ===")
try:
    tq = tdx.get_tq()
    if tq is None:
        _log("tqcenter.connect", "❌ FAIL", {"error": "get_tq() 返回 None,后续测试无法进行"})
        print("\n[FATAL] tqcenter 不可用,中止后续测试")
        with open(os.path.join(os.path.dirname(__file__), "tdx_data_gaps_result.json"),
                  "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        sys.exit(1)
    else:
        _log("tqcenter.connect", "✅ OK", {"tq_type": str(type(tq).__name__)})
except Exception as e:
    _log("tqcenter.connect", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})
    sys.exit(1)


# ---------------------------------------------------------------------------
# 1. P0-§2.2 上证指数 sh000001 — 关键!之前 close=10.68 错位
# ---------------------------------------------------------------------------
print("\n=== [1] §2.2 上证指数 sh000001 (close 应在 1500-7000) ===")

# 1a. K 线
try:
    klines = tdx.get_kline("999999.SH", period="1d", limit=5)
    if klines and len(klines) > 0:
        latest = klines[-1] if isinstance(klines, list) else None
        if latest:
            close = latest.get("close") or latest.get("Close")
            if close and 1500 <= float(close) <= 7000:
                _log("kline_999999_close", "✅ OK", {
                    "latest_close": float(close),
                    "latest_date": str(latest.get("date") or latest.get("Date") or ""),
                    "rows": len(klines),
                    "sample_keys": list(latest.keys())[:10],
                })
            else:
                _log("kline_999999_close", "⚠️ INVALID_RANGE", {
                    "latest_close": close,
                    "expected_range": "1500-7000",
                })
        else:
            _log("kline_999999_close", "⚠️ EMPTY_LATEST", {"rows": len(klines)})
    else:
        _log("kline_999999_close", "❌ NO_DATA", {"rows": 0})
except Exception as e:
    _log("kline_999999_close", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})

# 1b. 实时行情
try:
    quote = tdx.get_realtime_quote("999999.SH")
    if quote:
        price = (quote.get("LastPrice") or quote.get("ClosePx")
                 or quote.get("close") or quote.get("price"))
        if price and 1500 <= float(price) <= 7000:
            _log("realtime_999999", "✅ OK", {
                "price": float(price),
                "field_count": len(quote),
                "sample_keys": list(quote.keys())[:10],
            })
        else:
            _log("realtime_999999", "⚠️ INVALID_RANGE", {"price": price})
    else:
        _log("realtime_999999", "❌ NO_DATA", {})
except Exception as e:
    _log("realtime_999999", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# 2. P0-§2.1 北向资金 — tqcenter 是否提供
# ---------------------------------------------------------------------------
print("\n=== [2] §2.1 北向资金净流入(SC 字段尝试) ===")

# 2a. SC 市场字段(可能含北向)
try:
    today = datetime.now().strftime("%Y%m%d")
    last_week = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    sc_data = tdx.get_scjy_value(
        fields=["SC01", "SC02", "SC03", "SC04", "SC05", "SC25", "SC36"],
        start_time=last_week,
        end_time=today,
    )
    if sc_data and isinstance(sc_data, dict):
        non_empty = {}
        for field, vals in sc_data.items():
            if isinstance(vals, list):
                real, total = _count_real(vals)
                if real > 0:
                    non_empty[field] = f"{real}/{total} real"
        if non_empty:
            _log("scjy_north_fund", "⚠️ PARTIAL", {
                "fields": list(sc_data.keys()),
                "non_empty_fields": non_empty,
                "note": "SC 市场字段无明确北向定义,需逐字段语义识别",
            })
        else:
            _log("scjy_north_fund", "❌ ALL_PLACEHOLDER", {"fields": list(sc_data.keys())})
    else:
        _log("scjy_north_fund", "❌ NO_DATA", {})
except Exception as e:
    _log("scjy_north_fund", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})

# 2b. 个股北向持股(GP06 已知字段)
try:
    today = datetime.now().strftime("%Y%m%d")
    last_week = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
    gp_data = tdx.get_gpjy_value(
        codes=["600519.SH", "000001.SZ"],
        fields=["GP06"],  # 陆股通持股量
        start_time=last_week,
        end_time=today,
    )
    if gp_data:
        any_real = False
        sample = {}
        for code, fields in gp_data.items():
            if isinstance(fields, dict):
                for field, vals in fields.items():
                    if isinstance(vals, list):
                        real, total = _count_real(vals)
                        sample[f"{code}.{field}"] = f"{real}/{total}"
                        if real > 0:
                            any_real = True
        if any_real:
            _log("gpjy_GP06_north_holding", "✅ OK", {"sample": sample})
        else:
            _log("gpjy_GP06_north_holding", "❌ ALL_PLACEHOLDER", {"sample": sample})
    else:
        _log("gpjy_GP06_north_holding", "❌ NO_DATA", {})
except Exception as e:
    _log("gpjy_GP06_north_holding", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# 3. P2-§4.1.3 龙虎榜
# ---------------------------------------------------------------------------
print("\n=== [3] §4.1.3 龙虎榜每日明细 ===")
# tqcenter 没显式 API,看 GP 字段是否含
try:
    today = datetime.now().strftime("%Y%m%d")
    last_week = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    # 尝试常见 GP 字段(GP31/GP32 是龙虎榜?)
    gp_data = tdx.get_gpjy_value(
        codes=["600519.SH"],
        fields=["GP31", "GP32"],  # 经验上龙虎榜在这附近
        start_time=last_week,
        end_time=today,
    )
    if gp_data:
        sample = {}
        any_real = False
        for code, fields in gp_data.items():
            if isinstance(fields, dict):
                for field, vals in fields.items():
                    if isinstance(vals, list):
                        real, total = _count_real(vals)
                        sample[f"{code}.{field}"] = f"{real}/{total}"
                        if real > 0:
                            any_real = True
        if any_real:
            _log("gpjy_dragon_tiger", "✅ OK", {"sample": sample, "note": "需进一步确认字段语义"})
        else:
            _log("gpjy_dragon_tiger", "❌ ALL_PLACEHOLDER",
                 {"sample": sample, "note": "tqcenter 不直接提供龙虎榜,需走 EM/Tushare"})
    else:
        _log("gpjy_dragon_tiger", "❌ NO_DATA", {})
except Exception as e:
    _log("gpjy_dragon_tiger", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# 4. P2-§4.1.4 融资融券市场总额
# ---------------------------------------------------------------------------
print("\n=== [4] §4.1.4 融资融券(SC 市场字段) ===")
try:
    today = datetime.now().strftime("%Y%m%d")
    last_week = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    # SC25 / SC36 之前 probe 已知有数据
    sc_data = tdx.get_scjy_value(
        fields=["SC25", "SC36"],
        start_time=last_week,
        end_time=today,
    )
    if sc_data:
        sample = {}
        any_real = False
        for field, vals in sc_data.items():
            if isinstance(vals, list):
                real, total = _count_real(vals)
                sample[field] = {
                    "real_count": real,
                    "total": total,
                    "first_real": next((v for v in vals if _has_real_value(v)), None),
                }
                if real > 0:
                    any_real = True
        if any_real:
            _log("scjy_margin_total", "✅ OK", {"sample": sample, "note": "需进一步确认 SC25/SC36 字段语义"})
        else:
            _log("scjy_margin_total", "❌ ALL_PLACEHOLDER", {"sample": sample})
    else:
        _log("scjy_margin_total", "❌ NO_DATA", {})
except Exception as e:
    _log("scjy_margin_total", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# 5. P2-§4.1.1 db.stock_quotes 8.14% coverage — TDX 实时行情批量
# ---------------------------------------------------------------------------
print("\n=== [5] §4.1.1 全市场实时行情(market_snapshot) ===")
try:
    # tqcenter 提供 get_market_snapshot,看一次返回多少股票
    if hasattr(tq, "get_market_snapshot"):
        # 尝试调用前 50 个 HS300 成分
        stock_list = tdx.get_stock_list(market="23", list_type=1)
        if isinstance(stock_list, list) and len(stock_list) > 0:
            # 取前 50 测速
            sample_codes = [s.get("Code") for s in stock_list[:50] if s.get("Code")]
            t_start = time.time()
            snapshot = tq.get_market_snapshot(stock_list=sample_codes[:50])
            elapsed = round(time.time() - t_start, 2)
            if snapshot and isinstance(snapshot, dict):
                with_price = sum(
                    1 for code, data in snapshot.items()
                    if isinstance(data, dict)
                    and _has_real_value(data.get("LastPrice") or data.get("ClosePx") or data.get("price"))
                )
                _log("market_snapshot_50", "✅ OK", {
                    "requested": len(sample_codes[:50]),
                    "returned": len(snapshot),
                    "with_real_price": with_price,
                    "elapsed_sec": elapsed,
                    "throughput_qps": round(len(snapshot) / elapsed, 1) if elapsed > 0 else "inf",
                })
            else:
                _log("market_snapshot_50", "❌ NO_DATA", {"elapsed_sec": elapsed})
        else:
            _log("market_snapshot_50", "❌ NO_HS300_LIST", {})
    else:
        _log("market_snapshot_50", "❌ API_MISSING", {"note": "tqcenter 无 get_market_snapshot"})
except Exception as e:
    _log("market_snapshot_50", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# 6. P0-§2.3 strategy_factory zero_signal — TDX 能否提供因子计算所需字段
# ---------------------------------------------------------------------------
print("\n=== [6] §2.3 因子计算所需字段 (PE/PB/ROE/EPS) ===")
try:
    # get_stock_info 已知含 J_jyl(ROE) / J_mgsy(EPS) 等
    info = tdx.get_stock_info("600519.SH")
    if info and isinstance(info, dict):
        fin_fields = {
            "J_yysy(营收)": info.get("J_yysy"),
            "J_jly(净利润)": info.get("J_jly"),
            "J_jyl(ROE)": info.get("J_jyl"),
            "J_mgsy(EPS)": info.get("J_mgsy"),
            "J_mgjzc(BVPS)": info.get("J_mgjzc"),
            "EPS_TTM": info.get("EPS_TTM"),
            "PE_TTM": info.get("PE_TTM"),
            "PB": info.get("PB"),
            "TotalShares": info.get("TotalShares"),
        }
        real_fields = {k: v for k, v in fin_fields.items() if _has_real_value(v)}
        if len(real_fields) >= 3:
            _log("stock_info_fundamentals", "✅ OK", {
                "real_fields": real_fields,
                "missing_fields": [k for k, v in fin_fields.items() if not _has_real_value(v)],
                "total_keys": len(info),
            })
        else:
            _log("stock_info_fundamentals", "⚠️ PARTIAL", {
                "real_fields": real_fields,
                "missing_count": len(fin_fields) - len(real_fields),
            })
    else:
        _log("stock_info_fundamentals", "❌ NO_DATA", {})
except Exception as e:
    _log("stock_info_fundamentals", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})


# 6b. 专业财务数据 FN 字段
try:
    today = datetime.now().strftime("%Y%m%d")
    last_year = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
    fn_data = tdx.get_financial_data(
        codes=["600519.SH"],
        fields=["FN1", "FN6", "FN197", "FN210"],  # 一些常见利润表字段
        start_time=last_year,
        end_time=today,
    )
    if fn_data:
        sample = {}
        any_real = False
        for code, fields in fn_data.items():
            if isinstance(fields, dict):
                for field, val in fields.items():
                    sample[f"{code}.{field}"] = str(val)[:50]
                    if _has_real_value(val):
                        any_real = True
        if any_real:
            _log("financial_data_FN", "✅ OK", {"sample": sample})
        else:
            _log("financial_data_FN", "❌ ALL_PLACEHOLDER", {"sample": sample})
    else:
        _log("financial_data_FN", "❌ NO_DATA", {})
except Exception as e:
    _log("financial_data_FN", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# 7. P2-§4.5.4 行业分类 / 板块成分
# ---------------------------------------------------------------------------
print("\n=== [7] §4.5.4 行业分类与板块成分 ===")
try:
    sectors = tdx.get_sector_list(list_type=1)
    if isinstance(sectors, list) and len(sectors) > 100:
        _log("sector_list", "✅ OK", {
            "count": len(sectors),
            "first_3": sectors[:3],
            "note": "11_default_industry=127 / 12_concept=269 / 14_region=32",
        })
    else:
        _log("sector_list", "⚠️ FEW", {"count": len(sectors) if sectors else 0})
except Exception as e:
    _log("sector_list", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})

try:
    # 取一个行业板块的成分股
    members = tdx.get_stock_list_in_sector("881002.SH", block_type=0, list_type=1)
    if isinstance(members, list) and len(members) > 0:
        _log("stock_list_in_sector", "✅ OK", {
            "block_code": "881002.SH(煤炭开采)",
            "members": len(members),
            "first_3": [m.get("Code") for m in members[:3] if isinstance(m, dict)],
        })
    else:
        _log("stock_list_in_sector", "❌ EMPTY", {})
except Exception as e:
    _log("stock_list_in_sector", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# 8. P2-§5.4 分钟级 K 线
# ---------------------------------------------------------------------------
print("\n=== [8] §5.4 分钟 K 线 ===")
try:
    minute_klines = tdx.get_kline("600519.SH", period="5m", limit=20)
    if isinstance(minute_klines, list) and len(minute_klines) > 0:
        _log("kline_5min", "✅ OK", {
            "rows": len(minute_klines),
            "first_keys": list(minute_klines[0].keys())[:8] if isinstance(minute_klines[0], dict) else [],
            "latest_close": (minute_klines[-1] or {}).get("close")
                if isinstance(minute_klines[-1], dict) else None,
        })
    else:
        _log("kline_5min", "❌ EMPTY", {})
except Exception as e:
    _log("kline_5min", "❌ FAIL", {"error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# 9. 关键指数(深成指 399001 / 创业板 399006 / 沪深300 000300)
# ---------------------------------------------------------------------------
print("\n=== [9] 关键指数 K 线 ===")
for idx_code, idx_name, expected_range in [
    ("399001.SZ", "深证成指", (5000, 18000)),
    ("399006.SZ", "创业板指", (1000, 4000)),
    ("000300.SH", "沪深300", (2500, 6000)),
    ("000016.SH", "上证50", (1500, 5000)),
]:
    try:
        klines = tdx.get_kline(idx_code, period="1d", limit=3)
        if klines and len(klines) > 0:
            close = (klines[-1] or {}).get("close")
            if close and expected_range[0] <= float(close) <= expected_range[1]:
                _log(f"kline_{idx_code}", "✅ OK", {
                    "name": idx_name,
                    "close": float(close),
                    "expected_range": expected_range,
                })
            else:
                _log(f"kline_{idx_code}", "⚠️ INVALID_RANGE", {
                    "name": idx_name,
                    "close": close,
                    "expected_range": expected_range,
                })
        else:
            _log(f"kline_{idx_code}", "❌ NO_DATA", {"name": idx_name})
    except Exception as e:
        _log(f"kline_{idx_code}", "❌ FAIL", {"name": idx_name, "error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# 输出 + 总结
# ---------------------------------------------------------------------------
ok_count = sum(1 for v in results.values() if v["verdict"].startswith("✅"))
partial_count = sum(1 for v in results.values() if v["verdict"].startswith("⚠️"))
fail_count = sum(1 for v in results.values() if v["verdict"].startswith("❌"))
total = len(results)

print(f"\n=== 总结 ===")
print(f"✅ OK:      {ok_count}/{total}")
print(f"⚠️ Partial: {partial_count}/{total}")
print(f"❌ Fail:    {fail_count}/{total}")

# 写文件
out_path = os.path.join(os.path.dirname(__file__), "tdx_data_gaps_result.json")
summary = {
    "generated_at": datetime.now().isoformat(),
    "total_probes": total,
    "ok_count": ok_count,
    "partial_count": partial_count,
    "fail_count": fail_count,
    "results": results,
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

print(f"\n详细结果: {out_path}")
