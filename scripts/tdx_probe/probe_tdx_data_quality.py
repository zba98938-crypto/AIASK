"""
Deep data-quality probe for the local Tongdaxin/TdxQuant client.

Goal: classify what the client can *actually* return right now:
- real_data: non-empty and contains information-bearing values
- zero_only: data shape exists but values are all zero-like
- placeholder_only: only '--'/empty/None placeholders
- empty: empty list/dict/DataFrame
- error_payload: structured error payload
- not_implemented: documented/expected API missing from local SDK
- skipped_side_effect: intentionally not executed

The probe is read-only except for download_file checks, which download vendor data
files into the TDX PYPlugins/data directory.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).resolve().parent
RESULT_PATH = OUT_DIR / "data_quality_result.json"
REPORT_PATH = OUT_DIR / "data_quality_report.md"
SDK_PATH = Path(os.environ.get("TDX_TQCENTER_PATH", r"C:\new_tdx_test\PYPlugins\sys\tqcenter.py"))
TDX_PYPLUGINS = str(SDK_PATH.parent)
if TDX_PYPLUGINS not in sys.path:
    sys.path.insert(0, TDX_PYPLUGINS)

STOCKS = ["600519.SH", "000001.SZ", "688318.SH", "920000.BJ"]
INDEXES = ["999999.SH", "399001.SZ", "000300.SH", "399006.SZ"]
FUNDS = ["159001.SZ", "510300.SH", "588000.SH"]
BLOCKS = ["881001.SH", "880081.SH", "880501.SH"]
KZZ_FALLBACK = ["123054.SZ", "123059.SZ", "113001.SH"]

SIDE_EFFECT_APIS = [
    "order_stock",
    "cancel_order_stock",
    "create_sector",
    "delete_sector",
    "rename_sector",
    "clear_sector",
    "send_user_block",
    "send_message",
    "send_warn",
    "send_file",
    "send_bt_data",
    "print_to_tdx",
    "exec_to_tdx",
    "refresh_cache",
    "refresh_kline",
    "subscribe_hq",
    "unsubscribe_hq",
]


def _is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return stripped == "" or stripped == "--" or stripped.lower() in {"nan", "none", "null"}
    return False


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if stripped in {"", "--"}:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _walk_scalars(value: Any, depth: int = 0):
    if depth > 8:
        return
    if value is None or isinstance(value, (str, int, float, bool)):
        yield value
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"ErrorId", "run_id"}:
                continue
            yield from _walk_scalars(child, depth + 1)
        return
    if isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _walk_scalars(child, depth + 1)
        return
    if hasattr(value, "to_numpy"):
        try:
            arr = value.to_numpy().ravel()
            for item in arr:
                yield item.item() if hasattr(item, "item") else item
            return
        except Exception:
            pass
    if hasattr(value, "__iter__") and not isinstance(value, (bytes, bytearray)):
        try:
            for child in value:
                yield from _walk_scalars(child, depth + 1)
            return
        except Exception:
            pass
    yield str(value)


def _shape(value: Any) -> dict[str, Any]:
    info: dict[str, Any] = {"type": type(value).__name__}
    try:
        info["len"] = len(value) if hasattr(value, "__len__") else None
    except Exception:
        info["len"] = None
    if isinstance(value, dict):
        info["keys"] = [str(k) for k in list(value.keys())[:80]]
        if value and all(hasattr(v, "shape") for v in value.values()):
            fields = {}
            for key, child in value.items():
                try:
                    fields[str(key)] = {
                        "shape": list(child.shape),
                        "index_first": str(child.index[0]) if len(child.index) else None,
                        "index_last": str(child.index[-1]) if len(child.index) else None,
                        "columns": [str(c) for c in list(child.columns)[:20]],
                    }
                except Exception:
                    pass
            info["dataframe_fields"] = fields
    elif isinstance(value, list):
        info["first"] = _sample(value[:2])
    elif hasattr(value, "shape"):
        try:
            info["shape"] = list(value.shape)
            info["columns"] = [str(c) for c in list(getattr(value, "columns", []))[:40]]
            info["index_first"] = str(value.index[0]) if len(value.index) else None
            info["index_last"] = str(value.index[-1]) if len(value.index) else None
        except Exception:
            pass
    return info


def _sample(value: Any, depth: int = 0, max_items: int = 4, max_keys: int = 30) -> Any:
    if depth > 5:
        return "<truncated>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        out = {}
        for key, child in list(value.items())[:max_keys]:
            out[str(key)] = _sample(child, depth + 1, max_items, max_keys)
        if len(value) > max_keys:
            out["__more_keys__"] = len(value) - max_keys
        return out
    if isinstance(value, (list, tuple)):
        out = [_sample(child, depth + 1, max_items, max_keys) for child in list(value)[:max_items]]
        if len(value) > max_items:
            out.append(f"<+{len(value) - max_items} more>")
        return out
    if hasattr(value, "head"):
        try:
            return {
                "__type__": type(value).__name__,
                "__shape__": list(value.shape),
                "__head__": value.head(3).to_string()[:1000],
            }
        except Exception:
            pass
    return str(value)[:500]


def classify_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        if "error" in value and "msg" in value:
            return {"quality": "error_payload", "reason": str(value.get("msg"))}
        if value.get("ErrorId") not in (None, "0", 0):
            return {"quality": "error_payload", "reason": str(value)}
    if value is None:
        return {"quality": "empty", "reason": "None"}
    if hasattr(value, "empty"):
        try:
            if bool(value.empty):
                return {"quality": "empty", "reason": "empty DataFrame"}
        except Exception:
            pass
    try:
        if hasattr(value, "__len__") and len(value) == 0:
            return {"quality": "empty", "reason": "len == 0"}
    except Exception:
        pass

    scalars = list(_walk_scalars(value))
    relevant = [v for v in scalars if not _is_placeholder(v)]
    if not relevant:
        return {"quality": "placeholder_only", "reason": "all values are placeholders"}

    numbers = [_as_number(v) for v in relevant]
    numeric_values = [v for v in numbers if v is not None]
    non_numeric = [v for v, n in zip(relevant, numbers) if n is None]
    non_zero_numbers = [v for v in numeric_values if abs(v) > 1e-12]

    if not non_numeric and numeric_values and not non_zero_numbers:
        return {
            "quality": "zero_only",
            "reason": "all non-placeholder values are numeric zero",
            "scalar_count": len(scalars),
            "non_placeholder_count": len(relevant),
        }

    return {
        "quality": "real_data",
        "reason": "contains non-placeholder and information-bearing values",
        "scalar_count": len(scalars),
        "non_placeholder_count": len(relevant),
        "non_zero_numeric_count": len(non_zero_numbers),
        "non_numeric_count": len(non_numeric),
    }


def field_quality(value: Any, *, top_key: str | None = None) -> dict[str, Any]:
    if top_key and isinstance(value, dict):
        value = value.get(top_key)
    if not isinstance(value, dict):
        return {"total_fields": 0, "by_quality": {}, "fields": {}}
    fields = {}
    by_quality = defaultdict(list)
    for key, child in value.items():
        if key == "ErrorId":
            continue
        quality = classify_value(child)
        fields[str(key)] = quality
        by_quality[quality["quality"]].append(str(key))
    return {
        "total_fields": len(fields),
        "by_quality": {quality: sorted(names, key=_field_sort_key) for quality, names in by_quality.items()},
        "fields": fields,
    }


def _field_sort_key(name: str) -> tuple[str, int]:
    match = re.match(r"([A-Za-z]+)(\d+)", name)
    if not match:
        return (name, -1)
    return (match.group(1), int(match.group(2)))


def run_case(cases: list[dict[str, Any]], category: str, name: str, fn: Callable[[], Any], *, redacted: bool = False) -> Any:
    print(f"\n=== {category}/{name} ===", flush=True)
    started = time.time()
    try:
        value = fn()
        quality = classify_value(value)
        entry = {
            "category": category,
            "name": name,
            "status": "executed",
            "ok": True,
            "elapsed_sec": round(time.time() - started, 3),
            "quality": quality["quality"],
            "quality_reason": quality["reason"],
            "quality_detail": quality,
            "shape": _shape(value),
            "sample": "<redacted>" if redacted else _sample(value),
        }
        cases.append(entry)
        print(f"OK quality={entry['quality']} type={type(value).__name__}", flush=True)
        return value
    except Exception as exc:
        entry = {
            "category": category,
            "name": name,
            "status": "executed",
            "ok": False,
            "elapsed_sec": round(time.time() - started, 3),
            "quality": "exception",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=5),
        }
        cases.append(entry)
        print(f"FAIL {entry['error']}", flush=True)
        return None


def add_not_run(cases: list[dict[str, Any]], category: str, name: str, status: str, reason: str) -> None:
    cases.append({
        "category": category,
        "name": name,
        "status": status,
        "ok": None,
        "quality": status,
        "reason": reason,
    })


def main() -> None:
    from tqcenter import tq

    print(f"[tdx-quality] workspace = {ROOT}")
    print(f"[tdx-quality] sdk       = {SDK_PATH}")

    cases: list[dict[str, Any]] = []
    started_at = datetime.now().isoformat()

    run_case(cases, "system", "initialize", lambda: tq.initialize(__file__))
    run_case(cases, "calendar", "trading_dates_SH_2024_to_now_recent50", lambda: tq.get_trading_dates(market="SH", start_time="20240101", end_time="", count=50))

    market_codes = {
        "user_watchlist_0": "0",
        "positions_1": "1",
        "all_a_5": "5",
        "sh_components_6": "6",
        "sh_main_7": "7",
        "sz_main_8": "8",
        "key_indexes_9": "9",
        "all_blocks_10": "10",
        "default_industry_11": "11",
        "concept_blocks_12": "12",
        "style_blocks_13": "13",
        "region_blocks_14": "14",
        "industry_concept_15": "15",
        "research_l1_16": "16",
        "research_l2_17": "17",
        "research_l3_18": "18",
        "with_h_21": "21",
        "with_convertible_bond_22": "22",
        "hs300_23": "23",
        "zz500_24": "24",
        "zz1000_25": "25",
        "gz2000_26": "26",
        "zz2000_27": "27",
        "a500_28": "28",
        "reits_30": "30",
        "etf_31": "31",
        "convertible_bond_32": "32",
        "lof_33": "33",
        "tradeable_fund_34": "34",
        "hs_fund_35": "35",
        "t0_fund_36": "36",
        "financial_enterprise_49": "49",
        "hs_a_50": "50",
        "chinext_51": "51",
        "star_52": "52",
        "bj_53": "53",
        "etf_tracked_index_91": "91",
        "futures_main_92": "92",
        "domestic_futures_101": "101",
        "hk_102": "102",
        "us_103": "103",
    }
    lists: dict[str, Any] = {}
    for label, code in market_codes.items():
        value = run_case(cases, "stock_lists", label, lambda code=code: tq.get_stock_list(code, list_type=1))
        lists[label] = {
            "market": code,
            "quality": cases[-1]["quality"],
            "count": len(value) if isinstance(value, list) else None,
            "sample": _sample(value[:3]) if isinstance(value, list) else _sample(value),
        }

    sectors = run_case(cases, "sector", "sector_list_all", lambda: tq.get_sector_list(list_type=1))
    first_sector = "881001.SH"
    if isinstance(sectors, list) and sectors:
        for row in sectors:
            code = row.get("Code") if isinstance(row, dict) else row
            if str(code).startswith("881"):
                first_sector = str(code)
                break
    run_case(cases, "sector", f"sector_members_{first_sector}", lambda: tq.get_stock_list_in_sector(first_sector))
    run_case(cases, "watchlist", "user_sector_list", lambda: tq.get_user_sector())
    run_case(cases, "subscription", "current_subscribe_list", lambda: tq.get_subscribe_hq_stock_list())

    kline_targets = {
        "stock_600519": "600519.SH",
        "stock_000001": "000001.SZ",
        "bj_920000": "920000.BJ",
        "index_999999": "999999.SH",
        "index_399001": "399001.SZ",
        "etf_510300": "510300.SH",
        "bond_123054": "123054.SZ",
        "sector_881001": "881001.SH",
    }
    periods = ["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mon", "1q", "1y", "tick"]
    for target_label, code in kline_targets.items():
        for period in periods:
            dividend = "front" if period in {"1d", "1w", "1mon", "1q", "1y"} else "none"
            run_case(
                cases,
                "kline",
                f"{target_label}_{period}",
                lambda code=code, period=period, dividend=dividend: tq.get_market_data(
                    field_list=[],
                    stock_list=[code],
                    period=period,
                    start_time="",
                    end_time="",
                    count=20,
                    dividend_type=dividend,
                    fill_data=True,
                ),
            )

    run_case(cases, "kline", "multi_stock_daily_ohlcv", lambda: tq.get_market_data(field_list=["Open", "High", "Low", "Close", "Volume", "Amount"], stock_list=STOCKS[:3], period="1d", start_time="", end_time="", count=20, dividend_type="front", fill_data=True))
    run_case(cases, "kline", "multi_index_daily_ohlcv", lambda: tq.get_market_data(field_list=["Open", "High", "Low", "Close", "Volume", "Amount"], stock_list=INDEXES[:3], period="1d", start_time="", end_time="", count=20, dividend_type="none", fill_data=True))

    for code in STOCKS + INDEXES[:2] + FUNDS[:2] + [first_sector]:
        run_case(cases, "snapshot", f"snapshot_{code}", lambda code=code: tq.get_market_snapshot(stock_code=code, field_list=[]))
        run_case(cases, "more_info", f"more_info_{code}", lambda code=code: tq.get_more_info(stock_code=code, field_list=[]))

    for code in STOCKS + FUNDS[:2] + [first_sector]:
        run_case(cases, "stock_info", f"stock_info_{code}", lambda code=code: tq.get_stock_info(stock_code=code, field_list=[]))

    for code in ["600519.SH", "000001.SZ", "688318.SH", "920000.BJ", "510300.SH", "123054.SZ"]:
        run_case(cases, "corporate_action", f"divid_{code}", lambda code=code: tq.get_divid_factors(stock_code=code, start_time="20180101", end_time="20261231"))
    run_case(cases, "corporate_action", "gb_info_600519_dates", lambda: tq.get_gb_info(stock_code="600519.SH", date_list=["20240101", "20250520", "20260520"], count=3))
    run_case(cases, "corporate_action", "ipo_today_after", lambda: tq.get_ipo_info(ipo_type=2, ipo_date=1))
    for code in STOCKS:
        run_case(cases, "relation", f"relation_{code}", lambda code=code: tq.get_relation(stock_code=code))

    fn_fields = [f"FN{i}" for i in range(1, 585)]
    gp_fields = [f"GP{i:02d}" for i in range(1, 47)]
    bk_fields = [f"BK{i}" for i in range(5, 20)]
    sc_fields = [f"SC{i:02d}" for i in range(1, 43)]
    go_fields = [f"GO{i}" for i in range(1, 48)]

    fn_by_date = run_case(cases, "financial_fields", "FN_by_date_600519_all", lambda: tq.get_financial_data_by_date(stock_list=["600519.SH"], field_list=fn_fields, year=0, mmdd=0))
    fn_history = run_case(cases, "financial_fields", "FN_history_600519_all", lambda: tq.get_financial_data(stock_list=["600519.SH"], field_list=fn_fields, start_time="20240101", end_time="", report_type="announce_time"))
    go = run_case(cases, "financial_fields", "GO_600519_all", lambda: tq.get_gp_one_data(stock_list=["600519.SH"], field_list=go_fields))
    gp_hist = run_case(cases, "financial_fields", "GP_history_600519_all", lambda: tq.get_gpjy_value(stock_list=["600519.SH"], field_list=gp_fields, start_time="20240101", end_time=""))
    gp_date = run_case(cases, "financial_fields", "GP_by_date_600519_all", lambda: tq.get_gpjy_value_by_date(stock_list=["600519.SH"], field_list=gp_fields, year=0, mmdd=0))
    bk_hist = run_case(cases, "financial_fields", f"BK_history_{first_sector}_all", lambda: tq.get_bkjy_value(stock_list=[first_sector], field_list=bk_fields, start_time="20240101", end_time=""))
    bk_date = run_case(cases, "financial_fields", f"BK_by_date_{first_sector}_all", lambda: tq.get_bkjy_value_by_date(stock_list=[first_sector], field_list=bk_fields, year=0, mmdd=0))
    sc_hist = run_case(cases, "financial_fields", "SC_history_all", lambda: tq.get_scjy_value(field_list=sc_fields, start_time="20240101", end_time=""))
    sc_date = run_case(cases, "financial_fields", "SC_by_date_all", lambda: tq.get_scjy_value_by_date(field_list=sc_fields, year=0, mmdd=0))

    kzz_list = run_case(cases, "bond_etf", "convertible_bond_list_for_info", lambda: tq.get_stock_list("32", list_type=1))
    kzz_codes = []
    if isinstance(kzz_list, list):
        for row in kzz_list[:5]:
            kzz_codes.append(row.get("Code") if isinstance(row, dict) else str(row))
    kzz_codes = kzz_codes or KZZ_FALLBACK
    for code in kzz_codes[:5]:
        run_case(cases, "bond_etf", f"kzz_info_{code}", lambda code=code: tq.get_kzz_info(stock_code=code, field_list=[]))
    for code in ["000300.SH", "000016.SH", "000905.SH", "000852.SH", "881001.SH", "950162.CSI"]:
        run_case(cases, "bond_etf", f"trackzs_etf_{code}", lambda code=code: tq.get_trackzs_etf_info(zs_code=code))

    for code in ["280002.HG", "280001.HG", "880001.HG"]:
        run_case(cases, "macro", f"macro_kline_{code}", lambda code=code: tq.get_market_data(field_list=[], stock_list=[code], period="1mon", start_time="20200101", end_time="", count=60, dividend_type="none", fill_data=True))

    run_case(cases, "download_file", "top10_holder_600519_20241231", lambda: tq.download_file(stock_code="600519.SH", down_time="20241231", down_type=1))
    run_case(cases, "download_file", "latest_news_600519", lambda: tq.download_file(stock_code="600519.SH", down_time="", down_type=3))
    run_case(cases, "download_file", "stock_overview_600519", lambda: tq.download_file(stock_code="600519.SH", down_time="", down_type=4))

    run_case(cases, "formula", "formula_set_data_info_600519", lambda: tq.formula_set_data_info(stock_code="600519.SH", stock_period="1d", count=60, dividend_type=1))
    run_case(cases, "formula", "formula_get_data", lambda: tq.formula_get_data())
    run_case(cases, "formula", "formula_zb_MACD", lambda: tq.formula_zb(formula_name="MACD", formula_arg="12,26,9", xsflag=6))
    run_case(cases, "formula", "formula_zb_CCI", lambda: tq.formula_zb(formula_name="CCI", formula_arg="12", xsflag=6))
    run_case(cases, "formula", "formula_xg_UPN", lambda: tq.formula_xg(formula_name="UPN", formula_arg="3"))
    run_case(cases, "formula", "formula_exp_CCI", lambda: tq.formula_exp(formula_name="CCI", formula_arg="12"))
    run_case(cases, "formula", "formula_process_mul_zb_MACD", lambda: tq.formula_process_mul_zb(formula_name="MACD", formula_arg="12,26,9", return_count=3, return_date=True, stock_list=STOCKS[:3], stock_period="1d", count=60, dividend_type=1))
    run_case(cases, "formula", "formula_process_mul_xg_UPN", lambda: tq.formula_process_mul_xg(formula_name="UPN", formula_arg="3", return_count=3, return_date=True, stock_list=STOCKS[:3], stock_period="1d", count=60, dividend_type=1))

    account = os.environ.get("TDX_PROBE_ACCOUNT", "").strip()
    if account:
        account_id = run_case(cases, "trading_read", "stock_account_env", lambda: tq.stock_account(account=account, account_type=os.environ.get("TDX_PROBE_ACCOUNT_TYPE", "STOCK")), redacted=True)
        if isinstance(account_id, int) and account_id >= 0:
            run_case(cases, "trading_read", "query_stock_asset", lambda: tq.query_stock_asset(account_id=account_id), redacted=True)
            run_case(cases, "trading_read", "query_stock_orders", lambda: tq.query_stock_orders(account_id=account_id), redacted=True)
            run_case(cases, "trading_read", "query_stock_positions", lambda: tq.query_stock_positions(account_id=account_id), redacted=True)
    else:
        add_not_run(cases, "trading_read", "stock_account/query_stock_asset/query_stock_orders/query_stock_positions", "skipped_missing_env", "Set TDX_PROBE_ACCOUNT to test account read APIs; not run to avoid exposing account data.")

    for name in SIDE_EFFECT_APIS:
        add_not_run(cases, "side_effect_api", name, "skipped_side_effect", "Not executed during data-source quality test.")

    for name in ["get_full_tick", "get_real_time_data", "get_report_data", "get_gb_info_by_date", "get_benchmark_data", "get_valid_stock_codes"]:
        add_not_run(cases, "documented_only", name, "not_implemented", "Mentioned in docs/examples but absent from local tqcenter.py.")

    try:
        tq.close()
    except Exception:
        pass

    derived = build_derived(cases, {
        "FN_by_date": field_quality(fn_by_date, top_key="600519.SH"),
        "FN_history": field_quality(fn_history, top_key="600519.SH"),
        "GO": field_quality(go, top_key="600519.SH"),
        "GP_history": field_quality(gp_hist, top_key="600519.SH"),
        "GP_by_date": field_quality(gp_date, top_key="600519.SH"),
        "BK_history": field_quality(bk_hist, top_key=first_sector),
        "BK_by_date": field_quality(bk_date, top_key=first_sector),
        "SC_history": field_quality(sc_hist),
        "SC_by_date": field_quality(sc_date),
    }, lists)

    result = {
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "workspace": str(ROOT),
        "sdk_path": str(SDK_PATH),
        "cases": cases,
        "derived": derived,
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_report(result)
    print(f"\n[tdx-quality] result = {RESULT_PATH}")
    print(f"[tdx-quality] report = {REPORT_PATH}")
    print(f"[tdx-quality] executed = {sum(1 for c in cases if c['status'] == 'executed')}")


def build_derived(cases: list[dict[str, Any]], field_details: dict[str, Any], lists: dict[str, Any]) -> dict[str, Any]:
    by_category_quality: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    real_data = []
    empty_like = []
    error_like = []
    not_run = []
    for case in cases:
        by_category_quality[case["category"]][case["quality"]] += 1
        label = f"{case['category']}/{case['name']}"
        if case["quality"] == "real_data":
            real_data.append(label)
        elif case["quality"] in {"empty", "placeholder_only", "zero_only"}:
            empty_like.append(label)
        elif case["quality"] in {"error_payload", "exception"}:
            error_like.append(label)
        elif case["status"] != "executed":
            not_run.append(label)
    return {
        "by_category_quality": {cat: dict(counts) for cat, counts in by_category_quality.items()},
        "real_data_cases": real_data,
        "empty_or_placeholder_cases": empty_like,
        "error_cases": error_like,
        "not_run_cases": not_run,
        "field_details": field_details,
        "stock_list_summary": lists,
    }


def write_report(result: dict[str, Any]) -> None:
    cases = result["cases"]
    derived = result["derived"]
    executed = [c for c in cases if c["status"] == "executed"]
    lines = [
        "# TDX Data Quality Report",
        "",
        f"- Generated: {result['finished_at']}",
        f"- Executed probes: {len(executed)}",
        f"- Real-data probes: {len(derived['real_data_cases'])}",
        f"- Empty/placeholder/zero probes: {len(derived['empty_or_placeholder_cases'])}",
        f"- Error payload/exception probes: {len(derived['error_cases'])}",
        f"- Not-run probes: {len(derived['not_run_cases'])}",
        "",
        "## Quality By Category",
        "",
    ]
    for category, counts in sorted(derived["by_category_quality"].items()):
        lines.append(f"- `{category}`: {json.dumps(counts, ensure_ascii=False)}")

    lines.extend(["", "## Real Data Cases", ""])
    for label in derived["real_data_cases"]:
        lines.append(f"- `{label}`")

    lines.extend(["", "## Empty / Placeholder / Zero Cases", ""])
    for label in derived["empty_or_placeholder_cases"]:
        case = next(c for c in cases if f"{c['category']}/{c['name']}" == label)
        lines.append(f"- `{label}`: {case['quality']} ({case.get('quality_reason', case.get('reason', ''))})")

    lines.extend(["", "## Error Cases", ""])
    if derived["error_cases"]:
        for label in derived["error_cases"]:
            case = next(c for c in cases if f"{c['category']}/{c['name']}" == label)
            lines.append(f"- `{label}`: {case['quality']} ({case.get('quality_reason', case.get('error', ''))})")
    else:
        lines.append("- None")

    lines.extend(["", "## Field-Level Quality", ""])
    for name, info in derived["field_details"].items():
        lines.append(f"### {name}")
        for quality, fields in sorted(info.get("by_quality", {}).items()):
            lines.append(f"- `{quality}` ({len(fields)}): {fields[:80]}")
        if not info.get("by_quality"):
            lines.append("- No fields returned.")

    lines.extend(["", "## Stock List Summary", ""])
    for name, info in derived["stock_list_summary"].items():
        lines.append(f"- `{name}` market `{info['market']}`: quality={info['quality']}, count={info['count']}")

    lines.extend(["", "## Not Run", ""])
    for label in derived["not_run_cases"]:
        case = next(c for c in cases if f"{c['category']}/{c['name']}" == label)
        lines.append(f"- `{label}`: {case['quality']} ({case.get('reason', '')})")

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
