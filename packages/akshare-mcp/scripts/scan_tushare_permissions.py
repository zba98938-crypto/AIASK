#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare Pro 权限扫描脚本
- 以最小参数调用常用接口，判断是否具备访问权限
- 默认只输出概要，可用 --full 打印样例数据
- 可选 --json 输出扫描结果到文件
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import tushare as ts


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
    if "token" in message or "Token" in message or "token不对" in message:
        return "token_invalid"
    if "权限" in message:
        return "no_permission"
    if "接口名" in message or "接口" in message and "正确" in message:
        return "not_supported"
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


def init_pro(token: str, http_url: str | None) -> Any:
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
    parser = argparse.ArgumentParser(description="Tushare Pro 权限扫描脚本")
    parser.add_argument("--days", type=int, default=60, help="时间窗口天数（默认 60）")
    parser.add_argument("--full", action="store_true", help="打印样例数据")
    parser.add_argument("--json", dest="json_path", help="输出 JSON 结果到文件")
    parser.add_argument("--http-url", dest="http_url", help="自定义 Tushare API 地址（覆盖环境变量）")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[3]
    load_env_file(project_root / ".env")

    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        print("未检测到 TUSHARE_TOKEN，请先在 .env 或环境变量中配置。")
        return 1

    http_url = (args.http_url or os.getenv("TUSHARE_HTTP_URL", "")).strip() or None
    pro = init_pro(token, http_url)

    start_date, end_date = build_date_range(args.days)
    start_month = start_date[:6]
    end_month = end_date[:6]
    cache: Dict[str, Any] = {}

    def get_first_fut_code() -> str:
        if "fut_code" in cache:
            return cache["fut_code"]
        try:
            df = pro.fut_basic(exchange="CFFEX")
            if df is not None and not df.empty and "ts_code" in df.columns:
                cache["fut_code"] = str(df.iloc[0]["ts_code"])
                return cache["fut_code"]
        except Exception:
            pass
        cache["fut_code"] = ""
        return ""

    tests: List[Dict[str, Any]] = [
        {
            "name": "股票基础信息",
            "api": "stock_basic",
            "call": lambda: pro.stock_basic(
                exchange="", list_status="L", fields="ts_code,symbol,name,market,industry,list_date"
            ),
        },
        {
            "name": "交易日历",
            "api": "trade_cal",
            "call": lambda: pro.trade_cal(exchange="SSE", is_open="1", start_date=start_date, end_date=end_date),
        },
        {
            "name": "股票日线",
            "api": "daily",
            "call": lambda: pro.daily(ts_code="000001.SZ", start_date=start_date, end_date=end_date),
        },
        {
            "name": "股票估值（日线）",
            "api": "daily_basic",
            "call": lambda: pro.daily_basic(ts_code="000001.SZ", start_date=start_date, end_date=end_date),
        },
        {
            "name": "复权因子",
            "api": "adj_factor",
            "call": lambda: pro.adj_factor(ts_code="000001.SZ", start_date=start_date, end_date=end_date),
        },
        {
            "name": "北向资金",
            "api": "moneyflow_hsgt",
            "call": lambda: pro.moneyflow_hsgt(start_date=start_date, end_date=end_date),
        },
        {
            "name": "指数基础信息",
            "api": "index_basic",
            "call": lambda: pro.index_basic(market="SSE"),
        },
        {
            "name": "指数日线",
            "api": "index_daily",
            "call": lambda: pro.index_daily(ts_code="000001.SH", start_date=start_date, end_date=end_date),
        },
        {
            "name": "基金基础信息",
            "api": "fund_basic",
            "call": lambda: pro.fund_basic(market="E"),
        },
        {
            "name": "基金日线",
            "api": "fund_daily",
            "call": lambda: pro.fund_daily(ts_code="510300.SH", start_date=start_date, end_date=end_date),
        },
        {
            "name": "财务指标",
            "api": "fina_indicator",
            "call": lambda: pro.fina_indicator(
                ts_code="000001.SZ",
                start_date=(datetime.now() - timedelta(days=365)).strftime("%Y%m%d"),
                end_date=end_date,
            ),
        },
        {
            "name": "利润表",
            "api": "income",
            "call": lambda: pro.income(
                ts_code="000001.SZ",
                start_date=(datetime.now() - timedelta(days=365)).strftime("%Y%m%d"),
                end_date=end_date,
            ),
        },
        {
            "name": "资产负债表",
            "api": "balancesheet",
            "call": lambda: pro.balancesheet(
                ts_code="000001.SZ",
                start_date=(datetime.now() - timedelta(days=365)).strftime("%Y%m%d"),
                end_date=end_date,
            ),
        },
        {
            "name": "现金流量表",
            "api": "cashflow",
            "call": lambda: pro.cashflow(
                ts_code="000001.SZ",
                start_date=(datetime.now() - timedelta(days=365)).strftime("%Y%m%d"),
                end_date=end_date,
            ),
        },
        {
            "name": "分红送转",
            "api": "dividend",
            "call": lambda: pro.dividend(
                ts_code="000001.SZ",
                start_date=(datetime.now() - timedelta(days=365)).strftime("%Y%m%d"),
                end_date=end_date,
            ),
        },
        {
            "name": "公告信息",
            "api": "anns",
            "call": lambda: pro.anns(start_date=start_date, end_date=end_date),
        },
        {
            "name": "期货基础信息",
            "api": "fut_basic",
            "call": lambda: pro.fut_basic(exchange="CFFEX"),
        },
        {
            "name": "期货日线",
            "api": "fut_daily",
            "call": lambda: pro.fut_daily(
                ts_code=get_first_fut_code(),
                start_date=start_date,
                end_date=end_date,
            ),
        },
        {
            "name": "宏观-CPI",
            "api": "cpi",
            "call": lambda: pro.cpi(start_m=start_month, end_m=end_month),
        },
        {
            "name": "宏观-PPI",
            "api": "ppi",
            "call": lambda: pro.ppi(start_m=start_month, end_m=end_month),
        },
        {
            "name": "宏观-货币供应",
            "api": "money_supply",
            "call": lambda: pro.money_supply(start_m=start_month, end_m=end_month),
        },
        {
            "name": "宏观-Shibor",
            "api": "shibor",
            "call": lambda: pro.shibor(start_date=start_date, end_date=end_date),
        },
    ]

    results: List[ScanResult] = []

    print("=" * 80)
    print("Tushare Pro 权限扫描")
    print(f"时间窗口: {start_date} ~ {end_date}")
    if http_url:
        print(f"API 地址: {http_url}")
    print("=" * 80)

    for test in tests:
        name = test["name"]
        api = test["api"]
        try:
            df = test["call"]()
            if df is None or df.empty:
                results.append(ScanResult(name=name, api=api, status="empty", message="返回空数据"))
                print(f"[EMPTY] {name} ({api})")
                continue
            sample = df_to_sample(df) if args.full else None
            results.append(
                ScanResult(
                    name=name,
                    api=api,
                    status="ok",
                    rows=len(df),
                    cols=len(df.columns),
                    sample=sample,
                )
            )
            print(f"[OK] {name} ({api}) rows={len(df)} cols={len(df.columns)}")
            if args.full and sample:
                print(sample)
        except Exception as exc:
            msg = str(exc)
            status = classify_error(msg)
            results.append(ScanResult(name=name, api=api, status=status, message=msg))
            print(f"[{status.upper()}] {name} ({api}) -> {msg}")

    ok_count = sum(1 for r in results if r.status == "ok")
    print("=" * 80)
    print(f"完成: {ok_count}/{len(results)} 可用")

    if args.json_path:
        output = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "start_date": start_date,
            "end_date": end_date,
            "results": [r.__dict__ for r in results],
        }
        json_path = Path(args.json_path).expanduser().resolve()
        json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已输出 JSON: {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
