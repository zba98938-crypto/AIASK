import os
import sqlite3
import time


from ..utils import (
    attach_argument_contract_meta,
    fail,
    format_period,
    normalize_code,
    ok,
    parse_numeric,
    resolve_canonical_arg,
    resolve_existing_security_code_sync,
)
from ..provider_contracts import attach_tool_provider_contract_meta

# Import optimization modules
from ..core.cache_manager import cached
from ..core.rate_limiter import get_limiter


from typing import Any, Optional, Callable, TypeVar
import sys
from datetime import datetime, timedelta
from ..baostock_api import baostock_client
from ..cache import cache
from ..services.financial_schema import (
    FINANCIAL_PRIMARY_FIELDS as _FINANCIAL_PRIMARY_FIELDS,
    merge_financial_payload,
    normalize_financial_payload,
    financial_gap_summary,
    financial_payload_is_complete,
    financial_payload_is_usable,
    financial_payload_needs_enrichment,
)
from .data_quality import build_quality_meta, infer_missing_fields, normalize_reason_list
from ..date_utils import get_latest_trading_date
from ..data_source import data_source
from ..storage import run_with_db_cleanup
from ..storage.sqlite.schema_base import default_sqlite_path

_RETRY_SLEEP_SECONDS = float(os.getenv("AKSHARE_RETRY_SLEEP_SECONDS", "0.5"))
_FINANCE_RETRY = int(os.getenv("AKSHARE_FINANCE_RETRY", "2"))

T = TypeVar("T")
_financial_payload_is_complete = financial_payload_is_complete
_financial_payload_needs_enrichment = financial_payload_needs_enrichment
_financial_payload_is_usable = financial_payload_is_usable
_financial_gap_summary = financial_gap_summary
_merge_financial_payload = merge_financial_payload


async def _stock_info_from_db(code: str) -> Optional[dict]:
    try:
        from ..storage import get_db

        db = get_db()
        if hasattr(db, "get_stock_info"):
            payload = await db.get_stock_info(code)
            if isinstance(payload, dict) and payload:
                return {
                    "code": code,
                    "name": str(payload.get("name") or payload.get("stock_name") or ""),
                    "industry": str(payload.get("industry") or payload.get("sector") or ""),
                    "listDate": str(payload.get("list_date") or payload.get("listDate") or ""),
                    "totalShares": "",
                    "floatShares": "",
                    "totalMarketCap": str(payload.get("market_cap") or ""),
                    "floatMarketCap": "",
                    "raw": dict(payload),
                }
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/finance.py:78: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None
    return None


def _stock_info_from_sqlite(code: str) -> Optional[dict]:
    try:
        path = default_sqlite_path()
        if not path.exists():
            return None
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            cols_rows = conn.execute("PRAGMA table_info('stocks')").fetchall()
            columns = {str(row["name"]) for row in cols_rows or []}
            code_col = "stock_code" if "stock_code" in columns else ("code" if "code" in columns else "")
            if not code_col:
                return None
            row = conn.execute(
                f"SELECT * FROM stocks WHERE {code_col} = ? LIMIT 1",
                (code,),
            ).fetchone()
            if row is None:
                return None
            payload = dict(row)
            return {
                "code": code,
                "name": str(payload.get("name") or payload.get("stock_name") or ""),
                "industry": str(payload.get("industry") or payload.get("sector") or ""),
                "listDate": str(payload.get("list_date") or payload.get("listDate") or ""),
                "totalShares": "",
                "floatShares": "",
                "totalMarketCap": str(payload.get("market_cap") or ""),
                "floatMarketCap": "",
                "raw": payload,
            }
        finally:
            conn.close()
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/finance.py:116: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None


from .financials_helpers import (
    _build_financial_cache_entry,
    _calc_ratio,
    _fail_financial,
    _financial_missing_fields,
    _first_not_none,
    _ok_financial,
    _ok_stock_info_degraded,
    _pick_best_statement_row,
    _read_financial_cache_entry,
    _row_non_null_count,
)
import logging

logger = logging.getLogger(__name__)

def _call_with_retry(fn: Callable[[], T]) -> T:
    last_error: Optional[Exception] = None
    for _ in range(_FINANCE_RETRY):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if _RETRY_SLEEP_SECONDS > 0:
                time.sleep(_RETRY_SLEEP_SECONDS)
    if last_error:
        raise last_error
    raise RuntimeError("请求失败")


def _get_financials_tdx(code: str) -> Optional[dict]:
    """通过 tqcenter ``get_financial_data`` 获取最新一期专业财务（FN 字段）。

    数据源优先级最高，需通达信客户端在线 + 已下载"专业财务数据包"。
    若客户端没下数据包，所有 FN 字段返回 ``"--"``，函数返回 None
    （上层会自动降级到 Tushare/AKShare）。

    返回字段映射来自 ``TDX_DATA_SOURCE_MIGRATION_PLAN.md`` 附录 A。
    """
    from ..data_source import data_source

    fn_fields = [
        "FN1",    # 基本每股收益
        "FN4",    # 每股净资产
        "FN6",    # 净资产收益率（旧口径）
        "FN40",   # 资产总计
        "FN63",   # 负债合计
        "FN72",   # 所有者权益合计
        "FN107",  # 经营现金流净额
        "FN159",  # 流动比率
        "FN160",  # 速动比率
        "FN183",  # 营收增长率
        "FN184",  # 净利润增长率
        "FN197",  # 净资产收益率（新口径）
        "FN199",  # 销售净利率
        "FN202",  # 销售毛利率
        "FN206",  # 扣非净利润
        "FN210",  # 资产负债率
        "FN230",  # 营业收入
        "FN232",  # 归母净利润
        "FN238",  # 总股本
        "FN239",  # 流通A股
    ]
    try:
        data = data_source.get_financial_data_by_date(
            [code], fn_fields, year=0, mmdd=0,
        )
    except Exception as exc:
        print(f"[Finance] tqcenter get_financial_data_by_date failed: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        return None
    full_code = next(
        (k for k in data.keys() if isinstance(k, str) and k.split(".")[0] == code),
        None,
    )
    if full_code is None:
        return None
    payload = data.get(full_code) or {}
    if not isinstance(payload, dict) or not payload:
        return None

    def _f(key: str) -> Optional[float]:
        return parse_numeric(payload.get(key))

    # 任意一个核心 FN 字段非空才认为可用，否则视为客户端没下数据包
    if all(_f(k) is None for k in ("FN1", "FN6", "FN197", "FN230", "FN232")):
        return None

    return {
        "code": code,
        "reportDate": None,  # by_date(0,0) 不返回报告期；上层会从 ``best_payload`` 继承
        "revenue": _f("FN230"),
        "netProfit": _f("FN232"),
        "grossProfitMargin": _f("FN202"),
        "netProfitMargin": _f("FN199"),
        "roe": _f("FN197") if _f("FN197") is not None else _f("FN6"),
        "roa": None,
        "debtRatio": _f("FN210"),
        "currentRatio": _f("FN159"),
        "quickRatio": _f("FN160"),
        "eps": _f("FN1"),
        "bvps": _f("FN4"),
        "totalAssets": _f("FN40"),
        "totalLiab": _f("FN63"),
        "equity": _f("FN72"),
        "operatingCashFlow": _f("FN107"),
        "revenueGrowth": _f("FN183"),
        "profitGrowth": _f("FN184"),
        "totalShares": _f("FN238"),
        "floatShares": _f("FN239"),
        "source": "tqcenter",
    }


def _get_financials_tushare(code: str) -> Optional[dict]:
    pro = data_source.get_tushare_pro()
    if not pro:
        return None

    ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=550)).strftime("%Y%m%d")

    try:
        indicator_df = pro.fina_indicator(ts_code=ts_code, start_date=start_date, end_date=end_date)
    except Exception as e:
        print(f"[Finance] Tushare fina_indicator failed: {e}", file=sys.stderr)
        indicator_df = None

    try:
        income_df = pro.income(ts_code=ts_code, start_date=start_date, end_date=end_date)
    except Exception as e:
        print(f"[Finance] Tushare income failed: {e}", file=sys.stderr)
        income_df = None

    if (indicator_df is None or indicator_df.empty) and (income_df is None or income_df.empty):
        return None

    indicator_row = _pick_best_statement_row(
        indicator_df,
        ("end_date", "roe", "debt_to_assets", "current_ratio", "eps", "grossprofit_margin", "netprofit_margin"),
    )
    income_row = _pick_best_statement_row(
        income_df,
        ("end_date", "total_revenue", "operate_profit", "n_income"),
    )

    report_date = None
    if indicator_row is not None and indicator_row.get("end_date"):
        report_date = str(indicator_row.get("end_date"))
    elif income_row is not None and income_row.get("end_date"):
        report_date = str(income_row.get("end_date"))

    balance_row = None
    need_balance_sheet = (
        indicator_row is None
        or parse_numeric(indicator_row.get("debt_to_assets")) is None
        or parse_numeric(indicator_row.get("current_ratio")) is None
        or parse_numeric(indicator_row.get("roa")) is None
    )
    if need_balance_sheet:
        try:
            balance_df = pro.balancesheet(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception as e:
            print(f"[Finance] Tushare balancesheet failed: {e}", file=sys.stderr)
            balance_df = None
        balance_row = _pick_best_statement_row(
            balance_df,
            ("end_date", "total_assets", "total_liab", "total_cur_assets", "total_cur_liab", "total_hldr_eqy_exc_min_int"),
        )

    if report_date is None and balance_row is not None and balance_row.get("end_date"):
        report_date = str(balance_row.get("end_date"))

    debt_ratio = parse_numeric(indicator_row.get("debt_to_assets")) if indicator_row is not None else None
    if debt_ratio is None and balance_row is not None:
        debt_ratio = _calc_ratio(balance_row.get("total_liab"), balance_row.get("total_assets"))

    current_ratio = parse_numeric(indicator_row.get("current_ratio")) if indicator_row is not None else None
    if current_ratio is None and balance_row is not None:
        current_ratio = _calc_ratio(balance_row.get("total_cur_assets"), balance_row.get("total_cur_liab"), multiplier=1.0)

    # 获取ROA，如果为空则尝试计算
    roa_value = parse_numeric(indicator_row.get("roa")) if indicator_row is not None else None
    if roa_value is None and income_row is not None and balance_row is not None:
        roa_value = _calc_ratio(income_row.get("n_income"), balance_row.get("total_assets"))
        if roa_value is not None:
            print(f"[Finance] Calculated ROA for {code}: {roa_value:.2f}%", file=sys.stderr)

    revenue_growth = None
    profit_growth = None
    if indicator_row is not None:
        revenue_growth = _first_not_none(
            parse_numeric(indicator_row.get("tr_yoy")),
            parse_numeric(indicator_row.get("or_yoy")),
            parse_numeric(indicator_row.get("q_gr_yoy")),
        )
        profit_growth = _first_not_none(
            parse_numeric(indicator_row.get("netprofit_yoy")),
            parse_numeric(indicator_row.get("q_netprofit_yoy")),
            parse_numeric(indicator_row.get("profit_dedt")),
        )

    return {
        "code": code,
        "reportDate": report_date,
        "revenue": parse_numeric(income_row.get("total_revenue")) if income_row is not None else None,
        "netProfit": parse_numeric(income_row.get("n_income")) if income_row is not None else None,
        "grossProfitMargin": parse_numeric(indicator_row.get("grossprofit_margin")) if indicator_row is not None else None,
        "netProfitMargin": parse_numeric(indicator_row.get("netprofit_margin")) if indicator_row is not None else None,
        "roe": parse_numeric(indicator_row.get("roe")) if indicator_row is not None else None,
        "roa": roa_value,
        "debtRatio": debt_ratio,
        "currentRatio": current_ratio,
        "eps": parse_numeric(indicator_row.get("eps")) if indicator_row is not None else None,
        "bvps": None,
        "revenueGrowth": revenue_growth,
        "profitGrowth": profit_growth,
        "source": "tushare_pro",
    }

@cached(ttl=86400.0)  # 24h cache for financial data
async def get_financials(
    code: str = "",
    *,
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
) -> dict:
    """
    获取股票财务指标数据

    数据源优先级: SQLite → Tushare Pro → AkShare → Baostock
    """
    # Rate limiting
    limiter = get_limiter("finance", max_calls=5, period=1.0)
    limiter.acquire()

    raw_code, alias_hits, _ = resolve_canonical_arg(
        "code",
        code,
        stock_code=stock_code,
        symbol=symbol,
        ticker=ticker,
    )
    code, _, error = resolve_existing_security_code_sync(code=raw_code)
    canonical_args = {"code": code or raw_code}
    if error:
        return attach_argument_contract_meta(
            fail(error),
            canonical_tool="get_financials",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )
    started_at = datetime.now().astimezone()

    def _respond(payload: dict) -> dict:
        response = attach_argument_contract_meta(
            payload,
            canonical_tool="get_financials",
            canonical_args=canonical_args,
            alias_hits=alias_hits,
        )
        return attach_tool_provider_contract_meta(
            response,
            tool_name="get_financials",
            standard_model="FinancialMetrics",
        )

    # 0. Check Cache (TTL 24h)
    cached_entry = cache.get(f"financials_{code}", ttl_seconds=86400)
    cached_payload, cached_source_chain, cached_fallback_reason = _read_financial_cache_entry(cached_entry)
    if cached_payload and _financial_payload_is_complete(cached_payload) and not _financial_payload_needs_enrichment(cached_payload):
        return _respond(_ok_financial(
            cached_payload,
            source_chain=cached_source_chain or [cached_payload.get("source") or "cache.financials"],
            fallback_reason=cached_fallback_reason,
            started_at=started_at,
            cached_result=True,
        ))

    # 0.5. DB 优先：查 SQLite financials 表
    db_result = None
    best_payload = None
    fallback_reason: list[str] = []
    source_chain = ["db.get_financials"]
    try:
        from ..storage import get_db
        db = get_db()
        db_data = await db.get_financials(code)
        if db_data:
            row = db_data[0]
            db_result = {
                "code": code,
                **row,
                "source": "sqlite",
            }
            best_payload = normalize_financial_payload(db_result, source_label="sqlite")
            if _financial_payload_is_complete(best_payload) and not _financial_payload_needs_enrichment(best_payload):
                cache.set(
                    f"financials_{code}",
                    _build_financial_cache_entry(best_payload, source_chain=source_chain),
                )
                return _respond(_ok_financial(
                    best_payload,
                    source_chain=source_chain,
                    started_at=started_at,
                ))
    except Exception as e_db:
        import sys
        print(f"[Finance] SQLite query failed for {code}: {e_db}", file=sys.stderr)
        fallback_reason.append(f"db.get_financials failed: {e_db}")

    # Strategy (TDX-first):
    # 0. tqcenter FN 字段（客户端在线 + 已下载专业财务数据包时优先）
    # 1. Tushare Pro - 当 TDX 不可用 / 专业财务包未下载时降级
    # 2. AkShare THS / EM - 进一步降级
    # 3. Baostock - 最后兜底

    # 0. Try tqcenter (TDX 专业财务数据)
    try:
        source_chain.append("tqcenter")
        tdx_res = _get_financials_tdx(code)
        if tdx_res:
            merged = _merge_financial_payload(best_payload or db_result, tdx_res)
            if merged:
                best_payload = merged
            if _financial_payload_needs_enrichment(best_payload):
                fallback_reason.append(
                    f"tqcenter incomplete: {_financial_gap_summary(best_payload)}"
                )
            else:
                cache.set(
                    f"financials_{code}",
                    _build_financial_cache_entry(
                        best_payload,
                        source_chain=source_chain,
                        fallback_reason=fallback_reason,
                    ),
                )
                return _respond(_ok_financial(
                    best_payload,
                    source_chain=source_chain,
                    fallback_reason=fallback_reason,
                    started_at=started_at,
                ))
        else:
            fallback_reason.append(
                "tqcenter returned empty (客户端可能未在线或专业财务数据包未下载)"
            )
    except Exception as e:
        print(f"[Finance] tqcenter financial fetch failed for {code}: {e}", file=sys.stderr)
        fallback_reason.append(f"tqcenter failed: {e}")

    # 1. Try Tushare Pro (tqcenter 不可用时降级)
    try:
        source_chain.append("tushare_pro")
        tushare_res = _get_financials_tushare(code)
        if tushare_res:
            merged = _merge_financial_payload(best_payload or db_result, tushare_res)
            if merged:
                best_payload = merged
            if _financial_payload_needs_enrichment(best_payload):
                fallback_reason.append(f"tushare_pro incomplete: {_financial_gap_summary(best_payload)}")
            else:
                cache.set(
                    f"financials_{code}",
                    _build_financial_cache_entry(
                        best_payload,
                        source_chain=source_chain,
                        fallback_reason=fallback_reason,
                    ),
                )
                return _respond(_ok_financial(
                    best_payload,
                    source_chain=source_chain,
                    fallback_reason=fallback_reason,
                    started_at=started_at,
                ))
        else:
            fallback_reason.append("tushare_pro returned empty")
    except Exception as e:
        print(f"Tushare financial fetch failed for {code}: {e}", file=sys.stderr)
        fallback_reason.append(f"tushare_pro failed: {e}")

    # 2 & 3. Try AkShare (补充 EPS/BVPS/比率等)
    if best_payload is None or _financial_payload_needs_enrichment(best_payload):
        try:
            source_chain.append("akshare_financials")
            akshare_res = _get_financials_akshare(code)
        except Exception as e:
            print(f"AkShare financial fetch failed for {code}: {e}", file=sys.stderr)
            akshare_res = None
            fallback_reason.append(f"akshare_financials failed: {e}")
        if akshare_res:
            merged = _merge_financial_payload(best_payload or db_result, akshare_res)
            if merged:
                best_payload = merged
            if _financial_payload_needs_enrichment(best_payload):
                fallback_reason.append(f"akshare_financials incomplete: {_financial_gap_summary(best_payload)}")
            else:
                cache.set(
                    f"financials_{code}",
                    _build_financial_cache_entry(
                        best_payload,
                        source_chain=source_chain,
                        fallback_reason=fallback_reason,
                    ),
                )
                return _respond(_ok_financial(
                    best_payload,
                    source_chain=source_chain,
                    fallback_reason=fallback_reason,
                    started_at=started_at,
                ))
        elif "akshare_financials failed:" not in " | ".join(fallback_reason):
            fallback_reason.append("akshare_financials returned empty")

    # 4. Fallback to Baostock
    if best_payload is None or _financial_payload_needs_enrichment(best_payload):
        source_chain.append("baostock_financials")
        if not getattr(baostock_client, "available", True):
            reason = getattr(baostock_client, "unavailable_reason", None) or "baostock package is not installed"
            fallback_reason.append(f"baostock_optional_dependency_missing: {reason}")
            baostock_res = None
        else:
            try:
                # Baostock generally works with Quarter/Year, so try the latest available quarters.
                now = datetime.now()
                baostock_res = None
                for i in range(4):
                    q_date = now - timedelta(days=90 * i)
                    year = str(q_date.year)
                    month = q_date.month
                    quarter = "1" if month <= 3 else "2" if month <= 6 else "3" if month <= 9 else "4"
                    df_profit = baostock_client.get_profit_statement(code, year, quarter)
                    if not df_profit.empty:
                        row = df_profit.iloc[0]
                        baostock_res = {
                            "code": code,
                            "reportDate": f"{year}-Q{quarter}",
                            "eps": parse_numeric(row.get("epsTTM")),  # or mbEPS
                            "roe": parse_numeric(row.get("roeAvg")),
                            "grossProfitMargin": parse_numeric(row.get("grossMargin")),
                            "netProfitMargin": parse_numeric(row.get("netProfitMargin")),
                            "source": "baostock",
                        }
                        if baostock_res:
                            break
            except Exception as e:
                print(f"Baostock financial fetch failed for {code}: {e}", file=sys.stderr)
                baostock_res = None
                fallback_reason.append(f"baostock_financials failed: {e}")

        if baostock_res:
            merged = _merge_financial_payload(best_payload or db_result, baostock_res)
            if merged:
                best_payload = merged
        else:
            fallback_reason.append("baostock_financials returned empty")

    if best_payload and _financial_payload_is_usable(best_payload):
        if _financial_payload_needs_enrichment(best_payload):
            fallback_reason.append(f"final payload partial: {_financial_gap_summary(best_payload)}")
        cache.set(
            f"financials_{code}",
            _build_financial_cache_entry(
                best_payload,
                source_chain=source_chain,
                fallback_reason=fallback_reason,
            ),
        )
        return _respond(_ok_financial(
            best_payload,
            source_chain=source_chain,
            fallback_reason=fallback_reason,
            started_at=started_at,
        ))

    if db_result and _financial_payload_is_usable(db_result):
        best_payload = normalize_financial_payload(db_result, source_label="sqlite") or db_result
        if _financial_payload_needs_enrichment(best_payload):
            fallback_reason.append(f"final payload partial: {_financial_gap_summary(best_payload)}")
        cache.set(
            f"financials_{code}",
            _build_financial_cache_entry(
                best_payload,
                source_chain=source_chain,
                fallback_reason=fallback_reason,
            ),
        )
        return _respond(_ok_financial(
            best_payload,
            source_chain=source_chain,
            fallback_reason=fallback_reason,
            started_at=started_at,
        ))

    tried = " → ".join(source_chain) if source_chain else "无"
    return _respond(_fail_financial(
        f"所有数据源均无法获取 {code} 的财务数据（已尝试: {tried}）",
        source_chain=source_chain,
        fallback_reason=fallback_reason,
        started_at=started_at,
    ))

def _get_financials_akshare(code: str) -> Optional[dict]:
    """Removed with akshare: this project has no THS/East money financial provider.

    akshare is not declared in any pyproject and is not installed, so this link
    never ran. Returning None lets the caller's chain move to its next source,
    which is what already happened.
    """
    return None

def _get_financials_akshare_em(code: str) -> Optional[dict]:
    """Removed with akshare: this project has no THS/East money financial provider.

    akshare is not declared in any pyproject and is not installed, so this link
    never ran. Returning None lets the caller's chain move to its next source,
    which is what already happened.
    """
    return None


def _get_financials_akshare_indicator(code: str) -> Optional[dict]:
    """Removed with akshare: this project has no THS/East money financial provider.

    akshare is not declared in any pyproject and is not installed, so this link
    never ran. Returning None lets the caller's chain move to its next source,
    which is what already happened.
    """
    return None


@cached(ttl=86400.0)  # 24h cache for stock info
def get_stock_info(
    code: str = "",
    *,
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
) -> dict:
    """
    获取股票基本信息

    Args:
        stock_code: 股票代码
    """
    # Rate limiting
    limiter = get_limiter("info", max_calls=5, period=1.0)
    limiter.acquire()

    try:
        raw_code, alias_hits, _ = resolve_canonical_arg(
            "code",
            code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        code = normalize_code(raw_code)
        canonical_args = {"code": code}
        df = None

        def _respond(
            payload: dict,
            *,
            provider_requested: str = "finance.get_stock_info",
            provider_used: str = "unknown",
            source_chain: list[str] | None = None,
            data_timestamp: str | None = None,
        ) -> dict:
            response = attach_argument_contract_meta(
                payload,
                canonical_tool="get_stock_info",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )
            quality = (response.get("meta") or {}).get("quality")
            return attach_tool_provider_contract_meta(
                response,
                tool_name="get_stock_info",
                standard_model="StockInfo",
                provider_requested=provider_requested,
                provider_used=provider_used,
                source_chain=source_chain or [provider_requested],
                data_timestamp=data_timestamp,
                freshness={
                    "expectation": "latest_reference_or_profile_snapshot",
                    "data_timestamp_field": "listDate",
                },
                quality=quality if isinstance(quality, dict) else None,
            )

        if not code:
            return _respond(
                fail("需要提供股票代码（支持 code / stock_code / symbol / ticker）"),
                provider_used="none",
            )

        # 1. Try Tushare Pro
        try:
            pro = data_source.get_tushare_pro()
            if pro:
                ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
                df = pro.stock_basic(
                    ts_code=ts_code,
                    list_status="L",
                    fields="ts_code,symbol,name,market,industry,list_date",
                )
        except Exception:
            df = None

        if df is not None and not df.empty:
            row = df.iloc[0]
            total_shares = ""
            float_shares = ""
            total_market_cap = ""
            float_market_cap = ""
            # Try to enrich with daily_basic for share/cap data
            try:
                if pro:
                    ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
                    from datetime import datetime, timedelta
                    for days_back in range(1, 8):
                        trade_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y%m%d')
                        db_df = pro.daily_basic(ts_code=ts_code, trade_date=trade_date,
                                                fields='ts_code,total_share,float_share,total_mv,circ_mv')
                        if db_df is not None and not db_df.empty:
                            db_row = db_df.iloc[0]
                            ts_val = db_row.get('total_share')
                            total_shares = f"{float(ts_val) * 10000:.0f}" if ts_val and ts_val == ts_val else ""
                            fs_val = db_row.get('float_share')
                            float_shares = f"{float(fs_val) * 10000:.0f}" if fs_val and fs_val == fs_val else ""
                            tm_val = db_row.get('total_mv')
                            total_market_cap = f"{float(tm_val) * 10000:.0f}" if tm_val and tm_val == tm_val else ""
                            cm_val = db_row.get('circ_mv')
                            float_market_cap = f"{float(cm_val) * 10000:.0f}" if cm_val and cm_val == cm_val else ""
                            break
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/finance.py:840: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass
            payload = {
                "code": code,
                "name": str(row.get("name") or ""),
                "industry": str(row.get("industry") or ""),
                "listDate": str(row.get("list_date") or ""),
                "totalShares": total_shares,
                "floatShares": float_shares,
                "totalMarketCap": total_market_cap,
                "floatMarketCap": float_market_cap,
                "raw": {k: str(row.get(k, "")) for k in df.columns},
            }
            return _respond(
                ok(payload),
                provider_requested="tushare_pro.stock_basic",
                provider_used="tushare_pro",
                source_chain=["tushare_pro.stock_basic"],
                data_timestamp=payload.get("listDate") or None,
            )

        # The akshare individual-info / cninfo profile readers were removed with
        # the dependency. The local reference table is the provider; when it holds
        # nothing the response says so rather than presenting empty fields as data.
        info_source_chain = ["db.stocks"]
        info_provider_used = "db.stocks"
        db_payload = _stock_info_from_sqlite(code) or {}
        if db_payload:
            result = _ok_stock_info_degraded(
                db_payload,
                source_chain=info_source_chain,
                fallback_reason=(
                    "akshare profile providers removed with the dependency; "
                    "returned DB reference snapshot"
                ),
            )
            provider_used = "db.stocks"
            data_timestamp = db_payload.get("listDate") or None
        else:
            result = _ok_stock_info_degraded(
                {
                    "code": code,
                    "name": "",
                    "industry": "",
                    "listDate": "",
                    "totalShares": "",
                    "floatShares": "",
                    "totalMarketCap": "",
                    "floatMarketCap": "",
                    "raw": {},
                },
                source_chain=info_source_chain,
                fallback_reason="no reference row for this code",
            )
            provider_used = "none"
            data_timestamp = None
        return _respond(
            result,
            provider_requested=info_source_chain[0],
            provider_used=provider_used,
            source_chain=info_source_chain,
            data_timestamp=data_timestamp,
        )
    except Exception as e:
        response = attach_argument_contract_meta(
            fail(e),
            canonical_tool="get_stock_info",
            canonical_args={"code": normalize_code(code) if code else ""},
            alias_hits=[],
        )
        return attach_tool_provider_contract_meta(
            response,
            tool_name="get_stock_info",
            standard_model="StockInfo",
            provider_requested="finance.get_stock_info",
            provider_used="none",
            source_chain=["finance.get_stock_info"],
            fallback_reason=str(e),
            freshness={
                "expectation": "latest_reference_or_profile_snapshot",
                "data_timestamp_field": "listDate",
            },
        )

def register(mcp):
    mcp.tool()(get_financials)
    mcp.tool()(get_stock_info)
