"""K线数据模块"""

import asyncio
import os
import re
import json
import requests
from datetime import datetime, timedelta
from typing import Optional
from ...market.helpers import (
    normalize_code, safe_float, safe_int, parse_date_input,
    run_with_retry as _run_with_retry,
    _parse_timeout_list as _parse_timeout_list,
    KLINE_TIMEOUTS as _KLINE_TIMEOUTS,
    ok, fail
)
from ....core.cache_manager import cached
from ....core.rate_limiter import get_limiter
from ....core.validators import validate_kline_list
from ....data_source import data_source
from ....provider_contracts import attach_provider_contract_meta
from ....storage import get_db
from ....utils import (
    attach_argument_contract_meta,
    resolve_canonical_arg,
    safe_stderr_print,
    validate_stock_code_format,
)
from ...data_quality import build_quality_meta, infer_missing_fields
try:
    from ...baostock_api import baostock_client
except (ImportError, Exception):
    baostock_client = None
try:
    import akshare as ak
except ImportError:
    ak = None
import pandas as pd

_KLINE_TOTAL_TIMEOUT = float(os.getenv("KLINE_TOTAL_TIMEOUT", "45"))
_MINUTE_KLINE_TIMEOUTS = _parse_timeout_list("AKSHARE_MINUTE_KLINE_TIMEOUTS", [4.0, 8.0])
_MINUTE_SINA_TIMEOUT = float(os.getenv("AKSHARE_MINUTE_SINA_TIMEOUT", "6"))


_SOFT_KLINE_FIELDS = frozenset({"turnover", "change_pct"})


_DB_STALE_DAYS = int(os.getenv("KLINE_DB_STALE_DAYS", "5"))

from .kline_rows import (
    _append_chain_step,
    _fail_kline_response,
    _has_validated_kline_rows,
    _is_db_data_fresh,
    _is_fund_like_code,
    _kline_missing_core_fields,
    _kline_missing_fields,
    _kline_row_date,
    _kline_rows_usable,
    _latest_kline_row,
    _ok_kline_response,
    _process_kline_akshare,
    _validated_kline_rows,
)
from .kline_minute import (
    _get_minute_kline_from_akshare,
    _get_minute_kline_from_sina,
    _get_minute_kline_from_tencent,
    _parse_minute_period,
)

@cached(ttl=3600.0)
async def get_kline(stock_code: str, period: str = "daily", limit: int = 100) -> dict:
    """获取股票/ETF历史K线数据（日线/周线/月线）

    适用场景: 趋势分析、波动率估算、回测数据准备、技术指标计算

    数据源优先级: SQLite → DataSource(Tushare/公开源) → AkShare → Tencent → Baostock
    时效性: 日线通常 T+0~T+1；周线/月线按自然周期更新

    Args:
        stock_code (str, required): 证券代码，6位数字，如 "600519"(贵州茅台)、"510050"(50ETF)
        period (str, optional): K线周期，可选 "daily"/"weekly"/"monthly"，默认 "daily"
        limit (int, optional): 返回条数，默认 100

    Returns:
        dict: {"success": bool, "data": list[dict], "error": str|None}
        data 每条记录包含: date(str), open(float), close(float), high(float), low(float),
        volume(int), amount(float), source(str)

    Errors:
        - 股票代码无效或所有数据源均不可用时返回 success=false

    Examples:
        get_kline("600519")
        get_kline("510050", period="weekly", limit=52)
    """
    started_at = datetime.now().astimezone()
    try:
        return await asyncio.wait_for(
            _get_kline_impl(stock_code, period, limit, started_at),
            timeout=_KLINE_TOTAL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        safe_stderr_print(f"[Kline] total timeout ({_KLINE_TOTAL_TIMEOUT}s) exceeded for {stock_code}")
        return _fail_kline_response(
            f"K线请求总超时（>{_KLINE_TOTAL_TIMEOUT}s），所有数据源均未在时限内响应",
            source_chain=["total_timeout"],
            fallback_reason=[f"total_timeout_exceeded:{_KLINE_TOTAL_TIMEOUT}s"],
            started_at=started_at,
        )


def _time_remaining(started_at: datetime) -> float:
    return _KLINE_TOTAL_TIMEOUT - (datetime.now().astimezone() - started_at).total_seconds()


async def _get_kline_impl(stock_code: str, period: str, limit: int, started_at: datetime) -> dict:
    """K 线获取核心逻辑，由 get_kline 在总超时内调用。"""
    limiter = get_limiter("kline", max_calls=5, period=1.0)
    limiter.acquire()

    raw_code = str(stock_code or "").strip()

    # P3-B1 fix: 指数代码路由(对话式复测发现)
    # 历史问题:get_kline("000001") 返回平安银行(close=10.68)而非上证指数(close~4112)
    # 根因:验证器只接受 6 位数字,sh000001 被拒;000001 同时是平安银行和上证指数代码,
    #      get_klines 默认按个股查询,无法区分。
    # 修复:
    #   1. 接受 sh000001 / sz399001 / 000001.sh / 000001.SH 等带前缀格式
    #   2. 当代码命中 _INDEX_AK_MAP(主流指数代码)时,自动调用 get_index_kline
    #   3. 保持 6 位纯数字(默认个股语义)向后兼容,不破坏既有调用
    # 调用方如想强制走指数路径,使用 sh000001 / get_index_kline 即可
    code_upper = raw_code.upper()
    has_market_prefix = code_upper.startswith(("SH", "SZ", "BJ"))
    has_market_suffix = code_upper.endswith((".SH", ".SZ", ".BJ"))
    pure_six_digit = re.fullmatch(r"\d{6}", raw_code) is not None

    # 抽取 6 位代码部分(无论前缀后缀)
    six_digit_match = re.search(r"(\d{6})", raw_code)
    six_digit = six_digit_match.group(1) if six_digit_match else None

    # 强信号:带前缀且代码命中指数表 → 必走 get_index_kline
    if six_digit and (has_market_prefix or has_market_suffix) and six_digit in _INDEX_AK_MAP:
        try:
            return await get_index_kline(six_digit, period=period, limit=limit)
        except Exception as exc_idx:
            # 如果 get_index_kline 内部失败,降级为 fail_response 而非误返个股数据
            return _fail_kline_response(
                f"指数 K 线获取失败: {exc_idx}",
                source_chain=["validate.index_route", "get_index_kline"],
                fallback_reason=[f"index_route_failed:{type(exc_idx).__name__}"],
                started_at=started_at,
            )

    # 弱信号:纯 6 位且命中指数表(如 "000001"/"399001")— 默认仍按个股(保持向后兼容)
    # 但在响应中加 ambiguity warning,提示调用方使用 sh000001 形式可强制指数路径
    ambiguity_warning: str | None = None
    if pure_six_digit and raw_code in _INDEX_AK_MAP:
        ambiguity_warning = (
            f"代码 {raw_code} 同时存在个股(平安银行/A股)和指数(上证/深证/创业板)语义，"
            f"当前默认按个股查询。如需指数 K 线，请使用 'sh{raw_code}' / 'sz{raw_code}' 或调用 get_index_kline。"
        )

    if not pure_six_digit:
        # 接受 sh000001 / 000001.SH / 等 → 抽 6 位继续按个股流程
        if six_digit and (has_market_prefix or has_market_suffix):
            raw_code = six_digit
        else:
            return _fail_kline_response(
                "股票代码格式无效，应为6位数字（或 sh000001/000001.SH 等带市场前缀格式）",
                source_chain=["validate.stock_code"],
                fallback_reason=["invalid_stock_code"],
                started_at=started_at,
            )

    code = normalize_code(raw_code)
    fallback_reason: list[str] = []
    if ambiguity_warning:
        fallback_reason.append(ambiguity_warning)
    source_chain: list[str] = ["db.get_klines"] if period == "daily" else []
    _db_fallback: Optional[list[dict]] = None

    # 0. DB 优先：查 SQLite（仅日线）
    if period == "daily":
        try:
            db = get_db()
            db_data = await db.get_klines(code, limit=limit)
            if db_data:
                validated_results = _validated_kline_rows(db_data)
                has_turnover = any(item.get('turnover') is not None for item in validated_results)

                # 新鲜度检查: DB 数据过期时 fall through 到 API
                db_is_fresh = _is_db_data_fresh(validated_results)

                if db_is_fresh and (has_turnover or (_is_fund_like_code(code) and _kline_rows_usable(validated_results))):
                    return _ok_kline_response(validated_results, source="sqlite", source_chain=["db.get_klines"], started_at=started_at)
                if _kline_rows_usable(validated_results):
                    _db_fallback = validated_results
                if not db_is_fresh:
                    safe_stderr_print(f"[Kline] DB data for {code} is stale (last date: {validated_results[-1].get('date', '?') if validated_results else '?'}), falling through to API")
                    fallback_reason.append("db.get_klines data stale, falling through to API")
                else:
                    safe_stderr_print(f"[Kline] DB data for {code} has null turnover, falling through to fetch complete data")
                    fallback_reason.append("db.get_klines missing turnover, falling through")
        except Exception as e_db:
            safe_stderr_print(f"SQLite K-line query failed for {code}: {e_db}")
            fallback_reason.append(f"db.get_klines failed: {e_db}")

    _ds_fallback: Optional[list] = None
    _append_chain_step(source_chain, "data_source.get_kline")

    # 1. DataSource（Tushare/公开源）— 放入线程避免阻塞事件循环
    if _time_remaining(started_at) > 3:
        try:
            ds_results = await asyncio.to_thread(data_source.get_kline, code, period, limit)
            if ds_results:
                validated_results = _validated_kline_rows(ds_results)
                ds_has_turnover = any(item.get('turnover') is not None for item in validated_results)
                await _async_save_klines_to_db(code, validated_results)
                if ds_has_turnover or (_is_fund_like_code(code) and _kline_rows_usable(validated_results)):
                    return _ok_kline_response(
                        validated_results,
                        source="data_source",
                        source_chain=list(source_chain),
                        fallback_reason=fallback_reason,
                        started_at=started_at,
                    )
                _ds_fallback = validated_results if _kline_rows_usable(validated_results) else None
                safe_stderr_print(f"[Kline] DataSource for {code} has no turnover, trying Baostock")
                fallback_reason.append("data_source.get_kline missing turnover, trying richer fallback")
        except Exception as e:
            safe_stderr_print(f"DataSource K-line fetch failed for {code}: {e}")
            fallback_reason.append(f"data_source.get_kline failed: {e}")
    else:
        fallback_reason.append("data_source.get_kline skipped: time budget exhausted")

    # 2. Baostock 优先降级（仅日线，包含换手率/涨跌幅）
    if period == "daily" and baostock_client is not None and _time_remaining(started_at) > 3:
        _append_chain_step(source_chain, "baostock.get_history_k_data")
        try:
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=limit * 1.5 + 30)).strftime("%Y-%m-%d")
            df_bs = await asyncio.to_thread(baostock_client.get_history_k_data, code, start_date, end_date)
            if not df_bs.empty:
                results = []
                for _, row in df_bs.tail(limit).iterrows():
                    results.append({
                        "date": row["date"],
                        "open": safe_float(row["open"]),
                        "close": safe_float(row["close"]),
                        "high": safe_float(row["high"]),
                        "low": safe_float(row["low"]),
                        "volume": safe_int(row["volume"]),
                        "amount": safe_float(row["amount"]),
                        "turnover": safe_float(row.get("turn")),
                        "change_pct": safe_float(row.get("pctChg")),
                        "source": "baostock"
                    })
                validated_results = _validated_kline_rows(results)
                await _async_save_klines_to_db(code, validated_results)
                return _ok_kline_response(
                    validated_results,
                    source="baostock",
                    source_chain=list(source_chain),
                    fallback_reason=fallback_reason,
                    started_at=started_at,
                )
        except Exception as e2:
            safe_stderr_print(f"Baostock K-line fetch failed for {code}: {e2}")
            fallback_reason.append(f"baostock.get_history_k_data failed: {e2}")
            if _ds_fallback:
                return _ok_kline_response(
                    _ds_fallback,
                    source="data_source",
                    source_chain=list(source_chain),
                    fallback_reason=fallback_reason,
                    started_at=started_at,
                )
            if _db_fallback:
                reasons = list(dict.fromkeys(fallback_reason + ["using db.get_klines partial data after richer fallbacks failed"]))
                return _ok_kline_response(
                    _db_fallback,
                    source="sqlite",
                    source_chain=list(source_chain),
                    fallback_reason=reasons,
                    started_at=started_at,
                )

    # 有备用数据时直接返回（时间预算不足 or Baostock 跳过）
    if _ds_fallback:
        return _ok_kline_response(
            _ds_fallback,
            source="data_source",
            source_chain=list(source_chain),
            fallback_reason=fallback_reason,
            started_at=started_at,
        )
    if _db_fallback:
        reasons = list(dict.fromkeys(fallback_reason + ["using db.get_klines partial data after richer fallbacks failed"]))
        return _ok_kline_response(
            _db_fallback,
            source="sqlite",
            source_chain=list(source_chain),
            fallback_reason=reasons,
            started_at=started_at,
        )

    # 3. AkShare 降级
    if ak is not None and _time_remaining(started_at) > 5:
        _append_chain_step(source_chain, "akshare.stock_zh_a_hist")
        try:
            df = await asyncio.to_thread(
                _run_with_retry,
                lambda: ak.stock_zh_a_hist(symbol=code, period=period, adjust="qfq"),
                _KLINE_TIMEOUTS,
            )
            if df is not None and not df.empty:
                df = df.tail(int(limit))
                results = _process_kline_akshare(df, code)
                if results:
                    validated_results = _validated_kline_rows(results)
                    await _async_save_klines_to_db(code, validated_results)
                    return _ok_kline_response(
                        validated_results,
                        source="akshare",
                        source_chain=list(source_chain),
                        fallback_reason=fallback_reason,
                        started_at=started_at,
                    )
        except Exception as e:
            safe_stderr_print(f"AkShare K-line fetch failed for {code}: {e}")
            fallback_reason.append(f"akshare.stock_zh_a_hist failed: {e}")

        # 3.5 Tencent K线（仅日线，最后备用）
        if period == "daily" and _time_remaining(started_at) > 3:
            _append_chain_step(source_chain, "tencent.stock_zh_a_hist_tx")
            try:
                end_date = datetime.now().strftime("%Y%m%d")
                start_date = (datetime.now() - timedelta(days=int(limit) * 2 + 30)).strftime("%Y%m%d")
                market_prefix = "sh" if code.startswith("6") else "sz"
                symbol = f"{market_prefix}{code}"
                df_tx = await asyncio.to_thread(
                    ak.stock_zh_a_hist_tx,
                    symbol=symbol, start_date=start_date, end_date=end_date,
                    adjust="", timeout=min(_KLINE_TIMEOUTS[-1] if _KLINE_TIMEOUTS else 25, _time_remaining(started_at)),
                )
                if df_tx is not None and not df_tx.empty:
                    results = []
                    for _, row in df_tx.tail(int(limit)).iterrows():
                        results.append({
                            "date": str(row.get("date", ""))[:10],
                            "open": safe_float(row.get("open")),
                            "close": safe_float(row.get("close")),
                            "high": safe_float(row.get("high")),
                            "low": safe_float(row.get("low")),
                            "volume": safe_int(row.get("volume")),
                            "amount": safe_float(row.get("amount")),
                            "source": "tencent",
                        })
                    if results:
                        validated_results = _validated_kline_rows(results)
                        await _async_save_klines_to_db(code, validated_results)
                        return _ok_kline_response(
                            validated_results,
                            source="tencent",
                            source_chain=list(source_chain),
                            fallback_reason=fallback_reason,
                            started_at=started_at,
                        )
            except Exception as e_tx:
                safe_stderr_print(f"Tencent K-line fetch failed for {code}: {e_tx}")
                fallback_reason.append(f"tencent.stock_zh_a_hist_tx failed: {e_tx}")

    return _fail_kline_response(
        f"所有数据源均无法获取 {code} 的K线数据",
        source_chain=list(source_chain),
        fallback_reason=fallback_reason,
        started_at=started_at,
    )


async def _async_save_klines_to_db(code: str, klines: list) -> None:
    """异步回写 K线数据到 SQLite（静默失败，不影响主流程）"""
    try:
        from ...services.data_sync import data_sync_service
        await data_sync_service._enqueue_save_task(code, klines)
    except Exception as e:
        safe_stderr_print(f"Async DB writeback failed for {code}: {e}")


@cached(ttl=60.0)
def get_minute_kline(
    code: str = "",
    period: str = "5m",
    limit: int = 300,
    *,
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
) -> dict:
    """获取分钟级K线数据（盘中实时）

    数据源优先级: AkShare → Sina
    时效性: 仅交易时段（9:30-15:00）有效，盘后数据为当日最后快照

    Args:
        stock_code (str, required): 证券代码，6位数字
        period (str, optional): 分钟周期，可选 "1m"/"5m"/"15m"/"30m"/"60m"，默认 "5m"
        limit (int, optional): 返回条数，默认 300

    Returns:
        dict: 与 get_kline 结构一致，date 字段包含时间部分（如 "2025-06-01 14:30:00"）

    Errors:
        - period 不在枚举范围内时返回 success=false

    Examples:
        get_minute_kline("600519")
        get_minute_kline("000001", period="15m", limit=100)
    """
    limiter = get_limiter("kline", max_calls=5, period=1.0)
    limiter.acquire()
    started_at = datetime.now().astimezone()

    raw_code, alias_hits, _ = resolve_canonical_arg(
        "code",
        code,
        stock_code=stock_code,
        symbol=symbol,
        ticker=ticker,
    )
    code = normalize_code(raw_code)
    canonical_args = {"code": code, "period": period, "limit": limit}
    def _respond(payload: dict) -> dict:
        return attach_argument_contract_meta(
            payload,
            canonical_tool="get_minute_kline",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )

    minutes = _parse_minute_period(period)
    if minutes is None:
        return _respond(_fail_kline_response(
            "period 必须为 1m/5m/15m/30m/60m",
            source_chain=["validate.period"],
            fallback_reason=["invalid_period"],
            started_at=started_at,
        ))
    fallback_reason: list[str] = []
    source_chain: list[str] = ["akshare.stock_zh_a_hist_min_em"]
    results = _get_minute_kline_from_akshare(code, minutes, limit)
    if not results:
        fallback_reason.append("akshare.stock_zh_a_hist_min_em empty_or_failed")
        _append_chain_step(source_chain, "sina.getKLineData")
        results = _get_minute_kline_from_sina(code, minutes, limit)
        if not results:
            fallback_reason.append("sina.getKLineData empty_or_failed")
            # P3-5.4 fix: tencent 第三源(诊断报告 §5.4)
            _append_chain_step(source_chain, "tencent.mkline")
            results = _get_minute_kline_from_tencent(code, minutes, limit)
            if not results:
                fallback_reason.append("tencent.mkline empty_or_failed")

    if not results:
        return _respond(_fail_kline_response(
            f"所有数据源均无法获取 {code} 的{minutes}分钟K线数据",
            source_chain=list(source_chain),
            fallback_reason=fallback_reason,
            started_at=started_at,
        ))

    validated_results = _validated_kline_rows(results)
    if not _has_validated_kline_rows(validated_results):
        return _respond(_fail_kline_response(
            f"所有数据源均返回了无效的 {code} {minutes}分钟K线数据",
            source_chain=list(source_chain),
            fallback_reason=fallback_reason + ["all_intraday_rows_rejected"],
            started_at=started_at,
        ))
    resolved_source_raw = str((validated_results or [{}])[0].get("source") or "")
    if resolved_source_raw.startswith("akshare"):
        resolved_source = "akshare_minute"
    elif resolved_source_raw == "tencent":
        # P3-5.4: tencent 显式标识
        resolved_source = "tencent"
    else:
        resolved_source = "sina"
    return _respond(_ok_kline_response(validated_results, source=resolved_source, source_chain=list(source_chain), fallback_reason=fallback_reason, started_at=started_at))


async def get_kline_data(
    code: str = "",
    period: str = "daily",
    start_date: str = None,
    end_date: str = None,
    limit: int = 30,
    adjust: str = "",
    *,
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
) -> dict:
    """获取K线数据（Node.js 参数兼容入口，支持日期区间查询）

    与 get_kline 的区别: 支持 start_date/end_date 日期区间过滤和复权类型选择。
    Node 兼容映射: 已作为独立工具注册；无日期参数时内部 fallback 到 get_kline。

    数据源优先级: SQLite → DataSource(Tushare/公开源) → AkShare → Tencent → Baostock

    Args:
        code (str, required): 股票/ETF 代码，6位数字，如 "600519"、"510050"
        period (str, optional): K线周期，可选 "daily"/"weekly"/"monthly"/"1m"/"5m"/"15m"/"30m"/"60m"，默认 "daily"
        start_date (str, optional): 开始日期，格式 "YYYY-MM-DD" 或 "YYYYMMDD"
        end_date (str, optional): 结束日期，格式 "YYYY-MM-DD" 或 "YYYYMMDD"
        limit (int, optional): 未指定日期区间时生效，默认 30
        adjust (str, optional): 复权类型，""(不复权) / "qfq"(前复权) / "hfq"(后复权)，默认 ""

    参数依赖:
        - 当 start_date/end_date 存在时，按日期区间过滤，limit 不生效
        - 当 start_date/end_date 均为空时，按 limit 返回最近 N 条

    Returns:
        dict: 与 get_kline 返回结构一致

    Examples:
        get_kline_data("600519", start_date="2025-01-01", end_date="2025-06-30")
        get_kline_data("000001", period="weekly", limit=50, adjust="qfq")
    """
    period_map = {
        'daily': 'daily',
        'weekly': 'weekly',
        'monthly': 'monthly',
        '101': 'daily',
        '102': 'weekly',
        '103': 'monthly',
        '1m': '1',
        '5m': '5',
        '15m': '15',
        '30m': '30',
        '60m': '60',
    }
    valid_periods = set(period_map)
    raw_code, alias_hits, _ = resolve_canonical_arg(
        "code",
        code,
        stock_code=stock_code,
        symbol=symbol,
        ticker=ticker,
    )
    code = str(raw_code or "")
    canonical_args = {"code": normalize_code(code) if code else "", "period": period, "start_date": start_date, "end_date": end_date, "limit": limit, "adjust": adjust}

    def _respond(payload: dict) -> dict:
        return attach_argument_contract_meta(
            payload,
            canonical_tool="get_kline_data",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )

    if str(period or "").strip() not in valid_periods:
        return _respond(_fail_kline_response(
            f"period 无效: {period}. 支持: {', '.join(sorted(valid_periods))}",
            source_chain=["validate.period"],
            fallback_reason=["invalid_period"],
        ))

    mapped_period = period_map.get(period, period)
    started_at = datetime.now().astimezone()
    _, code_error = validate_stock_code_format(code)
    if code_error:
        return _respond(_fail_kline_response(
            code_error,
            source_chain=["validate.stock_code"],
            fallback_reason=["invalid_stock_code"],
            started_at=started_at,
        ))
    if start_date and parse_date_input(start_date) is None:
        return _respond(_fail_kline_response(
            f"start_date 无效: {start_date}",
            source_chain=["validate.start_date"],
            fallback_reason=["invalid_start_date"],
            started_at=started_at,
        ))
    if end_date and parse_date_input(end_date) is None:
        return _respond(_fail_kline_response(
            f"end_date 无效: {end_date}",
            source_chain=["validate.end_date"],
            fallback_reason=["invalid_end_date"],
            started_at=started_at,
        ))
    
    if start_date or end_date:
        limiter = get_limiter("kline", max_calls=5, period=1.0)
        limiter.acquire()

        code_normalized = normalize_code(code)
        fallback_reason: list[str] = []
        source_chain: list[str] = []

        # 0. DB 优先：查 SQLite（日期区间）
        if mapped_period == "daily":
            _append_chain_step(source_chain, "db.get_klines")
            try:
                db = get_db()
                sd_norm = (start_date or "").replace("-", "")[:8]
                ed_norm = (end_date or "").replace("-", "")[:8]
                sd_fmt = f"{sd_norm[:4]}-{sd_norm[4:6]}-{sd_norm[6:8]}" if len(sd_norm) >= 8 else None
                ed_fmt = f"{ed_norm[:4]}-{ed_norm[4:6]}-{ed_norm[6:8]}" if len(ed_norm) >= 8 else None
                db_data = await db.get_klines(code_normalized, start_date=sd_fmt, end_date=ed_fmt)
                if db_data:
                    validated_results = _validated_kline_rows(db_data)
                    if _has_validated_kline_rows(validated_results):
                        return _respond(_ok_kline_response(validated_results, source="sqlite", source_chain=list(source_chain), started_at=started_at))
            except Exception as e_db:
                safe_stderr_print(f"SQLite K-line (date range) query failed for {code_normalized}: {e_db}")
                fallback_reason.append(f"db.get_klines failed: {e_db}")

        # 1. DataSource 优先：Tushare / 公开源
        _append_chain_step(source_chain, "data_source.get_kline")
        try:
            from datetime import datetime as _dt
            start_d = (start_date or "").replace("-", "")[:8] or "19900101"
            end_d = (end_date or "").replace("-", "")[:8] or _dt.now().strftime("%Y%m%d")
            if start_d > end_d:
                start_d, end_d = end_d, start_d
            limit_est = min(500, max(100, (_dt.strptime(end_d, "%Y%m%d") - _dt.strptime(start_d, "%Y%m%d")).days + 50))
            ds_results = data_source.get_kline(code_normalized, mapped_period, limit_est)
            if ds_results:
                start_norm = start_d[:4] + "-" + start_d[4:6] + "-" + start_d[6:8]
                end_norm = end_d[:4] + "-" + end_d[4:6] + "-" + end_d[6:8]
                filtered = [r for r in ds_results if start_norm <= (r.get("date") or "")[:10] <= end_norm]
                if filtered:
                    validated_results = _validated_kline_rows(filtered)
                    if _has_validated_kline_rows(validated_results):
                        await _async_save_klines_to_db(code_normalized, validated_results)
                        return _respond(_ok_kline_response(
                            validated_results,
                            source="data_source",
                            source_chain=list(source_chain),
                            fallback_reason=fallback_reason,
                            started_at=started_at,
                        ))
        except Exception as e_ds:
            safe_stderr_print(f"DataSource K-line (date range) failed for {code_normalized}: {e_ds}")
            fallback_reason.append(f"data_source.get_kline failed: {e_ds}")

        if ak is None:
            return _respond(_fail_kline_response(
                f'无法获取 {code} 的K线数据 (日期范围查询, 所有数据源均失败)',
                source_chain=list(source_chain),
                fallback_reason=fallback_reason or [f"date_range fetch failed for {code}"],
                started_at=started_at,
            ))
        _append_chain_step(source_chain, "akshare.stock_zh_a_hist")
        try:
            df = ak.stock_zh_a_hist(
                symbol=code_normalized,
                period=mapped_period,
                start_date=start_date.replace('-', '') if start_date else None,
                end_date=end_date.replace('-', '') if end_date else None,
                adjust=adjust or "qfq"
            )
            if df is None or df.empty:
                fallback_reason.append(f"no_kline_data:{code}")
                return _respond(_fail_kline_response(
                    f'No kline data for {code}',
                    source_chain=list(source_chain),
                    fallback_reason=fallback_reason,
                    started_at=started_at,
                ))
            results = _process_kline_akshare(df, code_normalized)
            validated_results = _validated_kline_rows(results)
            if _has_validated_kline_rows(validated_results):
                await _async_save_klines_to_db(code_normalized, validated_results)
                return _respond(_ok_kline_response(
                    validated_results,
                    source="akshare",
                    source_chain=list(source_chain),
                    fallback_reason=fallback_reason,
                    started_at=started_at,
                ))
        except Exception as e:
            fallback_reason.append(str(e))
            return _respond(_fail_kline_response(
                f'Failed to get kline data: {str(e)}',
                source_chain=list(source_chain),
                fallback_reason=fallback_reason,
                started_at=started_at,
            ))

    return _respond(await get_kline(code, mapped_period, limit))

# ============================================================
# 指数 K 线（独立于个股 K 线，避免代码混淆）
# ============================================================

# 指数代码 → AkShare symbol 映射
_INDEX_AK_MAP = {
    "000001": "sh000001",
    "000300": "sh000300",
    "000016": "sh000016",
    "000905": "sh000905",
    "399001": "sz399001",
    "399006": "sz399006",
    "399005": "sz399005",
}


def _has_index_like_price_scale(index_code: str, rows: list[dict]) -> bool:
    """Reject stock-like bars accidentally returned for major index codes."""
    if index_code not in _INDEX_AK_MAP:
        return True
    closes = [safe_float(row.get("close") or row.get("Close")) for row in rows if isinstance(row, dict)]
    closes = [value for value in closes if value > 0]
    if not closes:
        return False
    # Major mainland China equity indices are not single-digit/low double-digit
    # instruments.  This catches the common 000001 Ping An Bank pollution case.
    return max(closes[-min(len(closes), 20):]) >= 100.0


async def get_index_kline(index_code: str, period: str = "daily", limit: int = 60) -> dict:
    """获取指数K线数据（专用函数，避免与个股代码混淆）

    适用场景: 指数趋势分析、大盘走势回顾

    数据源优先级: SQLite → Tushare → AkShare (stock_zh_index_daily_em)
    时效性: 日线 T+0~T+1

    Args:
        index_code (str, required): 指数代码，如 "000001"(上证指数)、"399001"(深证成指)、"399006"(创业板指)
        period (str, optional): K线周期，目前仅支持 "daily"，默认 "daily"
        limit (int, optional): 数据条数，默认 60

    Returns:
        dict: {"success": bool, "data": list[dict]}
        data 每条记录包含: date(str), open(float), close(float), high(float), low(float),
        volume(int), amount(float), source(str)

    Errors:
        - 所有数据源均不可用时返回 success=false

    Examples:
        get_index_kline("000001")
        get_index_kline("399006", limit=120)
    """
    code = normalize_code(index_code)
    ak_symbol = _INDEX_AK_MAP.get(code)
    if ak_symbol is None:
        ak_symbol = f"sz{code}" if code.startswith("39") else f"sh{code}"

    # 0. DB 优先：仅查指数前缀代码，避免与同名个股代码串码
    if period == "daily":
        try:
            db = get_db()
            db_data = await db.get_klines(ak_symbol, limit=limit)
            if db_data:
                validated_results = _validated_kline_rows(db_data)
                if _has_validated_kline_rows(validated_results):
                    if _has_index_like_price_scale(code, validated_results):
                        return ok(validated_results)
                    safe_stderr_print(
                        f"SQLite index K-line identity mismatch for {code}: stock-like price scale"
                    )
        except Exception as e_db:
            safe_stderr_print(f"SQLite index K-line query failed for {ak_symbol}: {e_db}")

    # 1. Tushare：优先使用付费/授权数据源，避免公开源代理波动影响主流程
    ts_pro = data_source.get_tushare_pro()
    if ts_pro is not None and period == "daily":
        try:
            # 指数在 Tushare 中的代码格式
            if code.startswith("39"):
                ts_code = f"{code}.SZ"
            else:
                ts_code = f"{code}.SH"
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=limit * 2 + 30)).strftime("%Y%m%d")
            df = ts_pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                df = df.iloc[::-1].tail(limit)
                results = []
                for _, row in df.iterrows():
                    td = str(row.get("trade_date", ""))
                    results.append({
                        "date": f"{td[:4]}-{td[4:6]}-{td[6:]}" if len(td) >= 8 else td,
                        "open": safe_float(row.get("open")),
                        "close": safe_float(row.get("close")),
                        "high": safe_float(row.get("high")),
                        "low": safe_float(row.get("low")),
                        "volume": safe_float(row.get("vol")),
                        "amount": safe_float(row.get("amount")),
                        "source": "tushare_index",
                    })
                if results:
                    if _has_index_like_price_scale(code, results):
                        return ok(results)
                    safe_stderr_print(
                        f"Tushare index K-line identity mismatch for {code}: stock-like price scale"
                    )
        except Exception as e:
            safe_stderr_print(f"Tushare index kline failed for {code}: {e}")

    # 2. AkShare：指数专用公开源，作为最后降级
    if ak is not None:
        try:
            df = _run_with_retry(
                lambda: ak.stock_zh_index_daily_em(symbol=ak_symbol),
                _KLINE_TIMEOUTS,
            )
            if df is not None and not df.empty:
                df = df.tail(int(limit))
                results = []
                for _, row in df.iterrows():
                    date_val = row.get("date") or row.get("日期") or ""
                    results.append({
                        "date": str(date_val)[:10],
                        "open": safe_float(row.get("open") or row.get("开盘")),
                        "close": safe_float(row.get("close") or row.get("收盘")),
                        "high": safe_float(row.get("high") or row.get("最高")),
                        "low": safe_float(row.get("low") or row.get("最低")),
                        "volume": safe_int(row.get("volume") or row.get("成交量")),
                        "amount": safe_float(row.get("amount") or row.get("成交额")),
                        "source": "akshare_index",
                    })
                if results:
                    if _has_index_like_price_scale(code, results):
                        return ok(results)
                    safe_stderr_print(
                        f"AkShare index K-line identity mismatch for {code}: stock-like price scale"
                    )
        except Exception as e:
            safe_stderr_print(f"AkShare index kline failed for {code}: {e}")

    return fail(f"所有数据源均无法获取指数 {code} 的K线数据")
