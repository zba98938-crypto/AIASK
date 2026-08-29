"""
fund_flow_market.py
Dragon-tiger board, margin trading, and block trades functions.
"""

from datetime import datetime, timedelta
from typing import Any, Optional

try:
    import akshare as ak
except ImportError:
    ak = None
import requests

from ..utils import fail, normalize_code, ok, parse_date_input, parse_numeric, resolve_existing_security_code_sync, safe_float, validate_int_range
from ..provider_contracts import attach_tool_provider_contract_meta
from ..core.rate_limiter import get_limiter
from ..date_utils import get_latest_trading_date, format_date_dash
from ..data_source import data_source
from ..storage import get_db
from .data_quality import build_quality_meta
from .market.helpers import get_name_map as _get_cached_name_map

from .fund_flow_common import _fetch_eastmoney_datacenter, _run_storage_call_sync


def _with_provider_contract(result: dict, tool_name: str, **kwargs: Any) -> dict:
    return attach_tool_provider_contract_meta(result, tool_name=tool_name, **kwargs)


def _dragon_tiger_rows_from_db(requested_date: str, stock_code: str = "", limit: int = 200) -> tuple[list[dict], str | None]:
    async def _load() -> tuple[list[dict], str | None]:
        db = get_db()
        target_code = normalize_code(stock_code) if stock_code else ""
        async with db.acquire() as conn:
            clauses = ["trade_date <= $1"]
            params: list[Any] = [format_date_dash(requested_date)]
            if target_code:
                clauses.append("code = $2")
                params.append(target_code)
            rows = []
            try:
                rows = await conn.fetch(
                    f"""
                    SELECT code, trade_date, reason, buy_amount, sell_amount,
                           COALESCE(net_buy, COALESCE(buy_amount, 0) - COALESCE(sell_amount, 0)) AS net_amount,
                           buyer_type
                    FROM dragon_tiger
                    WHERE {' AND '.join(clauses)}
                    ORDER BY trade_date DESC, ABS(COALESCE(net_buy, 0)) DESC
                    LIMIT {int(limit)}
                    """,
                    *params,
                )
            except Exception:
                rows = []
            if not rows:
                clauses = ["trade_date <= $1"]
                params = [format_date_dash(requested_date)]
                if target_code:
                    clauses.append("stock_code = $2")
                    params.append(target_code)
                try:
                    rows = await conn.fetch(
                        f"""
                        SELECT stock_code AS code, stock_name AS name, trade_date, reason,
                               buy_amount, sell_amount, net_amount
                        FROM dragon_tiger_list
                        WHERE {' AND '.join(clauses)}
                        ORDER BY trade_date DESC, ABS(COALESCE(net_amount, 0)) DESC
                        LIMIT {int(limit)}
                        """,
                        *params,
                    )
                except Exception:
                    rows = []
        items: list[dict] = []
        resolved: str | None = None
        for row in rows or []:
            item = dict(row)
            if resolved is None and item.get("trade_date"):
                resolved = str(item.get("trade_date")).replace("-", "")
            items.append(
                {
                    "code": normalize_code(item.get("code") or ""),
                    "name": str(item.get("name") or ""),
                    "closePrice": 0.0,
                    "changePercent": 0.0,
                    "reason": str(item.get("reason") or ""),
                    "buyAmount": parse_numeric(item.get("buy_amount")) or 0.0,
                    "sellAmount": parse_numeric(item.get("sell_amount")) or 0.0,
                    "netAmount": parse_numeric(item.get("net_amount")) or 0.0,
                    "source": "db.dragon_tiger",
                }
            )
        return items, resolved

    return _run_storage_call_sync(_load, timeout=20.0)


# =====================
# Dragon-tiger board
# =====================

def get_dragon_tiger(date: str = "", stock_code: str = "") -> dict:
    """
    获取龙虎榜数据

    Args:
        date: 日期，格式 YYYY-MM-DD，默认为最近交易日
        stock_code: 指定股票代码，不传则返回当日所有
    """
    try:
        requested_date = (date or "").strip().replace("-", "")
        if not requested_date:
            requested_date = get_latest_trading_date()

        candidate_dates = [requested_date]
        if not date:
            try:
                anchor = datetime.strptime(requested_date, "%Y%m%d")
                for offset in range(1, 6):
                    candidate_dates.append((anchor - timedelta(days=offset)).strftime("%Y%m%d"))
            except ValueError:
                pass

        resolved_date = requested_date
        results: list[dict] = []
        df = None
        source = "unknown"
        fallback_reasons: list[str] = []

        for candidate_date in candidate_dates:
            date_dash = format_date_dash(candidate_date)
            df = None
            source = "unknown"

            sina_fn = getattr(ak, "stock_lhb_detail_daily_sina", None) if ak is not None else None
            if callable(sina_fn):
                try:
                    df = sina_fn(date=date_dash)
                    if df is not None and not df.empty:
                        source = "sina"
                        resolved_date = candidate_date
                        break
                except Exception as exc:
                    fallback_reasons.append(f"sina:{candidate_date}:{exc}")
            else:
                fallback_reasons.append(f"sina:{candidate_date}:provider_unavailable")

            em_fn = getattr(ak, "stock_lhb_detail_em", None) if ak is not None else None
            if callable(em_fn):
                try:
                    df = em_fn(start_date=candidate_date, end_date=candidate_date)
                    if df is not None and not df.empty:
                        source = "eastmoney"
                        resolved_date = candidate_date
                        break
                except Exception as exc:
                    fallback_reasons.append(f"eastmoney:{candidate_date}:{exc}")
            else:
                fallback_reasons.append(f"eastmoney:{candidate_date}:provider_unavailable")

            # P0-4 fix (诊断报告 §5.5): 加 tushare top_list 第三 source 兜底
            # 历史问题: sina+eastmoney 6 个交易日全跪 (5/15~5/20)
            try:
                from ..data_source import data_source as _ds
                ts_pro = _ds.get_tushare_pro()
                if ts_pro is not None:
                    tushare_df = ts_pro.top_list(trade_date=candidate_date)
                    if tushare_df is not None and not tushare_df.empty:
                        df = tushare_df
                        source = "tushare_top_list"
                        resolved_date = candidate_date
                        break
                else:
                    fallback_reasons.append(f"tushare_top_list:{candidate_date}:tushare_pro_unavailable")
            except Exception as exc:
                fallback_reasons.append(f"tushare_top_list:{candidate_date}:{exc}")

        if df is None or df.empty:
            db_rows, db_date = _dragon_tiger_rows_from_db(requested_date, stock_code=stock_code)
            if db_rows:
                response = ok(db_rows)
                response.update(
                    build_quality_meta(
                        source="db.dragon_tiger",
                        source_chain=["dragon_tiger.sina", "dragon_tiger.eastmoney", "dragon_tiger.tushare_top_list", "db.dragon_tiger"],
                        fallback_reason=fallback_reasons or "live providers unavailable; using DB fallback",
                        asof_value=db_date or requested_date,
                        missing_fields=[],
                        degraded=True,
                        success=True,
                        accepted_count=len(db_rows),
                        rejected_count=0,
                    )
                )
                response["requested_date"] = requested_date
                response["resolved_date"] = db_date or requested_date
                return _with_provider_contract(
                    response,
                    "get_dragon_tiger",
                    standard_model="DragonTiger",
                    provider_used="db.dragon_tiger",
                    source_chain=["dragon_tiger.sina", "dragon_tiger.eastmoney", "dragon_tiger.tushare_top_list", "db.dragon_tiger"],
                    fallback_reason=fallback_reasons or "live providers unavailable; using DB fallback",
                    data_timestamp=db_date or requested_date,
                )

            response = ok([])
            response.update(
                build_quality_meta(
                    source="none",
                    source_chain=["dragon_tiger.sina", "dragon_tiger.eastmoney", "dragon_tiger.tushare_top_list"],
                    fallback_reason=fallback_reasons or f"未获取到 {requested_date} 龙虎榜数据",
                    asof_value=requested_date,
                    missing_fields=[],
                    degraded=True,
                    success=True,
                    accepted_count=0,
                    rejected_count=0,
                )
            )
            response["requested_date"] = requested_date
            return _with_provider_contract(
                response,
                "get_dragon_tiger",
                standard_model="DragonTiger",
                provider_used="none",
                source_chain=["dragon_tiger.sina", "dragon_tiger.eastmoney", "dragon_tiger.tushare_top_list"],
                fallback_reason=fallback_reasons,
                data_timestamp=requested_date,
            )

        target_code = normalize_code(stock_code) if stock_code else ""

        for _, row in df.iterrows():
            try:
                if source == "sina":
                    code_val = row.get("股票代码")
                    name = str(row.get("股票名称", ""))
                    close = safe_float(row.get("收盘价"))
                    change = safe_float(row.get("对应值"))
                    reason = str(row.get("指标", ""))
                    if reason.lower() == "nan":
                        reason = ""
                    buy = None
                    sell = None
                    net = None
                elif source == "tushare_top_list":
                    # P0-4 fix (诊断报告 §5.5): tushare top_list 字段映射
                    raw_code = row.get("ts_code") or row.get("code")
                    if raw_code is None:
                        continue
                    raw_code_str = str(raw_code).split(".")[0]
                    code_val = raw_code_str
                    name = str(row.get("name", ""))
                    close = safe_float(row.get("close"))
                    change = safe_float(row.get("pct_change"))
                    reason = str(row.get("reason", ""))
                    if reason.lower() == "nan":
                        reason = ""
                    buy = safe_float(row.get("l_buy"))
                    sell = safe_float(row.get("l_sell"))
                    net = safe_float(row.get("net_amount"))
                else:
                    code_val = row.get("代码")
                    name = str(row.get("名称", ""))
                    close = safe_float(row.get("收盘价"))
                    change = safe_float(row.get("涨跌幅"))
                    reason = str(row.get("上榜原因", ""))
                    if reason.lower() == "nan":
                        reason = ""
                    buy = safe_float(row.get("买入额"))
                    sell = safe_float(row.get("卖出额"))
                    net = safe_float(row.get("净买额"))

                if code_val is None:
                    continue
                code = normalize_code(str(code_val))
                if target_code and code != target_code:
                    continue

                results.append(
                    {
                        "code": code,
                        "name": name,
                        "closePrice": close,
                        "changePercent": change,
                        "reason": reason,
                        "buyAmount": buy if buy is not None else 0.0,
                        "sellAmount": sell if sell is not None else 0.0,
                        "netAmount": net if net is not None else 0.0,
                        "source": source,
                    }
                )
            except Exception:
                continue

        response = ok(results)
        response.update(
            build_quality_meta(
                source=source,
                source_chain=[f"dragon_tiger.{source}"],
                fallback_reason=(
                    [f"resolved_date={resolved_date}"]
                    if resolved_date != requested_date
                    else (fallback_reasons or None)
                ),
                asof_value=resolved_date,
                missing_fields=[],
                degraded=resolved_date != requested_date,
                success=True,
                accepted_count=len(results),
                rejected_count=0,
            )
        )
        response["requested_date"] = requested_date
        response["resolved_date"] = resolved_date
        return _with_provider_contract(
            response,
            "get_dragon_tiger",
            standard_model="DragonTiger",
            provider_used=source,
            source_chain=[f"dragon_tiger.{source}"],
            fallback_reason=fallback_reasons or ([f"resolved_date={resolved_date}"] if resolved_date != requested_date else None),
            data_timestamp=resolved_date,
        )
    except Exception as e:
        response = fail(e)
        response.update(
            build_quality_meta(
                source="none",
                source_chain=["dragon_tiger"],
                fallback_reason=str(e),
                asof_value=(date or "").strip().replace("-", "") or None,
                missing_fields=[],
                degraded=True,
                success=False,
            )
        )
        return _with_provider_contract(
            response,
            "get_dragon_tiger",
            standard_model="DragonTiger",
            provider_used="none",
            source_chain=["dragon_tiger"],
            fallback_reason=str(e),
        )


# =====================
# Margin data
# =====================


def _stock_ts_code(code: str) -> str:
    normalized = normalize_code(code)
    if normalized.startswith(("5", "6", "9")):
        return f"{normalized}.SH"
    return f"{normalized}.SZ"


def _margin_rows_from_db(stock_code: str = "", days: int = 30) -> list[dict]:
    try:
        db = get_db()
        limit = min(max(int(days or 30), 1), 200)
        code = normalize_code(stock_code) if stock_code else ""
        if code:
            rows = _run_storage_call_sync(
                lambda: db.get_margin_detail_latest(limit=limit, ts_code=_stock_ts_code(code)),
                timeout=20.0,
            )
            if not isinstance(rows, list) or not rows:
                return []
            name_map = _get_cached_name_map() or {}
            return [
                {
                    "date": str(row.get("trade_date") or ""),
                    "code": normalize_code(row.get("code") or code),
                    "name": str(name_map.get(normalize_code(row.get("code") or code), "")),
                    "marginBalance": parse_numeric(row.get("marginBalance")),
                    "marginBuy": parse_numeric(row.get("marginBuy")),
                    "marginRepay": parse_numeric(row.get("marginRepay")),
                    "shortBalance": parse_numeric(row.get("shortBalance")),
                    "shortSell": parse_numeric(row.get("shortSell")),
                    "shortRepay": parse_numeric(row.get("shortRepay")),
                    "totalBalance": parse_numeric(row.get("totalBalance")),
                    "source": row.get("source") or "margin_detail",
                }
                for row in rows
                if row.get("trade_date")
            ]

        rows = _run_storage_call_sync(
            lambda: db.get_margin_market_history(days=limit),
            timeout=20.0,
        )
        if not isinstance(rows, list) or not rows:
            return []
        return [
            {
                "date": str(row.get("trade_date") or ""),
                "code": "",
                "name": "",
                "marginBalance": parse_numeric(row.get("marginBalance")),
                "marginBuy": parse_numeric(row.get("marginBuy")),
                "marginRepay": parse_numeric(row.get("marginRepay")),
                "shortBalance": parse_numeric(row.get("shortBalance")),
                "shortSell": parse_numeric(row.get("shortSell")),
                "shortRepay": None,
                "totalBalance": parse_numeric(row.get("totalBalance")),
                "source": row.get("source") or "margin_market_flow",
            }
            for row in rows
            if row.get("trade_date")
        ]
    except Exception:
        return []


def _margin_ranking_from_db(top_n: int = 20, sort_by: str = "balance") -> list[dict]:
    try:
        db = get_db()
        rows = _run_storage_call_sync(
            lambda: db.get_margin_ranking(top_n=top_n, sort_by=sort_by),
            timeout=20.0,
        )
        if not isinstance(rows, list) or not rows:
            return []
        name_map = _get_cached_name_map() or {}
        return [
            {
                "date": str(row.get("trade_date") or ""),
                "code": normalize_code(row.get("code") or ""),
                "name": str(name_map.get(normalize_code(row.get("code") or ""), "")),
                "marginBalance": parse_numeric(row.get("marginBalance")),
                "marginBuy": parse_numeric(row.get("marginBuy")),
                "shortSell": parse_numeric(row.get("shortSell")),
                "totalBalance": parse_numeric(row.get("totalBalance")),
                "source": row.get("source") or "margin_detail_ranking",
            }
            for row in rows
            if row.get("trade_date")
        ]
    except Exception:
        return []

def get_margin_data(stock_code: str = "", days: int = 30) -> dict:
    """获取融资融券明细数据（支持单只股票或市场摘要）"""
    try:
        limiter = get_limiter("fund_flow", max_calls=3, period=1.0)
        limiter.acquire()

        days = int(days) if int(days or 0) > 0 else 30
        db_rows = _margin_rows_from_db(stock_code=stock_code, days=days)
        if db_rows:
            return _with_provider_contract(
                ok(db_rows),
                "get_margin_data",
                standard_model="MarginData",
                provider_used=str(db_rows[0].get("source") or "db.margin_detail"),
                source_chain=["db.margin_detail"],
                data_timestamp=str(db_rows[0].get("date") or "") or None,
            )
        params: dict[str, Any] = {
            "sortColumns": "DATE",
            "sortTypes": -1,
            "pageSize": min(max(days, 1), 200),
            "pageNumber": 1,
            "reportName": "RPTA_WEB_RZRQ_GGMX",
            "columns": "DATE,SCODE,SECNAME,RZYE,RZMRE,RZCHE,RQYE,RQMCL,RQCHL,RZRQYE",
        }

        if stock_code:
            code = normalize_code(stock_code)
            params["filter"] = f'(SCODE="{code}")'

        items = _fetch_eastmoney_datacenter(params)
        results: list[dict] = []
        for item in items:
            results.append(
                {
                    "date": str(item.get("DATE", "")).split(" ")[0],
                    "code": normalize_code(item.get("SCODE") or stock_code),
                    "name": str(item.get("SECNAME") or ""),
                    "marginBalance": parse_numeric(item.get("RZYE")),
                    "marginBuy": parse_numeric(item.get("RZMRE")),
                    "marginRepay": parse_numeric(item.get("RZCHE")),
                    "shortBalance": parse_numeric(item.get("RQYE")),
                    "shortSell": parse_numeric(item.get("RQMCL")),
                    "shortRepay": parse_numeric(item.get("RQCHL")),
                    "totalBalance": parse_numeric(item.get("RZRQYE")),
                }
            )

        if results:
            return _with_provider_contract(
                ok(results),
                "get_margin_data",
                standard_model="MarginData",
                provider_used="eastmoney.datacenter",
                source_chain=["eastmoney.datacenter"],
                data_timestamp=str(results[0].get("date") or "") if results else None,
            )

        # Fallback: market-level summary
        def scale_margin(val: Any) -> Optional[float]:
            num = parse_numeric(val)
            return num * 1e8 if num is not None else None

        df = ak.stock_margin_account_info()
        if df is not None and not df.empty:
            row = df.iloc[-1]
            summary = {
                "date": str(row.get("日期", "")),
                "code": normalize_code(stock_code) if stock_code else "",
                "name": "",
                "marginBalance": scale_margin(row.get("融资余额")),
                "marginBuy": scale_margin(row.get("融资买入额")),
                "marginRepay": None,
                "shortBalance": scale_margin(row.get("融券余额")),
                "shortSell": scale_margin(row.get("融券卖出额")),
                "shortRepay": None,
                "totalBalance": None,
            }
            return _with_provider_contract(
                ok([summary]),
                "get_margin_data",
                standard_model="MarginData",
                provider_used="akshare.stock_margin_account_info",
                source_chain=["eastmoney.datacenter", "akshare.stock_margin_account_info"],
                data_timestamp=str(summary.get("date") or "") or None,
            )

        df = ak.stock_margin_sse(
            start_date="20010106",
            end_date=datetime.now().strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return _with_provider_contract(
                fail("未获取到融资融券数据"),
                "get_margin_data",
                standard_model="MarginData",
                provider_used="none",
                source_chain=["eastmoney.datacenter", "akshare.stock_margin_account_info", "akshare.stock_margin_sse"],
                fallback_reason="empty margin data",
            )

        row = df.iloc[-1]
        summary = {
            "date": str(row.get("信用交易日期", "")),
            "code": normalize_code(stock_code) if stock_code else "",
            "name": "",
            "marginBalance": safe_float(row.get("融资余额")),
            "marginBuy": safe_float(row.get("融资买入额")),
            "marginRepay": safe_float(row.get("融资偿还额")),
            "shortBalance": safe_float(row.get("融券余量")),
            "shortSell": safe_float(row.get("融券卖出量")),
            "shortRepay": safe_float(row.get("融券偿还量")),
            "totalBalance": None,
        }
        return _with_provider_contract(
            ok([summary]),
            "get_margin_data",
            standard_model="MarginData",
            provider_used="akshare.stock_margin_sse",
            source_chain=["eastmoney.datacenter", "akshare.stock_margin_account_info", "akshare.stock_margin_sse"],
            data_timestamp=str(summary.get("date") or "") or None,
        )
    except Exception as e:
        return _with_provider_contract(
            fail(e),
            "get_margin_data",
            standard_model="MarginData",
            provider_used="none",
            fallback_reason=str(e),
        )


def get_margin_ranking(top_n: int = 20, sort_by: str = "balance") -> dict:
    """获取融资融券排行"""
    try:
        limiter = get_limiter("fund_flow", max_calls=3, period=1.0)
        limiter.acquire()

        top_n, top_n_error = validate_int_range(top_n, field_name="top_n", minimum=1)
        if top_n_error:
            return _with_provider_contract(
                fail(top_n_error),
                "get_margin_ranking",
                standard_model="MarginRanking",
                provider_used="none",
                fallback_reason=top_n_error,
            )
        db_rows = _margin_ranking_from_db(top_n=top_n, sort_by=sort_by)
        if db_rows:
            return _with_provider_contract(
                ok(db_rows),
                "get_margin_ranking",
                standard_model="MarginRanking",
                provider_used=str(db_rows[0].get("source") or "db.margin_detail_ranking"),
                source_chain=["db.margin_detail_ranking"],
                data_timestamp=str(db_rows[0].get("date") or "") or None,
            )
        sort_map = {"balance": "RZRQYE", "buy": "RZMRE", "sell": "RQMCL"}
        sort_column = sort_map.get(sort_by, "RZRQYE")

        latest = _fetch_eastmoney_datacenter(
            {
                "sortColumns": "DATE",
                "sortTypes": -1,
                "pageSize": 1,
                "pageNumber": 1,
                "reportName": "RPTA_WEB_RZRQ_GGMX",
                "columns": "DATE",
            }
        )
        if not latest:
            return _with_provider_contract(
                ok([]),
                "get_margin_ranking",
                standard_model="MarginRanking",
                provider_used="eastmoney.datacenter",
                source_chain=["eastmoney.datacenter"],
                fallback_reason="latest date empty",
            )
        latest_date = latest[0].get("DATE")
        if not latest_date:
            return _with_provider_contract(
                ok([]),
                "get_margin_ranking",
                standard_model="MarginRanking",
                provider_used="eastmoney.datacenter",
                source_chain=["eastmoney.datacenter"],
                fallback_reason="latest date missing",
            )

        items = _fetch_eastmoney_datacenter(
            {
                "sortColumns": sort_column,
                "sortTypes": -1,
                "pageSize": top_n,
                "pageNumber": 1,
                "reportName": "RPTA_WEB_RZRQ_GGMX",
                "columns": "DATE,SCODE,SECNAME,RZYE,RZMRE,RQMCL,RZRQYE",
                "filter": f"(DATE='{latest_date}')",
            }
        )
        results = []
        for item in items:
            results.append(
                {
                    "date": str(item.get("DATE", "")).split(" ")[0],
                    "code": normalize_code(item.get("SCODE")),
                    "name": str(item.get("SECNAME") or ""),
                    "marginBalance": parse_numeric(item.get("RZYE")),
                    "marginBuy": parse_numeric(item.get("RZMRE")),
                    "shortSell": parse_numeric(item.get("RQMCL")),
                    "totalBalance": parse_numeric(item.get("RZRQYE")),
                }
            )
        return _with_provider_contract(
            ok(results),
            "get_margin_ranking",
            standard_model="MarginRanking",
            provider_used="eastmoney.datacenter",
            source_chain=["eastmoney.datacenter"],
            data_timestamp=str(results[0].get("date") or "") if results else None,
        )
    except Exception as e:
        return _with_provider_contract(
            fail(e),
            "get_margin_ranking",
            standard_model="MarginRanking",
            provider_used="none",
            fallback_reason=str(e),
        )


# =====================
# Block trades
# =====================

def _normalize_name_text(value: Any) -> str:
    return "".join(str(value or "").strip().upper().split())


def _get_security_meta_map(codes: set[str]) -> dict[str, dict[str, str]]:
    meta_map: dict[str, dict[str, str]] = {}
    if not codes:
        return meta_map

    try:
        name_map = _get_cached_name_map()
    except Exception:
        name_map = {}

    for code in codes:
        if code in name_map:
            meta_map[code] = {"name": str(name_map.get(code) or "").strip(), "industry": ""}

    if len(codes) > 50:
        return meta_map

    try:
        pro = data_source.get_tushare_pro()
    except Exception:
        pro = None

    if not pro:
        return meta_map

    for code in sorted(codes):
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        try:
            df = pro.stock_basic(ts_code=ts_code, list_status="L", fields="ts_code,name,industry")
        except Exception:
            continue
        if df is None or df.empty:
            continue
        row = df.iloc[0]
        meta_map[code] = {
            "name": str(row.get("name") or meta_map.get(code, {}).get("name") or "").strip(),
            "industry": str(row.get("industry") or "").strip(),
        }
    return meta_map


def _finalize_block_trade_results(
    items: list[dict],
    *,
    source_chain: list[str],
    fallback_reason: list[str] | None = None,
) -> tuple[list[dict], dict]:
    results: list[dict] = []
    codes = {normalize_code(item.get("code")) for item in items if item.get("code")}
    security_meta = _get_security_meta_map(codes)

    name_backfilled = 0
    name_corrected = 0
    code_mismatch_count = 0
    industry_backfilled = 0
    missing_name_count = 0

    for item in items:
        code = normalize_code(item.get("code"))
        if not code:
            continue

        raw_name = str(item.get("name") or "").strip()
        alt_code = normalize_code(item.get("alt_code") or "")
        if alt_code and alt_code != code:
            code_mismatch_count += 1

        meta = security_meta.get(code, {})
        canonical_name = str(meta.get("name") or "").strip()
        canonical_industry = str(meta.get("industry") or "").strip()

        derived_fields: list[str] = []
        final_name = raw_name
        if canonical_name:
            if not raw_name:
                final_name = canonical_name
                name_backfilled += 1
                derived_fields.append("name")
            elif _normalize_name_text(raw_name) != _normalize_name_text(canonical_name):
                final_name = canonical_name
                name_corrected += 1
                derived_fields.append("name")
        if not final_name:
            missing_name_count += 1

        industry = str(item.get("industry") or "").strip()
        if not industry and canonical_industry:
            industry = canonical_industry
            industry_backfilled += 1
            derived_fields.append("industry")

        missing_fields = []
        if not final_name:
            missing_fields.append("name")
        if item.get("price") is None:
            missing_fields.append("price")
        if item.get("volume") is None:
            missing_fields.append("volume")
        if item.get("amount") is None:
            missing_fields.append("amount")

        results.append(
            {
                "date": item.get("date"),
                "code": code,
                "name": final_name,
                "industry": industry,
                "price": item.get("price"),
                "volume": item.get("volume"),
                "amount": item.get("amount"),
                "premium": item.get("premium"),
                "buyer": item.get("buyer"),
                "seller": item.get("seller"),
                "dataQuality": {
                    "source": "eastmoney_block_trade",
                    "derived_fields": derived_fields,
                    "missing_fields": missing_fields,
                },
            }
        )

    data_quality = {
        "count": len(results),
        "name_backfilled_count": name_backfilled,
        "name_corrected_count": name_corrected,
        "industry_backfilled_count": industry_backfilled,
        "missing_name_count": missing_name_count,
        "code_mismatch_count": code_mismatch_count,
        "fallback_used": len(source_chain) > 1,
        "source_chain": source_chain,
        "fallback_reason": [str(item).strip() for item in list(fallback_reason or []) if str(item).strip()],
    }
    return results, data_quality

def get_block_trades(date: str = "", stock_code: str = "", limit: int = 500) -> dict:
    """获取大宗交易数据"""
    try:
        limiter = get_limiter("fund_flow", max_calls=3, period=1.0)
        limiter.acquire()
        limit, limit_error = validate_int_range(limit, field_name="limit", minimum=1, maximum=1000)
        if limit_error:
            response = fail("limit 必须为正整数")
            response["source"] = "none"
            return _with_provider_contract(
                response,
                "get_block_trades",
                standard_model="BlockTrades",
                provider_used="none",
                fallback_reason=limit_error,
            )
        if date and parse_date_input(date) is None:
            response = fail(f"date 无效: {date}")
            response["source"] = "none"
            return _with_provider_contract(
                response,
                "get_block_trades",
                standard_model="BlockTrades",
                provider_used="none",
                fallback_reason=f"invalid date: {date}",
            )

        target = date or datetime.now().strftime("%Y-%m-%d")
        if stock_code:
            code, _, code_error = resolve_existing_security_code_sync(stock_code=stock_code)
            if code_error:
                response = fail(code_error)
                response["source"] = "none"
                return _with_provider_contract(
                    response,
                    "get_block_trades",
                    standard_model="BlockTrades",
                    provider_used="none",
                    fallback_reason=code_error,
                )
        else:
            code = ""
        source_chain = ["eastmoney.block_trades"]
        fallback_reason: list[str] = []

        def _fetch_for_trade_date(trade_date: str) -> list[dict]:
            params: dict[str, Any] = {
                "sortColumns": "SECURITY_CODE",
                "sortTypes": "1",
                "pageSize": min(max(int(limit), 1), 1000),
                "pageNumber": 1,
                "reportName": "RPT_DATA_BLOCKTRADE",
                "columns": (
                    "TRADE_DATE,SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,CHANGE_RATE,"
                    "CLOSE_PRICE,DEAL_PRICE,PREMIUM_RATIO,DEAL_VOLUME,DEAL_AMT,TURNOVER_RATE,"
                    "BUYER_NAME,SELLER_NAME,CHANGE_RATE_1DAYS,CHANGE_RATE_5DAYS,"
                    "CHANGE_RATE_10DAYS,CHANGE_RATE_20DAYS,BUYER_CODE,SELLER_CODE"
                ),
                "source": "WEB",
                "client": "WEB",
                "filter": f"(SECURITY_TYPE_WEB=1)(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')",
            }
            if code:
                params["filter"] += f'(SECURITY_CODE="{code}")'
            return _fetch_eastmoney_datacenter(params)

        items = _fetch_for_trade_date(target)
        if code and not date and not items:
            for offset in range(1, 31):
                fallback_date = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
                items = _fetch_for_trade_date(fallback_date)
                if items:
                    source_chain.append("eastmoney.block_trades.backtrack")
                    fallback_reason.append(f"no block trades on {target}, fallback to {fallback_date}")
                    break
        results = []
        for item in items:
            security_code = normalize_code(item.get("SECURITY_CODE"))
            secucode = normalize_code(str(item.get("SECUCODE", "")).split(".")[0] if item.get("SECUCODE") else "")
            normalized_code = security_code or secucode
            results.append(
                {
                    "date": str(item.get("TRADE_DATE", "")).split(" ")[0],
                    "code": normalized_code,
                    "alt_code": secucode,
                    "name": str(item.get("SECURITY_NAME_ABBR") or ""),
                    "price": parse_numeric(item.get("DEAL_PRICE")),
                    "volume": parse_numeric(item.get("DEAL_VOLUME")),
                    "amount": parse_numeric(item.get("DEAL_AMT")),
                    "premium": parse_numeric(item.get("PREMIUM_RATIO")),
                    "buyer": str(item.get("BUYER_NAME") or ""),
                    "seller": str(item.get("SELLER_NAME") or ""),
                }
            )
        # P3-5.3 fix: 同股同价拆单聚合 cluster_id(诊断报告 §5.3)
        # 历史问题:同一笔大宗交易因量太大被拆成多笔(date+code+price 一致),AI 误判成多笔不同交易
        cluster_groups: dict[tuple[str, str, float], list[dict]] = {}
        for trade in results:
            try:
                price_key = round(float(trade.get("price") or 0.0), 4)
            except (TypeError, ValueError):
                price_key = 0.0
            cluster_key = (
                str(trade.get("date") or ""),
                str(trade.get("code") or ""),
                price_key,
            )
            cluster_groups.setdefault(cluster_key, []).append(trade)
        cluster_id_counter = 0
        for cluster_key, trades_in_group in cluster_groups.items():
            if len(trades_in_group) <= 1:
                continue
            cluster_id_counter += 1
            cluster_id = f"cluster_{cluster_key[1]}_{cluster_key[0]}_{cluster_id_counter:03d}"
            total_volume = sum(float(t.get("volume") or 0) for t in trades_in_group)
            total_amount = sum(float(t.get("amount") or 0) for t in trades_in_group)
            for t in trades_in_group:
                t["cluster_id"] = cluster_id
                t["cluster_size"] = len(trades_in_group)
                t["cluster_total_volume"] = total_volume
                t["cluster_total_amount"] = total_amount
        finalized, data_quality = _finalize_block_trade_results(
            results,
            source_chain=source_chain,
            fallback_reason=fallback_reason,
        )
        response = ok(finalized)
        response["source"] = "eastmoney_block_trade"
        response["source_chain"] = source_chain
        if fallback_reason:
            response["fallback_reason"] = fallback_reason
        response["degraded"] = bool(data_quality.get("missing_name_count")) or bool(data_quality.get("code_mismatch_count"))
        response["data_quality"] = data_quality
        return _with_provider_contract(
            response,
            "get_block_trades",
            standard_model="BlockTrades",
            provider_used="eastmoney_block_trade",
            source_chain=source_chain,
            fallback_reason=fallback_reason or None,
            quality=data_quality,
            data_timestamp=finalized[0].get("date") if finalized else target,
        )
    except Exception as e:
        return _with_provider_contract(
            fail(e),
            "get_block_trades",
            standard_model="BlockTrades",
            provider_used="none",
            fallback_reason=str(e),
        )
