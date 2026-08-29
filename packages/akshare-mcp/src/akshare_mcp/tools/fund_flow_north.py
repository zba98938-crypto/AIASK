"""
fund_flow_north.py
North-bound fund data: Tushare / HKEX / Eastmoney sources,
plus the public ``get_north_fund``, ``get_north_fund_holding``,
and ``get_north_fund_top`` functions.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

import pandas as pd
import requests

from ..utils import fail, normalize_code, ok, ok_degraded_empty, parse_numeric, resolve_existing_security_code_sync, safe_float, validate_int_range
from ..data_source import data_source
from ..provider_contracts import attach_tool_provider_contract_meta
from ..core.cache_manager import cached
from ..core.rate_limiter import get_limiter
from ..date_utils import get_latest_trading_date
from ..storage import get_db

from .fund_flow_common import (
    _NORTH_FUND_STALE_DAYS,
    _NORTH_FUND_DAILY_QUOTA,
    _NORTH_FUND_FAST_MODE,
    _HKEX_DAILY_STAT_URL,
    _fetch_eastmoney_datacenter,
    _run_storage_call_sync,
logger,
)


def _with_provider_contract(result: dict, tool_name: str, **kwargs: Any) -> dict:
    return attach_tool_provider_contract_meta(result, tool_name=tool_name, **kwargs)


# =====================
# Internal helpers
# =====================

def _parse_date(value: Any) -> Optional[date]:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (date, datetime)):
        return value if isinstance(value, date) else value.date()
    s = str(value).strip()
    if not s:
        return None
    s = s[:10]
    try:
        if "-" in s:
            return datetime.strptime(s, "%Y-%m-%d").date()
        if len(s) >= 8:
            return datetime.strptime(s[:8], "%Y%m%d").date()
    except ValueError:
        return None
    return None


def _format_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _north_fund_is_valid(results: list[dict], stale_days: int) -> bool:
    if not _north_fund_has_values(results) or not _north_fund_is_plausible(results):
        return False
    if not _north_fund_has_flow(results):
        return False
    latest = _north_fund_latest_date(results)
    if latest is None:
        return False
    if stale_days > 0 and (date.today() - latest).days > stale_days:
        return False
    return True


def _north_fund_latest_date(results: list[dict]) -> Optional[date]:
    return max((_parse_date(r.get("date")) for r in results), default=None)


def _north_fund_age_days(results: list[dict]) -> Optional[int]:
    latest = _north_fund_latest_date(results)
    if latest is None:
        return None
    return max((date.today() - latest).days, 0)


def _north_fund_has_values(results: list[dict]) -> bool:
    if not results:
        return False
    return any(
        r.get("shConnect") is not None
        and r.get("szConnect") is not None
        and r.get("total") is not None
        for r in results
    )


def _north_fund_has_flow(results: list[dict]) -> bool:
    if not _north_fund_has_values(results):
        return False
    return any(
        abs(parse_numeric(r.get("shConnect")) or 0.0) > 0
        or abs(parse_numeric(r.get("szConnect")) or 0.0) > 0
        or abs(parse_numeric(r.get("total")) or 0.0) > 0
        for r in results
    )


def _north_fund_is_plausible(results: list[dict]) -> bool:
    if not results:
        return False
    quota = _NORTH_FUND_DAILY_QUOTA
    saturation_rows = 0
    comparable_rows = 0
    for row in results:
        sh = parse_numeric(row.get("shConnect"))
        sz = parse_numeric(row.get("szConnect"))
        if sh is None or sz is None:
            continue
        comparable_rows += 1
        if abs(sh) >= quota * 0.98 and abs(sz) >= quota * 0.98:
            saturation_rows += 1
    if comparable_rows and saturation_rows / comparable_rows >= 0.6:
        return False
    return True


def _tushare_pick_multiplier(
    totals: list[float], quota: float, tolerance: float
) -> Optional[float]:
    if not totals:
        return None
    totals_sorted = sorted(abs(val) for val in totals if val is not None)
    if not totals_sorted:
        return None
    median_total = totals_sorted[len(totals_sorted) // 2]
    if median_total <= 0:
        return None

    min_flow = float(os.getenv("NORTH_FUND_MIN_FLOW", "5e7"))
    max_flow = quota + tolerance
    candidates = (1e6, 1e4, 1e8)
    for mult in candidates:
        scaled = median_total * mult
        if min_flow <= scaled <= max_flow:
            return mult
    for mult in candidates:
        if median_total * mult <= max_flow:
            return mult
    return None


def _normalize_north_fund_results(rows: list[dict], days: int) -> list[dict]:
    seen: set[str] = set()
    normalized: list[dict] = []
    for r in rows:
        d = _parse_date(r.get("date"))
        if d is None:
            continue
        key = _format_date(d)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "date": key,
                "shConnect": r.get("shConnect"),
                "szConnect": r.get("szConnect"),
                "total": r.get("total"),
                "shCumulative": r.get("shCumulative"),
                "szCumulative": r.get("szCumulative"),
                "cumulative": r.get("cumulative"),
            }
        )
    normalized.sort(key=lambda x: x["date"])

    has_any_cumulative = any(r.get("cumulative") is not None for r in normalized)
    if not has_any_cumulative and normalized:
        sh_sum = 0.0
        sz_sum = 0.0
        for r in normalized:
            sh_val = r.get("shConnect") or 0.0
            sz_val = r.get("szConnect") or 0.0
            sh_sum += sh_val
            sz_sum += sz_val
            r["shCumulative"] = sh_sum
            r["szCumulative"] = sz_sum
            r["cumulative"] = sh_sum + sz_sum

    return normalized[-days:] if days > 0 else normalized


def _get_anchor_date() -> date:
    try:
        s = get_latest_trading_date()
        return datetime.strptime(s, "%Y%m%d").date()
    except Exception:
        pass
    return date.today()


def _north_fund_from_db(days: int) -> list[dict]:
    try:
        db = get_db()
        rows = _run_storage_call_sync(
            lambda: db.get_north_fund_history(days=max(int(days or 1), 1), end_date=date.today()),
            timeout=20.0,
        )
        if not isinstance(rows, list) or not rows:
            return []
        normalized = []
        for row in rows:
            trade_date = _parse_date(row.get("trade_date"))
            if trade_date is None:
                continue
            normalized.append(
                {
                    "date": _format_date(trade_date),
                    "shConnect": parse_numeric(row.get("hgt")),
                    "szConnect": parse_numeric(row.get("sgt")),
                    "total": parse_numeric(row.get("north_money") or row.get("net_amount")),
                    "shCumulative": None,
                    "szCumulative": None,
                    "cumulative": None,
                }
            )
        return _normalize_north_fund_results(normalized, days)
    except Exception as exc:
        logger.warning("_north_fund_from_db failed: %s", exc)
        return []


# =====================
# Data source: Tushare
# =====================

def _north_fund_from_tushare(days: int) -> list[dict]:
    try:
        pro = data_source.get_tushare_pro()
        if not pro:
            return []
        anchor = _get_anchor_date()
        end_date = anchor.strftime("%Y%m%d")
        start_date = (anchor - timedelta(days=days * 3)).strftime("%Y%m%d")
        df = pro.moneyflow_hsgt(start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return []

        quota = _NORTH_FUND_DAILY_QUOTA
        tolerance = quota * 0.05
        totals: list[float] = []
        for _, row in df.iterrows():
            sh_raw = parse_numeric(row.get("hgt"))
            sz_raw = parse_numeric(row.get("sgt"))
            if sh_raw is None or sz_raw is None:
                continue
            totals.append(abs(sh_raw) + abs(sz_raw))
        multiplier = _tushare_pick_multiplier(totals, quota, tolerance)
        if multiplier is None:
            return []

        results: list[dict] = []
        for _, row in df.iterrows():
            trade_date = row.get("trade_date") or row.get("date")
            d = _parse_date(trade_date)
            if d is None:
                continue
            sh = parse_numeric(row.get("hgt"))
            sz = parse_numeric(row.get("sgt"))
            if sh is None or sz is None:
                continue
            sh_val = sh * multiplier
            sz_val = sz * multiplier
            if (
                abs(sh_val) > quota + tolerance
                or abs(sz_val) > quota + tolerance
            ):
                continue
            results.append(
                {
                    "date": _format_date(d),
                    "shConnect": sh_val,
                    "szConnect": sz_val,
                    "total": sh_val + sz_val,
                    "shCumulative": None,
                    "szCumulative": None,
                    "cumulative": None,
                }
            )
        return _normalize_north_fund_results(results, days)
    except Exception:
        return []


# =====================
# Data source: HKEX
# =====================

def _hkex_schema_index(schema_row: list[Any], candidates: tuple[str, ...]) -> Optional[int]:
    for idx, raw in enumerate(schema_row):
        label = str(raw or "").strip().lower()
        if not label:
            continue
        for cand in candidates:
            if cand in label:
                return idx
    return None


def _hkex_extract_table_value(table: dict, idx: Optional[int]) -> Any:
    if idx is None:
        return None
    tr = table.get("tr", [])
    if not isinstance(tr, list) or idx >= len(tr):
        return None
    try:
        return tr[idx].get("td", [[None]])[0][0]
    except Exception:
        return None


def _hkex_is_sentinel_value(raw: Any) -> bool:
    s = str(raw or "").strip()
    if not s:
        return True
    lower = s.lower()
    if lower in {"-", "--", "na", "n/a", "none"}:
        return True
    s = s.replace(",", "")
    return re.fullmatch(r"9{6,}", s) is not None


def _hkex_parse_dqb(raw: Any) -> Optional[float]:
    if _hkex_is_sentinel_value(raw):
        return None
    val = parse_numeric(raw)
    if val is None or val < 0:
        return None
    if val < 1e7:
        val *= 1e6
    if val > _NORTH_FUND_DAILY_QUOTA * 1.05:
        return None
    return val


def _north_fund_from_hkex(days: int) -> list[dict]:
    results: list[dict] = []
    max_lookback = max(days * 3, 10)
    quota = _NORTH_FUND_DAILY_QUOTA
    tolerance = quota * 0.05
    anchor = _get_anchor_date()
    for i in range(max_lookback):
        day = anchor - timedelta(days=i)
        url = _HKEX_DAILY_STAT_URL.format(date=day.strftime("%Y%m%d"))
        try:
            resp = requests.get(url, timeout=6)
            if resp.status_code != 200 or "tabData" not in resp.text:
                continue
            payload = resp.text.strip()
            if payload.startswith("tabData ="):
                payload = payload[len("tabData ="):].strip()
            if payload.endswith(";"):
                payload = payload[:-1]
            data = json.loads(payload)
        except Exception:
            continue

        sh_dqb = None
        sz_dqb = None
        for item in data:
            market = item.get("market")
            if market not in ("SSE Northbound", "SZSE Northbound"):
                continue
            if not item.get("tradingDay"):
                continue
            table = item.get("content", [{}])[0].get("table", {})
            schema = table.get("schema", [])
            if not schema or not isinstance(schema, list) or not schema[0]:
                continue
            schema_row = schema[0]
            if not isinstance(schema_row, list):
                continue
            dqb_idx = _hkex_schema_index(schema_row, ("dqb", "daily quota balance"))
            if dqb_idx is None:
                continue
            raw_dqb = _hkex_extract_table_value(table, dqb_idx)
            dqb_val = _hkex_parse_dqb(raw_dqb)
            if dqb_val is None:
                continue
            turnover_idx = _hkex_schema_index(schema_row, ("total turnover",))
            trade_idx = _hkex_schema_index(schema_row, ("total trade count", "trade count"))
            if turnover_idx is not None:
                turnover = parse_numeric(_hkex_extract_table_value(table, turnover_idx))
                if turnover is None or turnover < 0:
                    continue
            if trade_idx is not None:
                trade_count = parse_numeric(_hkex_extract_table_value(table, trade_idx))
                if trade_count is None or trade_count < 0:
                    continue
            if market == "SSE Northbound":
                sh_dqb = dqb_val
            else:
                sz_dqb = dqb_val

        if sh_dqb is None or sz_dqb is None:
            continue

        sh_net = quota - sh_dqb
        sz_net = quota - sz_dqb
        if (
            sh_net < -tolerance
            or sz_net < -tolerance
            or sh_net > quota + tolerance
            or sz_net > quota + tolerance
        ):
            continue

        results.append(
            {
                "date": _format_date(day),
                "shConnect": sh_net,
                "szConnect": sz_net,
                "total": sh_net + sz_net,
                "shCumulative": None,
                "szCumulative": None,
                "cumulative": None,
            }
        )
        if len(results) >= days:
            break

    return _normalize_north_fund_results(results, days)


# =====================
# Data source: Eastmoney datacenter (direct API, no AKShare)
# =====================

def _north_fund_from_eastmoney_direct(days: int) -> list[dict]:
    """从东方财富 datacenter API 直接获取北向资金数据。"""
    try:
        items = _fetch_eastmoney_datacenter(
            {
                "sortColumns": "TRADE_DATE",
                "sortTypes": -1,
                "pageSize": days,
                "pageNumber": 1,
                "reportName": "RPT_MUTUAL_DEAL_HISTORY",
                "columns": "TRADE_DATE,MUTUAL_TYPE,NET_DEAL_AMT,ACCUM_DEAL_AMT",
                "filter": '(MUTUAL_TYPE="001")(MUTUAL_TYPE="003")',
            }
        )
        if not items:
            return []

        # 按日期聚合沪深两市
        daily: dict[str, dict[str, Optional[float]]] = {}
        for item in items:
            trade_date = _parse_date(item.get("TRADE_DATE"))
            if trade_date is None:
                continue
            date_str = _format_date(trade_date)
            mutual_type = str(item.get("MUTUAL_TYPE") or "")
            net = parse_numeric(item.get("NET_DEAL_AMT"))
            if net is None:
                continue

            if date_str not in daily:
                daily[date_str] = {"sh": None, "sz": None}

            # 001=沪股通, 003=深股通
            if mutual_type == "001":
                daily[date_str]["sh"] = net
            elif mutual_type == "003":
                daily[date_str]["sz"] = net

        results: list[dict] = []
        for d, values in daily.items():
            sh = values.get("sh")
            sz = values.get("sz")
            if sh is None and sz is None:
                continue
            sh_val = sh or 0.0
            sz_val = sz or 0.0
            results.append(
                {
                    "date": d,
                    "shConnect": sh_val,
                    "szConnect": sz_val,
                    "total": sh_val + sz_val,
                    "shCumulative": None,
                    "szCumulative": None,
                    "cumulative": None,
                }
            )

        return _normalize_north_fund_results(results, days)
    except Exception as e:
        logger.warning("_north_fund_from_eastmoney_direct failed: %s", e)
        return []


# =====================
# Public API functions
# =====================


async def _read_tdx_north_holding(db, code: str) -> Optional[dict]:
    """从 tdx_gpjy_daily 读 GP06（陆股通持股量）。

    tqcenter 的 GP06 ``Value=[持股数量(股)]``，写入 tdx_gpjy_daily 时
    value_a 即持股数量。变化由前一交易日比对得出。
    """
    if not code:
        return None
    bare = code.split(".")[0] if "." in code else code
    full_code = code if "." in code else (
        f"{bare}.SH" if bare.startswith(("6", "9", "5")) else
        (f"{bare}.BJ" if bare.startswith(("920", "810")) else f"{bare}.SZ")
    )
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT trade_date, value_a, value_b
            FROM tdx_gpjy_daily
            WHERE code = $1 AND gp_code = 'GP06'
            ORDER BY trade_date DESC
            LIMIT 2
            """,
            full_code,
        )
    if not rows:
        return None
    latest = dict(rows[0])
    prev = dict(rows[1]) if len(rows) > 1 else None
    latest_shares = latest.get("value_a") or 0.0
    prev_shares = prev.get("value_a") if prev else 0.0
    return {
        "shares": float(latest_shares),
        "ratio": 0.0,  # GP06 不带占比，留待 GP19/GP20 等后续补
        "change": float(latest_shares - (prev_shares or 0.0)),
        "trade_date": latest.get("trade_date"),
        "source": "tqcenter.gpjy.GP06",
    }


@cached(ttl=300.0)
def get_north_fund(days: int = 30) -> dict:
    """
    获取北向资金数据（沪股通+深股通）

    Args:
        days: 获取最近多少天的数据，默认30天
    """
    limiter = get_limiter("fund_flow", max_calls=3, period=1.0)
    limiter.acquire()

    try:
        days = int(days)
        if days <= 0:
            return _with_provider_contract(
                fail("days 必须为正整数"),
                "get_north_fund",
                standard_model="NorthFundFlow",
                provider_used="none",
                fallback_reason="invalid days",
            )

        sources_status = []
        fallback_candidates: list[tuple[str, list[dict], int, bool]] = []
        max_stale_age_days = max(int(_NORTH_FUND_STALE_DAYS) * 3, 45)

        if _NORTH_FUND_FAST_MODE:
            source_chain = [
                ("north_fund_flow", _north_fund_from_db),
                ("eastmoney_direct", _north_fund_from_eastmoney_direct),
            ]
        else:
            source_chain = [
                ("north_fund_flow", _north_fund_from_db),
                ("tushare", _north_fund_from_tushare),
                ("hkex", _north_fund_from_hkex),
                ("eastmoney_direct", _north_fund_from_eastmoney_direct),
            ]

        for name, fetcher in source_chain:
            results = fetcher(days)
            if _north_fund_is_valid(results, _NORTH_FUND_STALE_DAYS):
                return _with_provider_contract(
                    ok({"items": results, "source": name}),
                    "get_north_fund",
                    standard_model="NorthFundFlow",
                    provider_used=name,
                    source_chain=[source for source, _ in source_chain[: source_chain.index((name, fetcher)) + 1]],
                    data_timestamp=results[-1].get("date") if results else None,
                )
            age_days = _north_fund_age_days(results)
            has_values = _north_fund_has_values(results)
            has_flow = _north_fund_has_flow(results)
            plausible = _north_fund_is_plausible(results)

            if (
                has_values
                and plausible
                and age_days is not None
                and age_days <= max_stale_age_days
            ):
                fallback_candidates.append((name, results, age_days, has_flow))

            reason_bits = []
            if not results:
                reason_bits.append("empty")
            elif not plausible:
                reason_bits.append("implausible")
            if age_days is None:
                reason_bits.append("bad_date")
            elif age_days > _NORTH_FUND_STALE_DAYS:
                reason_bits.append("stale")
            if has_values and not has_flow:
                reason_bits.append("no_flow")
            if not has_values:
                reason_bits.append("missing_values")
            sources_status.append(f"{name}: {'/'.join(dict.fromkeys(reason_bits)) or 'invalid'}")

        if fallback_candidates:
            best_source, best_rows, stale_age_days, has_flow = max(
                fallback_candidates,
                key=lambda item: (
                    1 if item[3] else 0,
                    -(item[2]),
                    len(item[1]),
                ),
            )
            is_stale = stale_age_days > _NORTH_FUND_STALE_DAYS
            is_partial = (not has_flow) or len(best_rows) < min(days, 2)
            suffix: list[str] = []
            if is_partial:
                suffix.append("partial")
            if is_stale:
                suffix.append("stale")
            source_name = best_source if not suffix else f"{best_source}_{'_'.join(suffix)}"
            message = None
            if is_partial and not has_flow:
                message = "已回退到最近可用的结构化北向资金快照，但净流值仍为 0，请结合交易时段或上游刷新状态判断。"
            elif is_stale:
                message = "已回退到最近有效交易日的北向资金数据。"
            result = ok(
                {
                    "items": best_rows,
                    "source": source_name,
                    "stale": is_stale,
                    "stale_age_days": stale_age_days,
                    "partial": is_partial,
                    "message": message,
                }
            )
            result["_no_cache"] = True
            return _with_provider_contract(
                result,
                "get_north_fund",
                standard_model="NorthFundFlow",
                provider_used=best_source,
                source_chain=[source for source, _ in source_chain],
                fallback_reason=sources_status,
                data_timestamp=best_rows[-1].get("date") if best_rows else None,
            )

        return _with_provider_contract(
            ok_degraded_empty(
                {
                    "items": [],
                    "source": "none",
                    "stale": True,
                    "partial": True,
                    "message": "northbound fund data unavailable or stale",
                    "sources_status": sources_status,
                    "policy": {
                        "rfc_id": "RFC-001",
                        "policy_id": "north_fund_em_deprecated_2024_08_19",
                        "summary": "EM RPT_MUTUAL_DEAL_HISTORY 2024-08-19 后被监管要求停止公开,日频净流入 NET_DEAL_AMT 不再返回",
                        "since": "2024-08-19",
                        "alternatives": [
                            "get_north_fund_holding(stock_code) — 季度持股快照 (EM datacenter 仍可用)",
                            "get_north_fund_top(top_n) — 北向持股排行",
                            "scripts/backfill_north_fund_em.py — TDX 付费包/历史 dump 离线灌库",
                        ],
                        "non_blocking": True,
                    },
                },
                fallback_reason=sources_status,
                source_chain=[source for source, _ in source_chain],
                quality_flags=["empty_upstream", "stale", "rfc_001_north_fund_unavailable"],
            ),
            "get_north_fund",
            standard_model="NorthFundFlow",
            provider_used="none",
            source_chain=[source for source, _ in source_chain],
            fallback_reason=sources_status,
        )
    except Exception as e:
        return _with_provider_contract(
            ok_degraded_empty(
                {
                    "items": [],
                    "source": "none",
                    "stale": True,
                    "partial": True,
                    "message": "northbound fund provider exception",
                },
                fallback_reason=str(e),
                source_chain=["north_fund_flow", "tushare", "hkex", "eastmoney_direct"],
                quality_flags=["upstream_exception"],
            ),
            "get_north_fund",
            standard_model="NorthFundFlow",
            provider_used="none",
            fallback_reason=str(e),
        )


def get_north_fund_holding(stock_code: str) -> dict:
    """获取单只股票北向资金持股"""
    try:
        code, _, error = resolve_existing_security_code_sync(stock_code=stock_code)
        if error:
            return _with_provider_contract(
                fail(error),
                "get_north_fund_holding",
                standard_model="NorthFundHolding",
                provider_used="none",
                fallback_reason=error,
            )

        # 0. tqcenter 主路径：tdx_gpjy_daily 表 GP06 字段（陆股通持股量）
        try:
            from ..storage import get_db
            db = get_db()
            # 同步访问；fund_flow_north 整体是 sync 函数，这里包一层
            from .fund_flow_common import _run_storage_call_sync
            tdx_payload, _ = _run_storage_call_sync(
                lambda: _read_tdx_north_holding(db, code),
                timeout=4.0,
            )
            if tdx_payload:
                return _with_provider_contract(
                    ok(tdx_payload),
                    "get_north_fund_holding",
                    standard_model="NorthFundHolding",
                    provider_used=str(tdx_payload.get("source") or "tqcenter.gpjy.GP06"),
                    source_chain=["tqcenter.gpjy.GP06"],
                    data_timestamp=str(tdx_payload.get("trade_date") or "") or None,
                )
        except Exception:
            pass

        items = _fetch_eastmoney_datacenter(
            {
                "sortColumns": "TRADE_DATE",
                "sortTypes": -1,
                "pageSize": 2,
                "pageNumber": 1,
                "reportName": "RPT_MUTUAL_HOLDSTOCKNORTH_STA",
                "columns": "TRADE_DATE,SECURITY_CODE,SECURITY_NAME,HOLD_SHARES,HOLD_MARKET_CAP,HOLD_SHARES_RATIO",
                "filter": f'(SECURITY_CODE="{code}")',
            }
        )
        if not items:
            return _with_provider_contract(
                ok({
                    "shares": 0,
                    "ratio": 0,
                    "change": 0,
                    # P2-4.5.11 fix: change 字段语义明确(诊断报告 §4.5.11)
                    "change_semantics": "delta_shares_vs_previous_trading_day",
                    "change_unit": "shares",
                    "field_doc": {
                        "shares": "当日陆股通持股数(股)",
                        "ratio": "持股占流通股比例(%)",
                        "change": "持股数变化 = 最新持股数 - 上一交易日持股数(股); 正=增持, 负=减持",
                    },
                }),
                "get_north_fund_holding",
                standard_model="NorthFundHolding",
                provider_used="eastmoney.datacenter",
                source_chain=["eastmoney.datacenter"],
            )
        latest = items[0]
        prev = items[1] if len(items) > 1 else None
        latest_shares = parse_numeric(latest.get("HOLD_SHARES")) or 0
        # P3-B2 fix: prev 缺失时 change 应为 null 而非 latest_shares(对话式复测发现)
        # 历史问题:EM 仅返回 1 条数据时,prev_shares = 0 → change = latest_shares - 0 = latest_shares
        #          AI 误读为"持股全部为新增"。
        # 修复:严格区分"无前期数据"(prev_shares=None, change=None)和"前期为零"(prev_shares=0, change=latest_shares)。
        prev_shares = parse_numeric(prev.get("HOLD_SHARES")) if prev else None
        change_value: int | float | None
        change_semantics = "delta_shares_vs_previous_trading_day"
        if prev_shares is None:
            change_value = None
            change_semantics = "no_prior_period_data_available"
        else:
            change_value = latest_shares - prev_shares
        return _with_provider_contract(
            ok(
                {
                    "shares": latest_shares,
                    "ratio": parse_numeric(latest.get("HOLD_SHARES_RATIO")) or 0,
                    "change": change_value,
                    # P2-4.5.11 fix: 显式标注 change 字段单位/语义
                    "change_semantics": change_semantics,
                    "change_unit": "shares",
                    "previous_shares": prev_shares,
                    "previous_trade_date": str((prev or {}).get("TRADE_DATE") or "") if prev else None,
                    "field_doc": {
                        "shares": "当日陆股通持股数(股)",
                        "ratio": "持股占流通股比例(%)",
                        "change": "持股数变化 = 最新持股数 - 上一交易日持股数(股); 正=增持, 负=减持, null=无前期数据",
                    },
                }
            ),
            "get_north_fund_holding",
            standard_model="NorthFundHolding",
            provider_used="eastmoney.datacenter",
            source_chain=["eastmoney.datacenter"],
            data_timestamp=str(latest.get("TRADE_DATE") or "") or None,
        )
    except Exception as e:
        return _with_provider_contract(
            fail(e),
            "get_north_fund_holding",
            standard_model="NorthFundHolding",
            provider_used="none",
            fallback_reason=str(e),
        )


def get_north_fund_top(top_n: int = 20) -> dict:
    """获取北向资金持股排行"""
    try:
        top_n, top_n_error = validate_int_range(top_n, field_name="top_n", minimum=1)
        if top_n_error:
            return _with_provider_contract(
                fail(top_n_error),
                "get_north_fund_top",
                standard_model="NorthFundTop",
                provider_used="none",
                fallback_reason=top_n_error,
            )
        items = _fetch_eastmoney_datacenter(
            {
                "sortColumns": "HOLD_MARKET_CAP",
                "sortTypes": -1,
                "pageSize": top_n,
                "pageNumber": 1,
                "reportName": "RPT_MUTUAL_HOLDSTOCKNORTH_STA",
                "columns": "SECURITY_CODE,SECURITY_NAME,HOLD_SHARES,HOLD_MARKET_CAP,HOLD_SHARES_RATIO",
            }
        )
        results = []
        for item in items:
            results.append(
                {
                    "code": normalize_code(item.get("SECURITY_CODE")),
                    "name": str(item.get("SECURITY_NAME") or ""),
                    "shares": parse_numeric(item.get("HOLD_SHARES")) or 0,
                    "ratio": parse_numeric(item.get("HOLD_SHARES_RATIO")) or 0,
                    "marketCap": parse_numeric(item.get("HOLD_MARKET_CAP")) or 0,
                }
            )
        return _with_provider_contract(
            ok(results),
            "get_north_fund_top",
            standard_model="NorthFundTop",
            provider_used="eastmoney.datacenter",
            source_chain=["eastmoney.datacenter"],
        )
    except Exception as e:
        return _with_provider_contract(
            fail(e),
            "get_north_fund_top",
            standard_model="NorthFundTop",
            provider_used="none",
            fallback_reason=str(e),
        )
