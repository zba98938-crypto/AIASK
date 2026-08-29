#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare 代理支持度扫描脚本
- 逐个探测接口是否可用/无权限/不存在
- 默认输出概要，可用 --full 打印样例数据
- 可选 --json 输出扫描结果到文件
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import tushare as ts


@dataclass
class Endpoint:
    name: str
    api: str
    params: Dict[str, Any] | Callable[[], Dict[str, Any]]
    fields: str = ""


@dataclass
class ScanResult:
    name: str
    api: str
    status: str
    rows: int = 0
    cols: int = 0
    message: str = ""
    sample: Optional[List[Dict[str, Any]]] = None


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def classify_error(message: str) -> str:
    if not message:
        return "error"
    lower = message.lower()
    if "token" in lower or "token不对" in message:
        return "token_invalid"
    if "权限" in message:
        return "no_permission"
    if "接口" in message and "正确" in message:
        return "not_supported"
    if "参数" in message or "fields" in lower:
        return "bad_params"
    if "频率" in message or "访问次数" in message or "限流" in message:
        return "rate_limited"
    return "error"


def df_to_sample(df, limit: int = 2) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.head(limit).to_dict(orient="records")


def build_date_range(days: int) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def init_pro(token: str, http_url: str | None):
    ts.set_token(token)
    pro = ts.pro_api(token)
    if http_url:
        http_url = http_url.rstrip("/")
        try:
            pro._DataApi__token = token
            pro._DataApi__http_url = http_url
        except Exception:
            pass
    return pro


def main() -> int:
    parser = argparse.ArgumentParser(description="Tushare 代理支持度扫描")
    parser.add_argument("--days", type=int, default=60, help="时间窗口天数（默认 60）")
    parser.add_argument("--full", action="store_true", help="打印样例数据")
    parser.add_argument("--json", dest="json_path", help="输出 JSON 结果到文件")
    parser.add_argument("--whitelist", dest="whitelist_path", help="输出可用接口白名单 JSON（默认写入配置路径）")
    parser.add_argument("--sleep", type=float, default=0.2, help="接口间隔秒数（默认 0.2）")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[3]
    load_env_file(project_root / ".env")

    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        print("未检测到 TUSHARE_TOKEN，请先在 .env 或环境变量中配置。")
        return 1

    http_url = os.getenv("TUSHARE_HTTP_URL", "").strip() or None
    whitelist_path = (args.whitelist_path or os.getenv("TUSHARE_WHITELIST_PATH", "")).strip()
    if not whitelist_path:
        whitelist_path = str((project_root / "src" / "akshare_mcp" / "config" / "tushare_proxy_whitelist.json").resolve())
    pro = init_pro(token, http_url)

    start_date, end_date = build_date_range(args.days)

    cache: Dict[str, str] = {}

    def get_first_ts_code(api_name: str, params: Dict[str, Any], fields: str = "ts_code") -> str:
        cache_key = f"{api_name}:{fields}"
        if cache_key in cache:
            return cache[cache_key]
        try:
            df = pro.query(api_name, fields=fields, **params)
            if df is not None and not df.empty and "ts_code" in df.columns:
                cache[cache_key] = str(df.iloc[0]["ts_code"])
                return cache[cache_key]
        except Exception:
            pass
        cache[cache_key] = ""
        return ""

    def cb_code() -> str:
        code = get_first_ts_code("cb_basic", {})
        return code or "110030.SH"

    def bond_code() -> str:
        code = get_first_ts_code("bond_basic", {})
        return code or "019547.SH"

    def hk_code() -> str:
        code = get_first_ts_code("hk_basic", {})
        return code or "00001.HK"

    def us_code() -> str:
        code = get_first_ts_code("us_basic", {})
        return code or "AAPL.O"

    def fund_code() -> str:
        return "510300.SH"

    endpoints: List[Endpoint] = [
        Endpoint("股票基础信息", "stock_basic", {"exchange": "", "list_status": "L"}, "ts_code,symbol,name,market,industry,list_date"),
        Endpoint("交易日历", "trade_cal", {"exchange": "SSE", "is_open": "1", "start_date": start_date, "end_date": end_date}),
        Endpoint("股票日线", "daily", {"ts_code": "000001.SZ", "start_date": start_date, "end_date": end_date}),
        Endpoint("股票周线", "weekly", {"ts_code": "000001.SZ", "start_date": start_date, "end_date": end_date}),
        Endpoint("股票月线", "monthly", {"ts_code": "000001.SZ", "start_date": start_date, "end_date": end_date}),
        Endpoint("股票估值（日线）", "daily_basic", {"ts_code": "000001.SZ", "start_date": start_date, "end_date": end_date}),
        Endpoint("复权因子", "adj_factor", {"ts_code": "000001.SZ", "start_date": start_date, "end_date": end_date}),
        Endpoint("资金流向（个股）", "moneyflow", {"ts_code": "000001.SZ", "start_date": start_date, "end_date": end_date}),
        Endpoint("涨跌停", "stk_limit", {"trade_date": end_date}),
        Endpoint("停复牌", "suspend", {"ts_code": "000001.SZ", "trade_date": end_date}),
        Endpoint("名称变更", "namechange", {"ts_code": "000001.SZ", "start_date": start_date, "end_date": end_date}),
        Endpoint("股东人数", "stk_holdernumber", {"ts_code": "000001.SZ", "start_date": start_date, "end_date": end_date}),
        Endpoint("龙虎榜", "top_list", {"trade_date": end_date}),
        Endpoint("融资融券(市场)", "margin", {"trade_date": end_date}),
        Endpoint("北向资金", "moneyflow_hsgt", {"start_date": start_date, "end_date": end_date}),
        Endpoint("指数基础信息", "index_basic", {"market": "SSE"}),
        Endpoint("指数日线", "index_daily", {"ts_code": "000001.SH", "start_date": start_date, "end_date": end_date}),
        Endpoint("指数权重", "index_weight", {"index_code": "000001.SH", "trade_date": end_date}),
        Endpoint("指数成分", "index_member", {"index_code": "000001.SH"}),
        Endpoint("指数分类", "index_classify", {}),
        Endpoint("基金基础信息", "fund_basic", {"market": "E"}),
        Endpoint("基金日线", "fund_daily", {"ts_code": fund_code(), "start_date": start_date, "end_date": end_date}),
        Endpoint("基金净值", "fund_nav", {"ts_code": fund_code(), "start_date": start_date, "end_date": end_date}),
        Endpoint("基金管理人", "fund_manager", {}),
        Endpoint("基金公司", "fund_company", {}),
        Endpoint("基金持仓", "fund_portfolio", {"ts_code": fund_code(), "start_date": (datetime.now()-timedelta(days=365)).strftime("%Y%m%d"), "end_date": end_date}),
        Endpoint("财务指标", "fina_indicator", {"ts_code": "000001.SZ", "start_date": (datetime.now()-timedelta(days=365)).strftime("%Y%m%d"), "end_date": end_date}),
        Endpoint("利润表", "income", {"ts_code": "000001.SZ", "start_date": (datetime.now()-timedelta(days=365)).strftime("%Y%m%d"), "end_date": end_date}),
        Endpoint("资产负债表", "balancesheet", {"ts_code": "000001.SZ", "start_date": (datetime.now()-timedelta(days=365)).strftime("%Y%m%d"), "end_date": end_date}),
        Endpoint("现金流量表", "cashflow", {"ts_code": "000001.SZ", "start_date": (datetime.now()-timedelta(days=365)).strftime("%Y%m%d"), "end_date": end_date}),
        Endpoint("分红送转", "dividend", {"ts_code": "000001.SZ", "start_date": (datetime.now()-timedelta(days=365)).strftime("%Y%m%d"), "end_date": end_date}),
        Endpoint("公告", "anns", {"ts_code": "000001.SZ", "start_date": start_date, "end_date": end_date}),
        Endpoint("新闻", "news", {"start_date": start_date, "end_date": end_date, "src": "sina"}),
        Endpoint("可转债基础信息", "cb_basic", {}),
        Endpoint("可转债日线", "cb_daily", lambda: {"ts_code": cb_code(), "start_date": start_date, "end_date": end_date}),
        Endpoint("债券基础信息", "bond_basic", {}),
        Endpoint("债券日线", "bond_daily", lambda: {"ts_code": bond_code(), "start_date": start_date, "end_date": end_date}),
        Endpoint("港股基础信息", "hk_basic", {}),
        Endpoint("港股日线", "hk_daily", lambda: {"ts_code": hk_code(), "start_date": start_date, "end_date": end_date}),
        Endpoint("美股基础信息", "us_basic", {}),
        Endpoint("美股日线", "us_daily", lambda: {"ts_code": us_code(), "start_date": start_date, "end_date": end_date}),
        Endpoint("期货基础信息", "fut_basic", {"exchange": "CFFEX"}),
        Endpoint("期货日线", "fut_daily", {"ts_code": "IF2403.CFX", "start_date": start_date, "end_date": end_date}),
        Endpoint("期货周线", "fut_weekly", {"ts_code": "IF2403.CFX", "start_date": start_date, "end_date": end_date}),
        Endpoint("期货月线", "fut_monthly", {"ts_code": "IF2403.CFX", "start_date": start_date, "end_date": end_date}),
        Endpoint("宏观-CPI", "cpi", {}),
        Endpoint("宏观-PPI", "ppi", {}),
        Endpoint("宏观-货币供应", "money_supply", {}),
        Endpoint("宏观-Shibor", "shibor", {"start_date": start_date, "end_date": end_date}),
    ]

    results: List[ScanResult] = []

    print("=" * 80)
    print("Tushare 代理支持度扫描")
    print(f"时间窗口: {start_date} ~ {end_date}")
    if http_url:
        print(f"API 地址: {http_url}")
    print("=" * 80)

    for endpoint in endpoints:
        try:
            params = endpoint.params() if callable(endpoint.params) else endpoint.params
            df = pro.query(endpoint.api, fields=endpoint.fields, **params)
            if df is None or df.empty:
                results.append(ScanResult(name=endpoint.name, api=endpoint.api, status="empty", message="返回空数据"))
                print(f"[EMPTY] {endpoint.name} ({endpoint.api})")
            else:
                sample = df_to_sample(df) if args.full else None
                results.append(
                    ScanResult(
                        name=endpoint.name,
                        api=endpoint.api,
                        status="ok",
                        rows=len(df),
                        cols=len(df.columns),
                        sample=sample,
                    )
                )
                print(f"[OK] {endpoint.name} ({endpoint.api}) rows={len(df)} cols={len(df.columns)}")
                if args.full and sample:
                    print(sample)
        except Exception as exc:
            msg = str(exc)
            status = classify_error(msg)
            results.append(ScanResult(name=endpoint.name, api=endpoint.api, status=status, message=msg))
            print(f"[{status.upper()}] {endpoint.name} ({endpoint.api}) -> {msg}")

        if args.sleep > 0:
            time.sleep(args.sleep)

    summary = {
        "ok": [r.api for r in results if r.status == "ok"],
        "empty": [r.api for r in results if r.status == "empty"],
        "no_permission": [r.api for r in results if r.status == "no_permission"],
        "not_supported": [r.api for r in results if r.status == "not_supported"],
        "bad_params": [r.api for r in results if r.status == "bad_params"],
        "token_invalid": [r.api for r in results if r.status == "token_invalid"],
        "rate_limited": [r.api for r in results if r.status == "rate_limited"],
        "error": [r.api for r in results if r.status == "error"],
    }

    print("=" * 80)
    print(f"完成: {len(summary['ok'])}/{len(results)} 可用")
    print("支持: " + ", ".join(summary["ok"]))
    if summary["not_supported"]:
        print("不支持: " + ", ".join(summary["not_supported"]))
    if summary["no_permission"]:
        print("无权限: " + ", ".join(summary["no_permission"]))
    if summary["bad_params"]:
        print("参数异常: " + ", ".join(summary["bad_params"]))
    if summary["error"]:
        print("其他错误: " + ", ".join(summary["error"]))

    if args.json_path:
        output = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "start_date": start_date,
            "end_date": end_date,
            "results": [r.__dict__ for r in results],
            "summary": summary,
        }
        json_path = Path(args.json_path).expanduser().resolve()
        json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已输出 JSON: {json_path}")

    # 输出白名单配置（供运行时直接读取）
    try:
        whitelist_payload = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "http_url": http_url or "",
            "supported": summary["ok"],
            "empty": summary["empty"],
            "no_permission": summary["no_permission"],
            "not_supported": summary["not_supported"],
        }
        whitelist_file = Path(whitelist_path).expanduser().resolve()
        whitelist_file.parent.mkdir(parents=True, exist_ok=True)
        whitelist_file.write_text(json.dumps(whitelist_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已输出白名单: {whitelist_file}")
    except Exception as exc:
        print(f"白名单输出失败: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
