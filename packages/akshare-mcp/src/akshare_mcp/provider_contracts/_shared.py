"""Explicit provider-backed tool contracts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .base import FreshnessPolicy, SourcePolicy, json_schema
from .builder import build_contract
from .models import (
    BlockStocks,
    BlockStocksQuery,
    BlockTrades,
    BlockTradesQuery,
    ConceptFundFlow,
    ConceptFundFlowQuery,
    DragonTiger,
    DragonTigerQuery,
    EquityHistorical,
    EquityHistoricalQuery,
    EquityQuote,
    EquityQuoteQuery,
    FinancialMetrics,
    FinancialMetricsQuery,
    MacroIndicator,
    MacroIndicatorQuery,
    MarginData,
    MarginDataQuery,
    MarginRanking,
    MarginRankingQuery,
    MarketBlocks,
    MarketBlocksQuery,
    NorthFundFlow,
    NorthFundFlowQuery,
    NorthFundHolding,
    NorthFundHoldingQuery,
    NorthFundTop,
    NorthFundTopQuery,
    OptionChain,
    OptionChainQuery,
    SectorFundFlow,
    SectorFundFlowQuery,
    StockInfo,
    StockInfoQuery,
    StockFundFlow,
    StockFundFlowQuery,
    TradingCalendar,
    TradingCalendarQuery,
)


def _contract(
    *,
    name: str,
    title: str,
    category: str,
    description: str,
    required_params: list[str],
    query_model: type,
    data_model: type | dict[str, Any],
    freshness: FreshnessPolicy,
    source_policy: SourcePolicy,
    examples: list[dict[str, Any]],
    tags: list[str],
    standard_model: str | None = None,
) -> dict[str, Any]:
    return build_contract(
        name=name,
        title=title,
        category=category,
        description=description,
        required_params=required_params,
        query_model=query_model,
        data_model=data_model,
        freshness=freshness,
        source_policy=source_policy,
        examples=examples,
        tags=tags,
        standard_model=standard_model,
    )


_QUOTE_FRESHNESS = FreshnessPolicy(
    expectation="intraday_or_latest_quote_snapshot",
    max_stale_seconds=900,
    data_timestamp_field="data_timestamp",
)
_KLINE_FRESHNESS = FreshnessPolicy(
    expectation="daily_kline_t0_to_t1_or_requested_period_snapshot",
    data_timestamp_field="latest_bar.date",
)
_INFO_FRESHNESS = FreshnessPolicy(
    expectation="latest_reference_or_profile_snapshot",
    data_timestamp_field="listDate",
)
_CALENDAR_FRESHNESS = FreshnessPolicy(
    expectation="static_calendar_snapshot_with_period_filter",
    data_timestamp_field="dates[-1]",
)
_FINANCIAL_FRESHNESS = FreshnessPolicy(
    expectation="latest_report_period_financial_metrics",
    data_timestamp_field="reportDate",
)
_FUND_FLOW_FRESHNESS = FreshnessPolicy(
    expectation="intraday_or_latest_trading_day_fund_flow_snapshot",
    max_stale_seconds=86400,
    data_timestamp_field="tradeDate",
)
_NORTH_FUND_FRESHNESS = FreshnessPolicy(
    expectation="latest_northbound_flow_or_holding_snapshot",
    max_stale_seconds=86400,
    data_timestamp_field="date",
)
_SECTOR_FRESHNESS = FreshnessPolicy(
    expectation="intraday_or_latest_sector_snapshot",
    max_stale_seconds=1800,
)
_MACRO_FRESHNESS = FreshnessPolicy(
    expectation="latest_published_macro_indicator_snapshot",
    data_timestamp_field="records[0].publishDate",
)
_OPTIONS_FRESHNESS = FreshnessPolicy(
    expectation="near_real_time_option_chain_snapshot",
    max_stale_seconds=60,
    data_timestamp_field="underlying.time",
)

_TDX_PRIORITY_NOTE = "TDX_LOCAL_ONLY=1 keeps the chain on local/TDX sources and does not force online fallback."

_FINANCIAL_SOURCE_POLICY = SourcePolicy(
    priority=["db.get_financials", "tqcenter", "tushare_pro", "akshare_financials", "baostock_financials"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="kept within the existing get_financials implementation",
    notes=[
        _TDX_PRIORITY_NOTE,
        "This contract records the current AIASK source chain; it does not introduce a new provider path.",
    ],
)
_STOCK_INFO_SOURCE_POLICY = SourcePolicy(
    priority=["tushare_pro.stock_basic", "tushare_pro.daily_basic", "akshare.stock_individual_info_em", "akshare.stock_profile_cninfo"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="kept within the existing get_stock_info implementation",
    notes=["This contract documents the current tool behavior and does not add a new provider path."],
)
_STOCK_FUND_FLOW_SOURCE_POLICY = SourcePolicy(
    priority=["db.stock_fund_flow", "tqcenter.more_info", "tushare.moneyflow", "eastmoney.push2.fflow"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="disabled for TDX local paths; existing Tushare/Eastmoney fallback remains unchanged",
    notes=[_TDX_PRIORITY_NOTE],
)
_NORTH_FUND_SOURCE_POLICY = SourcePolicy(
    priority=["north_fund_flow", "tushare.moneyflow_hsgt", "hkex.daily_stat", "eastmoney.datacenter"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="existing function may use online Tushare/HKEX/Eastmoney unless local-only runtime disables upstream access",
    notes=["Validity gates reject empty, stale, implausible, or no-flow northbound records before fallback selection."],
)
_SECTOR_FUND_FLOW_SOURCE_POLICY = SourcePolicy(
    priority=["memory_cache", "cache.sector_fund_flow", "db.market_blocks", "eastmoney.push2", "tushare_pro.moneyflow_ind"],
    online_fallback="existing sector flow fallback chain is preserved",
    notes=["DB market_blocks fallback is marked degraded because it uses sector heat proxies when true flow is unavailable."],
)
_CONCEPT_FUND_FLOW_SOURCE_POLICY = SourcePolicy(
    priority=["eastmoney.push2.concept"],
    online_fallback="existing Eastmoney direct API path is preserved",
)
_DRAGON_TIGER_SOURCE_POLICY = SourcePolicy(
    priority=["sina.stock_lhb_detail_daily", "eastmoney.stock_lhb_detail"],
    online_fallback="fallback to Eastmoney and recent trading-date backtracking remains unchanged",
)
_MARGIN_SOURCE_POLICY = SourcePolicy(
    priority=["db.margin_detail", "db.margin_market_flow", "eastmoney.datacenter", "akshare.margin_account", "akshare.stock_margin_sse"],
    online_fallback="existing Eastmoney/AkShare fallback chain is preserved",
)
_BLOCK_TRADES_SOURCE_POLICY = SourcePolicy(
    priority=["eastmoney.block_trades", "eastmoney.block_trades.backtrack", "tushare_pro.stock_basic"],
    online_fallback="existing Eastmoney backtracking and Tushare metadata enrichment are preserved",
)
_MARKET_BLOCK_SOURCE_POLICY = SourcePolicy(
    priority=["db.market_blocks", "akshare.stock_sector_spot", "akshare.ths_board_names", "akshare.eastmoney_board", "akshare.area_summary"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="existing DB-first then AKShare fallback chain is preserved",
    notes=[_TDX_PRIORITY_NOTE],
)
_BLOCK_STOCK_SOURCE_POLICY = SourcePolicy(
    priority=["tqcenter.get_stock_list_in_sector", "db.block_stocks", "akshare.eastmoney_cons", "akshare.sina_sector_detail", "akshare.ths_detail", "tushare_pro.concept_detail"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="existing block constituent fallback chain is preserved",
    notes=[_TDX_PRIORITY_NOTE],
)
_MACRO_SOURCE_POLICY = SourcePolicy(
    priority=["tushare_pro.macro", "akshare.macro", "curl.mofcom"],
    online_fallback="existing Tushare/AkShare/curl fallback chain is preserved",
)
_OPTIONS_SOURCE_POLICY = SourcePolicy(
    priority=["akshare.option_sse_list_sina", "akshare.option_sse_codes_sina", "akshare.option_sse_spot_price_sina", "akshare.option_sse_underlying_spot_price_sina"],
    online_fallback="no additional provider path; degraded empty-chain responses are preserved when Sina upstream is partial",
)


def _array_of(model: type) -> dict[str, Any]:
    return {"type": "array", "items": json_schema(model)}


def _schema(title: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "title": title,
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": True,
    }


_GENERIC_RECORD_LIST = {"type": "array", "items": _schema("Record")}

_TECHNICAL_FRESHNESS = FreshnessPolicy(
    expectation="latest_kline_derived_technical_snapshot",
    data_timestamp_field="latest_bar.date",
)
_BACKTEST_FRESHNESS = FreshnessPolicy(
    expectation="deterministic_result_from_requested_historical_window",
    data_timestamp_field="end_date",
)
_VALUATION_FRESHNESS = FreshnessPolicy(
    expectation="latest_financial_and_market_context_snapshot",
    data_timestamp_field="valuation_date",
)
_PORTFOLIO_FRESHNESS = FreshnessPolicy(
    expectation="latest_holdings_and_recent_return_snapshot",
    data_timestamp_field="as_of",
)
_DECISION_FRESHNESS = FreshnessPolicy(
    expectation="current_or_as_of_decision_context_snapshot",
    data_timestamp_field="as_of",
)
_RESEARCH_FRESHNESS = FreshnessPolicy(
    expectation="latest_research_news_or_sentiment_snapshot",
    max_stale_seconds=86400,
    data_timestamp_field="publish_time",
)
_ALERT_FRESHNESS = FreshnessPolicy(
    expectation="latest_read_only_alert_evaluation_snapshot",
    max_stale_seconds=900,
    data_timestamp_field="checked_at",
)
_TDX_EXT_FRESHNESS = FreshnessPolicy(
    expectation="latest_tdx_or_reference_data_snapshot",
    data_timestamp_field="asof_time",
)

_TECHNICAL_SOURCE_POLICY = SourcePolicy(
    priority=["db.get_klines", "market.get_kline", "technical_analysis", "pattern_recognition"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="existing market.get_kline fallback is preserved",
    notes=[_TDX_PRIORITY_NOTE],
)
_BACKTEST_SOURCE_POLICY = SourcePolicy(
    priority=["db.get_klines", "market.get_kline", "quant_engine", "strategy_factory"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="existing kline fallback chain is preserved",
    notes=["Backtest tools remain read-only simulations and do not route to live trading."],
)
_VALUATION_SOURCE_POLICY = SourcePolicy(
    priority=["db.stocks", "db.get_stock_info", "db.get_financials", "finance.get_financials", "valuation_peer"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="existing valuation fallback chain is preserved",
    notes=[_TDX_PRIORITY_NOTE],
)
_PORTFOLIO_SOURCE_POLICY = SourcePolicy(
    priority=["input_holdings", "db.get_klines", "market.get_kline", "portfolio_engine", "risk_engine"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="existing portfolio data fallback chain is preserved",
)
_DECISION_SOURCE_POLICY = SourcePolicy(
    priority=["decision_context", "db", "market", "fund_flow", "valuation", "news_sentiment"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="existing decision context fallback chain is preserved",
)
_RESEARCH_SOURCE_POLICY = SourcePolicy(
    priority=["db.research", "tushare_pro", "akshare", "eastmoney", "sina"],
    online_fallback="existing research/news fallback chain is preserved",
)
_SENTIMENT_SOURCE_POLICY = SourcePolicy(
    priority=["db.news", "db.northbound", "db.margin", "market_news", "text_signal"],
    online_fallback="existing sentiment context fallback chain is preserved",
)
_ALERT_SOURCE_POLICY = SourcePolicy(
    priority=["db.alerts", "market.quote", "market.kline", "indicator_engine"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="existing alert evaluation market fallback chain is preserved",
)
_TDX_EXT_SOURCE_POLICY = SourcePolicy(
    priority=["tqcenter", "tdx_local", "tushare_pro", "akshare", "eastmoney"],
    local_only_env="TDX_LOCAL_ONLY",
    online_fallback="disabled for local-only TDX paths; existing fallback remains unchanged",
    notes=[_TDX_PRIORITY_NOTE],
)

__all__ = [
    "Any",
    "BlockStocks",
    "BlockStocksQuery",
    "BlockTrades",
    "BlockTradesQuery",
    "ConceptFundFlow",
    "ConceptFundFlowQuery",
    "DragonTiger",
    "DragonTigerQuery",
    "EquityHistorical",
    "EquityHistoricalQuery",
    "EquityQuote",
    "EquityQuoteQuery",
    "FinancialMetrics",
    "FinancialMetricsQuery",
    "FreshnessPolicy",
    "MacroIndicator",
    "MacroIndicatorQuery",
    "MarginData",
    "MarginDataQuery",
    "MarginRanking",
    "MarginRankingQuery",
    "MarketBlocks",
    "MarketBlocksQuery",
    "NorthFundFlow",
    "NorthFundFlowQuery",
    "NorthFundHolding",
    "NorthFundHoldingQuery",
    "NorthFundTop",
    "NorthFundTopQuery",
    "OptionChain",
    "OptionChainQuery",
    "SectorFundFlow",
    "SectorFundFlowQuery",
    "SourcePolicy",
    "StockFundFlow",
    "StockFundFlowQuery",
    "StockInfo",
    "StockInfoQuery",
    "TradingCalendar",
    "TradingCalendarQuery",
    "_ALERT_FRESHNESS",
    "_ALERT_SOURCE_POLICY",
    "_BACKTEST_FRESHNESS",
    "_BACKTEST_SOURCE_POLICY",
    "_BLOCK_STOCK_SOURCE_POLICY",
    "_BLOCK_TRADES_SOURCE_POLICY",
    "_CALENDAR_FRESHNESS",
    "_CONCEPT_FUND_FLOW_SOURCE_POLICY",
    "_DECISION_FRESHNESS",
    "_DECISION_SOURCE_POLICY",
    "_DRAGON_TIGER_SOURCE_POLICY",
    "_FINANCIAL_FRESHNESS",
    "_FINANCIAL_SOURCE_POLICY",
    "_FUND_FLOW_FRESHNESS",
    "_GENERIC_RECORD_LIST",
    "_INFO_FRESHNESS",
    "_KLINE_FRESHNESS",
    "_MACRO_FRESHNESS",
    "_MACRO_SOURCE_POLICY",
    "_MARGIN_SOURCE_POLICY",
    "_MARKET_BLOCK_SOURCE_POLICY",
    "_NORTH_FUND_FRESHNESS",
    "_NORTH_FUND_SOURCE_POLICY",
    "_OPTIONS_FRESHNESS",
    "_OPTIONS_SOURCE_POLICY",
    "_PORTFOLIO_FRESHNESS",
    "_PORTFOLIO_SOURCE_POLICY",
    "_QUOTE_FRESHNESS",
    "_RESEARCH_FRESHNESS",
    "_RESEARCH_SOURCE_POLICY",
    "_SECTOR_FRESHNESS",
    "_SECTOR_FUND_FLOW_SOURCE_POLICY",
    "_SENTIMENT_SOURCE_POLICY",
    "_STOCK_FUND_FLOW_SOURCE_POLICY",
    "_STOCK_INFO_SOURCE_POLICY",
    "_TDX_EXT_FRESHNESS",
    "_TDX_EXT_SOURCE_POLICY",
    "_TDX_PRIORITY_NOTE",
    "_TECHNICAL_FRESHNESS",
    "_TECHNICAL_SOURCE_POLICY",
    "_VALUATION_FRESHNESS",
    "_VALUATION_SOURCE_POLICY",
    "_array_of",
    "_contract",
    "_schema",
    "annotations",
    "build_contract",
    "deepcopy",
    "json_schema",
]
