from ._decision_common import *
from ..utils import propagate_data_quality_to_top

async def build_stock_context(
    code: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """构建股票上下文：基础骨架 + 行情快照 + 资金流 + 产业链。"""
    try:
        code, error_response = await _resolve_existing_stock_code_or_fail(
            code=code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        if error_response is not None:
            return error_response
        return propagate_data_quality_to_top(ok(await _build_stock_context(code)))
    except Exception as e:
        return fail(str(e))

async def build_quant_context(
    code: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """构建量化上下文：因子画像 + 条件收益 + 相似形态 + OOS 验证。"""
    try:
        code, error_response = await _resolve_existing_stock_code_or_fail(
            code=code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        if error_response is not None:
            return error_response
        return propagate_data_quality_to_top(ok(await _build_quant_context(code)))
    except Exception as e:
        return fail(str(e))

async def build_event_context(
    code: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
    news_limit: int = 12,
    notice_days: int = 30,
    report_limit: int = 6,
):
    """构建事件上下文：新闻/公告/研报聚合、事件分类与 veto 候选。"""
    try:
        code, error_response = await _resolve_existing_stock_code_or_fail(
            code=code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        if error_response is not None:
            return error_response
        return propagate_data_quality_to_top(ok(
            await _build_event_context(
                code,
                news_limit=news_limit,
                notice_days=notice_days,
                report_limit=report_limit,
            )
        ))
    except Exception as e:
        return fail(str(e))
