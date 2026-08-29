"""涨停板数据模块 — 数据源: Tushare stk_limit + daily 组合

代理 stk_limit 仅返回 5 个字段 (ts_code, trade_date, pre_close, up_limit, down_limit)，
缺少 close/pct_chg/limit/name/lu_count。

解决方案：
1. 用 stk_limit 获取涨停价 (up_limit) 和昨收 (pre_close)
2. 用 daily 获取当日 close/pct_chg
3. 通过 close == up_limit 判断涨停
4. 用 stock_basic 补全名称
"""

import logging
import sqlite3
from datetime import datetime

import pandas as pd
import requests
try:
    import akshare as ak
except ImportError:
    ak = None

from ..market.helpers import (
    normalize_code, parse_numeric, pick_value, parse_date_input,
    ok, fail
)
from ...core.cache_manager import cached
from ...core.rate_limiter import get_limiter
from ...data_source import data_source
from ...storage.sqlite.schema_base import default_sqlite_path

logger = logging.getLogger(__name__)
_LIMIT_UP_TRACKED_FIELDS = (
    "name",
    "firstLimitTime",
    "lastLimitTime",
    "openTimes",
    "continuousDays",
    "turnoverRate",
    "marketCap",
    "industry",
)


def _format_trade_date(check_date: str) -> str:
    text = str(check_date or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _read_tdx_limit_up_from_sqlite(target_iso: str) -> list[dict]:
    path = default_sqlite_path()
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT t.code, s.stock_name AS name, s.tdx_industry AS industry,
                   t.up_limit AS limitUpPrice, t.fc_amo AS fcAmo,
                   t.ever_zt_count AS continuousDays,
                   t.turnover_rate AS turnoverRate,
                   t.zsz AS marketCap,
                   t.zaf AS changePercent,
                   t.trade_date
            FROM tdx_stock_extra t
            LEFT JOIN stocks s ON s.stock_code = t.code
            WHERE t.trade_date = ?
              AND t.fc_amo IS NOT NULL
              AND t.fc_amo > 0
            ORDER BY t.fc_amo DESC
            """,
            (target_iso,),
        ).fetchall()
        return [dict(row) for row in rows or []]
    except Exception:
        return []
    finally:
        conn.close()


def _normalize_limit_time(value) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "nan", "--"}:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 6:
        return f"{digits[:2]}:{digits[2:4]}:{digits[4:6]}"
    return text


def _get_limit_up_stocks_from_akshare(check_date: str) -> list[dict]:
    if ak is None or not hasattr(ak, "stock_zt_pool_em"):
        return []
    try:
        df = ak.stock_zt_pool_em(date=check_date)
    except Exception:
        return []
    if df is None or df.empty:
        return []

    trade_date = _format_trade_date(check_date)
    results: list[dict] = []
    for _, row in df.iterrows():
        code = normalize_code(pick_value(row, ["代码", "code"]))
        if not code:
            continue
        latest_price = parse_numeric(pick_value(row, ["最新价", "price"])) or 0.0
        total_mv = parse_numeric(pick_value(row, ["总市值", "marketCap"]))
        float_mv = parse_numeric(pick_value(row, ["流通市值"]))
        market_cap = total_mv if total_mv is not None else (float_mv or 0.0)
        continuous_days = parse_numeric(pick_value(row, ["连板数", "连续涨停天数"])) or 0.0
        results.append({
            "code": code,
            "name": str(pick_value(row, ["名称", "name"]) or ""),
            "price": float(latest_price),
            "changePercent": float(parse_numeric(pick_value(row, ["涨跌幅", "changePercent"])) or 0.0),
            "limitUpPrice": float(latest_price),
            "firstLimitTime": _normalize_limit_time(pick_value(row, ["首次封板时间"])),
            "lastLimitTime": _normalize_limit_time(pick_value(row, ["最后封板时间"])),
            "openTimes": int(parse_numeric(pick_value(row, ["炸板次数", "openTimes"])) or 0),
            "continuousDays": int(continuous_days or 0),
            "turnoverRate": float(parse_numeric(pick_value(row, ["换手率", "turnoverRate"])) or 0.0),
            "marketCap": float(market_cap or 0.0),
            "industry": str(pick_value(row, ["所属行业", "industry"]) or ""),
            "concept": "",
            "tradeDate": trade_date,
            "_derived_fields": set(),
        })
    return results


def _mark_limit_up_field(item: dict, field: str, value) -> None:
    item[field] = value
    if field != "continuousDays" or value is not None:
        item.setdefault("_derived_fields", set()).add(field)


def _finalize_limit_up_response(
    results: list[dict],
    *,
    source_used: str,
    source_chain: list[str],
    fallback_reason: list[str] | None = None,
):
    derived_field_counts: dict[str, int] = {}
    missing_field_counts: dict[str, int] = {}

    for item in results:
        derived_fields = sorted(str(field) for field in item.pop("_derived_fields", set()) if str(field))
        missing_fields: list[str] = []
        for field in _LIMIT_UP_TRACKED_FIELDS:
            value = item.get(field)
            if value is None or value == "":
                missing_fields.append(field)
                missing_field_counts[field] = missing_field_counts.get(field, 0) + 1
        for field in derived_fields:
            derived_field_counts[field] = derived_field_counts.get(field, 0) + 1
        item["dataQuality"] = {
            "source": source_used,
            "derived_fields": derived_fields,
            "missing_fields": missing_fields,
        }

    response = ok(results)
    response["source"] = source_used
    response["source_chain"] = [str(item).strip() for item in source_chain if str(item).strip()]
    reasons = [str(item).strip() for item in list(fallback_reason or []) if str(item).strip()]
    if reasons:
        response["fallback_reason"] = reasons
    response["degraded"] = bool(missing_field_counts)
    response["data_quality"] = {
        "count": len(results),
        "source_used": source_used,
        "source_chain": response["source_chain"],
        "derived_field_counts": derived_field_counts,
        "missing_field_counts": missing_field_counts,
        "fallback_used": len(response["source_chain"]) > 1,
    }
    return response


def _tushare_http_call(api_name: str, params: dict | None = None, fields: str = ""):
    """直接通过 HTTP 调用 Tushare 代理 API"""
    http_url = data_source.get_tushare_http_url()
    token = getattr(data_source, "tushare_token", "")
    if not http_url or not token:
        return None
    try:
        payload = {"api_name": api_name, "token": token, "params": params or {}, "fields": fields}
        resp = requests.post(http_url, json=payload, timeout=30)
        result = resp.json()
        if result.get("code") != 0:
            return None
        data = result.get("data", {})
        if data:
            return pd.DataFrame(data.get("items", []), columns=data.get("fields", []))
    except Exception:
        pass
    return None


def _get_name_map() -> dict:
    """获取 ts_code → name 映射"""
    try:
        df = _tushare_http_call("stock_basic", {"exchange": "", "list_status": "L"}, fields="ts_code,name")
        if df is not None and not df.empty:
            name_map = {}
            for _, row in df.iterrows():
                code = str(row.get("ts_code", "")).split(".")[0]
                name = str(row.get("name", "") or "")
                if code and name:
                    name_map[code] = name
            return name_map
    except Exception:
        pass
    return {}


@cached(ttl=300.0)
def get_limit_up_stocks(date: str = "") -> dict:
    """获取涨停板数据。

    策略：stk_limit (涨停价) + daily (收盘价/涨跌幅) 组合判断涨停。

    数据源优先级: Tushare Pro (stk_limit + daily + stock_basic)
    时效性: 日频，通常 T+1 发布；自动回溯最近 10 个交易日

    Args:
        date (str, optional): 日期，格式 YYYY-MM-DD 或 YYYYMMDD，默认最近交易日

    Returns:
        dict: {"success": bool, "data": list[dict]}
        每项含:
        - code (str): 股票代码
        - name (str): 股票名称
        - price (float): 收盘价/涨停价
        - changePercent (float): 涨跌幅(%)
        - limitUpPrice (float): 涨停价
        - continuousDays (int): 连板天数
        - openTimes (int): 开板次数
        - turnoverRate (float): 换手率
        - marketCap (float): 市值
        - industry (str): 所属行业

    Errors:
        - Tushare 数据源不可用或指定日期无数据时返回 success=true 但 data 为空列表

    Examples:
        get_limit_up_stocks()
        get_limit_up_stocks("2026-01-15")
    """
    from datetime import timedelta
    limiter = get_limiter("quote", max_calls=5, period=1.0)
    limiter.acquire()

    target_date = parse_date_input(date) if date else datetime.now().date()
    if date and target_date is None:
        return fail(f"date 无效: {date}")

    # 0. tqcenter 主路径：从 tdx_stock_extra 表读取涨停封单 (FCAmo > 0)
    # 该表由 sync_more_info 每日填充，含 EverZTCount(连板天)、ZTPrice(涨停价)。
    try:
        from ...storage import get_db, run_with_db_cleanup

        target_iso = target_date.strftime("%Y-%m-%d")

        async def _read_tdx_zt(target_iso: str) -> list[dict]:
            db = get_db()
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT t.code, s.stock_name AS name, s.tdx_industry AS industry,
                           t.up_limit AS limitUpPrice, t.fc_amo AS fcAmo,
                           t.ever_zt_count AS continuousDays,
                           t.turnover_rate AS turnoverRate,
                           t.zsz AS marketCap,
                           t.zaf AS changePercent,
                           t.trade_date
                    FROM tdx_stock_extra t
                    LEFT JOIN stocks s ON s.stock_code = t.code
                    WHERE t.trade_date = $1
                      AND t.fc_amo IS NOT NULL
                      AND t.fc_amo > 0
                    ORDER BY t.fc_amo DESC
                    """,
                    target_iso,
                )
            return [dict(r) for r in rows]

        rows = _read_tdx_limit_up_from_sqlite(target_iso)
        if rows:
            results = []
            for r in rows:
                results.append({
                    "code": r.get("code"),
                    "name": r.get("name") or "",
                    "price": r.get("limitUpPrice"),
                    "changePercent": r.get("changePercent"),
                    "limitUpPrice": r.get("limitUpPrice"),
                    "continuousDays": int(r.get("continuousDays") or 0),
                    "openTimes": 0,  # tqcenter snapshot 不直接给开板次数；需 GP14
                    "turnoverRate": r.get("turnoverRate"),
                    "marketCap": r.get("marketCap"),
                    "industry": r.get("industry") or "",
                })
            return ok({
                "date": target_iso,
                "stocks": results,
                "count": len(results),
                "source": "tqcenter.tdx_stock_extra",
            })
    except Exception as exc:
        from ...utils import safe_stderr_print
        safe_stderr_print(f"[limit_up] tqcenter path failed: {exc}")

    fallback_reason: list[str] = []
    # 尝试最近 10 个交易日
    for days_back in range(10):
        check_date = (target_date - timedelta(days=days_back)).strftime("%Y%m%d")
        results: list[dict] = []
        source_chain = ["tushare.stk_limit", "tushare.daily"]

        # 1) 获取 stk_limit（涨停价/跌停价/昨收）
        limit_df = _tushare_http_call("stk_limit", {"trade_date": check_date})
        if limit_df is not None and not limit_df.empty:
            # 2) 获取 daily（收盘价/涨跌幅/成交量）
            daily_df = _tushare_http_call(
                "daily",
                {"trade_date": check_date},
                fields="ts_code,trade_date,open,high,low,close,pct_chg,vol,amount",
            )
            if daily_df is not None and not daily_df.empty:
                # 3) 合并两个 DataFrame
                merged = pd.merge(limit_df, daily_df, on=["ts_code", "trade_date"], how="inner")

                if not merged.empty:
                    # 4) 筛选涨停：close >= up_limit（允许 0.01 误差）
                    merged["close_f"] = pd.to_numeric(merged["close"], errors="coerce").fillna(0)
                    merged["up_limit_f"] = pd.to_numeric(merged["up_limit"], errors="coerce").fillna(0)
                    merged["pre_close_f"] = pd.to_numeric(merged.get("pre_close", pd.Series(dtype=float)), errors="coerce").fillna(0)
                    merged["pct_chg_f"] = pd.to_numeric(merged.get("pct_chg", pd.Series(dtype=float)), errors="coerce").fillna(0)

                    up_mask = (merged["up_limit_f"] > 0) & (merged["close_f"] >= merged["up_limit_f"] - 0.01)
                    up_df = merged[up_mask]

                    # 5) 构建结果
                    for _, row in up_df.iterrows():
                        ts_code = str(row.get("ts_code", ""))
                        code = ts_code.split(".")[0] if ts_code else ""
                        if not code:
                            continue

                        close_price = float(row.get("close_f", 0))
                        up_limit_price = float(row.get("up_limit_f", 0))
                        pct_chg = float(row.get("pct_chg_f", 0))
                        price = close_price or up_limit_price

                        results.append({
                            "code": normalize_code(code),
                            "name": "",  # 后面批量补全
                            "price": price,
                            "changePercent": pct_chg,
                            "limitUpPrice": up_limit_price or price,
                            "firstLimitTime": "",
                            "lastLimitTime": "",
                            "openTimes": None,
                            "continuousDays": None,  # 后面尝试计算
                            "turnoverRate": None,
                            "marketCap": None,
                            "industry": "",
                            "concept": "",
                            "tradeDate": _format_trade_date(check_date),
                            "_derived_fields": set(),
                        })

                    if results:
                        # 6) 批量补全名称
                        name_map = _get_name_map()
                        if name_map:
                            source_chain.append("tushare.stock_basic")
                        if name_map:
                            for r in results:
                                name = name_map.get(r["code"], "")
                                if name:
                                    _mark_limit_up_field(r, "name", name)

                        # 6.5) 补全 turnoverRate / industry 从 daily_basic
                        try:
                            basic_df = _tushare_http_call(
                                "daily_basic",
                                {"trade_date": check_date},
                                fields="ts_code,turnover_rate,pe,total_mv",
                            )
                            if basic_df is not None and not basic_df.empty:
                                source_chain.append("tushare.daily_basic")
                                basic_map = {}
                                for _, brow in basic_df.iterrows():
                                    bc = str(brow.get("ts_code", "")).split(".")[0]
                                    if bc:
                                        basic_map[bc] = brow
                                for r in results:
                                    brow = basic_map.get(r["code"])
                                    if brow is not None:
                                        tr = parse_numeric(brow.get("turnover_rate"))
                                        if tr is not None:
                                            _mark_limit_up_field(r, "turnoverRate", float(tr))
                                        mv = parse_numeric(brow.get("total_mv"))
                                        if mv is not None:
                                            _mark_limit_up_field(r, "marketCap", float(mv))
                        except Exception as e:
                            fallback_reason.append(f"tushare.daily_basic failed: {e}")

                        # 6.6) 补全 industry 从 stock_basic
                        try:
                            industry_df = _tushare_http_call(
                                "stock_basic",
                                {"exchange": "", "list_status": "L"},
                                fields="ts_code,industry",
                            )
                            if industry_df is not None and not industry_df.empty:
                                if "tushare.stock_basic" not in source_chain:
                                    source_chain.append("tushare.stock_basic")
                                ind_map = {}
                                for _, irow in industry_df.iterrows():
                                    ic = str(irow.get("ts_code", "")).split(".")[0]
                                    if ic:
                                        ind_map[ic] = str(irow.get("industry", "") or "")
                                for r in results:
                                    ind = ind_map.get(r["code"], "")
                                    if ind:
                                        _mark_limit_up_field(r, "industry", ind)
                        except Exception as e:
                            fallback_reason.append(f"tushare.stock_basic(industry) failed: {e}")

                        # 7) 尝试计算连板天数（往前查最多 10 天）
                        _fill_continuous_days(results, check_date)
                        if results:
                            return _finalize_limit_up_response(
                                results,
                                source_used="tushare_combo",
                                source_chain=source_chain,
                                fallback_reason=fallback_reason,
                            )

        if not results:
            results = _get_limit_up_stocks_from_akshare(check_date)
            if results:
                if days_back > 0:
                    fallback_reason.append(f"tushare_combo empty for {check_date}, fallback to akshare")
                return _finalize_limit_up_response(
                    results,
                    source_used="akshare_zt_pool",
                    source_chain=["akshare.stock_zt_pool_em"],
                    fallback_reason=fallback_reason,
                )

    response = ok([])
    response["source"] = "none"
    response["source_chain"] = ["tushare.stk_limit", "tushare.daily", "akshare.stock_zt_pool_em"]
    if fallback_reason:
        response["fallback_reason"] = fallback_reason
    response["degraded"] = True
    response["data_quality"] = {
        "count": 0,
        "source_used": "none",
        "source_chain": response["source_chain"],
        "derived_field_counts": {},
        "missing_field_counts": {},
        "fallback_used": True,
    }
    return response


def _fill_continuous_days(results: list[dict], current_date: str):
    """通过往前查 stk_limit + daily 计算连板天数。"""
    from datetime import timedelta

    if not results:
        return

    # 收集需要查连板的股票代码
    code_set = {r["code"] for r in results}
    # 记录每只股票的连板天数（当天已涨停 = 至少 1 天）
    cont_days = {code: 1 for code in code_set}

    base = datetime.strptime(current_date, "%Y%m%d").date()

    # 往前查最多 10 个自然日（约 7 个交易日）
    for days_back in range(1, 11):
        if not code_set:
            break
        prev_date = (base - timedelta(days=days_back)).strftime("%Y%m%d")

        limit_df = _tushare_http_call("stk_limit", {"trade_date": prev_date})
        if limit_df is None or limit_df.empty:
            continue

        daily_df = _tushare_http_call("daily", {"trade_date": prev_date}, fields="ts_code,close")
        if daily_df is None or daily_df.empty:
            continue

        merged = pd.merge(limit_df, daily_df, on="ts_code", how="inner")
        if merged.empty:
            continue

        merged["close_f"] = pd.to_numeric(merged["close"], errors="coerce").fillna(0)
        merged["up_limit_f"] = pd.to_numeric(merged["up_limit"], errors="coerce").fillna(0)

        # 找出当天涨停的股票
        up_codes_today = set()
        for _, row in merged.iterrows():
            if row["up_limit_f"] > 0 and row["close_f"] >= row["up_limit_f"] - 0.01:
                c = str(row.get("ts_code", "")).split(".")[0]
                if c in code_set:
                    up_codes_today.add(c)

        # 更新连板天数
        still_continuous = set()
        for c in code_set:
            if c in up_codes_today:
                cont_days[c] += 1
                still_continuous.add(c)
        # 不再连板的股票不需要继续查
        code_set = still_continuous

    # 写回结果
    for r in results:
        _mark_limit_up_field(r, "continuousDays", cont_days.get(r["code"], 1))


@cached(ttl=300.0)
def get_limit_up_statistics(date: str = "") -> dict:
    """获取涨停统计数据

    Args:
        date (str, optional): 日期，格式 YYYY-MM-DD 或 YYYYMMDD，默认最近交易日

    Returns:
        dict: {"success": bool, "data": {...}}
        data 字段:
        - date (str): 统计日期
        - totalLimitUp (int): 涨停总数
        - firstBoard (int): 首板数量
        - secondBoard (int): 二连板数量
        - thirdBoard (int): 三连板数量
        - higherBoard (int): 四连板及以上数量
        - failedBoard (int): 炸板数量
        - limitDown (int): 跌停数量
        - successRate (float): 封板成功率(%)

    Errors:
        - 内部调用 get_limit_up_stocks 失败时透传其错误

    Examples:
        get_limit_up_statistics()
        get_limit_up_statistics("2026-01-15")
    """
    res = get_limit_up_stocks(date)
    if not res.get("success"):
        return res
    raw_data = res.get("data") or []

    def _normalize_stat_item(item) -> dict:
        if isinstance(item, dict):
            return item
        if isinstance(item, str):
            return {"code": item}
        try:
            return dict(item)
        except Exception:
            return {"raw": item}

    if isinstance(raw_data, dict):
        for key in ("stocks", "items", "data", "list", "results"):
            if isinstance(raw_data.get(key), list):
                raw_data = raw_data.get(key) or []
                break
        else:
            raw_data = [raw_data]
    elif not isinstance(raw_data, list):
        raw_data = [raw_data]

    data = [_normalize_stat_item(item) for item in raw_data]
    total = len(data)

    def _safe_int(value, default: int = 0) -> int:
        try:
            if value is None or value == "":
                return default
            return int(float(value))
        except Exception:
            return default

    def count_boards(target: int) -> int:
        return sum(1 for item in data if _safe_int(item.get("continuousDays"), 0) == target)

    higher = sum(1 for item in data if _safe_int(item.get("continuousDays"), 0) >= 4)
    failed = sum(1 for item in data if _safe_int(item.get("openTimes"), 0) > 0)
    denom = total + failed
    success_rate = (total / denom) * 100 if denom > 0 else 0

    target_date = next((str(item.get("tradeDate")) for item in data if item.get("tradeDate")), None)
    if not target_date:
        target_date = (parse_date_input(date) or datetime.now().date()).isoformat()

    result = {
        "date": target_date,
        "totalLimitUp": total,
        "firstBoard": count_boards(1) if total > 0 else 0,
        "secondBoard": count_boards(2),
        "thirdBoard": count_boards(3),
        "higherBoard": higher,
        "failedBoard": failed,
        "limitDown": 0,
        "successRate": round(success_rate, 2),
    }
    open_times_missing = 0
    if isinstance(res.get("data_quality"), dict):
        open_times_missing = int((res["data_quality"].get("missing_field_counts") or {}).get("openTimes", 0) or 0)

    response = ok(result)
    response["source"] = res.get("source", "akshare")
    response["source_chain"] = res.get("source_chain", [])
    if res.get("fallback_reason"):
        response["fallback_reason"] = res.get("fallback_reason")
    estimated_zero_fields = []
    if open_times_missing > 0 and failed == 0:
        estimated_zero_fields.append("failedBoard")
    response["degraded"] = bool(estimated_zero_fields) or bool(res.get("degraded"))
    response["data_quality"] = {
        "base_count": total,
        "open_times_missing_count": open_times_missing,
        "estimated_zero_fields": estimated_zero_fields,
        "fallback_used": len(response.get("source_chain") or []) > 1,
    }
    return response
