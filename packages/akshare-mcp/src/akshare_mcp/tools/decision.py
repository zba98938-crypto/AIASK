"""决策工具"""

from __future__ import annotations

import asyncio
from typing import Any

from ..storage import get_db as _default_get_db
from ..services.decision_contracts import (
    get_unified_decision_details_payload as _default_get_unified_decision_details_payload,
    get_unified_decision_summary_payload as _default_get_unified_decision_summary_payload,
)
from . import _decision_buy as _decision_buy_mod
from . import _decision_common as _decision_common_mod
from . import _decision_context as _decision_context_mod
from . import _decision_sell as _decision_sell_mod
from . import _decision_unified as _decision_unified_mod
from .pit_middleware import build_pit_meta, create_pit_context

get_db = _default_get_db
technical_analysis = _decision_common_mod.technical_analysis
factor_calculator = _decision_common_mod.factor_calculator
get_unified_decision_summary_payload = _default_get_unified_decision_summary_payload
get_unified_decision_details_payload = _default_get_unified_decision_details_payload

_decision_lock = asyncio.Lock()


def _attach_pit_meta(result: Any, *, as_of: str | None) -> Any:
    """Attach pit_guard / pit metadata to a tool envelope.

    Decision tools accept an `as_of` argument but historically passed it as
    a plain string without surfacing PIT context to the caller. The MCP
    envelope contract requires every research / decision / sentiment / backtest
    tool to publish `meta.pit_guard` so audits can verify no future
    information was used. Wrap the envelope with this metadata uniformly.

    The middleware does not retro-filter records inside the decision body;
    that requires per-tool changes in `_decision_*.py`. What it does
    guarantee is:
      - meta.pit.as_of is present and ISO-normalized
      - meta.pit_guard.active reflects whether an explicit as_of was supplied
      - downstream auditors can match this against the tool's actual data
        access patterns to detect PIT regressions.
    """
    if not isinstance(result, dict):
        return result
    ctx = create_pit_context(as_of)
    pit_meta = build_pit_meta(ctx, total_records=0, filtered_records=0)
    meta = dict(result.get("meta") or {})
    meta.setdefault("tool", "decision")
    meta.update(pit_meta)
    pit_guard = dict(meta.get("pit_guard") or {})
    explicit_as_of = bool(as_of and str(as_of).strip())
    pit_guard.setdefault("active", explicit_as_of)
    pit_guard.setdefault("filtered_rows", 0)
    meta["pit_guard"] = pit_guard
    if explicit_as_of:
        meta.setdefault("time_precision", "historical_eod_close_as_of")
    else:
        meta.setdefault("time_precision", "historical_eod_close")
    result["meta"] = meta
    return result


def _sync_decision_overrides() -> None:
    """Keep decision submodules aligned with decision-level monkeypatches."""
    for module in (
        _decision_buy_mod,
        _decision_common_mod,
        _decision_context_mod,
        _decision_sell_mod,
        _decision_unified_mod,
    ):
        module.get_db = get_db
        module.technical_analysis = technical_analysis
        module.factor_calculator = factor_calculator
    _decision_common_mod.get_unified_decision_summary_payload = get_unified_decision_summary_payload
    _decision_common_mod.get_unified_decision_details_payload = get_unified_decision_details_payload
    _decision_unified_mod.get_unified_decision_summary_payload = get_unified_decision_summary_payload
    _decision_unified_mod.get_unified_decision_details_payload = get_unified_decision_details_payload
    for module in (
        _decision_buy_mod,
        _decision_context_mod,
        _decision_sell_mod,
        _decision_unified_mod,
    ):
        module.get_investment_analysis = get_investment_analysis


async def get_investment_analysis(
    code: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """投资分析数据汇聚：透传顶层 monkeypatch 到底层实现。

    .. deprecated:: 2026-05-24 (P2-4.6.1)
       此工具已 deprecated,后续版本可能移除。
       建议改用以下专用工具:
       - ``analyze_stock_workflow``: AI-facing stock snapshot 工作流
       - ``analyze_stock_product_workflow``: 统一深度分析工作流
       - ``get_unified_decision``: 统一决策汇总
       - ``should_i_buy`` / ``should_i_sell``: 单点决策建议

    注意：这里不能再次等待 ``_decision_lock``。
    ``should_i_buy`` / ``should_i_sell`` 会在持有该锁时调用当前函数；
    如果在这里重入同一把锁，会形成自锁并最终把整个 MCP 请求拖到超时。
    """
    _sync_decision_overrides()
    result = await _decision_common_mod.get_investment_analysis(
        code=code,
        stock_code=stock_code,
        symbol=symbol,
        ticker=ticker,
    )
    # P2-4.6.1 fix: 注入 deprecation 标记,提示 AI 不再依赖此工具(诊断报告 §4.6.1)
    if isinstance(result, dict):
        result.setdefault('deprecated', True)
        result.setdefault('deprecation_message',
            'get_investment_analysis 已 deprecated,建议改用 analyze_stock_workflow / get_unified_decision'
        )
        result.setdefault('replacement_tools', [
            'analyze_stock_workflow',
            'analyze_stock_product_workflow',
            'get_unified_decision',
            'should_i_buy',
            'should_i_sell',
        ])
    return result


async def should_i_buy(
    code: str | None = None,
    investment_style: str = 'balanced',
    as_of: str = '',
    adjust: str = '',
    price_source_policy: str = 'auto',
    explain: bool = True,
    strict_mode: bool = False,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """买入建议 - 综合估值、技术、基本面、因子分析"""
    async with _decision_lock:
        _sync_decision_overrides()
        result = await _decision_buy_mod.should_i_buy(
            code=code, investment_style=investment_style, as_of=as_of,
            adjust=adjust, price_source_policy=price_source_policy,
            explain=explain, strict_mode=strict_mode,
            stock_code=stock_code, symbol=symbol, ticker=ticker,
        )
    return _attach_pit_meta(result, as_of=as_of)


async def should_i_sell(
    code: str | None = None,
    buy_price: float = 0.0,
    holding_days: int = 0,
    as_of: str = '',
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """卖出建议 - 综合止盈止损、技术信号、持仓时间分析"""
    async with _decision_lock:
        _sync_decision_overrides()
        result = await _decision_sell_mod.should_i_sell(
            code=code, buy_price=buy_price, holding_days=holding_days,
            stock_code=stock_code, symbol=symbol, ticker=ticker,
        )
    return _attach_pit_meta(result, as_of=as_of)


async def build_stock_context(
    code: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """构建股票上下文：基础骨架 + 行情快照 + 资金流 + 产业链。"""
    async with _decision_lock:
        _sync_decision_overrides()
        return await _decision_context_mod.build_stock_context(
            code=code, stock_code=stock_code, symbol=symbol, ticker=ticker,
        )


async def build_quant_context(
    code: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """构建量化上下文：因子画像 + 条件收益 + 相似形态 + OOS 验证。"""
    async with _decision_lock:
        _sync_decision_overrides()
        return await _decision_context_mod.build_quant_context(
            code=code, stock_code=stock_code, symbol=symbol, ticker=ticker,
        )


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
    async with _decision_lock:
        _sync_decision_overrides()
        return await _decision_context_mod.build_event_context(
            code=code, stock_code=stock_code, symbol=symbol, ticker=ticker,
            news_limit=news_limit, notice_days=notice_days, report_limit=report_limit,
        )


async def run_decision_gate(
    code: str | None = None,
    investment_style: str = 'balanced',
    user_id: str | None = None,
    stock_context: dict | None = None,
    quant_context: dict | None = None,
    event_context: dict | None = None,
    user_context: dict | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """运行统一决策规则闸门，可传入现成上下文，也可按代码自动构建。"""
    async with _decision_lock:
        _sync_decision_overrides()
        return await _decision_unified_mod.run_decision_gate(
            code=code, investment_style=investment_style, user_id=user_id,
            stock_context=stock_context, quant_context=quant_context,
            event_context=event_context, user_context=user_context,
            stock_code=stock_code, symbol=symbol, ticker=ticker,
        )


async def fuse_decision_payload(
    code: str | None = None,
    investment_style: str = 'balanced',
    user_id: str | None = None,
    stock_context: dict | None = None,
    quant_context: dict | None = None,
    event_context: dict | None = None,
    user_context: dict | None = None,
    gate: dict | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """融合统一决策上下文，输出 action/summary/weights/raw_ai_output。"""
    async with _decision_lock:
        _sync_decision_overrides()
        return await _decision_unified_mod.fuse_decision_payload(
            code=code, investment_style=investment_style, user_id=user_id,
            stock_context=stock_context, quant_context=quant_context,
            event_context=event_context, user_context=user_context,
            gate=gate, stock_code=stock_code, symbol=symbol, ticker=ticker,
        )


async def get_unified_decision_summary(
    code: str | None = None,
    investment_style: str = 'balanced',
    user_id: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """统一决策摘要：输出前端友好的 summary 卡片。"""
    async with _decision_lock:
        _sync_decision_overrides()
        return await _decision_unified_mod.get_unified_decision_summary(
            code=code, investment_style=investment_style, user_id=user_id,
            stock_code=stock_code, symbol=symbol, ticker=ticker,
        )


async def get_unified_decision_details(
    code: str | None = None,
    investment_style: str = 'balanced',
    user_id: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """统一决策详情：输出 summary + 全量 details 证据。"""
    async with _decision_lock:
        _sync_decision_overrides()
        return await _decision_unified_mod.get_unified_decision_details(
            code=code, investment_style=investment_style, user_id=user_id,
            stock_code=stock_code, symbol=symbol, ticker=ticker,
        )


async def get_unified_decision(
    code: str | None = None,
    detail_level: str = 'summary',
    investment_style: str = 'balanced',
    user_id: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """统一决策兼容包装器：按 detail_level 返回 summary 或 details。"""
    async with _decision_lock:
        _sync_decision_overrides()
        return await _decision_unified_mod.get_unified_decision(
            code=code, detail_level=detail_level, investment_style=investment_style,
            user_id=user_id, stock_code=stock_code, symbol=symbol, ticker=ticker,
        )


def register(mcp):
    """注册决策工具"""
    mcp.tool()(should_i_buy)
    mcp.tool()(should_i_sell)
    mcp.tool()(build_stock_context)
    mcp.tool()(build_quant_context)
    mcp.tool()(build_event_context)
    mcp.tool()(run_decision_gate)
    mcp.tool()(fuse_decision_payload)
    mcp.tool()(get_unified_decision_summary)
    mcp.tool()(get_unified_decision_details)
    mcp.tool()(get_unified_decision)
    mcp.tool()(get_investment_analysis)
