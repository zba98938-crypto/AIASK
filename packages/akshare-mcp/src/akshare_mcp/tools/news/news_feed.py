"""新闻/研报工具 — 个股新闻 & 市场新闻"""

from datetime import date, timedelta

from ...services.db_first_market_context import load_db_first_document_context
from ...storage import get_db
from ...core.cache_manager import cached
from ...core.rate_limiter import get_limiter
from ...utils import (
    attach_argument_contract_meta,
    fail,
    ok,
    resolve_canonical_arg,
    resolve_existing_security_code_sync,
    validate_int_range,
)
from ..fund_flow_common import _run_storage_call_sync
from .helpers import (
    _map_news_rows,
    _map_research_rows,
    _try_akshare_news_functions,
    _try_tushare_anns,
    _try_tushare_news,
)
from .notices import fetch_market_notice_head, get_stock_notices
from .research import get_research_reports, get_stock_research


@cached(ttl=1800.0)
def get_stock_news(
    code: str = "",
    limit: int = 20,
    *,
    stock_code: str = "",
    symbol: str = "",
    ticker: str = "",
    prefer_db: bool = True,
) -> dict:
    """
    获取个股新闻列表（优先使用 AkShare 内置接口，失败则回退公告/研报）

    数据源优先级: Tushare(announcements) → AkShare(news) → 公告回退 → 研报回退
    时效性: 近30天新闻
    """
    limiter = get_limiter("news", rate=3.0)
    limiter.acquire()

    try:
        raw_code, alias_hits, _ = resolve_canonical_arg(
            "code",
            code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        code, _, error = resolve_existing_security_code_sync(code=raw_code)
        if error:
            return attach_argument_contract_meta(
                fail(error),
                canonical_tool="get_stock_news",
                canonical_args={"code": raw_code, "limit": limit},
                alias_hits=alias_hits,
            )
        limit, limit_error = validate_int_range(limit, field_name="limit", minimum=1)
        canonical_args = {"code": code, "limit": limit}
        if limit_error:
            return attach_argument_contract_meta(
                fail(limit_error),
                canonical_tool="get_stock_news",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )

        def _respond(payload: dict) -> dict:
            return attach_argument_contract_meta(
                payload,
                canonical_tool="get_stock_news",
                canonical_args=canonical_args,
                alias_hits=alias_hits,
            )

        # 0. Try Tushare announcements as news
        end_date = date.today()
        start_date = end_date - timedelta(days=30)

        if prefer_db:
            try:
                db_context, _ = _run_storage_call_sync(
                    lambda: load_db_first_document_context(
                        get_db(),
                        code,
                        start_date=start_date,
                        end_date=end_date,
                        news_limit=max(limit, 1),
                    ),
                    timeout=8.0,
                )
                db_news = list((db_context or {}).get("news") or [])
                if db_news:
                    return _respond(ok(db_news[:limit]))
            except Exception:
                pass

        items = _try_tushare_anns(start_date.isoformat(), end_date.isoformat(), code, limit)
        if items:
            return _respond(ok(items[:limit]))

        items = _try_akshare_news_functions(code, limit)
        if items:
            return _respond(ok(items[:limit]))

        # 回退1：使用公告数据充当新闻
        fallback = get_stock_notices(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            types=["全部"],
            stock_code=code,
            prefer_db=prefer_db,
        )
        if fallback.get("success") and fallback.get("data"):
            events = fallback["data"].get("events", [])
            mapped = _map_news_rows(events)
            if mapped:
                return _respond(ok(mapped[:limit]))

        # 回退2：使用研报数据充当新闻
        research = get_stock_research(code, limit=max(limit, 10))
        if research.get("success") and research.get("data"):
            reports = research["data"].get("reports", [])
            mapped = _map_research_rows(reports)
            if mapped:
                return _respond(ok(mapped[:limit]))

        # 回退3：使用研报通用接口
        reports = get_research_reports(code, limit=max(limit, 10), prefer_db=prefer_db)
        if reports.get("success") and reports.get("data"):
            mapped = _map_research_rows(reports["data"] if isinstance(reports["data"], list) else [])
            if mapped:
                return _respond(ok(mapped[:limit]))

        return _respond(fail(f"未获取到 {code} 的新闻数据"))
    except Exception as e:
        return attach_argument_contract_meta(
            fail(e),
            canonical_tool="get_stock_news",
            canonical_args={"code": code, "limit": limit},
            alias_hits=[],
        )


@cached(ttl=1800.0)
def get_market_news(limit: int = 20) -> dict:
    """
    获取市场新闻

    数据源优先级: Tushare(news) → AkShare → 公告回退
    时效性: 近7天新闻，缓存30分钟
    """
    limiter = get_limiter("news", rate=3.0)
    limiter.acquire()

    try:
        limit, limit_error = validate_int_range(limit, field_name="limit", minimum=1)
        if limit_error:
            return fail(limit_error)
        end_date = date.today()
        start_date = end_date - timedelta(days=7)

        # Fast path: market notices head is consistently faster than cold-starting
        # upstream news providers and still provides fresh market-moving events.
        fast_events = fetch_market_notice_head(
            start_iso=start_date.isoformat(),
            end_iso=end_date.isoformat(),
            max_items=limit,
        )
        mapped = _map_news_rows(fast_events)
        if mapped:
            return ok(mapped[:limit])

        items = _try_tushare_news(start_date.isoformat(), end_date.isoformat(), limit)
        if items:
            return ok(items[:limit])

        items = _try_akshare_news_functions("", limit)
        if items:
            return ok(items[:limit])

        fallback = get_stock_notices(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            types=["全部"],
            stock_code="",
        )
        if fallback.get("success") and fallback.get("data"):
            events = fallback["data"].get("events", [])
            mapped = _map_news_rows(events)
            if mapped:
                return ok(mapped[:limit])

        return fail("市场新闻暂不可用")
    except Exception as e:
        return fail(e)
