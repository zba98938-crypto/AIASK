"""
Documentation-driven TDX/TdxQuant capability probe.

This script scans every Markdown file under tdx_quant_docs, compares the documented
API surface with the local tqcenter.py implementation, and runs a broad read-only
probe against the local Tongdaxin client.

Safety:
- Does not place or cancel orders.
- Does not create/delete/rename/clear custom sectors.
- Does not send UI messages/warnings/files or call arbitrary exec_to_tdx URLs.
- Trading account queries are attempted only when TDX_PROBE_ACCOUNT is set; outputs
  are redacted to shape/keys/counts.
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = ROOT / "tdx_quant_docs"
OUT_DIR = Path(__file__).resolve().parent
RESULT_PATH = OUT_DIR / "docs_full_result.json"
REPORT_PATH = OUT_DIR / "docs_full_report.md"
SDK_PATH = Path(os.environ.get("TDX_TQCENTER_PATH", r"C:\new_tdx_test\PYPlugins\sys\tqcenter.py"))
TDX_PYPLUGINS = str(SDK_PATH.parent)

if TDX_PYPLUGINS not in sys.path:
    sys.path.insert(0, TDX_PYPLUGINS)

FUNC_PREFIXES = (
    "get_",
    "query_",
    "formula_",
    "download_",
    "refresh_",
    "subscribe",
    "unsubscribe",
    "send_",
    "print_",
    "exec_",
    "create_",
    "delete_",
    "clear_",
    "rename_",
    "order_",
    "cancel_",
    "stock_account",
)
SIDE_EFFECT_FUNCS = {
    "cancel_order_stock": "trading side effect",
    "clear_sector": "custom sector write",
    "create_sector": "custom sector write",
    "delete_sector": "custom sector write",
    "exec_to_tdx": "client command side effect",
    "order_stock": "trading side effect",
    "print_to_tdx": "client UI output side effect",
    "refresh_cache": "client cache/download side effect",
    "refresh_kline": "client cache/download side effect",
    "rename_sector": "custom sector write",
    "send_bt_data": "client UI output side effect",
    "send_file": "client UI output side effect",
    "send_message": "client UI output side effect",
    "send_user_block": "custom sector write",
    "send_warn": "client UI output side effect",
    "subscribe_hq": "real-time subscription side effect",
    "unsubscribe_hq": "subscription side effect",
}
TRADING_READ_FUNCS = {"stock_account", "query_stock_asset", "query_stock_orders", "query_stock_positions"}
IGNORE_NAMES = {"print", "dict", "list", "set", "len", "range", "int", "str", "float", "bool", "pd", "DataFrame"}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def scan_docs() -> dict[str, Any]:
    func_pat = re.compile(r"\b(?:tq\.)?([a-zA-Z_]\w*)\s*\(")
    code_sig_pat = re.compile(r"(?:def\s+)?([a-zA-Z_]\w*)\s*\([^\n`]*\)")
    field_pat = re.compile(r"\b(FN\d+|GP\d+|BK\d+|SC\d+|GO\d+)\b")

    md_files = sorted(DOCS_ROOT.rglob("*.md"))
    doc_funcs: dict[str, set[str]] = {}
    field_sets: dict[str, set[str]] = {"FN": set(), "GP": set(), "BK": set(), "SC": set(), "GO": set()}
    headings: list[dict[str, str]] = []
    read_errors: list[dict[str, str]] = []

    for path in md_files:
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        try:
            text = _read_text(path)
        except Exception as exc:
            read_errors.append({"path": rel, "error": str(exc)})
            continue

        first_heading = ""
        for line in text.splitlines():
            if line.startswith("#"):
                first_heading = line.lstrip("#").strip()
                break
        headings.append({"path": rel, "heading": first_heading})

        names = set()
        for match in func_pat.finditer(text):
            names.add(match.group(1))
        for match in code_sig_pat.finditer(text):
            names.add(match.group(1))
        for name in names:
            if name in IGNORE_NAMES or name.startswith("__"):
                continue
            doc_funcs.setdefault(name, set()).add(rel)

        for field in field_pat.findall(text):
            prefix = re.match(r"[A-Z]+", field)
            if prefix:
                field_sets[prefix.group(0)].add(field)

    sdk_methods = set()
    try:
        tree = ast.parse(SDK_PATH.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sdk_methods.add(node.name)
    except Exception as exc:
        read_errors.append({"path": str(SDK_PATH), "error": f"SDK parse failed: {exc}"})

    manual_funcs = sorted(
        name
        for name in doc_funcs
        if name == "initialize" or name.startswith(FUNC_PREFIXES)
    )
    implemented = sorted(name for name in manual_funcs if name in sdk_methods)
    not_implemented = sorted(name for name in manual_funcs if name not in sdk_methods)
    normalized_fields = {}
    for prefix, values in field_sets.items():
        normalized_set = set()
        for value in values:
            match = re.search(r"\d+", value)
            if not match:
                continue
            number = int(match.group(0))
            normalized_set.add(f"{prefix}{number:02d}" if prefix in {"GP", "SC"} else f"{prefix}{number}")
        normalized = sorted(normalized_set, key=lambda item: int(re.search(r"\d+", item).group(0)))
        normalized_fields[prefix] = normalized

    return {
        "markdown_count": len(md_files),
        "read_errors": read_errors,
        "headings": headings,
        "manual_api_candidate_count": len(manual_funcs),
        "manual_api_candidates": manual_funcs,
        "implemented_in_local_sdk": implemented,
        "documented_not_implemented_in_local_sdk": not_implemented,
        "doc_sources_by_function": {k: sorted(v) for k, v in sorted(doc_funcs.items())},
        "field_enums": normalized_fields,
        "sdk_path": str(SDK_PATH),
    }


def _json_safe(value: Any, *, depth: int = 0, max_items: int = 5, max_keys: int = 40) -> Any:
    if depth > 6:
        return "<truncated depth>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        out = {}
        items = list(value.items())
        for key, child in items[:max_keys]:
            out[str(key)] = _json_safe(child, depth=depth + 1, max_items=max_items, max_keys=max_keys)
        if len(items) > max_keys:
            out["__more_keys__"] = len(items) - max_keys
        return out
    if isinstance(value, (list, tuple)):
        out = [_json_safe(child, depth=depth + 1, max_items=max_items, max_keys=max_keys) for child in value[:max_items]]
        if len(value) > max_items:
            out.append(f"<+{len(value) - max_items} more>")
        return out
    if hasattr(value, "shape") and hasattr(value, "head"):
        try:
            head = value.head(3)
            return {
                "__type__": type(value).__name__,
                "__shape__": list(value.shape),
                "__columns__": [str(col) for col in getattr(value, "columns", [])[:max_keys]],
                "__index_first__": str(value.index[0]) if len(value.index) else None,
                "__index_last__": str(value.index[-1]) if len(value.index) else None,
                "__head__": head.to_string()[:1200],
            }
        except Exception:
            return f"<{type(value).__name__}> {str(value)[:400]}"
    return f"<{type(value).__name__}> {str(value)[:400]}"


def _shape_summary(value: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"type": type(value).__name__}
    try:
        summary["len"] = len(value) if hasattr(value, "__len__") else None
    except Exception:
        summary["len"] = None

    if isinstance(value, dict):
        summary["keys"] = [str(k) for k in list(value.keys())[:60]]
        if value and all(hasattr(v, "shape") for v in value.values()):
            summary["dataframe_fields"] = {}
            for key, child in list(value.items())[:30]:
                summary["dataframe_fields"][str(key)] = {
                    "shape": list(child.shape),
                    "index_first": str(child.index[0]) if len(child.index) else None,
                    "index_last": str(child.index[-1]) if len(child.index) else None,
                    "columns_sample": [str(col) for col in list(child.columns)[:8]],
                }
    elif isinstance(value, list):
        summary["first"] = _json_safe(value[:2])
    elif hasattr(value, "shape"):
        summary["shape"] = list(value.shape)
    return summary


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in {"", "--"}
    if isinstance(value, (int, float, bool)):
        return True
    if isinstance(value, dict):
        if "error" in value and "msg" in value:
            return False
        if value.get("ErrorId") not in (None, "0", 0):
            return False
        return any(_non_empty(v) for k, v in value.items() if k != "ErrorId")
    if isinstance(value, (list, tuple)):
        return any(_non_empty(v) for v in value)
    if hasattr(value, "empty"):
        try:
            return not bool(value.empty)
        except Exception:
            return True
    return True


def _run_case(results: list[dict[str, Any]], name: str, category: str, fn: Callable[[], Any], *, redacted: bool = False) -> Any:
    print(f"\n=== {category}/{name} ===", flush=True)
    started = time.time()
    try:
        value = fn()
        elapsed = round(time.time() - started, 3)
        entry = {
            "name": name,
            "category": category,
            "status": "executed",
            "ok": True,
            "elapsed_sec": elapsed,
            "value_present": _non_empty(value),
            "summary": _shape_summary(value),
            "sample": "<redacted>" if redacted else _json_safe(value),
        }
        results.append(entry)
        print(f"OK {elapsed}s type={type(value).__name__} present={entry['value_present']}", flush=True)
        return value
    except Exception as exc:
        elapsed = round(time.time() - started, 3)
        entry = {
            "name": name,
            "category": category,
            "status": "executed",
            "ok": False,
            "elapsed_sec": elapsed,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=5),
        }
        results.append(entry)
        print(f"FAIL {elapsed}s {type(exc).__name__}: {exc}", flush=True)
        return None


def _field_value_entries(data: Any, top_key: str | None = None) -> dict[str, Any]:
    if top_key and isinstance(data, dict):
        data = data.get(top_key) or {}
    if not isinstance(data, dict):
        return {"non_empty_count": 0, "fields": [], "sample": {}}

    non_empty: dict[str, Any] = {}
    for field, raw in data.items():
        if field == "ErrorId":
            continue
        if not _non_empty(raw):
            continue
        if isinstance(raw, list) and len(raw) == 2 and all(item == "--" for item in raw):
            continue
        non_empty[field] = _json_safe(raw, max_items=2)

    return {
        "non_empty_count": len(non_empty),
        "fields": sorted(non_empty.keys(), key=lambda item: int(re.search(r"\d+", item).group(0)) if re.search(r"\d+", item) else 0),
        "sample": dict(list(non_empty.items())[:20]),
    }


def _add_status_for_not_run(results: list[dict[str, Any]], name: str, category: str, status: str, reason: str, docs: list[str] | None = None) -> None:
    results.append({
        "name": name,
        "category": category,
        "status": status,
        "ok": None,
        "reason": reason,
        "docs": docs or [],
    })


def run_probe(doc_scan: dict[str, Any]) -> dict[str, Any]:
    from tqcenter import tq

    cases: list[dict[str, Any]] = []
    started_at = datetime.now().isoformat()

    _run_case(cases, "initialize", "general", lambda: tq.initialize(__file__))
    _run_case(cases, "get_trading_dates_SH_recent10", "general", lambda: tq.get_trading_dates(market="SH", start_time="20240101", end_time="", count=10))
    _run_case(cases, "get_subscribe_hq_stock_list", "general", lambda: tq.get_subscribe_hq_stock_list())

    stock_markets = {
        "0_user_watchlist": "0",
        "1_positions": "1",
        "5_all_a": "5",
        "6_sh_index_components": "6",
        "7_sh_main": "7",
        "8_sz_main": "8",
        "9_key_indexes": "9",
        "10_all_block_indexes": "10",
        "11_default_industry": "11",
        "12_concept_blocks": "12",
        "13_style_blocks": "13",
        "14_region_blocks": "14",
        "15_industry_and_concept": "15",
        "16_research_l1": "16",
        "17_research_l2": "17",
        "18_research_l3": "18",
        "21_with_h": "21",
        "22_with_cb": "22",
        "23_hs300": "23",
        "24_zz500": "24",
        "25_zz1000": "25",
        "26_guozheng2000": "26",
        "27_zz2000": "27",
        "28_zz_a500": "28",
        "30_reits": "30",
        "31_etf": "31",
        "32_convertible_bond": "32",
        "33_lof": "33",
        "34_tradeable_fund": "34",
        "35_hs_fund": "35",
        "36_t0_fund": "36",
        "49_financial_enterprise": "49",
        "50_hs_a": "50",
        "51_chinext": "51",
        "52_star": "52",
        "53_bj": "53",
        "91_etf_tracked_index": "91",
        "92_domestic_futures_main": "92",
        "101_domestic_futures": "101",
        "102_hk": "102",
        "103_us": "103",
    }
    stock_list_counts: dict[str, Any] = {}
    for label, market in stock_markets.items():
        rows = _run_case(cases, f"get_stock_list_{label}", "sector_constituents", lambda market=market: tq.get_stock_list(market, list_type=1))
        stock_list_counts[label] = {"market": market, "count": len(rows) if isinstance(rows, list) else None, "first": _json_safe(rows[:2]) if isinstance(rows, list) else _json_safe(rows)}

    sectors = _run_case(cases, "get_sector_list_list_type_1", "sector_constituents", lambda: tq.get_sector_list(list_type=1))
    first_sector = "881001.SH"
    if isinstance(sectors, list) and sectors:
        for item in sectors:
            code = item.get("Code") if isinstance(item, dict) else item
            if str(code).startswith("881"):
                first_sector = str(code)
                break
    _run_case(cases, f"get_stock_list_in_sector_{first_sector}", "sector_constituents", lambda: tq.get_stock_list_in_sector(first_sector))
    _run_case(cases, "get_user_sector", "watchlist_custom_sector", lambda: tq.get_user_sector())

    stock_codes = ["600519.SH", "000001.SZ", "688318.SH", "920000.BJ"]
    periods = ["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mon", "1q", "1y", "tick"]
    kline_periods: dict[str, Any] = {}
    for period in periods:
        dividend = "front" if period in {"1d", "1w", "1mon", "1q", "1y"} else "none"
        value = _run_case(
            cases,
            f"get_market_data_{period}",
            "market_data",
            lambda period=period, dividend=dividend: tq.get_market_data(
                field_list=[],
                stock_list=["600519.SH"],
                period=period,
                start_time="",
                end_time="",
                count=5,
                dividend_type=dividend,
                fill_data=True,
            ),
        )
        kline_periods[period] = _shape_summary(value)

    _run_case(cases, "get_market_data_multi_stock_daily", "market_data", lambda: tq.get_market_data(field_list=["Open", "High", "Low", "Close", "Volume", "Amount"], stock_list=stock_codes[:3], period="1d", start_time="", end_time="", count=5, dividend_type="front", fill_data=True))
    _run_case(cases, "get_market_snapshot_stock", "market_data", lambda: tq.get_market_snapshot(stock_code="600519.SH", field_list=[]))
    _run_case(cases, "get_market_snapshot_index", "market_data", lambda: tq.get_market_snapshot(stock_code="000001.SH", field_list=[]))
    _run_case(cases, "get_stock_info", "market_data", lambda: tq.get_stock_info(stock_code="600519.SH", field_list=[]))
    _run_case(cases, "get_more_info_stock", "market_data", lambda: tq.get_more_info(stock_code="600519.SH", field_list=[]))
    _run_case(cases, "get_more_info_sector", "market_data", lambda: tq.get_more_info(stock_code=first_sector, field_list=[]))
    _run_case(cases, "get_relation", "market_data", lambda: tq.get_relation(stock_code="600519.SH"))
    _run_case(cases, "get_divid_factors", "market_data", lambda: tq.get_divid_factors(stock_code="600519.SH", start_time="20180101", end_time="20261231"))
    _run_case(cases, "get_ipo_info_today_and_after", "market_data", lambda: tq.get_ipo_info(ipo_type=2, ipo_date=1))
    _run_case(cases, "get_gb_info_dates", "market_data", lambda: tq.get_gb_info(stock_code="600519.SH", date_list=["20240101", datetime.now().strftime("%Y%m%d")], count=2))
    _add_status_for_not_run(cases, "get_gb_info_by_date", "market_data", "not_implemented_in_sdk", "Documented in Markdown but local tqcenter.py has get_gb_info only.")

    fields = doc_scan["field_enums"]
    fn_fields = fields.get("FN", [])
    gp_fields = fields.get("GP", [])
    bk_fields = fields.get("BK", [])
    sc_fields = fields.get("SC", [])
    go_fields = fields.get("GO", [])

    fn_by_date = _run_case(cases, f"get_financial_data_by_date_all_FN_{len(fn_fields)}", "financial_data", lambda: tq.get_financial_data_by_date(stock_list=["600519.SH"], field_list=fn_fields, year=0, mmdd=0))
    fn_history = _run_case(cases, f"get_financial_data_all_FN_{len(fn_fields)}", "financial_data", lambda: tq.get_financial_data(stock_list=["600519.SH"], field_list=fn_fields, start_time="20240101", end_time="", report_type="announce_time"))
    go_value = _run_case(cases, f"get_gp_one_data_all_GO_{len(go_fields)}", "financial_data", lambda: tq.get_gp_one_data(stock_list=["600519.SH"], field_list=go_fields))
    gp_history = _run_case(cases, f"get_gpjy_value_all_GP_{len(gp_fields)}", "financial_data", lambda: tq.get_gpjy_value(stock_list=["600519.SH"], field_list=gp_fields, start_time="20240101", end_time=""))
    gp_by_date = _run_case(cases, f"get_gpjy_value_by_date_all_GP_{len(gp_fields)}", "financial_data", lambda: tq.get_gpjy_value_by_date(stock_list=["600519.SH"], field_list=gp_fields, year=0, mmdd=0))
    bk_history = _run_case(cases, f"get_bkjy_value_all_BK_{len(bk_fields)}", "financial_data", lambda: tq.get_bkjy_value(stock_list=[first_sector], field_list=bk_fields, start_time="20240101", end_time=""))
    bk_by_date = _run_case(cases, f"get_bkjy_value_by_date_all_BK_{len(bk_fields)}", "financial_data", lambda: tq.get_bkjy_value_by_date(stock_list=[first_sector], field_list=bk_fields, year=0, mmdd=0))
    sc_history = _run_case(cases, f"get_scjy_value_all_SC_{len(sc_fields)}", "financial_data", lambda: tq.get_scjy_value(field_list=sc_fields, start_time="20240101", end_time=""))
    sc_by_date = _run_case(cases, f"get_scjy_value_by_date_all_SC_{len(sc_fields)}", "financial_data", lambda: tq.get_scjy_value_by_date(field_list=sc_fields, year=0, mmdd=0))

    kzz_list = _run_case(cases, "get_stock_list_convertible_bond_for_kzz", "etf_bond_futures", lambda: tq.get_stock_list("32", list_type=1))
    kzz_code = "123054.SZ"
    if isinstance(kzz_list, list) and kzz_list:
        first = kzz_list[0]
        kzz_code = first.get("Code") if isinstance(first, dict) else str(first)
    _run_case(cases, f"get_kzz_info_{kzz_code}", "etf_bond_futures", lambda: tq.get_kzz_info(stock_code=kzz_code, field_list=[]))
    for index_code in ["000300.SH", "000016.SH", "000905.SH", "000852.SH", "881001.SH", "950162.CSI"]:
        _run_case(cases, f"get_trackzs_etf_info_{index_code}", "etf_bond_futures", lambda index_code=index_code: tq.get_trackzs_etf_info(zs_code=index_code))

    # Data files. These download vendor data files into PYPlugins/data, so run only small documented examples.
    download_cases = [
        ("download_file_top10_holder", {"stock_code": "600519.SH", "down_time": "20241231", "down_type": 1}),
        ("download_file_latest_news", {"stock_code": "600519.SH", "down_time": "", "down_type": 3}),
        ("download_file_stock_overview", {"stock_code": "600519.SH", "down_time": "", "down_type": 4}),
    ]
    for name, kwargs in download_cases:
        _run_case(cases, name, "data_file", lambda kwargs=kwargs: tq.download_file(**kwargs))

    _run_case(cases, "formula_set_data_info", "formula", lambda: tq.formula_set_data_info(stock_code="600519.SH", stock_period="1d", count=30, dividend_type=1))
    _run_case(cases, "formula_get_data_after_set_data_info", "formula", lambda: tq.formula_get_data())
    _run_case(cases, "formula_zb_MACD", "formula", lambda: tq.formula_zb(formula_name="MACD", formula_arg="12,26,9", xsflag=6))
    _run_case(cases, "formula_xg_UPN", "formula", lambda: tq.formula_xg(formula_name="UPN", formula_arg="3"))
    _run_case(cases, "formula_exp_CCI", "formula", lambda: tq.formula_exp(formula_name="CCI", formula_arg="12"))
    _run_case(cases, "formula_process_mul_zb_MACD", "formula", lambda: tq.formula_process_mul_zb(formula_name="MACD", formula_arg="12,26,9", return_count=2, return_date=True, stock_list=stock_codes[:3], stock_period="1d", count=30, dividend_type=1))
    _run_case(cases, "formula_process_mul_xg_UPN", "formula", lambda: tq.formula_process_mul_xg(formula_name="UPN", formula_arg="3", return_count=2, return_date=True, stock_list=stock_codes[:3], stock_period="1d", count=30, dividend_type=1))
    _run_case(cases, "formula_format_data_daily", "formula", lambda: tq.formula_format_data(tq.get_market_data(field_list=[], stock_list=["600519.SH"], period="1d", start_time="", end_time="", count=5, dividend_type="front", fill_data=True)))

    account = os.environ.get("TDX_PROBE_ACCOUNT", "").strip()
    account_id = None
    if account:
        account_id = _run_case(cases, "stock_account_env_account", "trading_read", lambda: tq.stock_account(account=account, account_type=os.environ.get("TDX_PROBE_ACCOUNT_TYPE", "STOCK")), redacted=True)
    else:
        _add_status_for_not_run(cases, "stock_account", "trading_read", "skipped_missing_env", "Set TDX_PROBE_ACCOUNT to test trading read APIs without exposing account ids in this script.")
    if isinstance(account_id, int) and account_id >= 0:
        _run_case(cases, "query_stock_asset", "trading_read", lambda: tq.query_stock_asset(account_id=account_id), redacted=True)
        _run_case(cases, "query_stock_orders", "trading_read", lambda: tq.query_stock_orders(account_id=account_id), redacted=True)
        _run_case(cases, "query_stock_positions", "trading_read", lambda: tq.query_stock_positions(account_id=account_id), redacted=True)
    else:
        for name in ["query_stock_asset", "query_stock_orders", "query_stock_positions"]:
            _add_status_for_not_run(cases, name, "trading_read", "skipped_missing_valid_account_handle", "Requires valid stock_account handle; set TDX_PROBE_ACCOUNT.")

    doc_sources = doc_scan.get("doc_sources_by_function", {})
    for name, reason in SIDE_EFFECT_FUNCS.items():
        if name in {"download_file"}:
            continue
        if name in doc_scan.get("implemented_in_local_sdk", []):
            # Some side-effect APIs are already represented by explicit skipped entries below.
            if not any(case["name"] == name for case in cases):
                _add_status_for_not_run(cases, name, "side_effect_api", "skipped_side_effect", reason, doc_sources.get(name, []))
    for name in doc_scan.get("documented_not_implemented_in_local_sdk", []):
        if not any(case["name"] == name for case in cases):
            _add_status_for_not_run(cases, name, "documented_only", "not_implemented_in_sdk", "Documented or mentioned in Markdown, but absent from local tqcenter.py.", doc_sources.get(name, []))

    try:
        tq.close()
    except Exception:
        pass

    derived = {
        "stock_list_counts": stock_list_counts,
        "kline_periods": kline_periods,
        "financial_field_availability": {
            "FN_by_date": _field_value_entries(fn_by_date, "600519.SH"),
            "FN_history": _field_value_entries(fn_history, "600519.SH"),
            "GO": _field_value_entries(go_value, "600519.SH"),
            "GP_history": _field_value_entries(gp_history, "600519.SH"),
            "GP_by_date": _field_value_entries(gp_by_date, "600519.SH"),
            "BK_history": _field_value_entries(bk_history, first_sector),
            "BK_by_date": _field_value_entries(bk_by_date, first_sector),
            "SC_history": _field_value_entries(sc_history),
            "SC_by_date": _field_value_entries(sc_by_date),
        },
    }

    return {
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "python": sys.version,
        "cwd": os.getcwd(),
        "tdx_pyplugins": TDX_PYPLUGINS,
        "doc_scan": doc_scan,
        "cases": cases,
        "derived": derived,
    }


def write_report(result: dict[str, Any]) -> None:
    doc = result["doc_scan"]
    cases = result["cases"]
    executed = [case for case in cases if case["status"] == "executed"]
    executed_ok = [case for case in executed if case.get("ok")]
    executed_fail = [case for case in executed if case.get("ok") is False]
    value_present = [case for case in executed_ok if case.get("value_present")]

    status_counts: dict[str, int] = {}
    for case in cases:
        status_counts[case["status"]] = status_counts.get(case["status"], 0) + 1

    lines = [
        "# TDX Docs Full Probe Report",
        "",
        f"- Generated: {result['finished_at']}",
        f"- Markdown files scanned: {doc['markdown_count']}",
        f"- Manual/API candidates from docs: {doc['manual_api_candidate_count']}",
        f"- Implemented in local tqcenter.py: {len(doc['implemented_in_local_sdk'])}",
        f"- Documented but not implemented in local tqcenter.py: {len(doc['documented_not_implemented_in_local_sdk'])}",
        f"- Probe cases: {len(cases)}",
        f"- Executed: {len(executed)}; ok: {len(executed_ok)}; failed: {len(executed_fail)}; value-present ok: {len(value_present)}",
        f"- Status counts: {json.dumps(status_counts, ensure_ascii=False)}",
        "",
        "## Implemented API Surface",
        "",
        ", ".join(f"`{name}`" for name in doc["implemented_in_local_sdk"]),
        "",
        "## Documented But Missing In Local SDK",
        "",
        ", ".join(f"`{name}`" for name in doc["documented_not_implemented_in_local_sdk"]) or "(none)",
        "",
        "## Field Enums From Docs",
        "",
    ]
    for prefix, values in doc["field_enums"].items():
        lines.append(f"- `{prefix}`: {len(values)} fields, {values[:5]} ... {values[-5:] if values else []}")

    lines.extend(["", "## Stock List Counts", ""])
    for label, info in result["derived"]["stock_list_counts"].items():
        lines.append(f"- `{label}` market `{info['market']}`: count={info['count']}, first={json.dumps(info['first'], ensure_ascii=False)}")

    lines.extend(["", "## Kline Period Coverage", ""])
    for period, summary in result["derived"]["kline_periods"].items():
        keys = summary.get("keys")
        df_fields = summary.get("dataframe_fields") or {}
        sample_shape = next(iter(df_fields.values()), {}).get("shape") if df_fields else None
        lines.append(f"- `{period}`: type={summary.get('type')}, len={summary.get('len')}, keys={keys}, sample_shape={sample_shape}")

    lines.extend(["", "## Financial/Feature Field Availability", ""])
    for name, info in result["derived"]["financial_field_availability"].items():
        lines.append(f"- `{name}`: non_empty_count={info['non_empty_count']}, fields={info['fields'][:30]}")

    lines.extend(["", "## Failed Executed Cases", ""])
    if executed_fail:
        for case in executed_fail:
            lines.append(f"- `{case['category']}/{case['name']}`: {case.get('error')}")
    else:
        lines.append("- None")

    lines.extend(["", "## Skipped Or Non-Executable Cases", ""])
    for case in cases:
        if case["status"] != "executed":
            lines.append(f"- `{case['category']}/{case['name']}`: {case['status']} - {case.get('reason', '')}")

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    print(f"[tdx-docs-full] workspace = {ROOT}")
    print(f"[tdx-docs-full] docs      = {DOCS_ROOT}")
    print(f"[tdx-docs-full] sdk       = {SDK_PATH}")
    doc_scan = scan_docs()
    result = run_probe(doc_scan)
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_report(result)
    executed = [case for case in result["cases"] if case["status"] == "executed"]
    ok = sum(1 for case in executed if case.get("ok"))
    failed = sum(1 for case in executed if case.get("ok") is False)
    print(f"\n[tdx-docs-full] result = {RESULT_PATH}")
    print(f"[tdx-docs-full] report = {REPORT_PATH}")
    print(f"[tdx-docs-full] executed ok={ok} failed={failed} total={len(executed)}")


if __name__ == "__main__":
    main()
